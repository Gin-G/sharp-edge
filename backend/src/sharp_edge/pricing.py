"""Turn a pick plus a price into an expected value.

Hit rate is not ROI, and for a long time that framing hid the real problem.
The old screen hit 64.8% and its legs came back priced at a median of -260,
which is a break-even of 72% — so it was not a good bet that needed a better
price, it was a bet the market had already marked up past the read. The
picks were shortest exactly where the screen was most confident, because
"hot bat versus battered starter" is a story the book prices too.

Three steps:

  model_probability   a logistic regression over 29,777 settled board rows —
                      batter quality first, pitcher form second. Reported as
                      fit; see below for why there is no longer a correction
                      layered on top.
  devig               strip the book's margin out of the quoted price, so the
                      comparison is model-vs-market rather than model-vs-vig.
  expected_value      profit per $1 staked at the quoted price.

Coefficients come from scripts/calibrate_model.py over the 125-day backtest in
EXPERIMENTS.md, and should be refit whenever the selection rule changes — a
calibration is only valid for the population that produced it.
"""

from __future__ import annotations

import math
from typing import Optional

from .fanduel.odds import american_to_decimal, american_to_implied

# Base rate of the population actually bet — the top of the board, one batter
# per game — measured over 129 days: 77.1% per leg, against 64.8% for the
# retired screen's rows on the same days. Documentation only; nothing reads it.
BASE_RATE = 0.771

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
    "intercept": -1.087595,
    "vs_hand_avg": 5.962546,
    "recent_ab": 0.011948,
    "p_season_baa": 0.115625,
    "p_l3_k9": -0.018677,
    "p_sharp": -0.079013,
}

# Training medians, for imputing a missing feature. Using live medians instead
# would let a slate's own composition shift the model.
_MEDIANS = {
    "vs_hand_avg": 0.2510,
    "recent_ab": 17.0000,
    "p_season_baa": 0.2360,
    "p_l3_k9": 8.3100,
    "p_sharp": 0.0000,
}

# The Platt correction is gone, and its removal is the point.
#
# It existed to fix a selection effect: the logistic was fit over the whole
# board, the *screen* bet a filtered sub-population that hit 4.7 points higher
# than the features explained, so pick-level predictions had to be pushed up
# or the model would never find a bet. That correction was only ever valid for
# the population it was fit on, and it was applied by asking
# ``is_screen_pick`` — which is why it had to be scoped so carefully.
#
# Bets are now taken from the top of the whole board rather than from the
# screen, so the selection effect it corrected for no longer exists, and the
# raw logistic is what the population needs. Measured on 30,783 settled board
# rows it is already honest end to end:
#
#     predicted   actual      n
#      50.9%      49.8%     4,636
#      57.7%      57.3%     8,328
#      62.5%      63.2%    11,082
#      67.0%      67.1%     5,796
#      71.5%      72.8%       860
#
# At the very top — the two legs that actually get bet — it reads 74.4% and
# they land 76.7%, so it is about two points conservative exactly where it
# matters. That is the safe direction: it understates the parlay and so
# understates EV, rather than talking the stake up.
#
# Refitting Platt on the new population was tried and is worse. Fit over the
# top 8 of each day it returns (0.4849, 0.4913), which shrinks hard toward
# that pool's mean and drags the top-2 estimate *down* to 73.3% against a 76.7%
# outcome. The uncorrected number is the better one.

# The opposing starter is described by a SEASON rate, not a last-three-starts
# one. p_l3_h9 was the screen's original signal and it is the same small-sample
# trap the batter side has: p_season_baa correlates +0.0149 with a hit against
# +0.0111, and buckets monotonically where the last-3 version does not.
#
# p_sharp is a boolean left over from the retired screen — a threshold on the
# starter's recent form. It carries nothing the l3 stats do not, and it still
# earns its place, because the model is linear and the effect is not: being
# hard to hit matters more than being marginally harder to hit.
#
#     shipped before (l3_h9 + l3_k9)       AUC 0.5756
#     now (season_baa + l3_k9 + p_sharp)   AUC 0.5767
#     delta +0.0012, bootstrap 95% CI [+0.0003, +0.0020]
#
# Worth reading against the ceiling: batter terms alone score 0.5723, so the
# entire opposing-pitcher contribution is +0.0044 and this recovers a quarter
# of it. The pitcher side is small. It is now slightly less badly spent.
_FEATURES = ["vs_hand_avg", "recent_ab", "p_season_baa", "p_l3_k9", "p_sharp"]

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

    rec = _clip_features(rec)

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

    return _sigmoid(z)


# The model must not extrapolate past the data it was fitted on.
#
# vs_hand_avg is the batter's CAREER average against the hand, and it carries
# the largest coefficient in the model at +6.03. In 30,774 training rows it
# never exceeds .667, and only 5 rows clear .500 — the 99.9th percentile is
# .402. So a value of .972 is not an extreme observation, it is a value the
# fit has never seen, and the logistic happily extends a straight line into it:
# on 2026-09-14 the board priced Scott Bandura, a call-up with no MLB record
# this season, at 99.2% to record a hit off a near-perfect three-PA split.
#
# Clipping the feature at .450 — above the 99.9th percentile, so it binds on 7
# rows in the whole training set — costs nothing measurable and takes that
# quote to 84.0%, which is inside the range batters actually achieve.
#
#     rule                          AUC      log-loss   rows hit
#     uncapped                     0.5756     0.66033          0
#     clip at .450                 0.5756     0.66023          7
#
# What this deliberately does NOT do is discount a hot bat for having few plate
# appearances behind it. That was tried first and it was wrong. A thin split
# predicts as well as a thick one, and by this sample better:
#
#     vs_hand_pa    hot (>=.350)   cold (<.250)     gap
#       0-20 PA        64.7%          41.7%       +23.0  (n=17, p=0.11)
#      20-40 PA        84.8%          41.9%       +42.9  (p<0.001)
#     40-100 PA        78.3%          49.5%       +28.8  (p=0.009)
#       300+ PA        72.0%          55.3%       +16.7  (p=1.2e-21)
#
# Blanking splits under 20 PA cost 0.0012 AUC and would have cut a .400 bat in
# 30 PA from 79.6% to 61.3%, throwing away a real signal to fix a scaling bug.
# A limited sample does not make a hot bat less hot; it only means the model
# should not be asked about a number it has never seen.
VS_HAND_AVG_CAP: float = 0.450


def _clip_features(rec: dict) -> dict:
    """Hold features inside the range the model was fitted over.

    Returns a copy when anything is clipped, so a caller's row is never
    mutated by having been priced.
    """
    v = rec.get("vs_hand_avg")
    try:
        v = float(v) if v is not None else None
    except (TypeError, ValueError):
        return rec
    if v is None or v != v or v <= VS_HAND_AVG_CAP:
        return rec
    return {**rec, "vs_hand_avg": VS_HAND_AVG_CAP}


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
    """Does this row carry the old screen's tags?

    **This no longer selects anything.** It gates neither pricing nor betting;
    bets come from the top of the board by probability. It survives as a
    label — the odds snapshot records it so the archive can still tell which
    rows the old rules would have flagged, which is what makes the shipped
    rule and the retired one comparable on the same days.
    """
    return bool(
        rec.get("is_hot")
        and (
            rec.get("bvp_edge")
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
