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
