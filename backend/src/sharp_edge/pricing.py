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

import hashlib
import json
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
#
# vs_hand_avg here is the REGRESSED column, not the raw split: these were refit
# after VS_HAND_REGRESSION_PA went in, which is why its coefficient reads 7.07
# against the 5.96 of the raw-feature fit. The regression halves the feature's
# spread (sd .0331 -> .0172) and the fit answers with a larger coefficient;
# pasting one set onto the other scale would be a silent mis-calibration.
_COEF = {
    "intercept": -1.424086,
    "vs_hand_avg": 7.066928,
    "recent_ab": 0.014135,
    "p_season_baa": 0.118244,
    "p_l3_k9": -0.018421,
    "p_sharp": -0.081510,
}

# Training medians, for imputing a missing feature. Using live medians instead
# would let a slate's own composition shift the model.
_MEDIANS = {
    "vs_hand_avg": 0.2520,
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


# Which model produced a number, stamped onto every pick as it is recorded.
#
# The track record has to say what was suggested on the day, under the model
# in force on the day. Without a version on the row there is no way to tell a
# pick made under one fit from a pick made under another, and the two write
# paths that can touch a recorded day — the intra-day re-screen and a manual
# backfill — would silently mix them.
#
# Derived from the coefficients and the regression constants rather than typed
# by hand, so it cannot be forgotten: change any number the model depends on
# and the version changes with it.
def _model_version() -> str:
    payload = json.dumps(
        {"coef": _COEF, "medians": _MEDIANS, "features": _FEATURES,
         "cap": VS_HAND_AVG_CAP, "k": VS_HAND_REGRESSION_PA,
         "league": VS_HAND_LEAGUE_AVG},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]

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

    rec = _prepare_features(rec)

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
# the largest coefficient in the model at +7.07. In 30,774 training rows it
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
# This block used to end by arguing that a thin split must NOT be discounted
# for its sample size — thin splits predicted as well as thick ones, and by
# the backtest better (20-40 PA: .350+ hit 84.8% against 41.9% for sub-.250).
# Live results reversed it: see VS_HAND_REGRESSION_PA below, which now does
# the discounting that table said was unnecessary. The table was not wrong
# about the backtest. The backtest was the wrong population to ask, because
# box-score boards contain almost none of the thin-split call-ups the live
# roster board puts at the top.
#
# The cap survives that change with a narrower job. Regression already takes a
# three-PA .972 to league average, so the cap no longer catches small samples
# at all; what is left for it is a genuine long-sampled extreme, where a .667
# career split on 1,500 PA still regresses to .583 and out of the fitted
# range. It binds on 7 rows in 30,774 and costs nothing measurable.
VS_HAND_AVG_CAP: float = 0.450


# How hard to regress a career vs-hand average toward the league mean, and
# where that number comes from.
#
# vs_hand_avg entered the model raw, and the live board punished it. Over 275
# graded live legs the model claimed 71.9% and delivered 66.2%, and the whole
# gap sat on one population:
#
#     vs_hand_pa      n     model says   actual     gap
#     under 400      115       73.6%      58.3%   -15.3   (z = -3.75)
#     400+           160       70.7%      71.9%    +1.2   (z = +0.33)
#
# On a thick split the model was already honest. The error is entirely a
# small-sample effect: a .400 average on 30 PA is mostly sampling noise, and a
# +5.96 coefficient reads it as talent.
#
# This could not be seen in the backtest, and that is structural rather than
# bad luck. Historical boards are built from box scores, so they are made of
# men who were in the lineup — median vs_hand_pa at the betting bar is 837.
# The live board is the active roster, call-ups included, and its median at
# the same bar is 81. The population that produces the failure is almost
# absent from the sample the model was fitted and checked on, which is why
# MIN_VS_HAND_PA (20) measured as costing 0.0012 AUC and was reverted: the
# test set could not contain the rows it was meant to protect against.
#
# The constant is not fitted to any outcome. It is the regression-to-the-mean
# constant for the statistic itself, k = p(1-p)/var(true talent), measured on
# the 293 batters with 800+ PA against a hand:
#
#     observed sd .0245  -  sampling sd .0110  ->  true-talent sd .0218
#     k = .252 x .748 / .0218^2 = 395 PA
#
# That it lands inside the 200-400 plateau the live results show is a check,
# not a fit — the live gaps run -0.4, -0.3, +1.2, -0.2, +0.2 for gates of 200
# through 600 PA, so there is no edge to tune and nothing to overfit to.
#
#     a .400 split on  30 PA shrinks to .262
#     a .400 split on 100 PA shrinks to .282
#     a .400 split on 800 PA shrinks to .351
VS_HAND_REGRESSION_PA: float = 395.0
VS_HAND_LEAGUE_AVG: float = 0.252

# Defined here rather than beside _model_version because it hashes the two
# constants above; anything that changes the model must land before this line.
MODEL_VERSION: str = _model_version()


def shrink_vs_hand(avg: float, pa: Optional[float]) -> float:
    """Regress a vs-hand average toward league mean by its own sample size.

    ``pa`` missing or zero returns the league mean: a split with nothing
    behind it carries no information about the batter, which is a stronger
    and simpler statement than imputing a median and hoping.
    """
    try:
        pa = float(pa) if pa is not None else 0.0
    except (TypeError, ValueError):
        pa = 0.0
    if pa != pa or pa <= 0:
        return VS_HAND_LEAGUE_AVG
    k = VS_HAND_REGRESSION_PA
    return (pa * avg + k * VS_HAND_LEAGUE_AVG) / (pa + k)


def _prepare_features(rec: dict) -> dict:
    """Regress vs_hand_avg by its sample, then hold it inside the fitted range.

    Returns a copy when anything changes, so a caller's row is never mutated
    by having been priced. The cap runs after the shrink and now binds only on
    a genuine outlier with a long record behind it — a .667 career split on
    1,500 PA still shrinks to .583 — rather than on every three-PA call-up.
    """
    v = rec.get("vs_hand_avg")
    try:
        v = float(v) if v is not None else None
    except (TypeError, ValueError):
        return rec
    if v is None or v != v:
        return rec
    v = min(shrink_vs_hand(v, rec.get("vs_hand_pa")), VS_HAND_AVG_CAP)
    if v == rec.get("vs_hand_avg"):
        return rec
    return {**rec, "vs_hand_avg": v}


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
