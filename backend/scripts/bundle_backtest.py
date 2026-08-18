#!/usr/bin/env python
"""Backtest bundling strategies — how would "take the best N picks" have done?

Two honest limits shape everything here, and they are worth stating before any
number is read:

1. **Historical odds are only just starting to exist.** FanDuel's public API
   serves the current board and nothing else, so prices come from the daily
   snapshots in ``data/odds/`` (``scripts/snapshot_odds.py``), which began on
   2026-08-07. Until those overlap the settled boards by a couple of months, a
   genuinely price-aware backtest is still out of reach and this script keeps
   reporting return as a function of an assumed price.

2. **Imputed prices cannot resolve the edge.** Fitting the market on a slate's
   worth of real prices gets R2 ~ 0.49 with a residual of ~4.2 implied-prob
   points, while the edge being hunted is 1-3 points. Per-pick imputation is
   therefore noise; only slate-level aggregates mean anything, and even those
   inherit any bias in the fit.

So this script does the two things that *are* sound:

  * scores the **selection rule** on sweep rate — the share of days where
    every leg won, which is the only outcome a parlay pays on — which needs no
    prices and is measured exactly against settled outcomes; and
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


def legacy_screen(df: pd.DataFrame) -> pd.Series:
    """The **retired** screen, recomputed from raw columns.

    Kept so the old rule can still be scored against the new one on the same
    days. It selects nothing in production any more.
    """
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
    screen: bool = False,
) -> pd.DataFrame:
    """The shipped selection: rank the board, keep the best ``top`` per day.

    Ranking is by ``pricing.model_probability`` evaluated on the **whole row**,
    not on the starter's H/9 alone — the batter's career average against the
    hand is the dominant term, and the old bare-float call threw it away, which
    made this script rank on "prefer the most battered starter" and measure a
    rule nobody ships.

    ``screen=True`` restricts to the retired screen's rows instead, so the two
    can be scored on the same days. That comparison is the point of the
    ``--compare`` table.

    ``cross_game`` keeps only the single best batter against each starter
    before taking the top N. Without it, 57% of top-2 bundles are two batters
    facing the same pitcher — a same-game parlay, which a book prices below
    the product of its legs because it knows the outcomes are correlated.
    Forcing one batter per starter costs a little sweep and makes the parlay
    pricing in this script actually achievable.
    """
    sel = df[legacy_screen(df)].copy() if screen else df.copy()
    if min_h9 is not None:
        sel = sel[sel["p_l3_h9"].ge(min_h9)]
    # The shipped playing-time floor. Live, the board is the whole active
    # roster, so this is what keeps a bench bat off the card.
    if not screen:
        floor = sel["recent_ab"].fillna(0) >= batters.MIN_RECENT_AB_TO_RANK
        if floor.any():
            sel = sel[floor]
    sel["model_p"] = [
        pricing.model_probability(r) for r in sel.to_dict(orient="records")
    ]
    order = ["pick_date", "model_p", "vs_hand_pa"]
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


def sweep_rate(sel: pd.DataFrame, legs: int) -> dict:
    """Share of days where **every** leg won — the only outcome a parlay pays.

    This is the objective, and it is not hit rate: a rule that maximises hit
    rate rewards taking every good bet, while a parlay punishes it, because
    each extra leg is another chance to lose the whole ticket.
    """
    d = sel[sel["result"].isin(["WIN", "LOSS"])]
    played = swept = 0
    for _, g in d.groupby("pick_date"):
        g = g.head(legs)
        if len(g) < legs:
            continue
        played += 1
        swept += int(g["result"].eq("WIN").all())
    return {"days": played, "sweep": 100 * swept / played if played else float("nan")}


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
    if top and top >= 1:
        sw = sweep_rate(sel, top)
        print(f"sweep rate {sw['sweep']:.1f}% of {sw['days']} carded days "
              f"— the objective; hit rate above is context")

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
    """The board against the retired screen, on the same days.

    Sweep is the column that decides — the share of carded days where every
    leg won. Hit rate is shown beside it because the two disagree, and that
    disagreement is the whole reason the screen was retired.
    """
    print(f"\n{'strategy':<28}{'/day':>7}{'dec':>7}{'hit%':>8}{'95% CI':>15}"
          f"{'sweep':>9}{'ROI@-200':>10}")
    print("-" * 84)
    imp200 = 1 / american_to_decimal(-200)
    rows = [(f"board, top {k}/day, x-game", k, None, True, False) for k in (1, 2, 3, 4)]
    rows += [(f"screen, top {k}/day, x-game", k, None, True, True) for k in (1, 2, 3)]
    rows += [("board, all, x-game", None, None, True, False),
             ("screen, all, x-game", None, None, True, True)]
    for label, top, min_h9, xg, screen in rows:
        sel = select(df, top, min_h9, xg, screen=screen)
        d = sel[sel["result"].isin(["WIN", "LOSS"])]
        if len(d) < 30:
            continue
        w = int((d["result"] == "WIN").sum())
        lo, hi = _wilson(w, len(d))
        days = sel["pick_date"].nunique()
        roi = straight_roi(sel, imp200)["roi"]
        sw = sweep_rate(sel, top)["sweep"] if top else float("nan")
        swtxt = "—" if sw != sw else f"{sw:.1f}%"
        print(f"{label:<28}{len(sel)/days:>7.1f}{len(d):>7}"
              f"{100*w/len(d):>7.1f}%{f'{lo:.1f}–{hi:.1f}':>15}{swtxt:>9}"
              f"{roi:>+9.1f}%")


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
