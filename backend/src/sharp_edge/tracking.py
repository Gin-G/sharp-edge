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
from typing import Optional

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

SCREENS = ("hr", "batter")

_db = None
_loop: Optional[asyncio.AbstractEventLoop] = None

_resolve_lock = threading.Lock()

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
    screen: str, picks_df: pd.DataFrame, pick_date: date, source: str = "live"
) -> int:
    """Write a picks frame to model_picks. Existing (screen, date, batter)
    rows are left untouched, so calling this twice for the same day — or
    backfilling a day the live screen already recorded — is safe."""
    if picks_df is None or picks_df.empty or "batter_id" not in picks_df.columns:
        return 0
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
    inserted = _run_db(_db.insert_picks(rows))
    logger.info(
        "[tracking] %s %s: %d picks persisted (%d new, source=%s)",
        screen, pick_date.isoformat(), len(rows), inserted, source,
    )
    return inserted


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
                ev = _events_for_date(date.fromisoformat(dstr))
                starters = _starters_for_date(dstr)
                results = []
                for r in by_date[dstr]:
                    sub = ev[ev["batter"] == r["batter_id"]]
                    pa = int(len(sub))
                    hr = int((sub["events"] == "home_run").sum()) if pa else 0
                    hits = int(sub["events"].isin(_HIT_EVENTS).sum()) if pa else 0
                    started = r["batter_id"] in starters if starters else pa > 0
                    if not started or pa == 0:
                        outcome = "VOID"
                    elif screen == "hr":
                        outcome = "WIN" if hr >= 1 else "LOSS"
                    else:
                        outcome = "WIN" if hits >= 1 else "LOSS"
                    results.append({
                        "batter_id": r["batter_id"],
                        "result": outcome,
                        "hr_actual": hr,
                        "hits_actual": hits,
                        "pa_actual": pa,
                    })
                resolved += _run_db(_db.update_pick_results(screen, dstr, results))
            summary[screen] = {"dates": len(by_date), "resolved": resolved}
            if by_date:
                logger.info(
                    "[tracking] %s: resolved %d picks across %d dates",
                    screen, resolved, len(by_date),
                )
        return summary


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
    """Same range, minus the (date, screen) pairs already in the database."""
    have = {s: _run_db(_db.pick_dates(s)) for s in screens}
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
                    else:
                        result = batters.screen_for_date(d, verbose=False)
                    n = persist_screen_result(screen, result.picks, d, source="backfill")
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

        resolve_pending()
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
