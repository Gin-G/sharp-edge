"""MLB batter screen — recent form + BvP + handedness splits + pitcher form.

Returns three frames:

  hot_bats : everyone hitting > min_recent_avg over last N days
  today    : every batter in today's lineups, enriched with recent form,
             splits vs the opposing SP's handedness, BvP, and the SP's
             last-3-starts form — with edge flags so you can slice it
             however you want
  picks    : the board ranked by the model's probability that the batter
             records a hit, one per game, truncated to MAX_PICKS_PER_DAY

Edges:
  bvp_edge         : bvp_avg ≥ min_bvp_avg & bvp_pa ≥ min_bvp_pa
  hittable_sp_edge : a hot bat facing a HITTABLE starter with ≥3 starts

  These are **labels, not filters**. They ride along on every row of
  ``today`` so the board can be sliced by them, and the odds archive records
  them, but they select nothing: measured over 129 days the tagged pool swept
  49.6% of two-leg days against 58.1% for the untagged board. See the ranking
  block in ``screen_for_date``.

  BvP in particular does not survive contact. A hot bat carrying the edge hit
  65.0% (n=391) against 62.6% for a hot bat without it — 2.4 points on a
  sample whose 95% interval is ±4.8 — and it is not monotone in sample size
  (61.3% at 1-5 PA, 64.8% at 5-10, 63.7% at 10-20). It is noise, kept only as
  a label because it is the number people reach for first.

Starting-pitcher form (last 3 starts):
  The bet settles on "did this batter get a hit", so the starter's recent
  *hit* suppression matters more than his ERA. Three solo homers push a
  start over 5.00 ERA while the lineup managed four hits; a starter on that
  run beats hit props. So each row carries the SP's last-3 H/9 and BAA
  alongside ERA, and rows are banded:

    SHARP    — h9 ≤ max_sharp_h9 or baa ≤ max_sharp_baa (≥ min_sharp_starts
               starts of evidence). Vetoed out of picks entirely.
    HITTABLE — h9 ≥ min_hittable_h9 or baa ≥ min_hittable_baa
    NEUTRAL  — in between
    UNKNOWN  — the game log carried no contact line; nothing is vetoed

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
    _pitcher_form,
    _pitcher_gamelog_starts,
    _pitcher_info,
    _roster_batters,
    fetch_schedule,
    statcast_is_stale,
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
# Whether the current _warm_result was built from degraded (stale parquet)
# Statcast data. When True, warm_async re-warms after a cooldown so the screen
# self-heals once upstream recovers, instead of serving stale data all day.
_warm_stale: bool = False

# Minimum gap between re-warm attempts while serving stale data, so a sustained
# upstream outage isn't hammered on every request.
_STALE_REWARM_COOLDOWN = 300.0

# Freshness window for a healthy (non-stale) result. Probable pitchers and
# lineups get announced and changed through the day, so the morning's warm-up
# goes out of date; past this age warm_async kicks a background re-warm (while
# still serving the current cache) so the board picks the changes up.
_WARM_TTL = 90 * 60.0


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
            "stale": _warm_stale,
        }


def _do_warm(target_date: date, replace: bool = False) -> None:
    """Run screen_today() and stash the result. Runs in a daemon thread."""
    global _warm_result, _warm_date, _warming, _warm_error, _warm_stale
    try:
        logger.info("[batters] warm-up started for %s", target_date.isoformat())
        result = screen_today(verbose=False)
        with _state_lock:
            _warm_result = result
            _warm_date = target_date
            _warm_error = None
            _warm_stale = statcast_is_stale()
        logger.info(
            "[batters] warm-up complete (%d picks, %d hot, %d today)%s",
            len(result.picks), len(result.hot_bats), len(result.today),
            " [STALE]" if _warm_stale else "",
        )
        # Persist today's picks and settle prior unresolved ones. Skip the
        # persist on a stale (degraded) load: persist_screen_result is
        # insert-once and marks the day screened, so recording degraded picks
        # would pin them for good and block the catch-up from redoing the day.
        # Leaving it unrecorded lets the re-warm / catch-up record it once a
        # fresh scrape lands. Tracking failures must never take down the screen.
        #
        # replace=True on an intra-day refresh: today's slate may have changed
        # (a swapped probable), so the fresh set supersedes the morning's still-
        # pending picks rather than piling new rows on top of it.
        try:
            from sharp_edge import tracking
            if not _warm_stale:
                tracking.persist_screen_result(
                    "batter", result.picks, target_date, replace=replace
                )
                # Shadow control: what the original rules would have picked
                # today, recorded alongside and settled identically.
                tracking.persist_screen_result(
                    "batter_simple", simple_picks(result.today),
                    target_date, replace=replace,
                )
            tracking.resolve_pending()
            # Legs are graded above; the card they belong to settles from them.
            tracking.resolve_parlays()
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
    """Trigger a background warm-up if the cache is absent, aged, or stale.
    Returns the current state immediately — does not block.

    A result younger than _WARM_TTL short-circuits. Past that age the morning's
    probables/lineups may have changed, so a background re-warm is kicked (it
    doesn't re-persist — the day's picks were recorded by the first warm-up). A
    *stale* result (built from fallback parquet) refreshes on its own shorter
    cooldown so the screen self-heals when upstream recovers.
    """
    global _warming, _warm_started_at
    today = date.today()
    now = time.time()
    with _state_lock:
        have_today = _warm_result is not None and _warm_date == today
        age = (now - _warm_started_at) if _warm_started_at is not None else None
        if have_today and not _warm_stale and age is not None and age < _WARM_TTL:
            return {"status": "ready"}
        if _warming:
            return {"status": "warming"}
        # Stale-but-usable: only re-warm after the cooldown to avoid hammering
        # an upstream that's still down.
        if have_today and _warm_stale and age is not None and age < _STALE_REWARM_COOLDOWN:
            return {"status": "ready", "stale": True}
        _warming = True
        _warm_started_at = now
    # Always replace for today. Today's picks are unresolved by definition and
    # the rules running right now should own the day's list.
    #
    # This used to key off ``have_today`` — the in-process cache — so the first
    # warm after a pod restart inserted instead of replacing. Insert is
    # ON CONFLICT DO NOTHING, so a slate that had been recorded by an earlier
    # pod simply survived. Any mid-day deploy or rule change therefore left the
    # *old* pick set in the track record: on 2026-08-10 the board showed the 2
    # picks the gate passed while the track record still showed the 13 the
    # morning's ungated build had written.
    #
    # Safe because delete_picks only clears *unresolved* rows, so a settled
    # outcome can't be rewritten by a later re-screen.
    threading.Thread(
        target=_do_warm, args=(today,), kwargs={"replace": True}, daemon=True
    ).start()
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


# -----------------------------------------------------------------------------
# Starting-pitcher hit suppression
# -----------------------------------------------------------------------------
#
# League-average starters sit near 8.5 H/9 and a .250 BAA.
#
# The SHARP bars are symmetric around that — roughly a run-and-a-half of hits
# per nine under league average. The 125-day backtest (EXPERIMENTS.md, run 1)
# swept them from 5.0 to 9.0 H/9 and .17 to .27 BAA and found the curve flat
# with no knee, so they stay where they were: there is no evidence to move them.
#
# The HITTABLE bars are *not* symmetric, and that's the run-1 finding. Hit
# suppression turned out to be a tail effect rather than a gradient — hot bats
# facing starters between 6.5 and 11.0 H/9 hit within noise of each other
# (59.8–63.7%, non-monotone), and separation only appears past 11. The original
# 9.50 / .270 guess sat in the flat region and bought nothing: HITTABLE and
# NEUTRAL both came back at 61.3% over the whole board.
#
# Both bars must move together. h9 >= 11.0 is a strict subset of baa >= .270,
# so raising only H/9 leaves the BAA arm of the OR binding and changes almost
# nothing (65.0% at 21.6 picks/day, versus 67.8% at 11.8 for the H/9 rule
# alone). .310 is the knee of the BAA sweep and the value that keeps the two
# arms selecting the same population.

MAX_SHARP_H9: float = 6.50
MAX_SHARP_BAA: float = 0.210
MIN_HITTABLE_H9: float = 11.00
MIN_HITTABLE_BAA: float = 0.310
MIN_SHARP_STARTS: int = 2

# Minimum at-bats in the trailing week for a batter to be rankable.
#
# This is a *playing-time* floor, not a form filter, and the distinction is
# the reason it survived while the hot-bat gate didn't. The old ``is_hot``
# rule bundled the two together — .300 or better over at least 10 AB — and
# only the at-bats half was doing any work.
#
# It matters far more live than it looks in the backtest. On a past date the
# board is built from the boxscore, so every row is a man who actually batted;
# live it is built from ``_roster_batters``, which is the entire active roster,
# bench and fresh call-ups included. ``recent_ab`` is the only column on the
# board that answers "does he play?", and without it the top of a live board
# can be a backup catcher with a flattering career split against the hand who
# is not in the lineup — a batter the backtest can never produce and so can
# never warn about.
#
# Measured over 129 days it is free, which is the point: it costs one day of
# the sample and nothing in accuracy.
#
#     no floor          129 days   58.9% two-leg   76.7% per leg
#     recent_ab >= 10   128 days   59.4%           77.3%           <- here
#     recent_ab >= 15   127 days   58.3%           76.4%
#     recent_ab >= 20   122 days   59.0%           77.5%
#
# 234 of the 258 legs it would have chosen are unchanged; it is a guard, not a
# selector. Ten AB in seven days is roughly "started half the week".
# How many threads scan the slate.
#
# Twelve was chosen for throughput and measured badly. The screen runs inside
# the API process, which is capped at 1 CPU, and each scan does DataFrame work
# between its network calls — so twelve scanning threads and the asyncio event
# loop all compete for one GIL, and the loop loses. Measured from inside the
# cluster during a cold warm-up, /health took 30+ seconds to answer: three
# consecutive 30s timeouts, then 15.4s, then instant once the scrape passed.
# That stall read as a dead process to the liveness probe, which SIGKILLed the
# pod about 90 seconds in, restarting the same warm-up to be killed again.
#
# Four trades warm-up duration for a responsive process. It is a mitigation
# rather than a cure and should be read as one: fewer threads means shorter
# stalls, not no stalls, because the contention is structural. More CPU would
# not help either — the GIL serialises Python bytecode however many cores the
# container is given, so this is a process-isolation problem, not a capacity
# one. The cure is to stop running a multi-minute scrape inside the process
# that serves the site.
SCAN_WORKERS: int = 4

MIN_RECENT_AB_TO_RANK: int = 10

# No probability bar. Picks are the top of the board, and a threshold on top
# of a ranking earns nothing.
#
# The bar used to be 0.68, and it was measured against the wrong pool — screen
# qualifiers only. Re-measured over the same 129 days against the ranked board,
# a gate is at best neutral and mostly costs days:
#
#     gate        days played   2-leg sweep   split-half
#     none            129          58.9%      62.5% / 55.4%   <- here
#     >= 0.70         124          58.9%      61.9% / 55.7%
#     >= 0.72          91          60.4%      67.6% / 55.6%   unstable
#     >= 0.74          31          48.4%      28.6% / 54.2%   falls apart
#
# The 0.72 row is the trap: a point and a half of sweep for sitting out 38
# days, and its two halves disagree by twelve points, which is the signature
# of a threshold fit to the sample rather than to the game. Ranking already
# says which two bets are best; a bar only decides whether to skip the day,
# and the evidence doesn't support skipping.
#
# Kept as a knob (None = off) for anyone who wants to sit out thin slates.
MIN_PICK_PROBABILITY: Optional[float] = None

# How many ranked rows to keep as the day's pick list. This is the list shown
# and tracked, not the parlay — bundle.build takes the top two off the front
# of it. Uncapped is no longer an option now that picks come from the board
# rather than from a filter: the board is the whole slate, and recording 240
# "picks" a day would drown the track record in bets nobody made.
MAX_PICKS_PER_DAY: Optional[int] = 10


def _sp_band(
    form: dict,
    max_sharp_h9: float = MAX_SHARP_H9,
    max_sharp_baa: float = MAX_SHARP_BAA,
    min_hittable_h9: float = MIN_HITTABLE_H9,
    min_hittable_baa: float = MIN_HITTABLE_BAA,
    min_sharp_starts: int = MIN_SHARP_STARTS,
) -> str:
    """Classify a starter's recent contact profile: SHARP / HITTABLE /
    NEUTRAL / UNKNOWN.

    Either rate alone is enough to land in a band — H/9 and BAA disagree
    mostly when walks make an outing short, and in that case whichever one
    fires is the one carrying the signal. UNKNOWN when the game log had no
    contact line at all, which keeps a missing field from vetoing picks.

    Both bands require ``min_sharp_starts`` of evidence. HITTABLE used to be
    ungated, which let one bad outing brand a starter: 15% of HITTABLE rows in
    the run-1 boards came off one or two starts, and on opening week every
    banded pitcher had exactly one. Picks were never affected — the caller
    checks ``starts >= 3`` itself — but the label was wrong on the board and in
    ``GET /batters/pitcher-form``.
    """
    h9, baa = form.get("h9"), form.get("baa")
    if h9 is None and baa is None:
        return "UNKNOWN"
    if form.get("starts", 0) < min_sharp_starts:
        return "NEUTRAL"
    if (h9 is not None and h9 <= max_sharp_h9) or (
        baa is not None and baa <= max_sharp_baa
    ):
        return "SHARP"
    if (h9 is not None and h9 >= min_hittable_h9) or (
        baa is not None and baa >= min_hittable_baa
    ):
        return "HITTABLE"
    return "NEUTRAL"


def screen_today(
    min_recent_avg: float = 0.300,
    min_recent_ab: int = 10,
    min_bvp_avg: float = 0.400,
    min_bvp_pa: int = 5,
    max_sharp_h9: float = MAX_SHARP_H9,
    max_sharp_baa: float = MAX_SHARP_BAA,
    min_hittable_h9: float = MIN_HITTABLE_H9,
    min_hittable_baa: float = MIN_HITTABLE_BAA,
    min_sharp_starts: int = MIN_SHARP_STARTS,
    veto_sharp_sp: bool = True,
    include_hittable_edge: bool = True,
    min_pick_probability: Optional[float] = MIN_PICK_PROBABILITY,
    max_picks: Optional[int] = MAX_PICKS_PER_DAY,
    min_recent_ab_to_rank: int = MIN_RECENT_AB_TO_RANK,
    one_pick_per_game: bool = True,
    days: int = 7,
    workers: int = SCAN_WORKERS,
    verbose: bool = True,
) -> ScreenResult:
    return screen_for_date(
        date.today(),
        min_recent_avg=min_recent_avg,
        min_recent_ab=min_recent_ab,
        min_bvp_avg=min_bvp_avg,
        min_bvp_pa=min_bvp_pa,
        max_sharp_h9=max_sharp_h9,
        max_sharp_baa=max_sharp_baa,
        min_hittable_h9=min_hittable_h9,
        min_hittable_baa=min_hittable_baa,
        min_sharp_starts=min_sharp_starts,
        veto_sharp_sp=veto_sharp_sp,
        include_hittable_edge=include_hittable_edge,
        min_pick_probability=min_pick_probability,
        min_recent_ab_to_rank=min_recent_ab_to_rank,
        max_picks=max_picks,
        one_pick_per_game=one_pick_per_game,
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
    max_sharp_h9: float = MAX_SHARP_H9,
    max_sharp_baa: float = MAX_SHARP_BAA,
    min_hittable_h9: float = MIN_HITTABLE_H9,
    min_hittable_baa: float = MIN_HITTABLE_BAA,
    min_sharp_starts: int = MIN_SHARP_STARTS,
    veto_sharp_sp: bool = True,
    include_hittable_edge: bool = True,
    min_pick_probability: Optional[float] = MIN_PICK_PROBABILITY,
    max_picks: Optional[int] = MAX_PICKS_PER_DAY,
    min_recent_ab_to_rank: int = MIN_RECENT_AB_TO_RANK,
    one_pick_per_game: bool = True,
    days: int = 7,
    workers: int = SCAN_WORKERS,
    verbose: bool = True,
) -> ScreenResult:
    """Run the batter screen for an arbitrary slate date.

    For today this is the live screen (active rosters + probable pitchers).
    For past dates the slate comes from that day's schedule and boxscores,
    and BvP / pitcher form are computed as-of that morning. Known limitation
    of the historical mode: handedness splits are the player's career line
    as of now, not as of the target date — career splits move slowly, so the
    lookahead is small.

    ``picks`` is the board ranked by the model's probability that the batter
    records a hit, one batter per game, truncated to ``max_picks``. The
    edge tags are still computed onto every row of ``today`` — the board
    displays them and the odds archive records them — but they no longer
    select anything; see the ranking block below for the measurements that
    retired them.

    ``veto_sharp_sp`` and ``include_hittable_edge`` are accepted and ignored.
    They configured the retired screen, and callers (the backtest harness,
    the tests) still pass them. They are kept rather than removed so those
    call sites don't break, and are documented as dead rather than quietly
    honoured, because a flag that looks like it changes the picks and doesn't
    is worse than no flag at all. ``batters.simple_picks`` is where the old
    rules still live, as the shadow control.
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
            "p_l3": _pitcher_form(ppid, season, starts=3, before=as_of),
            # Season line for context: it says whether a three-start SHARP
            # run is who the pitcher is or a hot streak he's riding.
            "p_szn": _pitcher_form(ppid, season, starts=None, before=as_of),
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
        p_szn = r["p_szn"]
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
        band = _sp_band(
            p_l3,
            max_sharp_h9=max_sharp_h9,
            max_sharp_baa=max_sharp_baa,
            min_hittable_h9=min_hittable_h9,
            min_hittable_baa=min_hittable_baa,
            min_sharp_starts=min_sharp_starts,
        )
        p_sharp = band == "SHARP"
        p_hittable = band == "HITTABLE"

        bvp_edge = (
            bvp["avg"] is not None
            and bvp["avg"] >= min_bvp_avg
            and bvp["pa"] >= min_bvp_pa
        )
        # hand_slump_edge used to be computed here and is gone. It required a
        # .400 career average against the hand over 50+ plate appearances, and
        # that bar is close to impossible: across 129 days and 30,783 settled
        # board rows it fired exactly **zero** times. It was not a rule that
        # rarely triggered, it was a rule that could not trigger, and it cost a
        # column, a tag, three tuning knobs and a table on the page.
        # Hot bat vs. a starter who has been getting hit — no BvP or career
        # split required, so it reaches far more of the board than the two
        # edges above. On by default since run 1 of the backtest, which is also
        # what moved the HITTABLE bars to 11.0 / .310; at the original 9.5/.270
        # this edge added volume at the hit rate of simply taking any hot bat.
        # Keeps its own starts >= 3 floor, stricter than the band's.
        hittable_sp_edge = is_hot and p_hittable and p_l3["starts"] >= 3

        tags = []
        if bvp_edge:
            tags.append("BvP")
        if hittable_sp_edge:
            tags.append("HOT+HITTABLE")
        if p_sharp:
            tags.append("SHARP-SP")

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
            "p_l3_hits": p_l3["hits"],
            "p_l3_h9": p_l3["h9"],
            "p_l3_baa": p_l3["baa"],
            "p_l3_whip": p_l3["whip"],
            "p_l3_k9": p_l3["k9"],
            "p_season_h9": p_szn["h9"],
            "p_season_baa": p_szn["baa"],
            "p_season_starts": p_szn["starts"],
            "p_form": band,
            "p_sharp": p_sharp,
            "p_hittable": p_hittable,
            "is_hot": is_hot,
            "bvp_edge": bvp_edge,
            "hittable_sp_edge": hittable_sp_edge,
            "tags": ",".join(tags),
            "game_time": _local_time_str(gtime),
        })

    today_df = pd.DataFrame(rows)

    if today_df.empty:
        picks = pd.DataFrame()
    else:
        # Picks are the top of the board, not the output of the filters.
        #
        # This is a reversal, and it is the whole change. The filters — hot
        # bat, BvP edge, hittable starter, SHARP veto — were selecting *worse*
        # bets than plain "who is most likely to get a hit", and charging more
        # for them. Over 129 days, one batter per game, two legs:
        #
        #     pool                        1-leg    2-leg   per leg
        #     full screen (retired)       71.0%    49.6%     70.5%
        #     hot bat only                73.4%    52.8%     72.8%
        #     edge tags only              70.3%    47.7%     69.1%
        #     whole board (here)          78.3%    58.1%     77.1%
        #
        # It beat the screen in every month of the sample — April 70.0/57.1,
        # May 51.6/35.5, June 63.3/63.3, July 51.7/42.3, August 55.6/55.6 —
        # which is the part that makes it a finding rather than a lucky cut.
        #
        # The reason is visible in the pieces. ``recent_avg``, the hot-bat
        # gate, barely moves the outcome at all: bats under .200 hit 61.3%,
        # bats at .300-.350 hit 64.4%, and bats over .350 fall back to 61.1%.
        # It isn't monotone, so it isn't signal. ``vs_hand_avg`` — career
        # average against the hand, the model's dominant term — runs 52.4% at
        # the bottom to 74.6% at the top, cleanly. The screen was gating hard
        # on the noisy variable and leaving the real one to break ties.
        #
        # And filtering on the pitcher makes it worse, not better: requiring a
        # battered starter (L3 BAA >= .250) drops the two-leg sweep to 52.0%.
        # A good hitter facing an ordinary arm beats a hot hitter facing a bad
        # one, and it is much cheaper, because the book prices the bad arm.
        #
        # The tags are still computed and still ride along on every row — they
        # are what the board displays, and the odds snapshot records them so
        # the retired rule stays measurable. They just don't choose the bets.
        from . import pricing

        # Playing time first — see MIN_RECENT_AB_TO_RANK. Live, the board is
        # the whole active roster, and a bench bat cannot be a pick however
        # good his career split looks.
        picks = today_df.copy()
        if min_recent_ab_to_rank:
            eligible = picks["recent_ab"].fillna(0) >= min_recent_ab_to_rank
            # Never hand back an empty card because the stats feed was thin;
            # if nothing clears the floor, rank what there is.
            if eligible.any():
                picks = picks[eligible].copy()
        picks["model_p"] = [
            pricing.model_probability(r)
            for r in picks.to_dict(orient="records")
        ]
        # Career sample size breaks ties rather than recent form: between two
        # batters the model likes equally, prefer the one whose number rests
        # on more plate appearances.
        picks = picks.sort_values(
            ["model_p", "vs_hand_pa"], ascending=[False, False]
        )
        if one_pick_per_game:
            # Two batters in one game are one bet on that pitcher having a
            # bad day, not two independent reads — and the best of them is
            # the one to have. This costs about 1.6 points of measured sweep
            # (58.9% against 60.5%) and is still right: the book prices a
            # same-game pair as an SGP, below the product of its legs, so the
            # higher number was never available at the quoted price.
            picks = picks.groupby("pitcher_id", sort=False).head(1)
        if min_pick_probability:
            picks = picks[picks["model_p"] >= min_pick_probability]
        if max_picks:
            picks = picks.head(max_picks)
        picks = picks.reset_index(drop=True)

    if verbose:
        bands = today_df["p_form"].value_counts().to_dict() if not today_df.empty else {}
        print(f"[screen] sp_form={bands} picks={len(picks)}")

    return ScreenResult(picks=picks, hot_bats=hot_df, today=today_df)


