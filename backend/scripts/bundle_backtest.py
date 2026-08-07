#!/usr/bin/env python
"""Backtest bundling strategies — how would "take the best N picks" have done?

Two honest limits shape everything here, and they are worth stating before any
number is read:

1. **There are no historical odds.** FanDuel's public API serves the current
   board and nothing else, so a genuine price-aware backtest is impossible
   until enough daily snapshots accumulate (``scripts/snapshot_odds.py``).

2. **Imputed prices cannot resolve the edge.** Fitting the market on a slate's
   worth of real prices gets R2 ~ 0.49 with a residual of ~4.2 implied-prob
   points, while the edge being hunted is 1-3 points. Per-pick imputation is
   therefore noise; only slate-level aggregates mean anything, and even those
   inherit any bias in the fit.

So this script does the two things that *are* sound:

  * scores the **selection rule** on hit rate, which needs no prices and is
    measured exactly against settled outcomes; and
  * reports return as a **function of the assumed price**, so the reader can
    see precisely which market assumption the strategy needs in order to win
    rather than being handed one number resting on a hidden guess.

Usage:
    python scripts/bundle_backtest.py --top 3
    python scripts/bundle_backtest.py --compare
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sharp_edge import batters, pricing  # noqa: E402
from sharp_edge.fanduel.odds import american_to_decimal  # noqa: E402

DEFAULT_DIR = Path.home() / ".sharp-edge" / "backtest" / "batters"


def load(outdir: Path) -> pd.DataFrame:
    files = sorted(outdir.glob("board_*.parquet"))
    if not files:
        sys.exit(f"no boards in {outdir} — run backtest_batters.py build first")
    frames = [pd.read_parquet(f) for f in files]
    df = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    return df


def current_rules(df: pd.DataFrame) -> pd.Series:
    """The shipped screen, recomputed from raw columns."""
    hot = df["recent_avg"].ge(0.300) & df["recent_ab"].ge(10)
    bvp = df["bvp_avg"].ge(0.400) & df["bvp_pa"].ge(5)
    hittable = (
        df["p_l3_h9"].ge(batters.MIN_HITTABLE_H9)
        | df["p_l3_baa"].ge(batters.MIN_HITTABLE_BAA)
    ) & df["p_l3_starts"].ge(3)
    sharp = (
        df["p_l3_h9"].le(batters.MAX_SHARP_H9) | df["p_l3_baa"].le(batters.MAX_SHARP_BAA)
    ) & df["p_l3_starts"].ge(batters.MIN_SHARP_STARTS)
    return hot & (bvp | hittable) & ~sharp


def select(
    df: pd.DataFrame,
    top: int | None,
    min_h9: float | None,
    cross_game: bool = False,
) -> pd.DataFrame:
    """Apply the screen, then keep the best ``top`` picks per day.

    Ranking is by the calibrated model probability, which on the current
    calibration is a monotone function of the starter's H/9 — so this is
    "prefer the most battered starter", with recent average breaking ties.

    ``cross_game`` keeps only the single best batter against each starter
    before taking the top N. Without it, 57% of top-2 bundles are two batters
    facing the same pitcher — a same-game parlay, which a book prices below
    the product of its legs because it knows the outcomes are correlated.
    Forcing one batter per starter costs a little hit rate and makes the
    parlay pricing in this script actually achievable.
    """
    sel = df[current_rules(df)].copy()
    if min_h9 is not None:
        sel = sel[sel["p_l3_h9"].ge(min_h9)]
    sel["model_p"] = sel["p_l3_h9"].map(pricing.model_probability)
    order = ["pick_date", "model_p", "recent_avg"]
    asc = [True, False, False]
    if cross_game:
        sel = (
            sel.sort_values(order, ascending=asc)
            .groupby(["pick_date", "pitcher_id"], group_keys=False)
            .head(1)
        )
    if top:
        sel = (
            sel.sort_values(order, ascending=asc)
            .groupby("pick_date", group_keys=False)
            .head(top)
        )
    return sel


def straight_roi(sel: pd.DataFrame, implied: float) -> dict:
    """Flat-stake ROI if every leg were priced at ``implied`` probability."""
    dec = 1 / implied
    d = sel[sel["result"].isin(["WIN", "LOSS"])]
    n = len(d)
    if not n:
        return {"n": 0, "hit": None, "roi": None}
    wins = int((d["result"] == "WIN").sum())
    profit = wins * (dec - 1) - (n - wins)
    return {"n": n, "hit": 100 * wins / n, "roi": 100 * profit / n}


def parlay_roi(sel: pd.DataFrame, implied: float, legs: int) -> dict:
    """Daily parlay of the day's selected picks, ``legs`` at a time.

    Only days with at least ``legs`` decided picks count, and the parlay is
    priced as the product of the individual prices — which is how a book
    prices a *cross-game* parlay. Same-game legs are quoted worse than that,
    so treat this as an upper bound whenever a bundle stacks one pitcher.
    """
    dec_leg = 1 / implied
    days, wins, staked, returned = 0, 0, 0.0, 0.0
    for _, g in sel[sel["result"].isin(["WIN", "LOSS"])].groupby("pick_date"):
        g = g.head(legs)
        if len(g) < legs:
            continue
        days += 1
        staked += 1.0
        if (g["result"] == "WIN").all():
            wins += 1
            returned += dec_leg ** legs
    if not days:
        return {"days": 0, "hit": None, "roi": None}
    return {
        "days": days,
        "hit": 100 * wins / days,
        "roi": 100 * (returned - staked) / staked,
    }


def _fmt(v, suffix="%"):
    return "—" if v is None else f"{v:+.1f}{suffix}"


def report(df: pd.DataFrame, top: int | None, min_h9: float | None,
           cross_game: bool = False) -> None:
    sel = select(df, top, min_h9, cross_game)
    d = sel[sel["result"].isin(["WIN", "LOSS"])]
    days = sel["pick_date"].nunique()
    wins = int((d["result"] == "WIN").sum())
    lo, hi = _wilson(wins, len(d))
    label = f"top {top}/day" if top else "all picks"
    if min_h9:
        label += f", SP h9 >= {min_h9}"
    if cross_game:
        label += ", cross-game"

    print(f"\n=== {label} ===")
    print(f"{len(sel)} picks over {days} days ({len(sel)/days:.1f}/day), "
          f"{len(d)} decided")
    print(f"hit rate {100*wins/len(d):.1f}%  (95% CI {lo:.1f}–{hi:.1f})")

    print("\nflat-stake ROI vs. the price you'd have to get:")
    print(f"{'avg price':<14}{'implied':>9}{'ROI/bet':>10}")
    for american in (-250, -220, -200, -180, -160, -140):
        imp = 1 / american_to_decimal(american)
        r = straight_roi(sel, imp)
        print(f"{american:<14}{100*imp:>8.1f}%{_fmt(r['roi']):>10}")

    if top and top >= 2:
        print(f"\ndaily {top}-leg parlay (cross-game pricing, an upper bound):")
        print(f"{'avg leg price':<16}{'days':>7}{'hit%':>8}{'ROI/bet':>10}")
        for american in (-250, -220, -200, -180, -160):
            imp = 1 / american_to_decimal(american)
            p = parlay_roi(sel, imp, top)
            print(f"{american:<16}{p['days']:>7}{p['hit']:>7.1f}%{_fmt(p['roi']):>10}")


def _wilson(w, n, z=1.96):
    if not n:
        return (float("nan"), float("nan"))
    p = w / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (100 * (c - m), 100 * (c + m))


def compare(df: pd.DataFrame) -> None:
    """Does being choosier actually help? The screen's edge is a tail effect,
    so in principle fewer-and-better should beat take-everything."""
    print(f"\n{'strategy':<24}{'picks':>7}{'/day':>7}{'dec':>7}{'hit%':>8}"
          f"{'95% CI':>15}{'ROI@-200':>10}")
    print("-" * 78)
    imp200 = 1 / american_to_decimal(-200)
    rows = [("all picks", None, None, False)]
    rows += [(f"top {k}/day", k, None, False) for k in (1, 2, 3, 5)]
    rows += [("SP h9 >= 13", None, 13.0, False), ("SP h9 >= 16", None, 16.0, False)]
    rows += [("top 2/day, h9 >= 13", 2, 13.0, False)]
    rows += [(f"top {k}/day cross-game", k, None, True) for k in (1, 2, 3)]
    for label, top, min_h9, xg in rows:
        sel = select(df, top, min_h9, xg)
        d = sel[sel["result"].isin(["WIN", "LOSS"])]
        if len(d) < 30:
            continue
        w = int((d["result"] == "WIN").sum())
        lo, hi = _wilson(w, len(d))
        days = sel["pick_date"].nunique()
        roi = straight_roi(sel, imp200)["roi"]
        print(f"{label:<24}{len(sel):>7}{len(sel)/days:>7.1f}{len(d):>7}"
              f"{100*w/len(d):>7.1f}%{f'{lo:.1f}–{hi:.1f}':>15}{roi:>+9.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--top", type=int, help="keep the best N picks per day")
    ap.add_argument("--min-h9", type=float, help="only starters at/above this H/9")
    ap.add_argument("--compare", action="store_true", help="scan strategies")
    ap.add_argument("--cross-game", action="store_true",
                    help="one batter per starter, so parlay pricing is achievable")
    args = ap.parse_args()

    df = load(args.dir)
    if args.compare or (args.top is None and args.min_h9 is None):
        compare(df)
    else:
        report(df, args.top, args.min_h9, args.cross_game)


if __name__ == "__main__":
    main()
