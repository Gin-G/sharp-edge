"""MLB batter screen — recent form + BvP + handedness splits + pitcher form.

Returns three frames:

  hot_bats : everyone hitting > min_recent_avg over last N days
  today    : every batter in today's lineups, enriched with recent form,
             splits vs the opposing SP's handedness, BvP, and the SP's
             last-3-starts ERA — with edge flags so you can slice it
             however you want
  picks    : hot bats facing an advantageous matchup (≥1 edge type)

Edges:
  bvp_edge       : bvp_avg ≥ min_bvp_avg & bvp_pa ≥ min_bvp_pa
  hand_slump_edge: vs_hand_avg ≥ min_hand_avg & vs_hand_pa ≥ min_hand_pa
                   & opposing SP last-3 ERA ≥ min_slump_era

deps: install with `pip install -e ".[models]"` (pybaseball, MLB-StatsAPI, pandas)
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import pybaseball as pb
import statsapi

from sharp_edge._data import (
    DEAD_STATES_SUBSTRINGS,
    _HIT_EVENTS,
    _NON_AB_EVENTS,
    _boxscore_summary,
    _load_statcast,
    _local_time_str,
    _norm,
    _pitcher_info,
    _pitcher_last_3,
    _roster_batters,
    fetch_schedule,
)

logger = logging.getLogger(__name__)


@dataclass
class ScreenResult:
    picks: pd.DataFrame
    hot_bats: pd.DataFrame
    today: pd.DataFrame

    def __repr__(self) -> str:
        return (
            f"ScreenResult(picks={len(self.picks)}, "
            f"hot_bats={len(self.hot_bats)}, today={len(self.today)})"
        )


# -----------------------------------------------------------------------------
# Daily result cache + background warm-up
# -----------------------------------------------------------------------------
#
# screen_today() is expensive: the first call after a pod restart scrapes 3
# seasons of Statcast events (multi-minute, ~500MB of pandas memory). We don't
# want to run it on every request, and we don't want to make users wait. So:
#
#   1. Module-level cache keyed by date — one scrape per day per process.
#   2. A daemon thread runs the scrape in the background; subsequent requests
#      either return the cached result or get a "warming" signal.
#   3. The FastAPI lifespan kicks off warm_async() at startup so a freshly-
#      deployed pod is usually ready by the time the first browser hits it.

_state_lock = threading.Lock()
_warm_result: Optional[ScreenResult] = None
_warm_date: Optional[date] = None
_warm_error: Optional[str] = None
_warming: bool = False
_warm_started_at: Optional[float] = None


def get_cached() -> Optional[ScreenResult]:
    """Return today's cached ScreenResult if present, else None."""
    with _state_lock:
        if _warm_result is not None and _warm_date == date.today():
            return _warm_result
    return None


def warm_status() -> dict:
    """Snapshot of cache state for diagnostics / frontend polling."""
    with _state_lock:
        return {
            "warming": _warming,
            "started_at": _warm_started_at,
            "elapsed_seconds": (time.time() - _warm_started_at) if _warm_started_at else None,
            "cached_date": _warm_date.isoformat() if _warm_date else None,
            "has_cache": _warm_result is not None and _warm_date == date.today(),
            "last_error": _warm_error,
        }


def _do_warm(target_date: date) -> None:
    """Run screen_today() and stash the result. Runs in a daemon thread."""
    global _warm_result, _warm_date, _warming, _warm_error
    try:
        logger.info("[batters] warm-up started for %s", target_date.isoformat())
        result = screen_today(verbose=False)
        with _state_lock:
            _warm_result = result
            _warm_date = target_date
            _warm_error = None
        logger.info(
            "[batters] warm-up complete (%d picks, %d hot, %d today)",
            len(result.picks), len(result.hot_bats), len(result.today),
        )
        # Persist today's picks and settle prior unresolved ones. Tracking
        # failures must never take down the screen itself.
        try:
            from sharp_edge import tracking
            tracking.persist_screen_result("batter", result.picks, target_date)
            tracking.resolve_pending()
        except Exception:
            logger.exception("[batters] pick tracking failed")
    except Exception as e:
        logger.exception("[batters] warm-up failed")
        with _state_lock:
            _warm_error = f"{type(e).__name__}: {e}"
    finally:
        with _state_lock:
            _warming = False


