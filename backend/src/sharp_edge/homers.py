"""MLB HR probability screen — power metrics, park factors, BvP, pitcher quality.

Returns three frames:
  hot_pop  : batters with ≥ HR_LAST_15D_HOT home runs in the last 15 days
  today    : every batter in today's lineups, enriched with power metrics,
             pitcher HR-suppression stats, BvP, and park context
  picks    : HOT-POP batters facing ≥1 additional power edge

Edges:
  POWER+HAND  : ISO vs SP hand ≥ ISO_HAND_MIN (≥ ISO_HAND_PA_MIN PA) AND
                opp SP HR/9 over last 3 starts ≥ HR9_L3_MIN
  BARREL-EDGE : batter barrel% ≥ BARREL_PCT_BATTER_MIN AND
                opp SP barrel% allowed ≥ BARREL_PCT_PITCHER_MIN
  PARK-BOOST  : park HR factor for batter's hand ≥ PARK_BOOST_MIN AND
                batter ISO_career ≥ ISO_CAREER_MIN
  BVP-HR      : bvp_hr ≥ BVP_HR_MIN AND bvp_pa ≥ BVP_PA_MIN
  HOT-POP     : hr_last_15d ≥ HR_LAST_15D_HOT

  PICKS = HOT-POP AND ≥1 of {POWER+HAND, BARREL-EDGE, PARK-BOOST, BVP-HR}

deps: install with `pip install -e ".[models]"` (pybaseball, MLB-StatsAPI, pandas)
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from sharp_edge._data import (
    DEAD_STATES_SUBSTRINGS,
    _NON_AB_EVENTS,
    _boxscore_summary,
    _ip_to_outs,
    _load_statcast,
    _local_time_str,
    _pitcher_gamelog_starts,
    _pitcher_info,
    _roster_batters,
    fetch_schedule,
    statcast_is_stale,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level thresholds — tune here, one touch
# ---------------------------------------------------------------------------

# Barrel definition fallback (when Statcast 'barrel' field is absent):
# exit velocity ≥ BARREL_MIN_EV mph and launch angle in [BARREL_MIN_LA, BARREL_MAX_LA]°.
BARREL_MIN_EV: float = 98.0
BARREL_MIN_LA: int = 26
BARREL_MAX_LA: int = 50

HARDHIT_EV: float = 95.0           # hard-hit threshold (mph)
FLY_BALL_LA: int = 25              # fly-ball launch-angle floor (°)

ISO_HAND_MIN: float = 0.250        # ISO vs SP hand threshold for POWER+HAND edge
ISO_HAND_PA_MIN: int = 50          # minimum PA vs that hand to surface ISO split
HR9_L3_MIN: float = 1.5            # SP HR/9 over last 3 starts for POWER+HAND edge

# Minimum outs (innings × 3) required before HR/9 is considered meaningful.
# Without a floor a pitcher with 1 HR in 1 IP produces 9.00 HR/9 and fires
# POWER+HAND on a single-inning sample (e.g. a rookie's first start).
HR9_MIN_OUTS_L3: int = 15          # 5 IP minimum across last 3 starts
HR9_MIN_OUTS_SEASON: int = 27      # 9 IP minimum for season HR/9

BARREL_PCT_BATTER_MIN: float = 12.0   # batter barrel% for BARREL-EDGE
BARREL_PCT_PITCHER_MIN: float = 10.0  # pitcher barrel% allowed for BARREL-EDGE

PARK_BOOST_MIN: int = 110          # park HR factor threshold for PARK-BOOST edge
ISO_CAREER_MIN: float = 0.200      # batter career ISO for PARK-BOOST edge

BVP_HR_MIN: int = 1                # BvP home runs for BVP-HR edge
BVP_PA_MIN: int = 5                # BvP plate appearances for BVP-HR edge

HR_LAST_15D_HOT: int = 2           # HR count to qualify as HOT-POP

# ---------------------------------------------------------------------------
# PICKS scoring — composite hr_score ranks candidates so PICKS stays tight
# ---------------------------------------------------------------------------
#
# hr_score weights each signal independently so you can see exactly why one
# player ranks above another.  PICKS = top TOP_N_PICKS scorers from the set
# of HOT-POP players who also have ≥1 non-HOT-POP edge.
#
# Tuning guide:
#   SCORE_PER_HR_15D   — raises players in a genuine HR hot streak
#   SCORE_PER_EDGE     — each confirming edge adds this; a second edge adds
#   SCORE_EDGE_BONUS     this extra bonus (rewards multi-signal confluence)
#   SCORE_ISO_HAND     — per unit of iso_vs_hand above ISO_HAND_MIN
#   SCORE_PITCHER_HR9  — per unit of p_hr9_l3 above HR9_L3_MIN
#   SCORE_PARK         — per park-factor point above 100
#   SCORE_BARREL       — per barrel-% point above 10

TOP_N_PICKS: int = 5

SCORE_PER_HR_15D: float = 3.0
SCORE_HR_CAP: int = 8           # caps hr_15d contribution so outliers don't dominate
SCORE_PER_EDGE: float = 8.0
SCORE_EDGE_BONUS: float = 4.0   # extra per edge beyond the first
SCORE_ISO_HAND: float = 15.0
SCORE_PITCHER_HR9: float = 3.0
SCORE_PARK: float = 0.2
SCORE_BARREL: float = 0.5

# pull_air% field coordinates (Statcast bird's-eye view, home plate ≈ x=125)
PULL_HCX_LEFT: int = 100           # RHB pulls to left field (hc_x ≤ this)
PULL_HCX_RIGHT: int = 150          # LHB pulls to right field (hc_x ≥ this)

# ---------------------------------------------------------------------------
# Park HR factors — hardcoded 2023-2025 Baseball Savant averages (index 100 = avg)
# Keys match the venue name in the MLB StatsAPI schedule payload.
# ---------------------------------------------------------------------------

PARK_HR_FACTORS: dict[str, dict[str, int]] = {
    # -- high-HR parks --
    "Yankee Stadium":              {"lhb": 134, "rhb": 108, "overall": 120},
    "Great American Ball Park":    {"lhb": 128, "rhb": 124, "overall": 126},
    "Coors Field":                 {"lhb": 122, "rhb": 133, "overall": 127},
    "Globe Life Field":            {"lhb": 119, "rhb": 113, "overall": 116},
    "Guaranteed Rate Field":       {"lhb": 116, "rhb": 119, "overall": 118},
    "Citizens Bank Park":          {"lhb": 114, "rhb": 119, "overall": 117},
    "American Family Field":       {"lhb": 112, "rhb": 110, "overall": 111},
    "Rogers Centre":               {"lhb": 109, "rhb": 113, "overall": 111},
    "Oriole Park at Camden Yards": {"lhb": 111, "rhb": 112, "overall": 111},
    "Wrigley Field":               {"lhb": 113, "rhb": 109, "overall": 111},
    # -- near-neutral parks --
    "Fenway Park":                 {"lhb":  96, "rhb": 111, "overall": 103},
    "Minute Maid Park":            {"lhb": 103, "rhb":  99, "overall": 101},
    "Target Field":                {"lhb": 104, "rhb": 101, "overall": 103},
    "Progressive Field":           {"lhb": 103, "rhb": 105, "overall": 104},
    "Truist Park":                 {"lhb": 103, "rhb": 108, "overall": 105},
    "Chase Field":                 {"lhb": 107, "rhb": 105, "overall": 106},
    "Nationals Park":              {"lhb": 106, "rhb":  99, "overall": 103},
    "Busch Stadium":               {"lhb":  96, "rhb":  92, "overall":  94},
    "Kauffman Stadium":            {"lhb":  97, "rhb":  94, "overall":  96},
    "Angel Stadium":               {"lhb":  97, "rhb":  93, "overall":  95},
    "Citi Field":                  {"lhb":  93, "rhb":  88, "overall":  91},
    # -- pitcher-friendly parks --
    "PNC Park":                    {"lhb":  94, "rhb":  91, "overall":  93},
    "Dodger Stadium":              {"lhb":  97, "rhb":  94, "overall":  96},
    "T-Mobile Park":               {"lhb":  91, "rhb":  88, "overall":  90},
    "Comerica Park":               {"lhb":  88, "rhb":  86, "overall":  87},
    "Tropicana Field":             {"lhb":  88, "rhb":  91, "overall":  89},
    "loanDepot park":              {"lhb":  81, "rhb":  83, "overall":  82},
    "Petco Park":                  {"lhb":  82, "rhb":  79, "overall":  81},
    "Oracle Park":                 {"lhb":  76, "rhb":  74, "overall":  75},
    # -- Athletics 2025 temporary venue --
    "Sutter Health Park":          {"lhb":  98, "rhb":  96, "overall":  97},
}
_DEFAULT_PARK = {"lhb": 100, "rhb": 100, "overall": 100}


def _park_for_venue(venue_name: str) -> dict[str, int]:
    return PARK_HR_FACTORS.get(venue_name, _DEFAULT_PARK)


# ---------------------------------------------------------------------------
# Composite HR score
# ---------------------------------------------------------------------------

def _compute_hr_score(row: dict) -> float:
    """Weighted signal score used to rank and cap the PICKS list.

    Components (all additive):
      • Recent form    — hr_last_15d, capped at SCORE_HR_CAP
      • Edge count     — base per edge + escalating bonus for confirmation
      • Power/matchup  — iso_vs_hand premium above ISO_HAND_MIN
      • Pitcher vuln   — p_hr9_l3 premium above HR9_L3_MIN
      • Park           — park_factor premium above 100
      • Barrel quality — barrel_pct premium above 10%
    """
    edge_count = sum([
        bool(row["power_hand_edge"]),
        bool(row["barrel_edge"]),
        bool(row["park_boost_edge"]),
        bool(row["bvp_hr_edge"]),
    ])
    return round(
        min(int(row["hr_last_15d"]), SCORE_HR_CAP) * SCORE_PER_HR_15D
        + edge_count * SCORE_PER_EDGE
        + max(0, edge_count - 1) * SCORE_EDGE_BONUS
        + max(0.0, float(row["iso_vs_hand"] or 0.0) - ISO_HAND_MIN) * SCORE_ISO_HAND
        + max(0.0, float(row["p_hr9_l3"] or 0.0) - HR9_L3_MIN) * SCORE_PITCHER_HR9
        + max(0.0, float(row["park_factor"]) - 100.0) * SCORE_PARK
        + max(0.0, float(row["barrel_pct"] or 0.0) - 10.0) * SCORE_BARREL,
        1,
    )


# ---------------------------------------------------------------------------
# Daily result cache + background warm-up (mirrors batters.py)
# ---------------------------------------------------------------------------

@dataclass
class HRScreenResult:
    picks: pd.DataFrame
    hot_pop: pd.DataFrame
    today: pd.DataFrame

    def __repr__(self) -> str:
        return (
            f"HRScreenResult(picks={len(self.picks)}, "
            f"hot_pop={len(self.hot_pop)}, today={len(self.today)})"
        )


_state_lock = threading.Lock()
_warm_result: Optional[HRScreenResult] = None
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


def get_cached() -> Optional[HRScreenResult]:
    with _state_lock:
        if _warm_result is not None and _warm_date == date.today():
            return _warm_result
    return None


def warm_status() -> dict:
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


def _do_warm(target_date: date, persist: bool = True) -> None:
    global _warm_result, _warm_date, _warming, _warm_error, _warm_stale
    try:
        logger.info("[homers] warm-up started for %s", target_date.isoformat())
        result = screen_today_hr(verbose=False)
        with _state_lock:
            _warm_result = result
            _warm_date = target_date
            _warm_error = None
            _warm_stale = statcast_is_stale()
        logger.info(
            "[homers] warm-up complete (%d picks, %d hot, %d today)%s",
            len(result.picks), len(result.hot_pop), len(result.today),
            " [STALE]" if _warm_stale else "",
        )
        # Persist today's picks and settle prior unresolved ones. Skip the
        # persist on a stale (degraded) load: persist_screen_result is
        # insert-once and marks the day screened, so recording degraded picks
        # would pin them for good and block the catch-up from redoing the day.
        # Leaving it unrecorded lets the re-warm / catch-up record it once a
        # fresh scrape lands. Tracking failures must never take down the screen.
        #
        # persist=False on an intra-day refresh: the day's picks were already
        # recorded by the first warm-up, and persist is insert-once, so a
        # refresh whose slate changed (a swapped probable) would *add* the new
        # batters alongside the originals and double-count the day. The board
        # still refreshes; only the recorded pick set stays the morning's.
        try:
            from sharp_edge import tracking
            if persist and not _warm_stale:
                tracking.persist_screen_result("hr", result.picks, target_date)
            tracking.resolve_pending()
        except Exception:
            logger.exception("[homers] pick tracking failed")
    except Exception as e:
        logger.exception("[homers] warm-up failed")
        with _state_lock:
            _warm_error = f"{type(e).__name__}: {e}"
    finally:
        with _state_lock:
            _warming = False


def warm_async() -> dict:
    """Trigger a background warm-up if the cache is absent, aged, or stale.
    Non-blocking — a live result keeps serving while a refresh runs.

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
    # An intra-day refresh (we already have today's result) must not re-persist.
    threading.Thread(
        target=_do_warm, args=(today,), kwargs={"persist": not have_today}, daemon=True
    ).start()
    return {"status": "warming"}


