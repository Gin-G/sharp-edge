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
NFLVERSE_SNAPS = (
    "https://github.com/nflverse/nflverse-data/releases/download/snap_counts/"
    "snap_counts_{season}.parquet"
)

# A rung counts as "has been hitting" at three of the player's last four.
RECORD_GAMES = 4
MIN_HITS = 3

# --- the role lotto ----------------------------------------------------------
#
# The lotto's legs, from week 9 on: players in a steady role, at rungs their
# own last five games and opportunity say they clear, priced above the book.
#
# Week 8's lotto drew backups — 26-snap tight ends at 5+ yards — because the
# streak rule cannot tell a role from a run of short rungs. Measured on every
# 2024-26 WR/TE/RB player-week at FanDuel's rung spacing (scripts in
# EXPERIMENTS_NFL.md, "the role lotto"):
#
# * Cushion — median of the last five over the rung — carries most of it; the
#   five-game streak adds to it, and so do target share and the snap floor.
#   Among legs streak and cushion alone rate 60-75%, the low-role quarter hit
#   59% and the steady-role quarter 70%.
# * The logistic fit below, trained on 2024-25, is calibrated on 2026 it never
#   saw: said 65 / hit 66, said 75 / hit 75, said 85 / hit 85.
# * Against week 8's archived ladders (the only priced week), the rungs where
#   our model saw an edge on a low-role player hit 29%; the same edge on a
#   player over the role floor hit 50% against 52% implied. The book knows who
#   is a backup. So the floor is a rule, not a weight.
ROLE_GAMES = 5
ROLE_MIN_HITS = 4
ROLE_SNAP_MIN = 0.50          # every one of his last five games
ROLE_TS_MIN = 0.12            # average target share over them
CONF_INTERCEPT = -1.835
CONF_COEF = {"hits5": 0.2313, "lcush": 1.3499, "rec": 0.4416, "tgt_mean": -0.0344,
             "snap_min": 0.4644, "ts_mean": 3.1899, "tgt_cv": 0.3594}
# Edge over the rung's fair price, on the confidence model, before a leg counts.
CONF_MIN_EDGE = 0.03
# The confidence model is calibrated on outcomes, not against the book; on the
# one priced week its edges came in at about half. Legs are priced halfway
# (in logits) between the book's fair price and the model.
CONF_KEEP = 0.5

# Rushing, back in the lotto on its own model (2026-11-06). Rushing left the
# board in Finding 14 because the props model loses on it; that model gets no
# say here. Fitted the same way on every 2024-25 RB/QB/WR rushing player-week
# (scripts/role_lotto/rush_frame.py, fit_rush.py) and held out on 2026: said
# 55 / hit 54, said 75 / hit 71, said 85 / hit 85. Role is carry share: legs
# rated 55-80% hit 69% for a back with 40%+ of his team's carries and 65%
# without; the snap floor added nothing. Against week 8's alt rushing prices
# the role legs hit 53% at 61% implied — no edge on the one priced week.
ROLE_CS_MIN = 0.40            # average share of his team's carries
RUSH_INTERCEPT = -1.8101
RUSH_COEF = {"hits5": 0.3174, "lcush": 1.4916, "car_mean": -0.0472, "snap_min": 0.6164,
             "cs_mean": 1.8977, "car_cv": 0.222, "qb": -0.3739}
RUSH = "rushing_yards"
ROLE_MARKETS = ("receiving_yards", "receptions", RUSH)

# Anytime touchdowns, for the likely scorers only: a fair chance of at least
# TD_MIN_FAIR, which on FanDuel is about even money or shorter. Priced at the
# book's own chance, not ours — the TD model is ungraded past one week, and on
# the short end it sits *below* the book (Gibbs -400: book 80%, model 69%).
# FanDuel posts no "No" side to de-vig against, so TD_FAIR_SHADE is an assumed
# margin on a favourite's quote.
TD_MIN_FAIR = 0.45
TD_FAIR_SHADE = 0.92
# Alt spreads and totals, held to the same kind of bar: a fair chance of at
# least half, about -115 or shorter. Uncapped, the first week-9 build took two
# plus-money totals at 38% and 42% to fill games with no role leg — a slot
# filled with a coin flip, which is the leg this lotto exists to avoid.
GAME_MIN_FAIR = 0.50

