"""Turn a pick plus a price into an expected value.

Hit rate is not ROI. The batter screen sits around 67% and break-even at -207
is 67.4%, so "the screen is right two times in three" and "the screen makes
money" are very nearly the same sentence — which means the price decides, not
the pick. This module is the part that decides.

Three steps:

  model_probability   what the screen's own history says a pick of this shape
                      wins, shrunk toward the base rate so a thin bucket can't
                      manufacture an edge.
  devig               strip the book's margin out of the quoted price, so the
                      comparison is model-vs-market rather than model-vs-vig.
  expected_value      profit per $1 staked at the quoted price.

Everything here is derived from the 125-day backtest in EXPERIMENTS.md and
should be re-derived whenever the screen's rules change — a calibration table
is only valid for the rule set that produced it.
"""

from __future__ import annotations

from typing import Optional

from .fanduel.odds import american_to_decimal, american_to_implied

# Base rate of the current rule set: 1,241 decided picks, 67.4%.
BASE_RATE = 0.674

# Hit rate by the opposing starter's last-3 H/9, over those same picks. The
# screen's signal is a tail effect (EXPERIMENTS.md, run 1) so the buckets are
# deliberately coarse and only really separate at the top.
#
#   (min_h9, wins, n)
_CALIBRATION = [
    (16.0, 52, 67),    # 77.6% raw — the extreme end, and the thinnest bucket
    (13.0, 236, 344),  # 68.6%
    (11.0, 421, 634),  # 66.4%
    (0.0, 128, 196),   # 65.3% — mostly BvP picks against ordinary starters
]

# Empirical-Bayes shrinkage toward BASE_RATE, in units of "prior observations".
# k=100 is strong on purpose: the SP-16+ bucket has 67 samples and swings from
# 69.2% to 82.9% between halves of the season, and an unshrunk 77.6% would
# quietly claim a double-digit edge on the strength of that noise.
SHRINKAGE_K = 100.0


def model_probability(p_l3_h9: Optional[float]) -> float:
    """The screen's calibrated win probability for a pick facing a starter at
    ``p_l3_h9`` hits per nine over his last three starts.

    ``None`` — no contact line in the game log — falls back to the base rate
    rather than guessing a bucket.
    """
    if p_l3_h9 is None:
        return BASE_RATE
    try:
        h9 = float(p_l3_h9)
    except (TypeError, ValueError):
        return BASE_RATE
    for min_h9, wins, n in _CALIBRATION:
        if h9 >= min_h9:
            return (wins + SHRINKAGE_K * BASE_RATE) / (n + SHRINKAGE_K)
    return BASE_RATE


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


def price_pick(p_l3_h9: Optional[float], american: Optional[int]) -> dict:
    """Everything the UI needs for one pick at one price."""
    p = model_probability(p_l3_h9)
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
        r.update(price_pick(r.get("p_l3_h9"), american))
    return records