# ---------------------------------------------------------------------------
# Per-batter Statcast metrics
# ---------------------------------------------------------------------------

def _batter_statcast_metrics(
    batter_id: int,
    sc_df: pd.DataFrame,
    season: int,
    today: date,
) -> dict:
    """Compute all power metrics for one batter from the shared Statcast cache."""
    df = sc_df[sc_df["batter"] == batter_id]
    null_result = {
        "iso_career": None, "iso_season": None,
        "iso_vs_r": None, "iso_vs_l": None,
        "barrel_pct": None, "hard_hit_pct": None,
        "hr_last_15d": 0, "hr_last_30d": 0, "pa_last_15d": 0,
        "pull_air_pct": None, "stand": None,
    }
    if df.empty:
        return null_result

    def _iso(subset: pd.DataFrame) -> Optional[float]:
        ab = (~subset["events"].isin(_NON_AB_EVENTS)).sum()
        if ab == 0:
            return None
        extra = (
            (subset["events"] == "double").sum()
            + 2 * (subset["events"] == "triple").sum()
            + 3 * (subset["events"] == "home_run").sum()
        )
        return round(int(extra) / int(ab), 3)

    # Batter's predominant stand (L/R)
    stand_series = df["stand"].dropna() if "stand" in df.columns else pd.Series([], dtype=str)
    batter_stand = str(stand_series.mode().iloc[0]) if not stand_series.empty else None

    # ISO career (all loaded seasons)
    iso_career = _iso(df)

    # ISO season
    season_df = df[df["game_date"].dt.year == season]
    iso_season = _iso(season_df)

    # ISO vs RHP / LHP (min ISO_HAND_PA_MIN PA to surface)
    if "p_throws" in df.columns:
        vs_r = df[df["p_throws"] == "R"]
        vs_l = df[df["p_throws"] == "L"]
        iso_vs_r = _iso(vs_r) if len(vs_r) >= ISO_HAND_PA_MIN else None
        iso_vs_l = _iso(vs_l) if len(vs_l) >= ISO_HAND_PA_MIN else None
    else:
        iso_vs_r = iso_vs_l = None

    # Batted-ball subset (launch_speed not null)
    batted = df[df["launch_speed"].notna()] if "launch_speed" in df.columns else pd.DataFrame()
    n_batted = len(batted)

    def _barrel_pct(subset: pd.DataFrame) -> Optional[float]:
        n = len(subset)
        if n < 10:
            return None
        if "barrel" in subset.columns and subset["barrel"].notna().any():
            return round(float(subset["barrel"].fillna(0).mean()) * 100, 1)
        if "launch_angle" in subset.columns:
            is_barrel = (
                (subset["launch_speed"] >= BARREL_MIN_EV)
                & (subset["launch_angle"] >= BARREL_MIN_LA)
                & (subset["launch_angle"] <= BARREL_MAX_LA)
            )
            return round(float(is_barrel.mean()) * 100, 1)
        return None

    barrel_pct = _barrel_pct(batted)

    hard_hit_pct: Optional[float] = None
    if n_batted >= 10:
        hard_hit_pct = round(float((batted["launch_speed"] >= HARDHIT_EV).mean()) * 100, 1)

    # Recent HR counts
    cutoff_15 = pd.Timestamp(today - timedelta(days=15))
    cutoff_30 = pd.Timestamp(today - timedelta(days=30))
    hr_last_15d = int((df[(df["game_date"] >= cutoff_15) & (df["events"] == "home_run")]).shape[0])
    hr_last_30d = int((df[(df["game_date"] >= cutoff_30) & (df["events"] == "home_run")]).shape[0])
    pa_last_15d = int(df[df["game_date"] >= cutoff_15].shape[0])

    # pull_air% — pulled fly balls / all batted balls
    pull_air_pct: Optional[float] = None
    if (
        n_batted >= 10
        and not batted.empty
        and "launch_angle" in batted.columns
        and "hc_x" in batted.columns
        and batter_stand is not None
    ):
        fly_balls = batted[batted["launch_angle"] >= FLY_BALL_LA]
        if not fly_balls.empty:
            if batter_stand == "R":
                is_pull = fly_balls["hc_x"] <= PULL_HCX_LEFT
            else:
                is_pull = fly_balls["hc_x"] >= PULL_HCX_RIGHT
            pull_air_pct = round(float(is_pull.sum()) / n_batted * 100, 1)

    return {
        "iso_career": iso_career,
        "iso_season": iso_season,
        "iso_vs_r": iso_vs_r,
        "iso_vs_l": iso_vs_l,
        "barrel_pct": barrel_pct,
        "hard_hit_pct": hard_hit_pct,
        "hr_last_15d": hr_last_15d,
        "hr_last_30d": hr_last_30d,
        "pa_last_15d": pa_last_15d,
        "pull_air_pct": pull_air_pct,
        "stand": batter_stand,
    }