def simple_picks(today_df: pd.DataFrame) -> pd.DataFrame:
    """The original screen, as a control: hot bat with a BvP or hand+slump edge.

    No veto, no probability gate, no one-per-game, no cap — the rules as they
    stood before any of it. Derived from a board that has already been built,
    so running it alongside the real screen costs nothing.

    It exists to settle an argument with evidence instead of backtests. Every
    improvement claimed since traces back to the same 129 days of boards, and
    only a handful of live picks have tested any of it; recording what the old
    rules would have picked, on the same days, under the same settlement, is
    the only way to find out which is actually better.
    """
    if today_df is None or today_df.empty:
        return pd.DataFrame()
    mask = today_df["is_hot"] & (
        today_df["bvp_edge"]
    )
    picks = today_df[mask].copy()
    if picks.empty:
        return picks
    return picks.sort_values(
        ["bvp_edge", "bvp_avg", "vs_hand_avg", "recent_avg"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)


def lookup_pitcher_form(
    pitcher_name: str, starts: int = 3, season: Optional[int] = None
) -> dict:
    """Last-N-starts form for one pitcher, plus the band the screen assigns.

    Exposed so a surprising pick (or a surprising *absence*) can be checked
    against the same numbers the screen used, rather than inferred from the
    board.
    """
    ps = statsapi.lookup_player(pitcher_name)
    if not ps:
        raise ValueError(f"pitcher not found: {pitcher_name!r}")
    pid = ps[0]["id"]
    season = season or date.today().year
    form = _pitcher_form(pid, season, starts=starts)
    return {
        "pitcher": ps[0]["fullName"],
        "pitcher_id": pid,
        "season": season,
        "window_starts": starts,
        "last_n": form,
        "season_to_date": _pitcher_form(pid, season, starts=None),
        "band": _sp_band(form),
        "game_log": [dict(g) for g in _pitcher_gamelog_starts(pid, season)[:starts]],
    }


def find_value_bats(**kwargs) -> pd.DataFrame:
    """Backwards-compatible: returns just the picks frame."""
    return screen_today(**kwargs).picks
