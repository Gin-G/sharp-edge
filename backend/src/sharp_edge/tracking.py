"""Pick persistence, retroactive backfill, and outcome tracking.

The screens in batters.py / homers.py generate daily pick lists but never
recorded them anywhere, so there was no way to measure a hit rate. This
module closes the loop:

  persist_screen_result — write a screen's picks to the model_picks table
                          (called from each screen's warm-up thread)
  resolve_pending       — settle past picks against actual Statcast
                          outcomes: WIN / LOSS / VOID (didn't play)
  start_catchup         — generate + settle any day of the season that has
                          no picks yet, using as-of stats. Runs itself at
                          startup, so the history builds once and then
                          persists; restarts find nothing left to do.
  start_backfill        — same machinery, but regenerates a date range
                          unconditionally (manual re-runs)
  build_track_record    — aggregate hit rate overall / per edge / per day

Everything here runs in worker threads (warm-up daemons, the backfill
thread, or asyncio.to_thread from an endpoint); DB access hops onto the
app's event loop via run_coroutine_threadsafe. configure() wires that up
at startup.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import json
import math
import threading
import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Iterable, Optional

import numpy as np
import pandas as pd
import pybaseball as pb

from sharp_edge._data import (
    DEAD_STATES_SUBSTRINGS,
    _HIT_EVENTS,
    _boxscore_summary,
    _load_statcast,
    fetch_schedule,
    statcast_loaded_on,
)

logger = logging.getLogger(__name__)

# "batter_simple" is a shadow of "batter": the original hot-bat + BvP rules,
# recorded daily and settled the same way so the two can be compared on live
# results rather than on backtests of the same 129 days.
SCREENS = ("hr", "batter", "batter_simple")

_db = None
_loop: Optional[asyncio.AbstractEventLoop] = None

_resolve_lock = threading.Lock()

_BACKFILL_PAUSE_SECONDS = 0.5

_backfill_lock = threading.Lock()
_backfill_state: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "start": None,
    "end": None,
    "screens": [],
    "current_date": None,
    "days_done": 0,
    "days_total": 0,
    "picks_written": 0,
    "errors": [],
    "last_error": None,
}


def configure(db, loop: asyncio.AbstractEventLoop) -> None:
    """Wire in the app's database and event loop. Called from the FastAPI
    lifespan at startup; until then persistence calls raise and are swallowed
    by the screens' try/except."""
    global _db, _loop
    _db = db
    _loop = loop


def _run_db(coro):
    """Run a database coroutine from a worker thread on the app's loop."""
    if _db is None or _loop is None:
        raise RuntimeError("tracking not configured (no db/event loop)")
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=300)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _json_safe(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return None if math.isnan(v) else v
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        f = float(v)
        return None if math.isnan(f) else f
    return str(v)


def persist_screen_result(
    screen: str, picks_df: pd.DataFrame, pick_date: date, source: str = "live",
    replace: bool = False,
) -> int:
    """Write a picks frame to model_picks. Existing (screen, date, batter)
    rows are left untouched, so calling this twice for the same day — or
    backfilling a day the live screen already recorded — is safe.

    ``replace`` first clears the date's *unresolved* picks, so an intra-day
    re-screen supersedes the morning's slate (a swapped probable pitcher
    produces a different pick set) instead of piling new rows on top of it.
    Settled outcomes are never touched, so this is only meaningful for today.

    The run itself is always logged, even when the frame is empty: a day
    with no picks writes no rows, and without that marker a catch-up would
    treat it as never screened and redo it on every restart."""
    empty = (
        picks_df is None or picks_df.empty or "batter_id" not in picks_df.columns
    )
    rows = [] if empty else _pick_rows(screen, picks_df, pick_date, source)
    deleted = _run_db(_db.delete_picks(screen, pick_date.isoformat())) if replace else 0
    inserted = _run_db(_db.insert_picks(rows)) if rows else 0
    _run_db(_db.record_screen_run(screen, pick_date.isoformat(), len(rows)))
    logger.info(
        "[tracking] %s %s: %d picks persisted (%d new, %d replaced, source=%s)",
        screen, pick_date.isoformat(), len(rows), inserted, deleted, source,
    )
    return inserted


def _pick_rows(
    screen: str, picks_df: pd.DataFrame, pick_date: date, source: str
) -> list[dict]:
    rows = []
    for rank, rec in enumerate(picks_df.to_dict(orient="records"), start=1):
        metrics = {k: _json_safe(v) for k, v in rec.items()}
        score = rec.get("hr_score")
        pitcher_id = rec.get("pitcher_id")
        rows.append({
            "screen": screen,
            "pick_date": pick_date.isoformat(),
            "batter_id": int(rec["batter_id"]),
            "batter": rec.get("batter"),
            "team": rec.get("team"),
            "pitcher_id": int(pitcher_id) if pitcher_id is not None else None,
            "opposing_pitcher": rec.get("opposing_pitcher"),
            "venue": rec.get("venue"),
            "score": _json_safe(score) if score is not None else None,
            "rank": rank,
            "tags": rec.get("tags"),
            "metrics": json.dumps(metrics),
            "source": source,
        })
    return rows


# ---------------------------------------------------------------------------
# Outcome resolution
# ---------------------------------------------------------------------------

def _events_for_date(d: date) -> pd.DataFrame:
    """PA-ending events for one game date, as ground truth for outcomes.

    Served from the in-memory Statcast cache when it's guaranteed complete
    for that date (the current-year scrape runs through the cache's load
    date, so any strictly earlier date is fully covered); otherwise a small
    single-day scrape fills in.
    """
    loaded_on = statcast_loaded_on()
    sc = _load_statcast()
    if loaded_on is not None and d < loaded_on:
        return sc[sc["game_date"] == pd.Timestamp(d)]

    raw = pb.statcast(start_dt=d.isoformat(), end_dt=d.isoformat(), verbose=False)
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["batter", "events"])
    out = raw.loc[raw["events"].notna(), ["batter", "events"]].copy()
    out["batter"] = pd.to_numeric(out["batter"], errors="coerce").fillna(0).astype("int64")
    return out


