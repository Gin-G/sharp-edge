"""Defence-vs-position adjustment — for tight ends, and only tight ends.

**This module is mostly a record of what did not work**, which is the useful
part. Four week-1 samples (2022-25, prior season only, scored against actuals):

    position   n     MAE            corr
    TE       183   18.67 -> 17.92   0.371 -> 0.424
    WR       392   26.73 -> 26.94   0.472 -> 0.457
    RB       188   13.59 -> 13.67   0.312 -> 0.321

Only the tight end moves, and it moves in both directions that matter — the
error falls and the correlation rises, which is what separates a real matchup
signal from a recalibration. It holds in three of the four seasons (+0.025,
+0.134, -0.011, +0.056).

**Wide receiver is actively worse, and the reason is instructive.** "Yards
allowed to WR" is spread across a defence's whole secondary and a team's whole
receiving corps, so it says almost nothing about the matchup an individual
receiver faces. The thing that would — which corner is travelling with him, and
whether he is any good — is a shadow-coverage assignment, and nflverse does not
publish one. There is no "who is covering him" field to read. A tight end draws
a much more specific assignment, usually a linebacker or safety, so the
team-level number is closer to a real matchup for him.

**Coverage scheme was tested too and does nothing.** Man/zone rates are
available (49% of snaps are classified) and a receiver's own man-vs-zone yards
per target can be computed, but regressed for sample size the resulting
adjustment spans 0.977 to 1.015 at the 10th and 90th percentiles — a two
percent nudge — and it changed MAE by 0.03 yards across 763 player-weeks. It is
not wired in. Reviving it would need per-route matchup data rather than a
team-level rate.

Harness: ``nfl-data-py/experiments/coverage_wk1.py``.
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

# Only TE, and only receiving. See the module docstring for the three positions
# tested and the two that failed.
ADJUSTED = {"TE": ("receiving_yards", "receptions")}

# How far the factor is allowed to move a projection. The measured spread over
# 2022-25 runs about 0.80 to 1.23 at the 10th and 90th percentiles, so this
# clamps the tail rather than the body — a defence that faced two elite tight
# ends in a small sample should not be allowed to move a projection by half.
MIN_FACTOR, MAX_FACTOR = 0.75, 1.30

_cache: dict = {"season": None, "factors": {}, "fetched_at": 0.0, "error": None}
_TTL_SECONDS = 6 * 3600


def _compute(season: int) -> dict:
    """``{team: factor}`` — receiving yards that team allowed to tight ends per
    game, over the league average, for the given season."""
    import pandas as pd

    df = pd.read_parquet(NFLVERSE_WEEKLY.format(season=season))
    df = df[(df.season_type == "REG") & (df.position == "TE")].copy()
    if df.empty:
        return {}
    df["receiving_yards"] = df.receiving_yards.fillna(0)

    allowed = df.groupby("opponent_team").agg(
        yds=("receiving_yards", "sum"), games=("game_id", "nunique")
    )
    allowed = allowed[allowed.games >= 8]
    if allowed.empty:
        return {}
    per_game = allowed.yds / allowed.games
    league = per_game.mean()
    if not league:
        return {}
    return {t: float(min(MAX_FACTOR, max(MIN_FACTOR, v / league)))
            for t, v in per_game.items()}


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
            _cache["error"] = f"no TE defence data for {season}"
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