# ---------------------------------------------------------------------------
# Per-pitcher Statcast metrics
# ---------------------------------------------------------------------------

def _pitcher_statcast_metrics(pitcher_id: int, sc_df: pd.DataFrame) -> dict:
    """Barrel%, hard-hit%, FB%, and HR/27PA vs each batter hand for one pitcher."""
    null = {
        "barrel_pct_allowed": None, "hard_hit_pct_allowed": None,
        "fb_pct": None, "hr9_vs_l": None, "hr9_vs_r": None,
    }
    df = sc_df[sc_df["pitcher"] == pitcher_id]
    if df.empty:
        return null

    batted = df[df["launch_speed"].notna()] if "launch_speed" in df.columns else pd.DataFrame()
    n_batted = len(batted)
    if n_batted < 10:
        return null

    # barrel% allowed
    if "barrel" in batted.columns and batted["barrel"].notna().any():
        barrel_pct = round(float(batted["barrel"].fillna(0).mean()) * 100, 1)
    elif "launch_angle" in batted.columns:
        is_barrel = (
            (batted["launch_speed"] >= BARREL_MIN_EV)
            & (batted["launch_angle"] >= BARREL_MIN_LA)
            & (batted["launch_angle"] <= BARREL_MAX_LA)
        )
        barrel_pct = round(float(is_barrel.mean()) * 100, 1)
    else:
        barrel_pct = None

    hard_hit_pct = round(float((batted["launch_speed"] >= HARDHIT_EV).mean()) * 100, 1)

    fb_pct: Optional[float] = None
    if "launch_angle" in batted.columns:
        fb_pct = round(float((batted["launch_angle"] >= FLY_BALL_LA).mean()) * 100, 1)

    # HR/27PA vs batter hand (approximate: PA-based, not IP-based)
    # Expressed per 27 PA as a proxy for per-9-innings rate.
    hr9_vs_l = hr9_vs_r = None
    if "stand" in df.columns:
        for hand, attr in (("L", "hr9_vs_l"), ("R", "hr9_vs_r")):
            sub = df[df["stand"] == hand]
            n_pa = len(sub)
            if n_pa >= 20:
                n_hr = int((sub["events"] == "home_run").sum())
                if attr == "hr9_vs_l":
                    hr9_vs_l = round(n_hr / n_pa * 27, 2)
                else:
                    hr9_vs_r = round(n_hr / n_pa * 27, 2)

    return {
        "barrel_pct_allowed": barrel_pct,
        "hard_hit_pct_allowed": hard_hit_pct,
        "fb_pct": fb_pct,
        "hr9_vs_l": hr9_vs_l,
        "hr9_vs_r": hr9_vs_r,
    }