@functools.lru_cache(maxsize=512)
def _starters_for_date(dstr: str) -> frozenset:
    """MLBAM ids of every batter who was in a starting lineup on a date.

    Lineups are final for past dates, so caching by date is safe. Returns an
    empty set on API failure — resolution then falls back to the PA-based
    rule rather than voiding everything."""
    starters: set[int] = set()
    try:
        games = fetch_schedule(date.fromisoformat(dstr))
    except Exception:
        return frozenset()
    for game in games:
        status = (game.get("status", {}) or {}).get("detailedState", "") or ""
        if any(bad in status for bad in DEAD_STATES_SUBSTRINGS):
            continue
        box = _boxscore_summary(game.get("gamePk", 0))
        for side in ("away", "home"):
            starters.update(box[side]["starters"])
    return frozenset(starters)


def _boxscore_batting_for_date(dstr: str) -> dict:
    """MLBAM id -> {'pa','hits','hr'} across every game on a date.

    The authoritative, lag-free settlement source: box scores post right after
    the game, so a non-empty result means "every plate appearance that day is
    accounted for." A player absent from it genuinely took no PA. Statcast, by
    contrast, can trail a day — which is why resolving against it alone voided
    players who had actually started and hit. Empty dict means the box scores
    couldn't be loaded for the date (API failure) and the caller must fall back.

    Not lru_cached: box scores fill in as a slate completes, so a partial
    result must not be pinned for the process lifetime. The underlying
    _boxscore_summary is cached per game_pk, so repeat calls stay cheap.
    """
    out: dict[int, dict] = {}
    try:
        games = fetch_schedule(date.fromisoformat(dstr))
    except Exception:
        return {}
    for game in games:
        status = (game.get("status", {}) or {}).get("detailedState", "") or ""
        if any(bad in status for bad in DEAD_STATES_SUBSTRINGS):
            continue
        box = _boxscore_summary(game.get("gamePk", 0))
        for side in ("away", "home"):
            out.update(box[side].get("batting") or {})
    return out


# ---------------------------------------------------------------------------
# The day's card, frozen and settled as one bet
# ---------------------------------------------------------------------------


