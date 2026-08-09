"""Turn a pick plus a price into an expected value.

Hit rate is not ROI. The batter screen sits around 67% and break-even at -207
is 67.4%, so "the screen is right two times in three" and "the screen makes
money" are very nearly the same sentence — which means the price decides, not
the pick. This module is the part that decides.

Three steps:

  model_probability   a logistic regression over 29,777 settled board rows —
                      batter quality first, pitcher form second — recalibrated
                      onto the pick population.
  devig               strip the book's margin out of the quoted price, so the
                      comparison is model-vs-market rather than model-vs-vig.
  expected_value      profit per $1 staked at the quoted price.

Coefficients come from scripts/calibrate_model.py over the 125-day backtest in
EXPERIMENTS.md, and should be refit whenever the screen's rules change — a
calibration is only valid for the population that produced it.
"""

from __future__ import annotations

import math
from typing import Optional

from .fanduel.odds import american_to_decimal, american_to_implied

# Base rate of the current rule set: 1,241 decided picks, 67.4%.
BASE_RATE = 0.674

# Logistic regression over 29,777 settled board rows, fit by
# scripts/calibrate_model.py. Re-run it whenever the screen's rules change.
#
# This replaced a four-bucket lookup on the pitcher's H/9 that, measured on a
# held-out half of the season, scored AUC 0.5047 — a coin flip — with a
# log-loss *worse* than predicting the base rate for everyone. It also gave
# every batter facing a given starter the same number, which is why board-wide
# EV had to be suppressed with an eligibility check. A model that knows who the
# batter is doesn't need that guard.
#
# The ordering of the coefficients is the finding. The batter's career average
# against the hand carries roughly ten times the weight of the starter's recent
# H/9 — which is the screen's whole original premise, and is close to how the
# market itself weights them.
_COEF = {
    "intercept": -1.133761,
    "vs_hand_avg": 6.028424,
    "recent_ab": 0.011981,
    "p_l3_h9": 0.004995,
    "p_l3_k9": -0.019688,
}

# Training medians, for imputing a missing feature. Using live medians instead
# would let a slate's own composition shift the model.
_MEDIANS = {
    "vs_hand_avg": 0.2510,
    "recent_ab": 17.0000,
    "p_l3_h9": 8.2000,
    "p_l3_k9": 8.3100,
}

# Platt correction, fit on the pick population alone, and **applied only to
# picks**. The logistic is fit over the whole board (60.6% base rate) but picks
# hit 68%, and selection the features don't fully explain pulls pick
# predictions 4.7 points low. Ranking is unaffected — Platt is monotone — but
# EV is computed from the *level*, so without this the model would essentially
# never find a bet.
#
# Applying it board-wide instead was measurably wrong: it lifts every
# non-pick toward a 68% population they aren't in, and 44% of the board came
# back +EV against a book that holds ~5%. Scope matters as much as the fit.
_PLATT = (0.226436, 0.893979)

_FEATURES = ["vs_hand_avg", "recent_ab", "p_l3_h9", "p_l3_k9"]

# Minimum model-vs-market gap, in probability points, before a pick counts as
# a bet.
#
# Not a taste for caution — a statement about precision. On held-out picks the
# model's level is off by ~1.7 points, and its per-bucket calibration error
# runs to 4.7. A quoted edge of half a point is therefore indistinguishable
# from zero, and betting it means paying the vig to act on rounding.
#
# Three points sits above the level error with a little room. It's a floor on
# *edge* rather than on EV deliberately: EV > 0 exactly when edge > 0, so the
# two agree on sign, but the same EV means different edges at different prices
# and edge is the thing the calibration error is denominated in.
#
# Revisit once the closing-price snapshots can measure the error directly
# rather than inferring it from a split half.
MIN_EDGE_PTS = 3.0


