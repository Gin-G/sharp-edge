"""Turn an NFL projection plus a posted line into a probability and an edge.

The football counterpart of ``pricing.py``. Three steps, same as baseball:

  model_probability   logistic over trailing/projected production, fit by
                      scripts/calibrate_nfl.py against 2015-2021 and scored on
                      2023-2025.
  devig               NFL props are quoted two-sided, so the margin comes out
                      properly rather than being guessed at. This is a real
                      improvement over the batter side, where a single
                      to-record-a-hit runner has no complement to devig
                      against.
  expected_value      profit per $1 staked at the quoted price.

**The projections need rescaling before any of this means anything, and that
is the most important thing in this module.** Measured against FanDuel's own
week-1 2026 lines, the projections regress hard toward the positional mean:
fitting projection on line across the board gives a slope of 0.59 for
receiving yards, 0.55 for receptions, 0.78 for rushing yards and 1.92 for
passing yards, none of them 1. That is expected — an ML model trained on
squared error shrinks, a betting line does not — but the consequence is
severe. A raw "projection is 10 yards above the line" rule reads the shrinkage
as signal and fires UNDER on every star and OVER on every backup: on the live
week-1 board it wanted the under on Lamb, Jefferson, Nacua, Smith-Njigba,
Gibbs and Bijan, and the over on Colby Parkinson and Woody Marks. That is a
portfolio of "fade the good players", which is a known way to lose money.

``calibrate_to_market`` removes it by regressing projection on line across the
week's board and reading the *residual* — how far a player sits from what his
own projection level predicts. On the same board that turns 87% of quarterbacks
reading UNDER into 39%, and the names it surfaces become ones with a real role
story behind them rather than just a big number.

The rescaling is refit every week from that week's board, so it tightens on its
own as in-season projections replace the preseason priors.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

from ..fanduel.odds import american_to_decimal, american_to_implied

# --- fitted by scripts/calibrate_nfl.py ------------------------------------
#
# Features are built by ``features()`` below, which is the same construction
# the calibration script uses — if you change one, change both.
#
# The ordering across markets is the finding worth keeping: ``gap_std``, the
# season-to-date estimate, carries far more weight than ``gap``, the trailing-4
# one, in every market except passing yards. Recent form matters less than the
# book's own framing of a player suggests.
_PROP_COEF = {
    "receiving_yards": {
        "intercept": 1.021798,
        "gap": 0.025416,
        "gap_std": 0.858422,
        "logvol": 1.126846,
        "logline": -0.952793,
    },  # AUC 0.8516, worst calibration bucket 1.6pts
    "receptions": {
        "intercept": -0.031197,
        "gap": 0.037857,
        "gap_std": 1.738031,
        "logvol": 1.012610,
        "logline": -1.487552,
    },  # AUC 0.9044, worst calibration bucket 1.9pts
    "rushing_yards": {
        "intercept": 1.009935,
        "gap": 0.094567,
        "gap_std": 0.749732,
        "logvol": 1.210494,
        "logline": -1.126234,
    },  # AUC 0.8981, worst calibration bucket 1.9pts
    "passing_yards": {
        "intercept": 16.079573,
        "gap": 1.185546,
        "gap_std": 0.973103,
        "logvol": -0.225882,
        "logline": -2.808608,
    },  # AUC 0.8105 — see PASSING_YARDS_CAVEAT
}

_TD_COEF = {
    "intercept": -3.047888,
    "log_td_rate": 0.867761,
    "log_touches": 0.766161,
    "log_tgt": 0.248786,
    "is_rb": -0.289385,
    "is_te": 0.071722,
}  # AUC 0.7284

# Passing yards is the one market whose fit is *overconfident*, and it is worth
# stating plainly rather than burying in a coefficient table. On held-out
# 2023-25 it reads 77.5% where the outcome is 72.8%, and 87.6% where it is
# 82.7% — a 4-5 point overstatement right through the range a bet would be
# taken from. Every other market misses by under 2 points and errs low.
#
# Overstating is the direction that costs money: it inflates EV, inflates the
# parlay, and inflates the Kelly stake all at once. So the market ships
# excluded from card selection by default (``BETTABLE``) while still being
# priced and displayed, which is how this repo has handled every rule that
# measured badly.
PASSING_YARDS_CAVEAT = (
    "The passing-yards fit runs 4-5 points hot on held-out seasons "
    "(reads 87.6%, lands 82.7%). Shown for context; not selected onto the card."
)

MARKETS = tuple(_PROP_COEF)
BETTABLE = ("receiving_yards", "receptions", "rushing_yards")

# Fallbacks for a missing feature. Live medians would let one week's own
# composition move the model, so these are fixed at the training medians.
_MEDIANS = {"gap": 0.0, "gap_std": 0.0, "logvol": 1.6, "logline": 3.0}
_TD_MEDIANS = {"log_td_rate": 0.18, "log_touches": 1.9, "log_tgt": 1.2,
               "is_rb": 0.0, "is_te": 0.0}


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def _f(v, fallback: float) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return fallback
    return fallback if v != v else v  # NaN


# ---------------------------------------------------------------------------
# Rescaling the projection onto the market's scale
# ---------------------------------------------------------------------------

def calibrate_to_market(
    pairs: Sequence[tuple[float, float]], min_n: int = 12
) -> Optional[tuple[float, float]]:
    """Least-squares ``(slope, intercept)`` mapping a line to a projection.

    ``pairs`` is ``[(line, projection), …]`` across one market for one week.
    Returns None when the board is too thin to fit — the caller then falls back
    to the raw gap and says so, rather than inventing a correction from six
    points.

    Fitting projection *on* line (rather than the reverse) is deliberate: the
    line is the better-measured quantity and belongs on the x axis, and the
    residual we want is "how far is this projection from where a projection
    normally sits for a line this size".
    """
    pts = [(x, y) for x, y in pairs
           if x is not None and y is not None and x == x and y == y]
    if len(pts) < min_n:
        return None
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    slope = sxy / sxx
    return slope, my - slope * mx


def market_residual(line: float, projection: float,
                    fit: Optional[tuple[float, float]]) -> float:
    """The projection's disagreement with the market, in the stat's own units.

    With no fit this is the raw difference, which is what the naive rule uses.
    With one it is the projection minus what the fitted relationship expects at
    that line — the same number once the systematic shrinkage is taken out.
    """
    if fit is None:
        return projection - line
    slope, intercept = fit
    return projection - (slope * line + intercept)


def adjusted_projection(line: float, projection: float,
                        fit: Optional[tuple[float, float]]) -> float:
    """The projection restated on the line's scale.

    This is what goes into the probability model: ``line + residual``. It keeps
    the market's level and takes only our disagreement with it, which is the
    only part of a shrunk projection worth believing.
    """
    return line + market_residual(line, projection, fit)


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------
#
# ``logvol`` carries the second-largest coefficient in every yardage model, and
# the projections do not include carries or attempts — only yards. Leaving it
# empty is not an option: at zero the model reads a starting back as a player
# with no touches and returns probabilities like 0.01, which then price as an
# 86% edge on the other side. It is the single easiest way to get a catastrophic
# number out of this module.
#
# So volume is derived from the estimate itself, at league-average efficiency.
# That is cruder than a real projection but it is roughly right, it moves in the
# right direction with the player, and — unlike a fixed median — it does not tell
# the model that Derrick Henry and a third-string back see the same workload.

_YARDS_PER = {
    "rushing_yards": 4.3,     # league rushing average, stable to a tenth for a decade
    "passing_yards": 7.0,     # yards per attempt
    "receiving_yards": 11.5,  # yards per reception
}


def derive_volume(market: str, estimate: float,
                  known: Optional[float] = None) -> Optional[float]:
    """Touches implied by a yardage estimate, or the known count when we have it.

    ``receptions`` is its own volume driver, and the receiving markets get real
    projected receptions passed in, so this only has to guess for rushing and
    passing.
    """
    if known is not None:
        try:
            k = float(known)
            if k == k and k >= 0:
                return k
        except (TypeError, ValueError):
            pass
    per = _YARDS_PER.get(market)
    if per is None:
        return None
    return max(0.0, float(estimate)) / per


# ---------------------------------------------------------------------------
# Anchoring the model's level to the market
# ---------------------------------------------------------------------------
#
# Rescaling the projection (``calibrate_to_market``) puts our estimate on the
# line's scale. It does not put our *probability* on the market's scale, and
# those come apart for a reason worth spelling out.
#
# The model answers "P(actual > L | the player's mean is X)". A book does not
# set L at the mean — yardage is right-skewed, so the median sits below it — and
# a two-way line is set where both sides are near 50%. Feed a player whose
# adjusted estimate equals the line and the model returns about 37%, not 50%.
# That is not an error in the model; it is the model answering a different
# question from the one the price answers. Left uncorrected it tilts every
# market toward the under.
#
# The fix is the same move, one level up: fit our log-odds against the market's
# devigged log-odds across the week's board, and keep only the residual as the
# disagreement. Rows where we agree with the market come out at the market's
# number and earn no edge; rows where we genuinely differ keep their difference.
# It also absorbs whatever the derived-volume approximation gets wrong on
# average, which is a second thing we would otherwise have to be right about.


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def probability_offset(
    pairs: Sequence[tuple[float, float]], min_n: int = 12
) -> Optional[float]:
    """Log-odds shift that centres our read on the market's.

    ``pairs`` is ``[(model_p, market_fair_p), …]`` for one market's board, and
    the answer is a single number: the mean market log-odds minus the mean
    model log-odds. Applied to every row it removes the systematic tilt while
    leaving each row's disagreement intact.

    A two-parameter fit was the obvious choice and is wrong here. Ten days out
    FanDuel quotes essentially every main prop at -114/-114, so the devigged
    market probability is exactly 0.500 on every row; regressing on a constant
    gives a slope of zero and flattens the whole board to 0.500, which is what
    it did. An offset has no such failure mode — with a constant target it
    reduces to "line the board up on 50%", which is exactly right.
    """
    pts = [(_logit(m), _logit(f)) for m, f in pairs
           if m is not None and f is not None and m == m and f == f
           and 0 < m < 1 and 0 < f < 1]
    if len(pts) < min_n:
        return None
    return (sum(y for _, y in pts) - sum(x for x, _ in pts)) / len(pts)


# How much of the centred disagreement to keep.
#
# The raw model is very confident: fed the week-1 board it reads James Cook's
# under at 99% and Derrick Henry's over at 97%. It is not being unreasonable on
# its own terms — it was trained on real trailing averages, where a 45-yard gap
# to the line really does predict the outcome that strongly. The gap here is
# not of that kind. A preseason projection of 21.8 rushing yards for a starting
# back is a statement about role uncertainty in August, and the market's 74.5
# is a statement about what he is expected to do on Sunday. On that question
# the market is better informed and we are not entitled to a 49-point edge.
#
# So the disagreement is shrunk, hard while the projections are preseason and
# less so once they have in-season usage behind them.
#
# **Both numbers are priors, not measurements, and they are the first thing to
# replace.** Nothing here has been fitted, because fitting it needs settled
# weeks with the line that was posted at the time, and no such archive exists
# yet — the odds snapshot this repo keeps for baseball is the model for it.
# Until then these are deliberately conservative: too much shrink costs missed
# bets, too little costs money.
SHRINK_PRESEASON = 0.25
SHRINK_INSEASON = 0.50


def anchor_probability(p: float, offset: Optional[float],
                       market_fair: Optional[float] = None,
                       shrink: float = SHRINK_INSEASON) -> float:
    """Restate a model probability on the market's scale, disagreement shrunk.

    With no offset (a board too thin to centre on) the raw model is returned
    and the caller should say so rather than quietly presenting it as priced.
    """
    if offset is None:
        return p
    z = _logit(p) + offset
    if market_fair is not None and 0 < market_fair < 1:
        anchor = _logit(market_fair)
        z = anchor + shrink * (z - anchor)
    return _sigmoid(z)


# ---------------------------------------------------------------------------
# Probabilities
# ---------------------------------------------------------------------------

def features(est: float, est_season: Optional[float], volume: Optional[float],
             line: float) -> dict:
    """Model inputs for one player at one line.

    Mirrors ``calibrate_nfl.add_prop_features`` exactly. Yardage goes negative
    on a bad day, so clip before log1p: log1p(-1) is -inf and below it is NaN.
    """
    est = max(0.0, _f(est, 0.0))
    season = max(0.0, _f(est_season if est_season is not None else est, est))
    vol = max(0.0, _f(volume, 0.0))
    ln = math.log1p(max(0.0, line))
    return {
        "gap": math.log1p(est) - ln,
        "gap_std": math.log1p(season) - ln,
        "logvol": math.log1p(vol),
        "logline": ln,
    }


def prop_probability(market: str, est: float, line: float,
                     est_season: Optional[float] = None,
                     volume: Optional[float] = None) -> float:
    """P(the player goes over ``line``), given an estimate on the line's scale.

    Feed ``adjusted_projection``, not the raw projection — see the module
    docstring for what happens otherwise.
    """
    coef = _PROP_COEF[market]
    f = features(est, est_season, volume, line)
    z = coef["intercept"]
    for k, v in f.items():
        z += coef[k] * _f(v, _MEDIANS[k])
    return _sigmoid(z)


def td_probability(td_rate: Optional[float], touches: Optional[float],
                   targets: Optional[float], position: Optional[str]) -> float:
    """P(the player scores a touchdown).

    Not a Poisson on the projected TD count, though that is the obvious move
    and was tried: ``1 - exp(-lambda)`` on a trailing rate reads 54% on its top
    bucket and delivers 39.6% over 39,450 player-weeks, because a touchdown
    rate is mostly noise and needs shrinking toward the position's base rate.
    The logistic does that shrinking; the Poisson does not.
    """
    f = {
        "log_td_rate": math.log1p(max(0.0, _f(td_rate, 0.0))),
        "log_touches": math.log1p(max(0.0, _f(touches, 0.0))),
        "log_tgt": math.log1p(max(0.0, _f(targets, 0.0))),
        "is_rb": 1.0 if (position or "").upper() == "RB" else 0.0,
        "is_te": 1.0 if (position or "").upper() == "TE" else 0.0,
    }
    z = _TD_COEF["intercept"]
    for k, v in f.items():
        z += _TD_COEF[k] * _f(v, _TD_MEDIANS[k])
    return _sigmoid(z)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def devig_two_way(over: Optional[int], under: Optional[int]) -> tuple[Optional[float], Optional[float]]:
    """Fair probabilities from a two-sided quote, margin removed proportionally.

    Both prices are needed. With only one side quoted the margin is unknown and
    the raw implied number is returned — honest about not knowing rather than
    assuming a standard vig, which is the same choice ``pricing.devig_probability``
    makes for baseball.
    """
    if over is None and under is None:
        return None, None
    if over is None:
        return None, american_to_implied(under)
    if under is None:
        return american_to_implied(over), None
    io, iu = american_to_implied(over), american_to_implied(under)
    total = io + iu
    if total <= 0:
        return None, None
    return io / total, iu / total


def expected_value(p: float, american: int) -> float:
    """Profit per $1 staked."""
    return p * (american_to_decimal(american) - 1) - (1 - p)


def kelly_fraction(p: float, american: int) -> float:
    b = american_to_decimal(american) - 1
    if b <= 0:
        return 0.0
    return max(0.0, (p * b - (1 - p)) / b)


def price_side(p_over: float, over: Optional[int], under: Optional[int]) -> dict:
    """Both sides of one prop, priced, with the better one named.

    ``side`` is whichever direction the model disagrees with the market in;
    ``ev`` and ``edge_pts`` describe that side. A prop is two bets and only one
    of them can be worth taking, so resolving it here keeps every caller from
    having to.
    """
    fair_over, fair_under = devig_two_way(over, under)
    out = {
        "model_p_over": round(p_over, 4),
        "over_odds": over, "under_odds": under,
        "fair_p_over": round(fair_over, 4) if fair_over is not None else None,
        "fair_p_under": round(fair_under, 4) if fair_under is not None else None,
        "overround": (round(american_to_implied(over) + american_to_implied(under), 4)
                      if over is not None and under is not None else None),
    }

    candidates = []
    if over is not None and fair_over is not None:
        candidates.append(("OVER", p_over, over, fair_over))
    if under is not None and fair_under is not None:
        candidates.append(("UNDER", 1 - p_over, under, fair_under))
    if not candidates:
        out.update({"side": None, "model_p": None, "odds": None, "ev": None,
                    "edge_pts": None, "kelly": None})
        return out

    side, p, odds, fair = max(candidates, key=lambda c: c[1] - c[3])
    out.update({
        "side": side,
        "model_p": round(p, 4),
        "odds": odds,
        "implied_p": round(american_to_implied(odds), 4),
        "fair_p": round(fair, 4),
        "ev": round(expected_value(p, odds), 4),
        "edge_pts": round(100 * (p - fair), 1),
        "kelly": round(kelly_fraction(p, odds), 4),
    })
    return out


def anchor_field(model_ps: Sequence[float], implied_ps: Sequence[float]) -> list[float]:
    """Shift the model's probabilities until they total what the market's do.

    An anytime-TD market is a field, not a two-way price: FanDuel books every
    scorer, and across a game the quoted probabilities sum to around 400%. The
    first instinct is to call that a 300% margin and normalise it away. That is
    wrong, and it was tried — roughly four different players score a touchdown
    in an NFL game, so most of that sum is real. Normalising to the model's own
    total halved every fair price and turned the whole board into a false
    double-digit edge.

    The margin is real but modest, and unknown. So it is left in on *both*
    sides instead of being estimated: the model is shifted in log-odds until
    its total matches the market's, and the comparison is then between two
    quantities carrying the same margin. What survives is the only thing we can
    honestly claim here — a disagreement about *which* players score, not how
    many, and not what the book is charging.

    That also means the result is a relative read, not a true probability, and
    a dollar EV must not be computed from it. See ``price_td``.
    """
    ps = [min(max(p, 1e-6), 1 - 1e-6) for p in model_ps]
    target = sum(implied_ps)
    if not ps or target <= 0:
        return list(model_ps)

    def total(offset: float) -> float:
        return sum(_sigmoid(_logit(p) + offset) for p in ps)

    # Monotone increasing in offset, so bisect. The bracket is generous; the
    # shifts actually needed are well under one log-odds unit.
    lo, hi = -20.0, 20.0
    if total(lo) > target or total(hi) < target:
        return list(model_ps)
    for _ in range(60):
        mid = (lo + hi) / 2
        if total(mid) < target:
            lo = mid
        else:
            hi = mid
    offset = (lo + hi) / 2
    return [_sigmoid(_logit(p) + offset) for p in ps]


def price_td(p_anchored: float, american: Optional[int],
             model_p: Optional[float] = None) -> dict:
    """An anytime-TD quote against the model, as a relative read.

    ``p_anchored`` comes from ``anchor_field`` and carries the book's margin,
    the same as the implied price it is compared against. That makes the edge
    meaningful and the EV meaningless, so **no EV is returned** — a positive
    number computed from a margin-inflated probability would be positive for
    most of the board and would be believed. The edge is a redistribution
    signal: this player is likelier to score than the field's shape implies.
    """
    out = {
        "model_p": round(p_anchored, 4),
        "model_p_unanchored": round(model_p, 4) if model_p is not None else None,
        "odds": american,
        "implied_p": None,
        "edge_pts": None,
        # Deliberately absent. See the docstring — this market's margin cannot
        # be stripped without knowing how many scorers the book is pricing for.
        "ev": None,
        "kelly": None,
    }
    if american is None:
        return out
    implied = american_to_implied(american)
    out["implied_p"] = round(implied, 4)
    out["edge_pts"] = round(100 * (p_anchored - implied), 1)
    return out