def freeze_parlay(pick_date: date, legs: list[dict], summary: dict) -> bool:
    """Record the day's card once, the first time it is built.

    Returns True if this call is the one that wrote it.

    The card has to be frozen because the live board cannot reproduce it. Legs
    are only bettable while FanDuel keeps the market open, so every game that
    starts removes its batters, and by late afternoon rebuilding from the board
    yields whoever is left rather than what was recommended — on 2026-08-20
    that was a single leg ranked 8th, while the seven names above it had
    already played (six of them hit). A record of what we said has to be
    written while it is still true.
    """
    if not legs:
        return False
    rows = [
        {
            "batter_id": int(r["batter_id"]) if r.get("batter_id") is not None else None,
            "batter": r.get("batter"),
            "team": r.get("team"),
            "opposing_pitcher": r.get("opposing_pitcher"),
            "pitcher_id": r.get("pitcher_id"),
            "fd_odds": r.get("fd_odds"),
            "model_p": r.get("model_p"),
            # Kept so the bet-slip link still builds from the frozen card.
            # They stop working once FanDuel pulls the market, which is the
            # same moment the leg stops being bettable, so nothing is lost.
            "fd_market_id": r.get("fd_market_id"),
            "fd_selection_id": r.get("fd_selection_id"),
            "fd_event_id": r.get("fd_event_id"),
            "implied_p": r.get("implied_p"),
            "ev": r.get("ev"),
            # Shown on the card; kept so the frozen version renders the same
            # as the live one did.
            "p_l3_h9": r.get("p_l3_h9"),
            "game_time": r.get("game_time"),
        }
        for r in legs
    ]
    if any(r["batter_id"] is None for r in rows):
        # Without an id there is nothing to settle against later.
        logger.warning("[tracking] parlay not frozen: a leg has no batter_id")
        return False
    wrote = _run_db(_db.insert_parlay({
        "pick_date": pick_date.isoformat(),
        "legs": json.dumps(rows),
        "leg_count": len(rows),
        "american": summary.get("american"),
        "decimal_odds": summary.get("decimal"),
        "model_p": summary.get("model_p"),
    }))
    if wrote:
        logger.info(
            "[tracking] parlay frozen for %s: %d legs at %s",
            pick_date.isoformat(), len(rows), summary.get("american"),
        )
    return bool(wrote)


def get_parlay(pick_date: date) -> Optional[dict]:
    """The frozen card for a date, or None if one was never written."""
    par = _run_db(_db.get_parlay(pick_date.isoformat()))
    if par and isinstance(par.get("legs"), str):
        par["legs"] = json.loads(par["legs"])
    return par


def resolve_parlays() -> dict:
    """Settle frozen cards whose legs have all come in.

    A parlay is one outcome, not an average: every leg wins or the ticket is
    dead. Voided legs are dropped rather than counted as losses, which is how
    a sportsbook treats a scratched player — the parlay reprices down and the
    rest of it stands. A card whose legs *all* voided is itself VOID.

    Legs are read from the picks table where they're there, but the card
    cannot depend on that table alone. An intra-day re-screen persists with
    ``replace=True``, so a leg the afternoon board dropped — its game had
    started, or its probable was swapped — loses its pick row, and a leg with
    no row can never be graded. One such leg pinned 2026-08-23's six-leg card
    in pending for good, five days after it settled. The card is frozen
    precisely because the board can't reproduce it, so whatever the table is
    missing is graded straight off the box score instead.

    Only cards from before today are touched. Grading direct from the box
    score would otherwise settle this evening's card off a game that hasn't
    been played, reading "no plate appearance yet" as VOID.
    """
    today_iso = date.today().isoformat()
    pending = [
        p for p in _run_db(_db.list_parlays())
        if not p.get("result") and p["pick_date"] < today_iso
    ]
    settled = 0
    for par in pending:
        pick_date = par["pick_date"]
        legs = par["legs"]
        if isinstance(legs, str):
            legs = json.loads(legs)
        picks = {
            p["batter_id"]: p.get("result")
            for p in _run_db(_db.list_picks(screen="batter", since=pick_date,
                                            until=pick_date))
        }
        outcomes = [picks.get(l["batter_id"]) for l in legs]
        orphans = [
            l["batter_id"] for l, o in zip(legs, outcomes) if o is None
        ]
        if orphans:
            graded = _grade_batters(pick_date, orphans, "batter")
            outcomes = [
                o if o is not None else (graded.get(l["batter_id"]) or {}).get("result")
                for l, o in zip(legs, outcomes)
            ]
        if any(o is None for o in outcomes):
            continue                      # a leg hasn't been graded yet
        decided = [o for o in outcomes if o != "VOID"]
        if not decided:
            result, won = "VOID", 0
        elif all(o == "WIN" for o in decided):
            result, won = "WIN", len(decided)
        else:
            result, won = "LOSS", sum(1 for o in decided if o == "WIN")
        _run_db(_db.settle_parlay(pick_date, result, won, len(decided)))
        settled += 1
        logger.info("[tracking] parlay %s settled %s (%d/%d legs)",
                    pick_date, result, won, len(decided))
    return {"pending": len(pending), "settled": settled}


