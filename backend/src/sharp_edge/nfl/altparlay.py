"""Alt-line parlays: a double-up, and a thousand-to-one lotto.

Both are built from FanDuel's alternate ladders — "Chuba Hubbard 25+ Yards" —
rather than the main lines, and both lean on the same two questions about each
rung: has the player been clearing it, and does the model price it above what
the book charges.

**Why a recent record is a filter and not the probability.** Measured on every
2025-26 player-week at FanDuel's rungs, a player who cleared a rung in all four
of his last games clears it the next week 70-85% of the time, not 100%, and how
far his average sits above the rung matters more than the streak (85-91% at
twice the rung, ~60% just above it). Priced against this week's ladders, the
streaks the book charges -600 to -1500 for come in BELOW their price (record
88% against 93% implied, 84% against 88%), while between -500 and -100 they
run at or slightly above it. So the record picks who is in a role, and the
model — fitted on exactly these rungs, see scripts/calibrate_nfl.py — prices
the leg.

**The probability is the props' own, with the props' own correction.** Each rung
is priced by ``model.prop_probability`` off the same adjusted projection as the
player's main line, centred on the market and shrunk exactly as the main line
is, and selected against the props' over bar (``card.MIN_EDGE_PTS`` to
``card.MAX_EDGE_PTS``). An alt ladder has no under to devig, so the fair price
is the rung's quoted probability scaled by the vig the same player's main line
carries.

The parlay's chance is then read with ``card.CALIBRATION_KEEP`` — the model's
measured overconfidence (it claimed 60% on legs that won 51%) — so the number
shown beside a ticket is the one the record says it lands at, not the model's.

**Neither is expected to be +EV.** Every leg pays the book's margin and a
parlay compounds it; a ten-leg ticket at a typical ladder margin gives up ~40%
before any edge. These are built to be the best version of a bet the owner
wants to make, and recorded so the record can say whether "best" is enough.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Iterable, Optional

from . import card as card_mod, model

logger = logging.getLogger(__name__)

NFLVERSE_WEEKLY = (
    "https://github.com/nflverse/nflverse-data/releases/download/stats_player/"
    "stats_player_week_{season}.parquet"
)

# A rung counts as "has been hitting" at three of the player's last four.
RECORD_GAMES = 4
MIN_HITS = 3

# Payout targets, as decimal odds, and the price window each draws legs from.
# The double-up stops at -500 because shorter than that the book prices the
# streak above what it earns (see the docstring); the lotto takes the same
# floor and reaches further up the ladder for payout.
KINDS = {
    "double_up": {"target": 2.0, "odds": (-500, 150), "min_legs": 2, "stake": 1.0},
    "lotto": {"target": 1000.0, "odds": (-500, 400), "min_legs": 2, "stake": 0.1},
}
# FanDuel's own parlay limit.
MAX_LEGS = 25

# Paper strategies: built and recorded daily beside the two above, never bet
# by the board, so the record can say which way of reaching a long price
# actually holds up. See ``build_paper``.
#
#   short_stack  one leg per game, the likeliest rung between -250 and -110,
#                stacked as far as the slate allows. The leg rules leave only
#                ten or so games with such a rung, so this tops out near 50-90x,
#                not 1000x — which is itself the finding: "stack -140s to a
#                thousand" is not buildable from legs we would back.
#   anchored     the lotto rebuilt as short rungs (-300 to -100) plus at most
#                two anchors at +100 to +400 from the strongest signals on the
#                board: the anytime-TD picks and the game model's moneyline
#                picks. Fewer legs at the same payout pays the margin fewer
#                times. The anchor cap is what keeps it a stack of short legs:
#                uncapped, the build reaches for +1600 touchdowns, where the
#                model's untested TD edge is largest, and five legs carry it.
PAPER = {
    "short_stack": {"target": None, "odds": (-250, -110), "min_legs": 4, "stake": 0.1},
    "anchored": {"target": 1000.0, "odds": (-300, -100), "min_legs": 2, "stake": 0.1,
                 "max_anchors": 2, "anchor_odds": (100, 400)},
}
STRATEGIES = {**KINDS, **PAPER}

_cache: dict = {"key": None, "values": {}, "fetched_at": 0.0}
_TTL_SECONDS = 6 * 3600


def _decimal(american: int) -> float:
    return 1 + (american / 100 if american > 0 else 100 / -american)


def _american(dec: float) -> int:
    return round((dec - 1) * 100) if dec >= 2 else round(-100 / (dec - 1))


def recent_values(season: int, week: int, force: bool = False) -> dict:
    """``{player_key: {market: [last four values]}}`` from games before ``week``.

    Reaches into the prior season for players with fewer than four games this
    year, the same way the trailing form the projections use spans seasons.
    """
    key = (season, week)
    now = time.time()
    if (not force and _cache["key"] == key
            and now - _cache["fetched_at"] < _TTL_SECONDS and _cache["values"]):
        return _cache["values"]
    import pandas as pd

    from .names import norm_name

    frames = []
    for s in (season - 1, season):
        try:
            df = pd.read_parquet(NFLVERSE_WEEKLY.format(season=s))
        except Exception as e:
            logger.warning("[nfl-alt] weekly stats %s unavailable: %s", s, e)
            continue
        df = df[df.season_type == "REG"]
        if s == season:
            df = df[df.week < week]
        frames.append(df)
    if not frames:
        return {}
    df = pd.concat(frames).sort_values(["season", "week"])
    df["key"] = df.player_display_name.map(norm_name)
    out: dict = {}
    for k, g in df.groupby("key"):
        g = g.tail(RECORD_GAMES)
        out[k] = {m: [float(v) if v == v else 0.0 for v in g[m]]
                  for m in ("receiving_yards", "receptions", "rushing_yards")}
    _cache.update({"key": key, "values": out, "fetched_at": now})
    return out


def rung_fair(odds: int, main_over: Optional[int],
              main_fair: Optional[float]) -> Optional[float]:
    """A fair price for a one-way rung: its quote, less the main line's vig.

    None when the main line has no two-way price to read the vig off.
    """
    from ..fanduel.odds import american_to_implied

    if odds is None or main_over is None or main_fair is None:
        return None
    main_implied = american_to_implied(main_over)
    if not main_implied:
        return None
    return min(0.99, american_to_implied(odds) * (main_fair / main_implied))


def price_rung(odds: int, p_shrunk: float, fair: float) -> Optional[dict]:
    """Edge and record-calibrated probability for one rung.

    ``p_shrunk`` is the model's read on the rung after the same centring and
    shrink toward ``fair`` that the main line gets.
    """
    from ..fanduel.odds import american_to_implied

    if not (0 < p_shrunk < 1) or not (0 < fair < 1):
        return None
    lf = math.log(fair / (1 - fair))
    z = lf + card_mod.CALIBRATION_KEEP * (math.log(p_shrunk / (1 - p_shrunk)) - lf)
    p_cal = 1 / (1 + math.exp(-z))
    return {"implied": round(american_to_implied(odds), 4), "fair": round(fair, 4),
            "model_p": round(p_shrunk, 4), "p_cal": round(p_cal, 4),
            "edge_pts": round(100 * (p_shrunk - fair), 1)}


def candidates(rungs: Iterable[dict]) -> list[dict]:
    """Rungs that clear every leg rule. See the module docstring for each."""
    out = []
    for r in rungs:
        last = r.get("last4") or []
        if len(last) < RECORD_GAMES or (r.get("hits4") or 0) < MIN_HITS:
            continue
        if r.get("market") not in model.BETTABLE:
            continue
        edge = r.get("edge_pts")
        if edge is None or not (card_mod.MIN_EDGE_PTS <= edge <= card_mod.MAX_EDGE_PTS):
            continue
        if r.get("role_conflict_verdict") == "market" or not card_mod._available(r):
            continue
        if card_mod.is_locked(r):
            continue                # its game is inside the lock: not a leg we can add
        out.append(r)
    return out


def build(cands: Iterable[dict], kind: str,
          anchors: Iterable[dict] = ()) -> Optional[dict]:
    """The likeliest ticket that reaches ``kind``'s payout, one leg per game.

    Exact, not greedy: a knapsack over games, maximising the summed log
    probability subject to the summed log payout reaching the target. Greedy
    by probability-per-payout overshot the week-8 double-up to +283 — a 24%
    ticket built to be a coin flip — because the last leg it added was the
    longest price it had. One leg per game keeps the legs independent, which is
    what makes multiplying their chances honest.

    Payout is bucketed by flooring each leg's log-decimal, so a ticket that
    reaches the target bucket is guaranteed to reach the target itself.

    ``anchors`` (see ``anchor_legs``) compete for their game's slot like any
    leg but skip the price window, up to the strategy's ``max_anchors``.
    """
    spec = STRATEGIES[kind]
    lo, hi = spec["odds"]
    max_anchors = spec.get("max_anchors", 0)
    by_game: dict = {}
    for c in cands:
        if (c.get("odds") is not None and lo <= c["odds"] <= hi
                and c.get("p_cal") and 0 < c["p_cal"] < 1):
            by_game.setdefault(c.get("fd_event_id"), []).append(c)
    if max_anchors:
        a_lo, a_hi = spec["anchor_odds"]
        for c in anchors:
            if (c.get("odds") is not None and a_lo <= c["odds"] <= a_hi
                    and c.get("p_cal") and 0 < c["p_cal"] < 1):
                by_game.setdefault(c.get("fd_event_id"), []).append(c)

    step = 0.01
    goal = math.ceil(math.log(spec["target"]) / step)
    # (payout bucket, legs, anchors) -> (log probability, legs chosen)
    dp: dict = {(0, 0, 0): (0.0, ())}
    for legs in by_game.values():
        nxt = dict(dp)
        for (b, k, a), (v, chosen) in dp.items():
            if k >= MAX_LEGS:
                continue
            for c in legs:
                na = a + bool(c.get("anchor"))
                if na > max_anchors:
                    continue
                key = (min(goal, b + math.floor(math.log(_decimal(c["odds"])) / step)),
                       k + 1, na)
                cand = (v + math.log(c["p_cal"]), chosen + (c,))
                if key not in nxt or cand[0] > nxt[key][0]:
                    nxt[key] = cand
        dp = nxt
    done = [val for (b, k, a), val in dp.items()
            if b >= goal and k >= spec["min_legs"]]
    if not done:
        return None
    _, chosen = max(done, key=lambda t: t[0])
    return summarise(sorted(chosen, key=lambda c: c.get("kickoff") or ""), kind)


def stack(cands: Iterable[dict], kind: str = "short_stack") -> Optional[dict]:
    """Every game's likeliest rung inside the price window, all of them."""
    spec = STRATEGIES[kind]
    lo, hi = spec["odds"]
    best: dict = {}
    for c in cands:
        if (c.get("odds") is not None and lo <= c["odds"] <= hi
                and c.get("p_cal") and 0 < c["p_cal"] < 1):
            g = c.get("fd_event_id")
            if g not in best or c["p_cal"] > best[g]["p_cal"]:
                best[g] = c
    if len(best) < spec["min_legs"]:
        return None
    legs = sorted(best.values(), key=lambda c: c.get("kickoff") or "")[:MAX_LEGS]
    return summarise(legs, kind)