def warm_async() -> dict:
    """Trigger a background warm-up if cache is stale or absent. Returns the
    current state immediately — does not block."""
    global _warming, _warm_started_at
    today = date.today()
    with _state_lock:
        if _warm_result is not None and _warm_date == today:
            return {"status": "ready"}
        if _warming:
            return {"status": "warming"}
        _warming = True
        _warm_started_at = time.time()
    threading.Thread(target=_do_warm, args=(today,), daemon=True).start()
    return {"status": "warming"}


def _bvp(
    batter_id: int, pitcher_id: int, sc_df: Optional[pd.DataFrame] = None
) -> dict | None:
    df = sc_df if sc_df is not None else _load_statcast()
    pa_df = df[(df["batter"] == batter_id) & (df["pitcher"] == pitcher_id)]
    if pa_df.empty:
        return None
    pa = len(pa_df)
    ab = pa - int(pa_df["events"].isin(_NON_AB_EVENTS).sum())
    hits = int(pa_df["events"].isin(_HIT_EVENTS).sum())
    if ab == 0:
        return None
    return {"pa": int(pa), "ab": ab, "hits": hits, "avg": round(hits / ab, 3)}


def debug_bvp_raw(batter_name: str, pitcher_name: str) -> dict:
    bs = statsapi.lookup_player(batter_name)
    ps = statsapi.lookup_player(pitcher_name)
    if not bs:
        raise ValueError(f"batter not found: {batter_name!r}")
    if not ps:
        raise ValueError(f"pitcher not found: {pitcher_name!r}")
    bid, pid = bs[0]["id"], ps[0]["id"]

    df = _load_statcast()
    pa_df = df[(df["batter"] == bid) & (df["pitcher"] == pid)].copy()
    pa_df = pa_df.sort_values("game_date").reset_index(drop=True)

    return {
        "batter": bs[0]["fullName"],
        "batter_id": bid,
        "pitcher": ps[0]["fullName"],
        "pitcher_id": pid,
        "events": pa_df[["game_date", "events"]].to_dict(orient="records"),
        "bvp": _bvp(bid, pid),
    }


@functools.lru_cache(maxsize=None)
def _handedness_splits(batter_id: int) -> dict:
    out = {"vs_R_avg": None, "vs_R_pa": 0, "vs_L_avg": None, "vs_L_pa": 0}

    for stat_type in ("careerStatSplits", "statSplits"):
        try:
            data = statsapi.get(
                "people",
                {
                    "personIds": str(batter_id),
                    "hydrate": (
                        f"stats(group=[hitting],type=[{stat_type}],"
                        f"sitCodes=[vr,vl],sportId=1)"
                    ),
                },
            )
        except Exception:
            continue

        for person in data.get("people", []):
            for block in person.get("stats", []):
                for split in block.get("splits", []):
                    code = split.get("split", {}).get("code", "")
                    stat = split.get("stat", {})
                    pa = stat.get("plateAppearances", 0)
                    if pa == 0:
                        continue
                    avg = float(stat.get("avg", "0") or 0)
                    if code == "vr" and pa > out["vs_R_pa"]:
                        out["vs_R_avg"], out["vs_R_pa"] = avg, pa
                    elif code == "vl" and pa > out["vs_L_pa"]:
                        out["vs_L_avg"], out["vs_L_pa"] = avg, pa

        if out["vs_R_pa"] > 0 or out["vs_L_pa"] > 0:
            break

    return out


def lookup_bvp(batter_name: str, pitcher_name: str) -> dict:
    bs = statsapi.lookup_player(batter_name)
    ps = statsapi.lookup_player(pitcher_name)
    if not bs:
        raise ValueError(f"batter not found: {batter_name!r}")
    if not ps:
        raise ValueError(f"pitcher not found: {pitcher_name!r}")
    bid, pid = bs[0]["id"], ps[0]["id"]
    return {
        "batter": bs[0]["fullName"],
        "batter_id": bid,
        "pitcher": ps[0]["fullName"],
        "pitcher_id": pid,
        "bvp": _bvp(bid, pid),
    }


def screen_today(
    min_recent_avg: float = 0.300,
    min_recent_ab: int = 10,
    min_bvp_avg: float = 0.400,
    min_bvp_pa: int = 5,
    min_hand_avg: float = 0.400,
    min_hand_pa: int = 50,
    min_slump_era: float = 5.00,
    days: int = 7,
    workers: int = 12,
    verbose: bool = True,
) -> ScreenResult:
    return screen_for_date(
        date.today(),
        min_recent_avg=min_recent_avg,
        min_recent_ab=min_recent_ab,
        min_bvp_avg=min_bvp_avg,
        min_bvp_pa=min_bvp_pa,
        min_hand_avg=min_hand_avg,
        min_hand_pa=min_hand_pa,
        min_slump_era=min_slump_era,
        days=days,
        workers=workers,
        verbose=verbose,
    )