# ---------------------------------------------------------------------------
# Pitcher HR/9 from official game log
# ---------------------------------------------------------------------------

def _pitcher_hr_gamelog(
    pitcher_id: int, season: int, before: Optional[str] = None
) -> dict:
    """Season HR/9 and last-3-starts HR/9 from the official StatsAPI game log.

    ``before`` (ISO date) restricts the log to starts strictly before that
    date, so historical screens see only what was known that morning. The raw
    game log itself is cached in _data._pitcher_gamelog_starts.
    """
    games = [
        g for g in _pitcher_gamelog_starts(pitcher_id, season)
        if not before or g["date"] < before
    ]

    total_outs = sum(_ip_to_outs(g["ip_str"]) for g in games)
    total_hr = sum(g["hr"] for g in games)
    hr9_season = (
        round(total_hr * 27 / total_outs, 2)
        if total_outs >= HR9_MIN_OUTS_SEASON
        else None
    )

    last3 = games[:3]
    l3_outs = sum(_ip_to_outs(g["ip_str"]) for g in last3)
    l3_hr = sum(g["hr"] for g in last3)
    hr9_l3 = (
        round(l3_hr * 27 / l3_outs, 2)
        if l3_outs >= HR9_MIN_OUTS_L3
        else None
    )

    return {"hr9_season": hr9_season, "hr9_l3": hr9_l3, "l3_starts": len(last3)}