def parlay_track_record(since: Optional[str] = None) -> dict:
    """Cards played, cards cashed, and what a flat stake would have returned."""
    rows = _run_db(_db.list_parlays(since=since))
    decided = [r for r in rows if r.get("result") in ("WIN", "LOSS")]
    wins = [r for r in decided if r["result"] == "WIN"]
    staked = len(decided)
    returned = sum((r.get("decimal_odds") or 1.0) for r in wins)
    return {
        "cards": len(rows),
        "decided": staked,
        "wins": len(wins),
        "losses": staked - len(wins),
        "pending": sum(1 for r in rows if not r.get("result")),
        "void": sum(1 for r in rows if r.get("result") == "VOID"),
        "sweep_rate": round(100 * len(wins) / staked, 1) if staked else None,
        "roi": round(100 * (returned - staked) / staked, 1) if staked else None,
        "avg_legs": round(sum(r["leg_count"] for r in rows) / len(rows), 1) if rows else None,
        "parlays": rows,
    }


def _grade_batters(
    dstr: str, batter_ids: Iterable[int], screen: str
) -> dict[int, dict]:
    """Grade batters for one past date against what actually happened.

    Returns ``{batter_id: {result, hr_actual, hits_actual, pa_actual}}``. HR
    picks win on >=1 home run, batter picks on >=1 hit. Sportsbook void rules
    apply: a player who was not in the starting lineup is VOID even if he
    pinch-hit, as is a starter with no plate appearance. Where lineup data is
    unavailable, "recorded a plate appearance" stands in for "started".

    An id is *absent from the result* when the day hasn't published yet — with
    no box score and no Statcast plate appearance there is no telling a real
    VOID from data still in flight, so the caller leaves it ungraded for a
    later sweep rather than freezing a guess.
    """
    starters = _starters_for_date(dstr)
    batting = _boxscore_batting_for_date(dstr)
    box_ok = bool(batting)  # box scores loaded => authoritative
    # Statcast is only the fallback when the box score is missing.
    ev = None if box_ok else _events_for_date(date.fromisoformat(dstr))
    out: dict[int, dict] = {}
    for bid in batter_ids:
        if box_ok:
            # Authoritative line. A batter absent from the box score
            # genuinely took no PA (pa=0 -> VOID below).
            line = batting.get(bid)
            pa = line["pa"] if line else 0
            hr = line["hr"] if line else 0
            hits = line["hits"] if line else 0
        else:
            sub = ev[ev["batter"] == bid]
            pa = int(len(sub))
            hr = int((sub["events"] == "home_run").sum()) if pa else 0
            hits = int(sub["events"].isin(_HIT_EVENTS).sum()) if pa else 0
            if pa == 0:
                continue
        started = bid in starters if starters else pa > 0
        if not started or pa == 0:
            outcome = "VOID"
        elif screen == "hr":
            outcome = "WIN" if hr >= 1 else "LOSS"
        else:
            outcome = "WIN" if hits >= 1 else "LOSS"
        out[bid] = {
            "result": outcome,
            "hr_actual": hr,
            "hits_actual": hits,
            "pa_actual": pa,
        }
    return out