# Payout targets, as decimal odds, and the price window each draws legs from.
# The double-up stops at -500 because shorter than that the book prices the
# streak above what it earns (see the docstring); the lotto takes the same
# floor and reaches further up the ladder for payout.
KINDS = {
    "double_up": {"target": 2.0, "odds": (-500, 150), "min_legs": 2, "stake": 1.0},
    "lotto": {"target": 1000.0, "odds": (-300, 150), "min_legs": 2, "stake": 0.1,
              "floor": 250.0, "per_game": 4},
}
# FanDuel's own parlay limit.
MAX_LEGS = 25
# FanDuel bundles the legs a parlay takes from one game into a same-game
# parlay and prices that bundle itself, below the product of its legs. This is
# the assumed price of each bundle against that product — a placeholder until
# a betslip quote calibrates it. A ticket with a bundle carries
# ``price_estimated``.
SGP_SHADE = 0.85

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
    # The lotto as it was built through week 8 — the 3-of-4 streak rule, any
    # role — kept on paper as the control for the role lotto.
    "lotto_streak": {"target": 1000.0, "odds": (-500, 400), "min_legs": 2, "stake": 0.1},
    # The role lotto held to one leg per game: every price a straight quote,
    # every leg independent — the control for the lotto's same-game bundles.
    "lotto_one": {"target": 1000.0, "odds": (-300, 150), "min_legs": 2, "stake": 0.1,
                  "floor": 250.0},
    # The role lotto as it was through week 9: receiving rungs only, no
    # rushing, touchdowns or game lines — the control for adding them.
    "lotto_receiving": {"target": 1000.0, "odds": (-300, 150), "min_legs": 2,
                        "stake": 0.1, "floor": 250.0, "per_game": 4},
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
    """``{player_key: {market: [last five values], "_role": {...} | None}}``.

    Built from games before ``week``, reaching into the prior season for
    players with fewer than five this year, the same way the trailing form the
    projections use spans seasons.

    **A game is a game he played, not a game he recorded a stat in.** The log
    is snap counts joined to the box score, with zeros where he took the field
    and did nothing. Built from box-score rows alone, a backup's "4 for 4"
    skipped the weeks he was on the field for 20 snaps and never targeted.

    ``_role`` is his opportunity over the same five games — the snap floor,
    target share, targets a game and how steady they were, and the same for
    carries — and None unless all five were for his current team (a mover's
    old role says little about his new one) and every one has a snap count.
    """
    key = (season, week)
    now = time.time()
    if (not force and _cache["key"] == key
            and now - _cache["fetched_at"] < _TTL_SECONDS and _cache["values"]):
        return _cache["values"]
    import pandas as pd

    from .names import norm_name

    stats_cols = ["receiving_yards", "receptions", "rushing_yards", "targets", "target_share",
                  "carries", "carry_share"]
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
        df = df.assign(key=df.player_display_name.map(norm_name), season=s)
        team_carries = df.groupby(["team", "week"]).carries.transform("sum")
        df["carry_share"] = df.carries.fillna(0) / team_carries.clip(lower=1)
        st = df[["key", "season", "week", "team"] + stats_cols]
        try:
            sn = pd.read_parquet(NFLVERSE_SNAPS.format(season=s))
            sn = sn[(sn.game_type == "REG") & (sn.offense_snaps.fillna(0) > 0)]
            if s == season:
                sn = sn[sn.week < week]
            sn = sn.assign(key=sn.player.map(norm_name), season=s)[
                ["key", "season", "week", "team", "offense_pct"]]
        except Exception as e:
            logger.warning("[nfl-alt] snap counts %s unavailable: %s", s, e)
            sn = pd.DataFrame(columns=["key", "season", "week", "team", "offense_pct"])
        g = st.merge(sn, on=["key", "season", "week"], how="outer", suffixes=("", "_snap"))
        g["team"] = g["team"].fillna(g.pop("team_snap"))
        g[stats_cols] = g[stats_cols].fillna(0.0)
        frames.append(g)
    if not frames:
        return {}
    df = pd.concat(frames).sort_values(["season", "week"])
    out: dict = {}
    for k, g in df.groupby("key"):
        g = g.tail(ROLE_GAMES)
        entry = {m: [float(v) for v in g[m]]
                 for m in ("receiving_yards", "receptions", "rushing_yards")}
        role = None
        if len(g) == ROLE_GAMES and g.team.nunique() == 1 and g.offense_pct.notna().all():
            t = g.targets.astype(float)
            role = {"team": g.team.iloc[-1],
                    "snap_min": round(float(g.offense_pct.min()), 3),
                    "ts_mean": round(float(g.target_share.mean()), 3),
                    "tgt_mean": round(float(t.mean()), 2),
                    "tgt_cv": round(float(t.std() / max(t.mean(), 0.1)), 3)}
            c = g.carries.astype(float)
            role.update({"cs_mean": round(float(g.carry_share.mean()), 3),
                         "car_mean": round(float(c.mean()), 2),
                         "car_cv": round(float(c.std() / max(c.mean(), 0.1)), 3)})
        entry["_role"] = role
        out[k] = entry
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