def _sigmoid(z: float) -> float:
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def model_probability(rec) -> float:
    """Calibrated probability that this batter records a hit.

    Accepts a board row. A bare number is still accepted and read as the
    starter's H/9 with every other feature imputed, so older call sites keep
    working — but that path throws away the batter, which is the strongest
    term in the model, and should not be relied on.
    """
    if not isinstance(rec, dict):
        try:
            rec = {"p_l3_h9": float(rec)} if rec is not None else {}
        except (TypeError, ValueError):
            rec = {}

    z = _COEF["intercept"]
    for f in _FEATURES:
        v = rec.get(f)
        try:
            v = float(v) if v is not None else _MEDIANS[f]
        except (TypeError, ValueError):
            v = _MEDIANS[f]
        if v != v:  # NaN
            v = _MEDIANS[f]
        z += _COEF[f] * v

    raw = _sigmoid(z)
    if not is_screen_pick(rec):
        return raw
    a, b = _PLATT
    lo = min(max(raw, 1e-6), 1 - 1e-6)
    return _sigmoid(a + b * math.log(lo / (1 - lo)))


def devig_probability(american: int, overround: float = 1.0) -> float:
    """Market probability with the margin removed.

    A single to-record-a-hit runner is quoted without its complement, so we
    can't two-way devig it directly. ``overround`` lets a caller pass the
    book's measured margin for this market type; the default of 1.0 leaves
    the raw implied number alone and is honest about not knowing.
    """
    return american_to_implied(american) / overround


def expected_value(p: float, american: int) -> float:
    """Profit per $1 staked. +0.05 means a nickel per dollar, long run."""
    return p * (american_to_decimal(american) - 1) - (1 - p)


def kelly_fraction(p: float, american: int) -> float:
    """Full-Kelly stake as a fraction of bankroll. Negative means no bet.

    Quarter-Kelly is the usual practical stake; this returns the full number
    and leaves that scaling to the caller.
    """
    b = american_to_decimal(american) - 1
    if b <= 0:
        return 0.0
    f = (p * b - (1 - p)) / b
    return max(0.0, f)


def is_screen_pick(rec: dict) -> bool:
    """Is this row one the screen actually bets?

    No longer gates pricing — the model knows who the batter is, so it can be
    trusted on any row. Kept because the bundle and the odds snapshot both
    need to know which rows are picks.
    """
    return bool(
        rec.get("is_hot")
        and (
            rec.get("bvp_edge")
            or rec.get("hand_slump_edge")
            or rec.get("hittable_sp_edge")
        )
        and not rec.get("p_sharp")
    )


def price_pick(rec, american: Optional[int]) -> dict:
    """Everything the UI needs for one row at one price.

    ``rec`` is a board row; a bare H/9 is still accepted for older callers.
    """
    p = model_probability(rec)
    if american is None:
        return {
            "model_p": round(p, 4),
            "fd_odds": None, "implied_p": None,
            "ev": None, "edge_pts": None, "kelly": None,
            "breakeven_odds": _breakeven(p),
        }
    implied = american_to_implied(american)
    return {
        "model_p": round(p, 4),
        "fd_odds": american,
        "implied_p": round(implied, 4),
        "ev": round(expected_value(p, american), 4),
        "edge_pts": round(100 * (p - implied), 1),
        "kelly": round(kelly_fraction(p, american), 4),
        "breakeven_odds": _breakeven(p),
    }


def _breakeven(p: float) -> int:
    """The worst price at which this pick is still break-even."""
    if p >= 1:
        return -1_000_000
    dec = 1 / p
    return round((dec - 1) * 100) if dec >= 2 else -round(100 / (dec - 1))


def enrich_records(records: list[dict], odds: dict) -> list[dict]:
    """Attach price, model probability and EV to screen rows in place.

    ``odds`` is keyed by normalised batter name and may be either a flat
    ``{name: american}`` map or the detailed ``{name: {...}}`` form. The
    detailed form additionally carries the FanDuel ids a bet-slip link is
    built from, so they're passed through when present.

    A row with no posted market still gets ``model_p`` and ``breakeven_odds``
    — knowing the price you'd need is useful even when there isn't one.
    """
    from ._data import _norm

    for r in records:
        name = r.get("batter")
        q = odds.get(_norm(name)) if name else None
        if isinstance(q, dict):
            american = q.get("odds")
            r["fd_market_id"] = q.get("market_id")
            r["fd_selection_id"] = q.get("selection_id")
            r["fd_event_id"] = q.get("event_id")
        else:
            american = q
            r.setdefault("fd_market_id", None)
            r.setdefault("fd_selection_id", None)
            r.setdefault("fd_event_id", None)
        r.update(price_pick(r, american))
    return records
