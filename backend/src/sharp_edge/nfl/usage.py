"""A usage projection for tight-end receiving, blended with the model's.

**Scope is one market, and the scope is the result.** Opportunity share —
a player's own share of his team's targets, times the team's target volume,
times his efficiency regressed toward the positional mean — was tested against
the *production* model over 2025 weeks 1-8, five seeds, with a bootstrap
interval on the difference:

    market          n     prod    usage    blend   blend-prod 95% CI
    TE receiving  456    16.11    15.38    15.49   [-1.00, -0.24]   significant
    RB receiving  531    11.52    11.80    11.44   [-0.39, +0.21]   straddles 0

Running-back receiving is a null and is not blended. Nor is anything else:
against a *trailing average* this decomposition won on four markets, but
production is not a trailing average and beat it by 1.5-1.8 MAE on running-back
rushing and wide-receiver receiving. Those earlier wins were a weak baseline
flattering the method.

**The blend rather than the usage projection alone**, and that is deliberate.
Usage on its own scores slightly better (15.38) but its interval runs to -0.00 —
touching zero — while the fifty-fifty blend's runs to -0.24 and is clear of it.
A better point estimate with an interval that includes zero is not a better
result.

Roughly a four per cent error reduction on one market. Small, measured, and the
only thing in this family that survived contact with the real model.
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

# Position -> the markets its usage projection is blended into.
BLENDED = {"TE": ("receiving_yards",)}

# Equal weight on the model and the usage projection. Not tuned: a weight fitted
# on the same eight weeks that measured the effect would be fitting noise, and
# the fifty-fifty split is what the interval above was computed for.
BLEND_WEIGHT = 0.5

# Prior strength when regressing a player's yards-per-target toward the
# positional mean. Matches the harness that produced the measurement.
EFFICIENCY_PRIOR_TARGETS = 40.0

# Below this the current season is not yet worth using on its own and the prior
# season is used instead. Two weeks is where the harness switched over.
MIN_CURRENT_WEEKS = 2

_cache: dict = {"key": None, "proj": {}, "fetched_at": 0.0, "error": None,
                "source": None}
_TTL_SECONDS = 6 * 3600


def _compute(season: int, week: int, position: str) -> tuple[dict, str]:
    """``({player_key: yards per game}, source)`` from everything before ``week``.

    Uses the current season once it has enough weeks, otherwise the prior one —
    the same switch the measurement used, so the shipped behaviour matches what
    was scored.
    """
    import pandas as pd

    from .names import norm_name

    def load(s):
        df = pd.read_parquet(NFLVERSE_WEEKLY.format(season=s))
        return df[df.season_type == "REG"]

    cur = load(season)
    cur = cur[cur.week < week]
    if cur.week.nunique() >= MIN_CURRENT_WEEKS:
        use, source = cur, f"{season} weeks 1-{week - 1}"
    else:
        use, source = load(season - 1), str(season - 1)

    if use.empty:
        return {}, "none"
    use = use.copy()
    for c in ("targets", "receiving_yards"):
        use[c] = use[c].fillna(0)

    # Team target volume per game, over every position — the pool being divided.
    team_targets = use.groupby("team").targets.sum()
    team_games = use.groupby("team").week.nunique().clip(lower=1)
    targets_pg = team_targets / team_games

    pos = use[use.position == position]
    if pos.empty or not team_targets.sum():
        return {}, source
    league_ypt = pos.receiving_yards.sum() / max(pos.targets.sum(), 1)

    grouped = pos.groupby(["player_id", "player_display_name", "team"]).agg(
        weeks=("week", "nunique"), targets=("targets", "sum"),
        yards=("receiving_yards", "sum"),
    ).reset_index()
    grouped = grouped[grouped.weeks >= MIN_CURRENT_WEEKS]

    out: dict = {}
    for _, r in grouped.iterrows():
        team_total = team_targets.get(r.team, 0)
        pg = targets_pg.get(r.team, 0)
        if not team_total or not pg:
            continue
        share = r.targets / team_total
        raw_ypt = (r.yards / r.targets) if r.targets else league_ypt
        ypt = ((raw_ypt * r.targets + league_ypt * EFFICIENCY_PRIOR_TARGETS)
               / (r.targets + EFFICIENCY_PRIOR_TARGETS))
        out[norm_name(r.player_display_name)] = float(share * pg * ypt)
    return out, source


def projections(season: int, week: int, position: str = "TE",
                force: bool = False) -> dict:
    """Cached ``{player_key: yards per game}``.

    Empty on any failure, and the caller then blends nothing — the model's own
    projection is the fallback, which is the state this shipped in.
    """
    key = (season, week, position)
    now = time.time()
    if (not force and _cache["key"] == key
            and now - _cache["fetched_at"] < _TTL_SECONDS and _cache["proj"]):
        return _cache["proj"]
    try:
        got, source = _compute(season, week, position)
        if got:
            _cache.update({"key": key, "proj": got, "fetched_at": now,
                           "error": None, "source": source})
        else:
            _cache["error"] = f"no usage data for {season} wk{week}"
    except Exception as e:
        logger.warning("[nfl-usage] %s wk%s failed: %s", season, week, e)
        _cache["error"] = str(e)
    return _cache["proj"] if _cache["key"] == key else {}


def source() -> Optional[str]:
    """Which season's usage the cached projections came from."""
    return _cache.get("source")


def blend(model_projection: float, key: str, position: Optional[str],
          market: str, usage: dict) -> tuple[float, Optional[float]]:
    """``(blended projection, the usage value used)``.

    Returns the model's own projection untouched wherever the blend does not
    apply, and says so by returning None for the second element — "no blend
    here" and "the blend happened to agree" are different facts and the board
    shows which.
    """
    markets = BLENDED.get((position or "").upper())
    if not markets or market not in markets:
        return model_projection, None
    u = usage.get(key)
    if u is None:
        return model_projection, None
    return (1 - BLEND_WEIGHT) * model_projection + BLEND_WEIGHT * u, u