def role_ok(r: dict) -> bool:
    """Over the role floor across his last five games.

    A receiver's floor is snaps in every game and target share; a runner's is
    his share of the team's carries.
    """
    if r.get("snap_min") is None:
        return False
    if r.get("market") == RUSH:
        return (r.get("cs_mean") or 0) >= ROLE_CS_MIN
    return r["snap_min"] >= ROLE_SNAP_MIN and (r.get("ts_mean") or 0) >= ROLE_TS_MIN


def confidence_p(r: dict) -> Optional[float]:
    """The chance he clears the rung, from his last five games and his role.

    None without five games for his current team.
    """
    last5 = r.get("last5") or []
    thr = r.get("threshold")
    if len(last5) < ROLE_GAMES or not thr or r.get("snap_min") is None:
        return None
    med = sorted(last5)[len(last5) // 2]
    hist = {"hits5": sum(1 for v in last5 if v >= thr),
            "lcush": math.log(min(10.0, max(0.05, med / thr)))}
    if r.get("market") == RUSH:
        x = {**hist, "car_mean": r.get("car_mean") or 0.0, "snap_min": r["snap_min"],
             "cs_mean": r.get("cs_mean") or 0.0, "car_cv": r.get("car_cv") or 0.0,
             "qb": 1.0 if r.get("position") == "QB" else 0.0}
        z = RUSH_INTERCEPT + sum(RUSH_COEF[k] * v for k, v in x.items())
    else:
        x = {**hist, "rec": 1.0 if r.get("market") == "receptions" else 0.0,
             "tgt_mean": r.get("tgt_mean") or 0.0, "snap_min": r["snap_min"],
             "ts_mean": r.get("ts_mean") or 0.0, "tgt_cv": r.get("tgt_cv") or 0.0}
        z = CONF_INTERCEPT + sum(CONF_COEF[k] * v for k, v in x.items())
    return 1 / (1 + math.exp(-z))


def role_candidates(rungs: Iterable[dict]) -> list[dict]:
    """The role lotto's player legs: a steady role, a record, and a price we beat.

    Each comes back with ``p_cal`` replaced by the confidence chance read
    toward the book (CONF_KEEP), the props model's own kept as ``p_props``.
    On receiving the props model is not asked to see an edge — on alt rungs it
    has not earned that — only not to disagree. On rushing, where it loses
    (Finding 14), it is not asked at all.
    """
    out = []
    for r in rungs:
        if r.get("market") not in ROLE_MARKETS or not role_ok(r):
            continue
        if (r.get("hits5") or 0) < ROLE_MIN_HITS:
            continue
        if r.get("role_conflict_verdict") == "market" or not card_mod._available(r):
            continue
        if card_mod.is_locked(r):
            continue
        if r["market"] != RUSH and (r.get("edge_pts") or 0) < 0:
            continue
        pc, fair = confidence_p(r), r.get("fair")
        if pc is None or not fair or not (0 < fair < 1) or pc - fair < CONF_MIN_EDGE:
            continue
        lf = math.log(fair / (1 - fair))
        z = lf + CONF_KEEP * (math.log(pc / (1 - pc)) - lf)
        out.append({**r, "p_props": r.get("p_cal"), "p_conf": round(pc, 4),
                    "p_cal": round(1 / (1 + math.exp(-z)), 4)})
    return out


def td_legs(tds: Iterable[dict]) -> list[dict]:
    """Anytime-TD legs for the likely scorers, at the book's own chance.

    ``fair`` is the quote less TD_FAIR_SHADE; a leg needs TD_MIN_FAIR. The
    model's chance rides along as ``p_model`` for the page, not the price.
    """
    from ..fanduel.odds import american_to_implied

    out = []
    for r in tds:
        if r.get("odds") is None or not card_mod._available(r) or card_mod.is_locked(r):
            continue
        fair = american_to_implied(r["odds"]) * TD_FAIR_SHADE
        if fair < TD_MIN_FAIR:
            continue
        out.append({"market": "anytime_td", "player": r.get("player"), "key": r.get("key"),
                    "team": r.get("team"), "position": r.get("position"),
                    "line": 0.5, "threshold": 1.0, "side": "OVER", "odds": r["odds"],
                    "implied": r.get("implied_p"), "fair": round(fair, 4),
                    "p_cal": round(fair, 4), "p_model": r.get("p_cal"),
                    "avail": r.get("avail"), "event": r.get("event"),
                    "fd_event_id": r.get("fd_event_id"), "kickoff": r.get("kickoff"),
                    "fd_market_id": r.get("fd_market_id"),
                    "over_selection_id": r.get("fd_selection_id")})
    return out


def game_legs(alt_games: Iterable[dict]) -> list[dict]:
    """Alternate spread and total rungs, each at its fair price.

    The fair price is the rung against its own opposite — Broncos +2.5 against
    the other side at -2.5, over 41.5 against under 41.5 — with the pair's
    margin split evenly. No model reads these: the game model adds nothing to
    the closing spread (Finding 14), so a game leg is the book's chance and
    the vig, and it makes the ticket only where nothing likelier pays as much.

    Only rungs with a fair chance of GAME_MIN_FAIR or better. Every game
    line of one game shares a key, so a ticket takes at most one
    of them per game — a spread and a total from one game move together.
    """
    from ..fanduel.odds import american_to_implied
    from .names import TEAM_ABBR

    rows = list(alt_games)
    by_event: dict = {}
    for g in rows:
        by_event.setdefault(g.get("fd_event_id"), []).append(g)
    out = []
    for eid, gs in by_event.items():
        teams = {}
        for g in gs:
            if g["market"] == "alt_spread":
                teams[g.get("side")] = TEAM_ABBR.get(g["name"], g["name"])
        home, away = teams.get("HOME"), teams.get("AWAY")
        if not home or not away:
            continue
        index = {(g["market"], g.get("side"), g["line"]): g for g in gs}
        for g in gs:
            if g["market"] == "alt_spread":
                other = index.get(("alt_spread", "AWAY" if g.get("side") == "HOME" else "HOME",
                                   -g["line"]))
                team = home if g.get("side") == "HOME" else away
                label = f"{team} {g['line']:+g}"
            else:
                other = index.get(("alt_total", "UNDER" if g.get("side") == "OVER" else "OVER",
                                   g["line"]))
                team = home
                label = f"{'Over' if g.get('side') == 'OVER' else 'Under'} {g['line']:g}"
            if other is None:
                continue
            a, b = american_to_implied(g["odds"]), american_to_implied(other["odds"])
            if not a or not b:
                continue
            fair = a / (a + b)
            if fair < GAME_MIN_FAIR:
                continue
            out.append({"market": g["market"], "player": label, "key": f"game:{eid}",
                        "team": team, "home_team": home, "away_team": away,
                        "line": g["line"], "side": "UNDER" if g.get("side") == "UNDER" else "OVER",
                        "odds": g["odds"], "implied": round(a, 4), "fair": round(fair, 4),
                        "p_cal": round(fair, 4), "event": g.get("event"),
                        "fd_event_id": eid, "kickoff": g.get("kickoff"),
                        "fd_market_id": g.get("fd_market_id"),
                        "over_selection_id": g.get("fd_selection_id"),
                        "under_selection_id": g.get("fd_selection_id")})
    return [l for l in out if not card_mod.is_locked(l)]


def _game_options(legs: list[dict], per_game: int, step: float) -> list[tuple]:
    """Every way to take 1..``per_game`` legs from one game, one rung per player.

    As ``(payout bucket, legs, anchors, log p, legs chosen)``, keeping only the
    options no other beats on both payout and chance — the rest can never be
    part of the best ticket. Built player by player, so a game with six
    players on six rungs apiece is not 50,000 combinations.
    """
    players: dict = {}
    for c in legs:
        players.setdefault(c.get("key"), []).append(c)
    # (payout bucket, legs, anchors) -> (log probability, legs chosen)
    opts: dict = {(0, 0, 0): (0.0, ())}
    for rungs in players.values():
        nxt = dict(opts)
        for (b, n, a), (lp, combo) in opts.items():
            if n == per_game:
                continue
            for c in rungs:
                key = (b + math.floor(math.log(_decimal(c["odds"])) / step), n + 1,
                       a + bool(c.get("anchor")))
                cand = (lp + math.log(c["p_cal"]), combo + (c,))
                if key not in nxt or cand[0] > nxt[key][0]:
                    nxt[key] = cand
        opts = _frontier(nxt)
    shade = math.floor(math.log(SGP_SHADE) / step)
    return [(b + (shade if n > 1 else 0), n, a, lp, combo)
            for (b, n, a), (lp, combo) in opts.items() if n]


def _frontier(table: dict) -> dict:
    """``{(payout bucket, *rest): (log p, ...)}`` cut to the entries no other
    with the same ``rest`` beats on both payout and chance."""
    out: dict = {}
    best_lp: dict = {}
    for key in sorted(table, key=lambda k: -k[0]):
        if table[key][0] > best_lp.get(key[1:], -math.inf):
            best_lp[key[1:]] = table[key][0]
            out[key] = table[key]
    return out


def build(cands: Iterable[dict], kind: str,
          anchors: Iterable[dict] = ()) -> Optional[dict]:
    """The likeliest ticket that reaches ``kind``'s payout.

    Exact, not greedy: a knapsack over games, maximising the summed log
    probability subject to the summed log payout reaching the target. Greedy
    by probability-per-payout overshot the week-8 double-up to +283 — a 24%
    ticket built to be a coin flip — because the last leg it added was the
    longest price it had.

    One leg per game unless the strategy's ``per_game`` says more, and never
    two rungs of one player. FanDuel prices the legs from one game as a
    same-game bundle, so a ticket with one is priced at ``SGP_SHADE`` per
    bundle and carries ``price_estimated``. Its chance is still the product of
    the legs: two players' yards in one game are close enough to independent
    for that, which one player's two rungs are not.

    A strategy with a ``floor`` that cannot reach its target on this slate
    gets as close as it can instead: the likeliest ticket within a quarter of
    the highest payout the slate allows, if that is at least the floor. It is
    marked ``short_of_target``.

    Payout is bucketed by flooring each leg's log-decimal, so a ticket that
    reaches the target bucket is guaranteed to reach the target itself.

    ``anchors`` (see ``anchor_legs``) compete for their game's slot like any
    leg but skip the price window, up to the strategy's ``max_anchors``.
    """
    spec = STRATEGIES[kind]
    lo, hi = spec["odds"]
    max_anchors = spec.get("max_anchors", 0)
    per_game = spec.get("per_game", 1)
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
    # (payout bucket, anchors, legs up to min_legs) -> (log probability, legs
    # chosen). Leg count past the minimum is not in the key — a ticket is only
    # ever cut for having more legs than FanDuel takes, and the likeliest way
    # to a payout is rarely the longest — which keeps a slate of four-leg game
    # bundles under a second.
    m = spec["min_legs"]
    dp: dict = {(0, 0, 0): (0.0, ())}
    for legs in by_game.values():
        options = _game_options(legs, per_game, step)
        nxt = dict(dp)
        for (b, a, _), (v, chosen) in dp.items():
            for ob, on, oa, olp, combo in options:
                if len(chosen) + on > MAX_LEGS or a + oa > max_anchors:
                    continue
                key = (min(goal, b + ob), a + oa, min(m, len(chosen) + on))
                cand = (v + olp, chosen + combo)
                if key not in nxt or cand[0] > nxt[key][0]:
                    nxt[key] = cand
        dp = _frontier(nxt)
    done = [val for (b, a, k), val in dp.items() if b >= goal and k >= m]
    short = False
    if not done and spec.get("floor"):
        reach = [(b, val) for (b, a, k), val in dp.items() if k >= m]
        top = max((b for b, _ in reach), default=0)
        if top >= math.log(spec["floor"]) / step:
            near = top - math.log(1.25) / step
            done = [val for b, val in reach if b >= near]
            short = True
    if not done:
        return None
    _, chosen = max(done, key=lambda t: t[0])
    out = summarise(sorted(chosen, key=lambda c: c.get("kickoff") or ""), kind)
    out["short_of_target"] = short
    out["price_estimated"] = len({c.get("fd_event_id") for c in chosen}) < len(chosen)
    return out


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


def build_paper(cands: list[dict], role: list[dict], anchors: list[dict],
                extra: list[dict] = ()) -> dict:
    """Every paper strategy for this board. None where the slate cannot build it.

    ``cands`` are the streak-rule legs, ``role`` the role lotto's player legs
    and ``extra`` its touchdown and game-line legs. The short stack and the
    anchored lotto draw from the role legs, the streak lotto keeps the old
    rule as the control, and the receiving lotto is the role lotto before
    rushing, touchdowns and game lines joined it.
    """
    extra = list(extra)
    return {"lotto_streak": build(cands, "lotto_streak"),
            "lotto_one": build(role + extra, "lotto_one"),
            "lotto_receiving": build([r for r in role if r.get("market") != RUSH],
                                     "lotto_receiving"),
            "short_stack": stack(role, "short_stack"),
            "anchored": build(role, "anchored", anchors)}


def ticket_decimal(legs: list[dict]) -> float:
    """The parlay's decimal odds: the product of the legs, each same-game
    bundle at ``SGP_SHADE``."""
    per_game: dict = {}
    for l in legs:
        g = l.get("fd_event_id")
        per_game[g] = per_game.get(g, 0) + 1
    bundles = sum(1 for g, n in per_game.items() if g is not None and n > 1)
    return math.prod(_decimal(l["odds"]) for l in legs) * SGP_SHADE ** bundles


def summarise(legs: list[dict], kind: str) -> dict:
    dec = ticket_decimal(legs)
    p = math.prod(l["p_cal"] for l in legs)
    stake = STRATEGIES[kind]["stake"]
    return {"kind": kind, "legs": legs, "leg_count": len(legs),
            "decimal_odds": round(dec, 2), "american": _american(dec),
            "model_p": round(p, 6), "stake": stake,
            "to_win": round(stake * (dec - 1), 2),
            "ev": round(p * dec - 1, 4)}