def resolve_pending() -> dict:
    """Settle every unresolved pick from before today against what actually
    happened. HR picks win on ≥1 home run, batter picks on ≥1 hit.

    Sportsbook settlement rules for voids: a pick on a player who was not in
    the starting lineup is VOID — even if they pinch-hit — as is a starter
    with no plate appearance (data gap / abandoned game). Voids are excluded
    from the hit rate. If lineup data is unavailable for a date, "recorded a
    plate appearance" stands in for "started"."""
    with _resolve_lock:
        today_iso = date.today().isoformat()
        summary: dict = {}
        for screen in SCREENS:
            pending = _run_db(_db.list_picks(screen, unresolved_only=True))
            by_date: dict[str, list] = defaultdict(list)
            for r in pending:
                if r["pick_date"] < today_iso:
                    by_date[r["pick_date"]].append(r)

            resolved = 0
            for dstr in sorted(by_date):
                graded = _grade_batters(
                    dstr, [r["batter_id"] for r in by_date[dstr]], screen
                )
                results = [{"batter_id": bid, **g} for bid, g in graded.items()]
                resolved += _run_db(_db.update_pick_results(screen, dstr, results))
            summary[screen] = {"dates": len(by_date), "resolved": resolved}
            if by_date:
                logger.info(
                    "[tracking] %s: resolved %d picks across %d dates",
                    screen, resolved, len(by_date),
                )
        return summary


def reresolve_voids(since: Optional[str] = None, dry_run: bool = False) -> dict:
    """Repair picks frozen as VOID against the authoritative box score.

    resolve_pending only ever revisits unresolved (result IS NULL) rows, so a
    VOID written prematurely from lagging Statcast data stays wrong forever.
    This re-checks every VOID pick against the box score batting line and
    corrects the ones where the player actually started and took a plate
    appearance — those become WIN/LOSS. Genuine voids (didn't start, or truly
    no PA) are left untouched, as are dates whose box scores can't be loaded.
    Idempotent: a second run over already-corrected data changes nothing.

    ``since`` limits the scan to pick_date >= that ISO date. ``dry_run`` reports
    the corrections without writing them.
    """
    with _resolve_lock:
        today_iso = date.today().isoformat()
        summary: dict = {}
        for screen in SCREENS:
            rows = _run_db(_db.list_picks(screen, since=since))
            by_date: dict[str, list] = defaultdict(list)
            for r in rows:
                if r.get("result") == "VOID" and r["pick_date"] < today_iso:
                    by_date[r["pick_date"]].append(r)

            fixed = 0
            corrections: list[dict] = []
            for dstr in sorted(by_date):
                batting = _boxscore_batting_for_date(dstr)
                if not batting:
                    # Can't authoritatively correct without box scores; a wrong
                    # VOID here is left for a later run once the API recovers.
                    continue
                starters = _starters_for_date(dstr)
                results = []
                for r in by_date[dstr]:
                    bid = r["batter_id"]
                    line = batting.get(bid)
                    if not line or line["pa"] == 0:
                        continue  # truly no PA — VOID stands
                    started = bid in starters if starters else True
                    if not started:
                        continue  # non-starter (e.g. pinch-hit) — VOID stands
                    if screen == "hr":
                        outcome = "WIN" if line["hr"] >= 1 else "LOSS"
                    else:
                        outcome = "WIN" if line["hits"] >= 1 else "LOSS"
                    results.append({
                        "batter_id": bid,
                        "result": outcome,
                        "hr_actual": line["hr"],
                        "hits_actual": line["hits"],
                        "pa_actual": line["pa"],
                    })
                    corrections.append({
                        "pick_date": dstr, "batter": r.get("batter"),
                        "batter_id": bid, "was": "VOID", "now": outcome,
                        "hits": line["hits"], "hr": line["hr"], "pa": line["pa"],
                    })
                if results and not dry_run:
                    fixed += _run_db(_db.update_pick_results(screen, dstr, results))
                elif results:
                    fixed += len(results)
            summary[screen] = {
                "dates": len(by_date), "fixed": fixed, "corrections": corrections,
            }
            if fixed:
                logger.info(
                    "[tracking] %s: %s %d VOID picks across %d dates%s",
                    screen, "would re-resolve" if dry_run else "re-resolved",
                    fixed, len(by_date), " (dry-run)" if dry_run else "",
                )
        return summary


def regenerate_today() -> dict:
    """Replace today's still-pending picks with each screen's current board.

    Forces the recorded picks to match the latest slate immediately — e.g.
    after a probable pitcher was swapped — instead of waiting for the next
    scheduled intra-day refresh. Reuses each screen's already-warm result, so
    it's cheap; a screen with no warm cache yet is skipped."""
    from sharp_edge import batters, homers
    today = date.today()
    out: dict = {}
    for screen, mod in (("hr", homers), ("batter", batters)):
        cached = mod.get_cached()
        if cached is None:
            out[screen] = {"status": "no-cache"}
            continue
        n = persist_screen_result(screen, cached.picks, today, replace=True)
        out[screen] = {"status": "ok", "picks": n}
    return out