# ---------------------------------------------------------------------------
# BvP HR metrics
# ---------------------------------------------------------------------------

def _bvp_hr(batter_id: int, pitcher_id: int, sc_df: pd.DataFrame) -> dict:
    df = sc_df[(sc_df["batter"] == batter_id) & (sc_df["pitcher"] == pitcher_id)]
    pa = len(df)
    hr = int((df["events"] == "home_run").sum()) if pa else 0

    bvp_barrel_pct: Optional[float] = None
    if pa >= 3:
        batted = df[df["launch_speed"].notna()] if "launch_speed" in df.columns else pd.DataFrame()
        if len(batted) >= 3:
            if "barrel" in batted.columns and batted["barrel"].notna().any():
                bvp_barrel_pct = round(float(batted["barrel"].fillna(0).mean()) * 100, 1)
            elif "launch_angle" in batted.columns:
                is_barrel = (
                    (batted["launch_speed"] >= BARREL_MIN_EV)
                    & (batted["launch_angle"] >= BARREL_MIN_LA)
                    & (batted["launch_angle"] <= BARREL_MAX_LA)
                )
                bvp_barrel_pct = round(float(is_barrel.mean()) * 100, 1)

    return {"bvp_hr": hr, "bvp_pa": pa, "bvp_barrel_pct": bvp_barrel_pct}


