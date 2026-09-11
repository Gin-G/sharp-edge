"""Defence-vs-position adjustment, opponent-adjusted, for three markets.

**The metric is not raw yards allowed, and that distinction is the whole
module.** A raw allowed-per-game number measures who a defence happened to draw
as much as how it played: face three elite tight ends and it looks terrible,
face three backups and it looks elite. Neither is a statement about the
defence.

So every player is compared against *himself*. For each player-game against a
defence, the baseline is that player's production over all his **other** games
that season, and the game scores as a ratio. Average those and opponent quality
cancels, because a good tight end's baseline is high and a weak one's is low.
The average is weighted by each player's baseline volume, so a starter's game
counts for more than a fringe player's — a fringe ratio is mostly noise.

Leave-one-out is load-bearing. A season average that includes the game being
scored leaks the answer into its own baseline and shrinks every ratio toward 1.

**What ships, and what was measured and rejected.** Four week-1 samples
(2022-25), prior season only, scored against actuals. The bar is that mean
absolute error improves in at least three of the four seasons — correlation
alone is not enough, since a metric can shuffle ranks without getting closer.

    market                MAE wins   corr wins   shipped
    TE receiving yards      4/4        3/4         yes
    RB receiving yards      4/4        3/4         yes
    RB receptions           3/4        3/4         yes
    TE receptions           2/4        3/4         no
    RB rushing yards        2/4        2/4         no
    WR (any market)         0-2/4      0-1/4       no

**Wide receiver fails everywhere and the reason is worth keeping.** Yards
allowed to WR is spread across a defence's whole secondary and a team's whole
receiving corps, so it says almost nothing about the matchup one receiver
faces. What would is a shadow-coverage assignment — which corner travels with
him and whether he is any good — and nflverse publishes no such field. That is
a missing data source, not a formula to fix.

**Rushing fails for a plainer reason:** the factor is built from receiving
yards allowed, which is a statement about pass defence. It was tested against
rushing anyway and came back a coin flip, as it should have.

Coverage scheme (man/zone) was also tested and does nothing at all — regressed
for sample size the adjustment spans 0.977 to 1.015 and moved MAE by 0.03 yards
over 763 player-weeks. Not wired in anywhere.

Harnesses: ``nfl-data-py/experiments/coverage_wk1.py`` and ``fpa_adjusted.py``.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

NFLVERSE_WEEKLY = (
    "https://github.com/nflverse/nflverse-data/releases/download/stats_player/"
    "stats_player_week_{season}.parquet"
)
NFLVERSE_PBP = (
    "https://github.com/nflverse/nflverse-data/releases/download/pbp/"
    "play_by_play_{season}.parquet"
)

# Position -> the markets that earned the adjustment. See the table in the
# module docstring for everything that was tested and did not.
ADJUSTED = {
    "TE": ("receiving_yards",),
    "RB": ("receiving_yards", "receptions"),
}

# A player whose own norm is below this is dropped from the factor: a receiver
# averaging two yards a game produces ratios of 0 and 15 and nothing in
# between, which is noise wearing a number.
MIN_BASELINE_YARDS = 10.0

# One freak game must not carry a defence's whole number.
MAX_RATIO = 4.0

# A second, independent defensive factor — team success rate allowed.
#
# It is by a distance the most *stable* defensive trait measured here:
# split-half r 0.618 over 186,137 plays, against 0.397 for EPA per play and
# 0.06-0.26 for everything in the prior work. It is also computed purely from
# play-by-play, so it owes nothing to the betting market.
#
# **Stability is not predictive power, and this is the case that proves it.**
# Tested as a player-level multiplier on four week-1 samples it does almost
# nothing, because the whole spread of the factor is 0.94 to 1.06 — a six per
# cent nudge cannot move a projection far however well it is measured:
#
#     market              MAE change   seasons won
#     TE receiving          -0.24         4/4
#     WR receiving          -0.03         3/4
#     RB rushing            +0.03         2/4
#     RB receiving          +0.02         1/4
#     QB passing            -0.98         2/4
#
# Only tight end clears the bar of improving in three seasons of four, which is
# why that is the only place it is applied. It earns its place there by being
# *independent* rather than large: stacked on the existing matchup factor it
# takes TE receiving from 16.71 to 16.52 MAE, 4/4 seasons, and the two factors
# correlate only 0.110 — different information, not the same signal twice.
SUCCESS_RATE_MARKETS = {"TE": ("receiving_yards",)}

# Tighter than the main factor's clamp because the measured spread is tighter.
SR_MIN_FACTOR, SR_MAX_FACTOR = 0.90, 1.12

# How far the factor is allowed to move a projection. The measured spread over
# 2022-25 runs about 0.80 to 1.23 at the 10th and 90th percentiles, so this
# clamps the tail rather than the body — a defence that faced two elite tight
# ends in a small sample should not be allowed to move a projection by half.
MIN_FACTOR, MAX_FACTOR = 0.75, 1.30

_cache: dict = {"season": None, "factors": {}, "fetched_at": 0.0, "error": None}
_TTL_SECONDS = 6 * 3600


def _compute(season: int) -> dict:
    """``{team: factor}`` — how the receivers a defence faced did against it,
    relative to their own norms.

    Built from receiving yards for every position in ``ADJUSTED`` pooled
    together, because the question a factor answers is "does this defence
    suppress the players it covers", and splitting it by position again would
    reintroduce the small samples the leave-one-out is meant to stabilise.
    """
    import numpy as np
    import pandas as pd

    df = pd.read_parquet(NFLVERSE_WEEKLY.format(season=season))
    df = df[(df.season_type == "REG") & df.position.isin(ADJUSTED)].copy()
    if df.empty:
        return {}
    df["receiving_yards"] = df.receiving_yards.fillna(0)

    total = df.groupby(["player_id"]).receiving_yards.transform("sum")
    count = df.groupby(["player_id"]).receiving_yards.transform("size")
    # The player's mean over every game except this one.
    df["baseline"] = (total - df.receiving_yards) / (count - 1).replace(0, np.nan)
    df = df[df.baseline >= MIN_BASELINE_YARDS]
    if df.empty:
        return {}

    df["ratio"] = (df.receiving_yards / df.baseline).clip(upper=MAX_RATIO)

    out: dict = {}
    for team, rows in df.groupby("opponent_team"):
        if len(rows) < 8:
            continue
        f = float(np.average(rows.ratio, weights=rows.baseline))
        out[team] = min(MAX_FACTOR, max(MIN_FACTOR, f))
    return out


def _compute_success_rate(season: int) -> dict:
    """``{team: factor}`` — success rate allowed, over the league average.

    Success rate rather than EPA on purpose: it is bounded per play, so a
    handful of explosive plays cannot drag a defence's number the way they drag
    a mean EPA, and that is exactly why it is the more stable of the two
    (0.618 against 0.397).
    """
    import pandas as pd

    df = pd.read_parquet(
        NFLVERSE_PBP.format(season=season),
        columns=["defteam", "success", "epa"],
    )
    df = df[df.epa.notna() & df.defteam.notna()]
    if df.empty:
        return {}
    by_team = df.groupby("defteam").agg(sr=("success", "mean"), n=("success", "size"))
    by_team = by_team[by_team.n >= 300]
    if by_team.empty or not by_team.sr.mean():
        return {}
    league = by_team.sr.mean()
    return {t: float(min(SR_MAX_FACTOR, max(SR_MIN_FACTOR, v / league)))
            for t, v in by_team.sr.items()}


_sr_cache: dict = {"season": None, "factors": {}, "fetched_at": 0.0, "error": None}


def success_rate_factors(season: int, force: bool = False) -> dict:
    """Cached ``{team: factor}``. Empty on any failure, and the caller then
    applies nothing — a missing adjustment is never worse than a wrong one."""
    now = time.time()
    fresh = (not force and _sr_cache["season"] == season
             and now - _sr_cache["fetched_at"] < _TTL_SECONDS and _sr_cache["factors"])
    if fresh:
        return _sr_cache["factors"]
    try:
        got = _compute_success_rate(season)
        if got:
            _sr_cache.update({"season": season, "factors": got,
                              "fetched_at": now, "error": None})
        else:
            _sr_cache["error"] = f"no play-by-play for {season}"
    except Exception as e:
        logger.warning("[nfl-matchup] success rate for %s failed: %s", season, e)
        _sr_cache["error"] = str(e)
    return _sr_cache["factors"] if _sr_cache["season"] == season else {}


def success_rate_for(row: dict, opponent_map: dict, team_factors: dict) -> Optional[float]:
    """The success-rate adjustment for one row, or None where it does not apply."""
    markets = SUCCESS_RATE_MARKETS.get((row.get("position") or "").upper())
    if not markets or row.get("market") not in markets:
        return None
    opp = opponent_map.get(row.get("team"))
    if opp is None:
        return None
    return team_factors.get(opp)


def factors(season: int, force: bool = False) -> dict:
    """Cached ``{team: factor}`` for the season whose defences we are reading.

    Returns an empty map on any failure, and the caller then applies nothing —
    a missing matchup adjustment is a projection without it, which is the state
    this shipped in and is never worse than a wrong one.
    """
    now = time.time()
    fresh = (not force and _cache["season"] == season
             and now - _cache["fetched_at"] < _TTL_SECONDS and _cache["factors"])
    if fresh:
        return _cache["factors"]
    try:
        got = _compute(season)
        if got:
            _cache.update({"season": season, "factors": got,
                           "fetched_at": now, "error": None})
        else:
            _cache["error"] = f"no defence data for {season}"
    except Exception as e:
        logger.warning("[nfl-matchup] factors for %s failed: %s", season, e)
        _cache["error"] = str(e)
    return _cache["factors"] if _cache["season"] == season else {}


def opponents(games: list[dict]) -> dict:
    """``{team: opponent}`` from a week's schedule rows."""
    out: dict = {}
    for g in games:
        home, away = g.get("home_team"), g.get("away_team")
        if home and away:
            out[home] = away
            out[away] = home
    return out


def factor_for(row: dict, opponent_map: dict, team_factors: dict) -> Optional[float]:
    """The adjustment for one board row, or None if it does not get one.

    None rather than 1.0 on purpose: the caller shows whether a row was
    adjusted, and "no adjustment applies here" and "the adjustment happened to
    be neutral" are different statements.
    """
    markets = ADJUSTED.get((row.get("position") or "").upper())
    if not markets or row.get("market") not in markets:
        return None
    opp = opponent_map.get(row.get("team"))
    if opp is None:
        return None
    return team_factors.get(opp)