def anchor_legs(td_picks: Iterable[dict], game_model: Iterable[dict],
                game_of_team: dict) -> list[dict]:
    """The anchored lotto's plus-money candidates, as parlay legs.

    Anytime-TD picks (card.td_suggestions, already availability-checked) and
    the game model's moneyline picks, at plus money only — a short anchor is
    just another short leg. The strategy's ``anchor_odds`` caps the top. A moneyline row carries no FanDuel event id, so
    its game is found through either team's other markets; one with no match
    cannot share the one-leg-per-game rule and is left out.
    """
    out = []
    for r in td_picks:
        if r.get("odds") is None or r["odds"] < 100 or card_mod.is_locked(r):
            continue
        out.append({"anchor": "td", "market": "anytime_td", "player": r.get("player"),
                    "key": r.get("key"), "team": r.get("team"),
                    "position": r.get("position"), "line": 0.5, "threshold": 1.0,
                    "side": "OVER", "odds": r["odds"], "p_cal": r.get("p_cal"),
                    "implied": r.get("implied_p"), "edge_pts": r.get("true_edge_pts"),
                    "event": r.get("event"), "fd_event_id": r.get("fd_event_id"),
                    "kickoff": r.get("kickoff"), "fd_market_id": r.get("fd_market_id"),
                    "over_selection_id": r.get("fd_selection_id")})
    for g in game_model:
        sim = g.get("sim") or {}
        for pk in sim.get("picks") or []:
            if pk.get("market") != "moneyline" or (pk.get("odds") or 0) < 100:
                continue
            team = pk.get("team")
            game = (game_of_team.get(g.get("home_team"))
                    or game_of_team.get(g.get("away_team")))
            leg = {"anchor": "ml", "market": "moneyline", "player": team,
                   "key": f"ml:{team}", "team": team,
                   "home_team": g.get("home_team"), "away_team": g.get("away_team"),
                   "line": 0.0, "side": pk.get("side"), "odds": pk["odds"],
                   "p_cal": pk.get("p"), "edge_pts": pk.get("edge_pts"),
                   "event": g.get("event"), "fd_event_id": game,
                   "kickoff": g.get("kickoff")}
            if game is None or card_mod.is_locked(leg):
                continue
            out.append(leg)
    return out


def build_paper(cands: list[dict], anchors: list[dict]) -> dict:
    """Every paper strategy for this board. None where the slate cannot build it."""
    return {"short_stack": stack(cands, "short_stack"),
            "anchored": build(cands, "anchored", anchors)}


def summarise(legs: list[dict], kind: str) -> dict:
    dec = math.prod(_decimal(l["odds"]) for l in legs)
    p = math.prod(l["p_cal"] for l in legs)
    stake = STRATEGIES[kind]["stake"]
    return {"kind": kind, "legs": legs, "leg_count": len(legs),
            "decimal_odds": round(dec, 2), "american": _american(dec),
            "model_p": round(p, 6), "stake": stake,
            "to_win": round(stake * (dec - 1), 2),
            "ev": round(p * dec - 1, 4)}