# ---------------------------------------------------------------------------
# Retroactive backfill
# ---------------------------------------------------------------------------

def backfill_status() -> dict:
    with _backfill_lock:
        state = dict(_backfill_state)
        state["errors"] = list(_backfill_state["errors"])
        if state["started_at"] and state["running"]:
            state["elapsed_seconds"] = round(time.time() - state["started_at"], 1)
        return state


def season_start(year: Optional[int] = None) -> date:
    """Conventional start of the regular season. Screening a date with no
    games is a no-op, so this only has to be early enough."""
    return date((year or date.today().year), 3, 20)


def _full_plan(start: date, end: date, screens: list[str]) -> dict[date, list[str]]:
    out, d = {}, start
    while d <= end:
        out[d] = list(screens)
        d += timedelta(days=1)
    return out


def _missing_plan(start: date, end: date, screens: list[str]) -> dict[date, list[str]]:
    """Same range, minus the (date, screen) pairs already screened."""
    have = {s: _run_db(_db.screened_dates(s)) for s in screens}
    out = {}
    d = start
    while d <= end:
        todo = [s for s in screens if d.isoformat() not in have[s]]
        if todo:
            out[d] = todo
        d += timedelta(days=1)
    return out


def start_backfill(start: date, end: date, screens: list[str]) -> dict:
    """Kick off a background backfill of picks for [start, end], regenerating
    every day in the range. One at a time; returns immediately."""
    return _launch(_full_plan(start, end, screens), start, end, screens)


def start_catchup(
    start: Optional[date] = None,
    end: Optional[date] = None,
    screens: Optional[list[str]] = None,
) -> dict:
    """Fill in only the days that have no picks yet, then settle them.

    Called at startup so the season's history builds itself once and then
    persists — no button to press, and a restart re-runs nothing because
    every day already recorded is skipped.
    """
    start = start or season_start()
    end = end or (date.today() - timedelta(days=1))
    screens = screens or list(SCREENS)
    if end < start:
        return {"status": "nothing-to-do", "days": 0}
    try:
        plan = _missing_plan(start, end, screens)
    except Exception as e:
        logger.warning("[tracking] catch-up planning failed: %s", e)
        return {"status": "error", "error": str(e)}
    if not plan:
        logger.info("[tracking] catch-up: history complete through %s", end)
        # Days already recorded may still be unsettled (picked yesterday,
        # resolved today), so always sweep for pending outcomes.
        threading.Thread(target=_safe_resolve, daemon=True).start()
        return {"status": "up-to-date", "days": 0}
    logger.info("[tracking] catch-up: %d day(s) missing between %s and %s",
                len(plan), start, end)
    return _launch(plan, start, end, screens)


def schedule_catchup(max_wait_seconds: int = 3600) -> None:
    """Run the catch-up once the shared Statcast cache is warm.

    Deferring keeps the season's regeneration from competing with the
    startup warm-up that today's screens are waiting on — by the time the
    cache is loaded the per-day work is mostly cheap API lookups.
    """
    def _wait_then_run() -> None:
        deadline = time.time() + max_wait_seconds
        while statcast_loaded_on() is None and time.time() < deadline:
            time.sleep(15)
        try:
            start_catchup()
        except Exception:
            logger.exception("[tracking] catch-up failed to start")

    threading.Thread(target=_wait_then_run, daemon=True).start()


def _safe_resolve() -> None:
    try:
        resolve_pending()
        resolve_parlays()
    except Exception:
        logger.exception("[tracking] resolve sweep failed")


def _launch(
    plan: dict[date, list[str]], start: date, end: date, screens: list[str]
) -> dict:
    with _backfill_lock:
        if _backfill_state["running"]:
            return {"status": "already-running", **backfill_status()}
        _backfill_state.update({
            "running": True,
            "started_at": time.time(),
            "finished_at": None,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "screens": list(screens),
            "current_date": None,
            "days_done": 0,
            "days_total": len(plan),
            "picks_written": 0,
            "errors": [],
            "last_error": None,
        })
    threading.Thread(target=_do_backfill, args=(plan,), daemon=True).start()
    return {"status": "started", "start": start.isoformat(), "end": end.isoformat(),
            "screens": list(screens), "days": len(plan)}