_RECENT_COLS = ["Name", "Tm", "BA", "AB", "H", "HR", "OBP", "OPS"]


def _batting_stats_range(start: date, end: date) -> pd.DataFrame:
    """Recent batting lines, tolerating a window with no games played.

    pybaseball parses an HTML table and indexes [0] into the result, so a
    window Baseball-Reference has no rows for raises IndexError rather than
    returning an empty frame. Every date in the first week of a season has
    that problem, since its trailing window sits in the pre-season.
    """
    try:
        return pb.batting_stats_range(start.isoformat(), end.isoformat())
    except (IndexError, ValueError) as e:
        logger.warning(
            "[batters] no batting lines for %s..%s (%s: %s) — treating as empty",
            start.isoformat(), end.isoformat(), type(e).__name__, e,
        )
        return pd.DataFrame(columns=_RECENT_COLS)


def screen_for_date(
    target_date: date,
    min_recent_avg: float = 0.300,
    min_recent_ab: int = 10,
    min_bvp_avg: float = 0.400,
    min_bvp_pa: int = 5,
    min_hand_avg: float = 0.400,
    min_hand_pa: int = 50,
    min_slump_era: float = 5.00,
    days: int = 7,
    workers: int = 12,
    verbose: bool = True,
) -> ScreenResult:
    """Run the batter screen for an arbitrary slate date.

    For today this is the live screen (active rosters + probable pitchers).
    For past dates the slate comes from that day's schedule and boxscores,
    and BvP / pitcher form are computed as-of that morning. Known limitation
    of the historical mode: handedness splits are the player's career line
    as of now, not as of the target date — career splits move slowly, so the
    lookahead is small.
    """
    today = target_date
    end = today - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    season = today.year
    is_live = today >= date.today()
    as_of = today.isoformat()

    # Settle the slate before doing any real work. A date with no games — the
    # pre-season window, an off-day, the All-Star break — has nothing to
    # screen, and both the Statcast copy below and the range query are
    # expensive enough to be worth skipping outright.
    raw_games = fetch_schedule(today)
    if not raw_games:
        empty = pd.DataFrame()
        return ScreenResult(picks=empty, hot_bats=empty.copy(), today=empty.copy())

    sc_df = _load_statcast()
    sc_df = sc_df[sc_df["game_date"] < pd.Timestamp(today)]

    recent = _batting_stats_range(start, end)
    cols = ["Name", "Tm", "BA", "AB", "H", "HR", "OBP", "OPS"]
    hot_df = recent[
        (recent["AB"] >= min_recent_ab) & (recent["BA"] >= min_recent_avg)
    ][cols].copy().rename(columns={"BA": "recent_avg", "AB": "recent_ab"})
    hot_df = hot_df.sort_values("recent_avg", ascending=False).reset_index(drop=True)

    all_recent = {
        _norm(row["Name"]): (float(row["BA"]), int(row["AB"]))
        for _, row in recent.iterrows()
    }

    games_kept = 0
    games_with_pp = 0
    targets = []

    for game in raw_games:
        status = (game.get("status", {}) or {}).get("detailedState", "") or ""
        if any(bad in status for bad in DEAD_STATES_SUBSTRINGS):
            continue
        games_kept += 1

        away = game["teams"]["away"]
        home = game["teams"]["home"]
        away_team_id = away["team"]["id"]
        home_team_id = home["team"]["id"]
        away_team_name = away["team"].get("name", "")
        home_team_name = home["team"].get("name", "")

        away_pp = away.get("probablePitcher") or {}
        home_pp = home.get("probablePitcher") or {}
        away_pp_id, away_pp_name = away_pp.get("id"), away_pp.get("fullName", "")
        home_pp_id, home_pp_name = home_pp.get("id"), home_pp.get("fullName", "")

        box = None if is_live else _boxscore_summary(game.get("gamePk", 0))
        if box is not None:
            # Historical: actual starter fills a missing probable pitcher.
            if not home_pp_id and box["home"]["starter"]:
                home_pp_id, home_pp_name = box["home"]["starter"]
            if not away_pp_id and box["away"]["starter"]:
                away_pp_id, away_pp_name = box["away"]["starter"]

        if away_pp_id or home_pp_id:
            games_with_pp += 1

        gtime = game.get("gameDate", "")

        for side, batting_team_id, batting_team, opp_pid, opp_pname in (
            ("away", away_team_id, away_team_name, home_pp_id, home_pp_name),
            ("home", home_team_id, home_team_name, away_pp_id, away_pp_name),
        ):
            if not opp_pid:
                continue
            batter_list = (
                _roster_batters(batting_team_id) if box is None
                else box[side]["batters"]
            )
            for bid, bname in batter_list:
                targets.append((bid, bname, batting_team, opp_pid, opp_pname, gtime))

    if verbose:
        print(
            f"[screen] schedule={len(raw_games)} kept={games_kept} "
            f"with_probable_pitcher={games_with_pp} targets={len(targets)}"
        )

    def _scan(t):
        bid, _, _, ppid, _, _ = t
        return {
            "target": t,
            "bvp": _bvp(bid, ppid, sc_df),
            "hand": _handedness_splits(bid),
            "pinfo": _pitcher_info(ppid),
            "p_l3": _pitcher_last_3(ppid, season, before=as_of),
        }

    if targets:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            scanned = list(pool.map(_scan, targets))
    else:
        scanned = []

    rows = []
    for r in scanned:
        bid, bname, team, ppid, ppname, gtime = r["target"]
        hand_split = r["hand"]
        p_hand = r["pinfo"]["hand"]
        p_l3 = r["p_l3"]
        bvp = r["bvp"] or {"avg": None, "pa": 0, "hits": 0}

        if p_hand == "R":
            vs_hand_avg = hand_split.get("vs_R_avg")
            vs_hand_pa = hand_split.get("vs_R_pa", 0)
        elif p_hand == "L":
            vs_hand_avg = hand_split.get("vs_L_avg")
            vs_hand_pa = hand_split.get("vs_L_pa", 0)
        else:
            vs_hand_avg, vs_hand_pa = None, 0

        rec = all_recent.get(_norm(bname))
        recent_avg = rec[0] if rec else None
        recent_ab = rec[1] if rec else 0

        is_hot = (
            recent_avg is not None
            and recent_avg >= min_recent_avg
            and recent_ab >= min_recent_ab
        )
        bvp_edge = (
            bvp["avg"] is not None
            and bvp["avg"] >= min_bvp_avg
            and bvp["pa"] >= min_bvp_pa
        )
        hand_slump_edge = (
            vs_hand_avg is not None
            and vs_hand_avg >= min_hand_avg
            and vs_hand_pa >= min_hand_pa
            and p_l3["era"] is not None
            and p_l3["era"] >= min_slump_era
            and p_l3["starts"] >= 3
        )

        tags = []
        if bvp_edge:
            tags.append("BvP")
        if hand_slump_edge:
            tags.append("HAND+SLUMP")

        rows.append({
            "batter": bname,
            "batter_id": bid,
            "team": team,
            "opposing_pitcher": ppname,
            "pitcher_id": ppid,
            "p_hand": p_hand,
            "recent_avg": round(recent_avg, 3) if recent_avg is not None else None,
            "recent_ab": recent_ab,
            "vs_hand_avg": round(vs_hand_avg, 3) if vs_hand_avg is not None else None,
            "vs_hand_pa": vs_hand_pa,
            "bvp_avg": bvp["avg"],
            "bvp_pa": bvp["pa"],
            "bvp_hits": bvp["hits"],
            "p_l3_era": p_l3["era"],
            "p_l3_ip": p_l3["ip"],
            "p_l3_starts": p_l3["starts"],
            "is_hot": is_hot,
            "bvp_edge": bvp_edge,
            "hand_slump_edge": hand_slump_edge,
            "tags": ",".join(tags),
            "game_time": _local_time_str(gtime),
        })

    today_df = pd.DataFrame(rows)

    if today_df.empty:
        picks = pd.DataFrame()
    else:
        mask = today_df["is_hot"] & (today_df["bvp_edge"] | today_df["hand_slump_edge"])
        picks = today_df[mask].copy()
        if not picks.empty:
            picks = picks.sort_values(
                ["bvp_edge", "bvp_avg", "vs_hand_avg", "recent_avg"],
                ascending=[False, False, False, False],
            ).reset_index(drop=True)

    return ScreenResult(picks=picks, hot_bats=hot_df, today=today_df)


def find_value_bats(**kwargs) -> pd.DataFrame:
    """Backwards-compatible: returns just the picks frame."""
    return screen_today(**kwargs).picks
