"""Game-level predictions: score, total and margin, independent of the market.

**Why this exists and what it is not.** The board prices moneyline, spread and
total off FanDuel but has never had a number of its own to compare them to, so
those markets could be displayed and never scored. This produces that number.
It is deliberately built from results only — opponent-adjusted points scored
and allowed — so it can be checked against the line rather than derived from
it.

That last point is the whole design constraint. ``nfl_projections.ratings``
already computes an expected total, but when a line is posted it passes Vegas's
own total and spread straight through (``total_source: "vegas"``); using that
to "predict" the market would be measuring the market against itself.

**It is worse than the market, and that is expected.** Refit each week over
2021-2025, predicting week N from weeks before it (1,039 games):

    margin     SRS MAE 10.98   r +0.376    market MAE  9.73   r +0.478
    total      SRS MAE 10.69   r +0.143    market MAE 10.32   r +0.289

The margin model correlates +0.797 with the closing spread, so it is mostly
rediscovering what the market already knows, 1.25 points less accurately. No
betting signal is derived from any of this and none should be until the live
record says something the backtest does not.

**Totals are shrunk hard, because raw ratings are worse than a constant.**
Unshrunk, the off/def total reads MAE 11.73 against 10.81 for simply guessing
the league average every week. Shrinking three quarters of the way back to that
average is what makes it useful at all:

    shrink w     0.00    0.10    0.20    0.25    0.30    0.50    1.00
    total MAE   10.81   10.73   10.69   ~10.69  10.69   10.82   11.73

Margin is not shrunk — there the ratings genuinely carry signal.

Constants are measured, not assumed. Home-field advantage is +2.06 points over
2021-2025; 2019-2020 are excluded deliberately, since empty-stadium 2020 ran
+0.05 and would drag a pooled estimate down by half a point.
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)

# Results come straight from nflverse rather than through the projections API,
# whose /schedules/ copy carried 272 fixtures for 2026 and zero scores — a
# model fitted on that would silently have no current season at all.
NFLVERSE_GAMES = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"

# Mean home margin, 2021-2025. See the module docstring for why 2019-20 are out.
HOME_FIELD = 2.063

# Standard deviation of (prediction - actual). Turns a margin into a win
# probability, and it is the width that makes that probability honest: a three
# point edge against sd 13.78 is 59%, not 80%.
MARGIN_SD = 13.78
TOTAL_SD = 15.03

# How much of the ratings-based total to keep. See the table above.
TOTAL_SHRINK = 0.25

# Games before the current season's ratings stand on their own. Below this the
# prior season is blended in, or week 2 would be fitted on one result a team.
MIN_GAMES = 4

# Weight on the prior season when the current one is empty, decaying as games
# accumulate: w = PRIOR_GAMES / (PRIOR_GAMES + games played).
PRIOR_GAMES = 6.0

# How much of last season's rating still applies. Measured by regressing each
# team's offensive rating on its own the year before, 2021-22 through 2024-25:
# slopes of +0.278, +0.519, +0.440, +0.285, mean 0.381. Roster turnover erases
# roughly two thirds of a rating over an offseason, so carrying one forward at
# full strength — which is what this did first — makes the early-season board
# far too confident: week 2 of 2026 came out at a +15.9 margin where the market
# had +7.0.
PRIOR_CARRYOVER = 0.381

ITERATIONS = 60


def _ratings(games: list[dict]) -> tuple[dict, dict, float]:
    """Opponent-adjusted points scored and allowed, centred on the league.

    ``games`` are completed results: home/away team and score. Home field is
    split off both sides before fitting so a team's rating is not inflated by
    where it happened to play.
    """
    rows = []
    for g in games:
        h, a = g["home_team"], g["away_team"]
        hs, as_ = float(g["home_score"]), float(g["away_score"])
        rows.append((h, a, hs - HOME_FIELD / 2, as_ + HOME_FIELD / 2))
        rows.append((a, h, as_ + HOME_FIELD / 2, hs - HOME_FIELD / 2))
    if not rows:
        return {}, {}, 0.0

    league = sum(r[2] for r in rows) / len(rows)
    by: dict = defaultdict(list)
    for team, opp, scored, allowed in rows:
        by[team].append((opp, scored, allowed))

    off = {t: 0.0 for t in by}
    dfn = {t: 0.0 for t in by}
    for _ in range(ITERATIONS):
        new_off = {t: sum(s - league - dfn.get(o, 0.0) for o, s, _ in gs) / len(gs)
                   for t, gs in by.items()}
        new_dfn = {t: sum(a - league - off.get(o, 0.0) for o, _, a in gs) / len(gs)
                   for t, gs in by.items()}
        co = sum(new_off.values()) / len(new_off)
        cd = sum(new_dfn.values()) / len(new_dfn)
        off = {t: v - co for t, v in new_off.items()}
        dfn = {t: v - cd for t, v in new_dfn.items()}
    return off, dfn, league


def _blend(cur: dict, prior: dict, played: dict) -> dict:
    """Shade a team's current rating toward last season's by how little it has played.

    A team four games in is not yet its own ratings, and a week-2 board fitted
    on one result apiece would swing violently. The prior fades on its own as
    games accumulate rather than switching off at a threshold.
    """
    out = {}
    for team in set(cur) | set(prior):
        n = played.get(team, 0)
        w = PRIOR_GAMES / (PRIOR_GAMES + n)
        out[team] = (1 - w) * cur.get(team, 0.0) + w * prior.get(team, 0.0)
    return out


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def build(completed: list[dict], upcoming: list[dict],
          prior_completed: Optional[list[dict]] = None) -> list[dict]:
    """Predictions for ``upcoming``, fitted on ``completed`` (and last season).

    Each row carries expected points for both sides, the total, the home margin
    and a home win probability. No price, no edge, no side — this says what we
    think happens, and comparing that with the market is a separate decision
    that is not yet being made.

    Teams absent from the ratings (an expansion side, or a fixture list that
    disagrees with the results) are skipped rather than given league-average
    ratings, so a missing team shows as a missing prediction.
    """
    off, dfn, league = _ratings(completed)
    if prior_completed:
        p_off, p_dfn, p_league = _ratings(prior_completed)
        # Regress last season before leaning on it — see PRIOR_CARRYOVER.
        p_off = {t: v * PRIOR_CARRYOVER for t, v in p_off.items()}
        p_dfn = {t: v * PRIOR_CARRYOVER for t, v in p_dfn.items()}
        played: dict = defaultdict(int)
        for g in completed:
            played[g["home_team"]] += 1
            played[g["away_team"]] += 1
        off = _blend(off, p_off, played)
        dfn = _blend(dfn, p_dfn, played)
        # The league scoring average needs the same treatment as the ratings,
        # and for a sharper reason: it is a level rather than a spread, so it
        # lands on every total at once. Fifteen games into 2026 the current
        # season read 25.0 points a team against a 22.5 norm, which put every
        # projected total four points high on its own.
        n_team_games = (2 * len(completed)) / 32 if completed else 0.0
        w = PRIOR_GAMES / (PRIOR_GAMES + n_team_games)
        if p_league:
            league = (1 - w) * league + w * p_league if league else p_league
    if not off or not league:
        return []

    games_played = len(completed)
    out = []
    for g in upcoming:
        h, a = g.get("home_team"), g.get("away_team")
        if h not in off or a not in off:
            continue
        exp_h = league + off[h] + dfn.get(a, 0.0) + HOME_FIELD / 2
        exp_a = league + off[a] + dfn.get(h, 0.0) - HOME_FIELD / 2
        flat = 2 * league
        total = flat + TOTAL_SHRINK * ((exp_h + exp_a) - flat)
        margin = exp_h - exp_a
        # Re-split the shrunk total around the (unshrunk) margin so the two
        # numbers stay consistent: a reader who adds the team scores must get
        # the total back.
        exp_h = (total + margin) / 2
        exp_a = (total - margin) / 2
        out.append({
            "event": g.get("event"),
            "fd_event_id": g.get("fd_event_id"),
            "kickoff": g.get("kickoff"),
            "home_team": h, "away_team": a,
            "exp_home_points": round(exp_h, 1),
            "exp_away_points": round(exp_a, 1),
            "exp_total": round(total, 1),
            # Positive means the home side is favoured, matching nflverse's
            # spread_line so the two can be compared without a sign flip.
            "exp_margin": round(margin, 1),
            "home_win_p": round(_norm_cdf(margin / MARGIN_SD), 4),
            "margin_sd": MARGIN_SD,
            "total_sd": TOTAL_SD,
            # How much football the ratings stand on. Below MIN_GAMES they are
            # mostly last season, and the board should say so rather than
            # presenting a week-2 number with the same confidence as a week-12 one.
            "games_fitted": games_played,
            "thin": games_played < MIN_GAMES * 16,
        })
    return out


_cache: dict = {"key": None, "rows": [], "fetched_at": 0.0}
_TTL_SECONDS = 6 * 3600


def _schedule(force: bool = False) -> list[dict]:
    """Every completed and scheduled regular-season game nflverse carries."""
    now = time.time()
    if (not force and _cache["rows"]
            and now - _cache["fetched_at"] < _TTL_SECONDS):
        return _cache["rows"]
    import pandas as pd

    df = pd.read_csv(NFLVERSE_GAMES, low_memory=False)
    df = df[df["game_type"] == "REG"]
    keep = ["season", "week", "gameday", "home_team", "away_team",
            "home_score", "away_score", "spread_line", "total_line"]
    df = df[[c for c in keep if c in df.columns]]
    rows = df.where(df.notna(), None).to_dict("records")
    _cache.update({"rows": rows, "fetched_at": now})
    return rows


def for_week(season: int, week: int, force: bool = False) -> list[dict]:
    """Predictions for one week's fixtures, fitted on everything before them.

    Fits on the current season's completed games with the prior season blended
    in, which is what carries weeks 1-4 — see PRIOR_CARRYOVER. Returns [] on
    any failure rather than raising: a board that cannot reach nflverse should
    lose its game predictions, not its props.
    """
    try:
        sched = _schedule(force=force)
    except Exception as exc:
        logger.warning("[nfl-gamemodel] schedule unavailable: %s", exc)
        return []

    def played(rows):
        return [r for r in rows
                if r.get("home_score") is not None and r.get("away_score") is not None]

    cur = played([r for r in sched
                  if r.get("season") == season and (r.get("week") or 0) < week])
    prior = played([r for r in sched if r.get("season") == season - 1])
    fixtures = [r for r in sched
                if r.get("season") == season and r.get("week") == week]
    if not fixtures:
        return []

    out = build(cur, [{"home_team": f["home_team"], "away_team": f["away_team"],
                       "event": f"{f['away_team']} @ {f['home_team']}",
                       "kickoff": f.get("gameday")} for f in fixtures],
                prior_completed=prior)
    # Carry the closing lines alongside, so the record can later be scored
    # against the market as well as against the result. Not a signal — the
    # comparison is recorded, not acted on.
    lines = {(f["home_team"], f["away_team"]):
             (f.get("spread_line"), f.get("total_line")) for f in fixtures}
    for r in out:
        sp, tl = lines.get((r["home_team"], r["away_team"]), (None, None))
        r["market_spread"] = sp
        r["market_total"] = tl
        r["season"], r["week"] = season, week
    return out