def _do_backfill(plan: dict[date, list[str]]) -> None:
    from sharp_edge import batters, homers
    from sharp_edge._data import _pitcher_gamelog_starts

    try:
        # Game logs may have been cached days ago on a long-lived pod; a
        # stale log would hide recent starts from the as-of filter.
        _pitcher_gamelog_starts.cache_clear()

        for d in sorted(plan):
            with _backfill_lock:
                _backfill_state["current_date"] = d.isoformat()
            for screen in plan[d]:
                try:
                    if screen == "hr":
                        result = homers.screen_hr_for_date(d, verbose=False)
                        picks = result.picks
                    else:
                        result = batters.screen_for_date(d, verbose=False)
                        # batter_simple is the shadow control — the original
                        # rules, derived from the same board so it costs no
                        # extra scraping.
                        picks = (
                            batters.simple_picks(result.today)
                            if screen == "batter_simple" else result.picks
                        )
                    n = persist_screen_result(screen, picks, d, source="backfill")
                    with _backfill_lock:
                        _backfill_state["picks_written"] += n
                except Exception as e:
                    logger.exception("[tracking] backfill %s %s failed", screen, d)
                    with _backfill_lock:
                        if len(_backfill_state["errors"]) < 50:
                            _backfill_state["errors"].append(
                                f"{d.isoformat()} {screen}: {type(e).__name__}: {e}"
                            )
            with _backfill_lock:
                _backfill_state["days_done"] += 1
            # The pod is capped at 1 CPU and serves the site from this same
            # process. Yielding between days keeps a long catch-up from
            # starving request handlers; it only stretches a ~120-day run by
            # about a minute.
            time.sleep(_BACKFILL_PAUSE_SECONDS)

        resolve_pending()
        resolve_parlays()
    except Exception as e:
        logger.exception("[tracking] backfill aborted")
        with _backfill_lock:
            _backfill_state["last_error"] = f"{type(e).__name__}: {e}"
    finally:
        with _backfill_lock:
            _backfill_state["running"] = False
            _backfill_state["current_date"] = None
            _backfill_state["finished_at"] = time.time()


# ---------------------------------------------------------------------------
# Track-record aggregation
# ---------------------------------------------------------------------------

_PICK_FIELDS = (
    "pick_date", "batter", "team", "opposing_pitcher", "venue", "score",
    "rank", "tags", "source", "result", "hr_actual", "hits_actual", "pa_actual",
)


def build_track_record(
    screen: str, rows: list[dict], recent_limit: int = 300
) -> dict:
    """Aggregate persisted picks into hit-rate stats. ``rows`` come from
    db.list_picks ordered newest-first. Hit rate = wins / (wins + losses);
    VOID (didn't play) and pending picks are excluded from the denominator."""

    def _bucket() -> dict:
        return {"picks": 0, "wins": 0, "losses": 0, "voids": 0, "pending": 0}

    def _finish(b: dict) -> dict:
        decided = b["wins"] + b["losses"]
        b["decided"] = decided
        b["hit_rate"] = round(b["wins"] / decided * 100, 1) if decided else None
        return b

    overall = _bucket()
    by_tag: dict[str, dict] = {}
    by_source: dict[str, dict] = {}
    daily: dict[str, dict] = {}

    for r in rows:
        res = r.get("result")
        key = {"WIN": "wins", "LOSS": "losses", "VOID": "voids"}.get(res, "pending")
        buckets = [
            overall,
            daily.setdefault(r["pick_date"], _bucket()),
            by_source.setdefault(r.get("source") or "live", _bucket()),
        ]
        for tag in (r.get("tags") or "").split(","):
            if tag:
                buckets.append(by_tag.setdefault(tag, _bucket()))
        for b in buckets:
            b["picks"] += 1
            b[key] += 1

    return {
        "screen": screen,
        "overall": _finish(overall),
        "by_tag": sorted(
            [{"tag": t, **_finish(b)} for t, b in by_tag.items()],
            key=lambda x: -x["picks"],
        ),
        "by_source": [{"source": s, **_finish(b)} for s, b in sorted(by_source.items())],
        "daily": [{"date": d, **_finish(b)} for d, b in sorted(daily.items())],
        "picks": [
            {k: r.get(k) for k in _PICK_FIELDS} for r in rows[:recent_limit]
        ],
    }