# ---------------------------------------------------------------------------
# Main screen
# ---------------------------------------------------------------------------

def screen_today_hr(workers: int = 12, verbose: bool = True) -> HRScreenResult:
    """Run the full HR probability screen for today's slate.

    Returns an HRScreenResult with three DataFrames:
      - hot_pop  : batters with ≥ HR_LAST_15D_HOT HRs in last 15 days
      - today    : all batters facing a probable pitcher, with full metrics
      - picks    : HOT-POP batters with ≥1 power edge
    """
    return screen_hr_for_date(date.today(), workers=workers, verbose=verbose)


def screen_hr_for_date(
    target_date: date, workers: int = 12, verbose: bool = True
) -> HRScreenResult:
    """Run the HR screen for an arbitrary slate date.

    For today this is the live screen: active rosters + probable pitchers.
    For past dates the slate is reconstructed from that day's schedule and
    boxscores — candidates are the players who actually dressed, and the
    actual starter fills in when no probable pitcher was recorded. Every
    stat is computed as-of that morning: Statcast events strictly before
    the date, pitcher game logs restricted to earlier starts.
    """
    today = target_date
    season = today.year
    is_live = today >= date.today()
    as_of = today.isoformat()

    sc_df = _load_statcast()
    sc_df = sc_df[sc_df["game_date"] < pd.Timestamp(today)]
    raw_games = fetch_schedule(today)

    # Build (batter_id, batter_name, team, pitcher_id, pitcher_name, game_time, venue) tuples
    targets: list[tuple] = []
    games_kept = 0
    games_with_pp = 0

    for game in raw_games:
        status = (game.get("status", {}) or {}).get("detailedState", "") or ""
        if any(bad in status for bad in DEAD_STATES_SUBSTRINGS):
            continue
        games_kept += 1

        venue_name = (game.get("venue") or {}).get("name", "")
        away = game["teams"]["away"]
        home = game["teams"]["home"]
        away_team_id = away["team"]["id"]
        home_team_id = home["team"]["id"]
        away_team_name = away["team"].get("name", "")
        home_team_name = home["team"].get("name", "")

        away_pp = away.get("probablePitcher") or {}
        home_pp = home.get("probablePitcher") or {}
        away_pp_id = away_pp.get("id")
        home_pp_id = home_pp.get("id")
        away_pp_name = away_pp.get("fullName", "")
        home_pp_name = home_pp.get("fullName", "")

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
                targets.append((bid, bname, batting_team, opp_pid, opp_pname, gtime, venue_name))

    if verbose:
        print(
            f"[hr_screen] schedule={len(raw_games)} kept={games_kept} "
            f"with_probable_pitcher={games_with_pp} targets={len(targets)}"
        )

    if not targets:
        empty = pd.DataFrame()
        return HRScreenResult(picks=empty, hot_pop=empty, today=empty)

    # Pre-compute pitcher metrics once per unique pitcher (avoids redundant df scans)
    pitcher_ids = {t[3] for t in targets}
    p_info_map = {pid: _pitcher_info(pid) for pid in pitcher_ids}
    p_sc_map = {pid: _pitcher_statcast_metrics(pid, sc_df) for pid in pitcher_ids}
    p_hr_map = {pid: _pitcher_hr_gamelog(pid, season, before=as_of) for pid in pitcher_ids}

    def _scan_batter(t: tuple) -> dict:
        bid, bname, team, ppid, ppname, gtime, venue_name = t
        return {
            "target": t,
            "bm": _batter_statcast_metrics(bid, sc_df, season, today),
            "bvp": _bvp_hr(bid, ppid, sc_df),
        }

    with ThreadPoolExecutor(max_workers=workers) as pool:
        scanned = list(pool.map(_scan_batter, targets))

    rows = []
    for r in scanned:
        bid, bname, team, ppid, ppname, gtime, venue_name = r["target"]
        bm = r["bm"]
        bvp = r["bvp"]
        pm = p_sc_map[ppid]
        ph = p_hr_map[ppid]
        p_hand = p_info_map[ppid]["hand"]
        park = _park_for_venue(venue_name)

        # ISO vs today's pitcher's hand
        if p_hand == "R":
            iso_vs_hand = bm["iso_vs_r"]
        elif p_hand == "L":
            iso_vs_hand = bm["iso_vs_l"]
        else:
            iso_vs_hand = None

        # Park factor for batter's batting hand
        batter_stand = bm.get("stand")
        if batter_stand == "L":
            park_factor = park["lhb"]
        elif batter_stand == "R":
            park_factor = park["rhb"]
        else:
            park_factor = park["overall"]

        # Edge flags
        power_hand_edge = bool(
            iso_vs_hand is not None
            and iso_vs_hand >= ISO_HAND_MIN
            and ph["hr9_l3"] is not None
            and ph["hr9_l3"] >= HR9_L3_MIN
        )
        barrel_edge = bool(
            bm["barrel_pct"] is not None
            and bm["barrel_pct"] >= BARREL_PCT_BATTER_MIN
            and pm["barrel_pct_allowed"] is not None
            and pm["barrel_pct_allowed"] >= BARREL_PCT_PITCHER_MIN
        )
        park_boost_edge = bool(
            park_factor >= PARK_BOOST_MIN
            and bm["iso_career"] is not None
            and bm["iso_career"] >= ISO_CAREER_MIN
        )
        bvp_hr_edge = bool(
            bvp["bvp_hr"] >= BVP_HR_MIN
            and bvp["bvp_pa"] >= BVP_PA_MIN
        )
        hot_pop = bool(bm["hr_last_15d"] >= HR_LAST_15D_HOT)
        is_pick = hot_pop and (power_hand_edge or barrel_edge or park_boost_edge or bvp_hr_edge)

        tags = [
            name for name, flag in (
                ("POWER+HAND", power_hand_edge),
                ("BARREL-EDGE", barrel_edge),
                ("PARK-BOOST", park_boost_edge),
                ("BVP-HR", bvp_hr_edge),
                ("HOT-POP", hot_pop),
            )
            if flag
        ]

        # HR/9 vs pitcher hand from Statcast (display only)
        if p_hand == "R":
            p_hr9_vs_hand = pm.get("hr9_vs_r")
        elif p_hand == "L":
            p_hr9_vs_hand = pm.get("hr9_vs_l")
        else:
            p_hr9_vs_hand = None

        rows.append({
            "batter": bname,
            "batter_id": bid,
            "team": team,
            "opposing_pitcher": ppname,
            "pitcher_id": ppid,
            "p_hand": p_hand,
            "game_time": _local_time_str(gtime),
            "venue": venue_name,
            "park_factor": park_factor,
            # batter power metrics
            "iso_career": bm["iso_career"],
            "iso_season": bm["iso_season"],
            "iso_vs_r": bm["iso_vs_r"],
            "iso_vs_l": bm["iso_vs_l"],
            "iso_vs_hand": iso_vs_hand,
            "barrel_pct": bm["barrel_pct"],
            "hard_hit_pct": bm["hard_hit_pct"],
            "hr_last_15d": bm["hr_last_15d"],
            "hr_last_30d": bm["hr_last_30d"],
            "pa_last_15d": bm["pa_last_15d"],
            "pull_air_pct": bm["pull_air_pct"],
            # pitcher metrics
            "p_hr9_season": ph["hr9_season"],
            "p_hr9_l3": ph["hr9_l3"],
            "p_barrel_pct": pm["barrel_pct_allowed"],
            "p_hard_hit_pct": pm["hard_hit_pct_allowed"],
            "p_fb_pct": pm["fb_pct"],
            "p_hr9_vs_hand": p_hr9_vs_hand,
            # BvP
            "bvp_hr": bvp["bvp_hr"],
            "bvp_pa": bvp["bvp_pa"],
            "bvp_barrel_pct": bvp["bvp_barrel_pct"],
            # edges / flags
            "power_hand_edge": power_hand_edge,
            "barrel_edge": barrel_edge,
            "park_boost_edge": park_boost_edge,
            "bvp_hr_edge": bvp_hr_edge,
            "hot_pop": hot_pop,
            "is_pick": is_pick,
            "tags": ",".join(tags),
        })

    today_df = pd.DataFrame(rows)

    if today_df.empty:
        empty = pd.DataFrame()
        return HRScreenResult(picks=empty, hot_pop=empty, today=today_df)

    today_df["hr_score"] = today_df.apply(
        lambda r: _compute_hr_score(r.to_dict()), axis=1
    )

    hot_df = (
        today_df[today_df["hot_pop"]]
        .sort_values("hr_last_15d", ascending=False)
        .reset_index(drop=True)
    )
    picks_df = (
        today_df[today_df["is_pick"]]
        .sort_values("hr_score", ascending=False)
        .head(TOP_N_PICKS)
        .reset_index(drop=True)
    )

    return HRScreenResult(picks=picks_df, hot_pop=hot_df, today=today_df)
