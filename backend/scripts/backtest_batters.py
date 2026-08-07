#!/usr/bin/env python
"""Backtest batter-screen rule changes against settled historical outcomes.

The screen is expensive (a full slate is hundreds of StatsAPI lookups), so
this splits into two phases:

  build  Replay a date range through ``batters.screen_for_date`` with every
         gate switched off, so each day's *whole board* is captured — not
         just the picks the current rules would have made. Each row is
         joined to what actually happened (box-score line: started / PA /
         hits) and written to one parquet per date. Resumable: a date whose
         file already exists is skipped.

  eval   Load those boards and replay candidate rule sets over them, purely
         in pandas. No network, instant, repeatable — so a rule can be
         tuned in a loop instead of one slate per re-scrape.

  sweep  Same, but scans one threshold across a range and prints the
         volume/hit-rate curve, which is where the interesting trade-off
         lives: almost any filter raises hit rate if it's allowed to cut
         volume to nothing.

Every variant is recomputed from *raw* metric columns (recent_avg, bvp_avg,
p_l3_h9, …) rather than from the boolean edge columns the screen stored, so
thresholds are free to vary at eval time without rebuilding the boards.

Settlement matches production ``tracking.resolve_pending`` exactly: a batter
who wasn't in the starting lineup, or who took no plate appearance, is VOID
and drops out of the denominator. Hit rate is wins / (wins + losses).

Usage:
    python scripts/backtest_batters.py build --start 2026-04-01 --end 2026-08-05
    python scripts/backtest_batters.py eval
    python scripts/backtest_batters.py sweep --param sharp_h9 --lo 5.0 --hi 9.0 --step 0.25
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from sharp_edge import batters, tracking

logger = logging.getLogger("backtest")

DEFAULT_DIR = Path.home() / ".sharp-edge" / "backtest" / "batters"


# ---------------------------------------------------------------------------
# Phase 1: build the historical boards
# ---------------------------------------------------------------------------

def _outcomes_for_date(d: date) -> tuple[dict, frozenset]:
    """Box-score batting lines and starting-lineup ids for a played date."""
    dstr = d.isoformat()
    return tracking._boxscore_batting_for_date(dstr), tracking._starters_for_date(dstr)


def _settle(row_ids: pd.Series, batting: dict, starters: frozenset) -> pd.DataFrame:
    """Attach started / pa / hits / result to a board, production rules."""
    out = []
    for bid in row_ids:
        line = batting.get(int(bid))
        pa = line["pa"] if line else 0
        hits = line["hits"] if line else 0
        started = int(bid) in starters if starters else pa > 0
        if not started or pa == 0:
            result = "VOID"
        else:
            result = "WIN" if hits >= 1 else "LOSS"
        out.append({"pa_actual": pa, "hits_actual": hits,
                    "started": started, "result": result})
    return pd.DataFrame(out)


def build(start: date, end: date, outdir: Path, force: bool = False) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    d = start
    while d <= end:
        path = outdir / f"board_{d.isoformat()}.parquet"
        if path.exists() and not force:
            logger.info("%s: already built, skipping", d)
            d += timedelta(days=1)
            continue
        try:
            # Gates off / new edge on: capture the full board so eval can
            # apply any rule set after the fact.
            res = batters.screen_for_date(
                d, veto_sharp_sp=False, include_hittable_edge=True, verbose=False
            )
        except Exception:
            logger.exception("%s: screen failed, skipping", d)
            d += timedelta(days=1)
            continue

        board = res.today
        if board.empty:
            logger.info("%s: no games", d)
            board = pd.DataFrame()
            board.to_parquet(path, index=False)
            d += timedelta(days=1)
            continue

        batting, starters = _outcomes_for_date(d)
        if not batting:
            # No box scores (API hiccup, or the slate hasn't finished).
            # Writing an unsettled board would poison every later eval.
            logger.warning("%s: no box scores available, not writing", d)
            d += timedelta(days=1)
            continue

        board = board.reset_index(drop=True)
        settled = _settle(board["batter_id"], batting, starters)
        board = pd.concat([board, settled], axis=1)
        board["pick_date"] = d.isoformat()
        board.to_parquet(path, index=False)
        logger.info(
            "%s: %d rows (%d decided)", d, len(board),
            int(board["result"].isin(["WIN", "LOSS"]).sum()),
        )
        d += timedelta(days=1)


def load_boards(outdir: Path, since: str | None = None) -> pd.DataFrame:
    files = sorted(outdir.glob("board_*.parquet"))
    if not files:
        sys.exit(f"no boards in {outdir} — run `build` first")
    frames = [pd.read_parquet(f) for f in files]
    frames = [f for f in frames if not f.empty]
    df = pd.concat(frames, ignore_index=True)
    if since:
        df = df[df["pick_date"] >= since]
    return df


# ---------------------------------------------------------------------------
# Phase 2: rule predicates, rebuilt from raw columns
# ---------------------------------------------------------------------------
#
# .ge/.le against NaN is False, which is the behavior we want throughout: a
# missing metric never qualifies a batter, and — importantly — never vetoes
# one either.

def hot(df, avg=0.300, ab=10):
    return df["recent_avg"].ge(avg) & df["recent_ab"].ge(ab)


def bvp(df, avg=0.400, pa=5):
    return df["bvp_avg"].ge(avg) & df["bvp_pa"].ge(pa)


def hand(df, avg=0.400, pa=50):
    return df["vs_hand_avg"].ge(avg) & df["vs_hand_pa"].ge(pa)


def era_slump(df, era=5.00, starts=3):
    return df["p_l3_era"].ge(era) & df["p_l3_starts"].ge(starts)


def sharp(df, h9=batters.MAX_SHARP_H9, baa=batters.MAX_SHARP_BAA,
          starts=batters.MIN_SHARP_STARTS):
    return (df["p_l3_h9"].le(h9) | df["p_l3_baa"].le(baa)) & df["p_l3_starts"].ge(starts)


def hittable(df, h9=batters.MIN_HITTABLE_H9, baa=batters.MIN_HITTABLE_BAA, starts=3):
    return (df["p_l3_h9"].ge(h9) | df["p_l3_baa"].ge(baa)) & df["p_l3_starts"].ge(starts)


def hits_allowed(df, n=24, starts=3):
    """The blunt version: raw hits given up over the last 3 starts."""
    return df["p_l3_hits"].ge(n) & df["p_l3_starts"].ge(starts)


def baseline(df):
    """Production rules before this change: hot bat with a BvP or
    hand+ERA-slump edge, no pitcher-form veto."""
    return hot(df) & (bvp(df) | (hand(df) & era_slump(df)))


VARIANTS = {
    # --- what we're measuring against ---
    "baseline": baseline,
    # --- the veto, applied to the existing rules ---
    "baseline+veto": lambda df: baseline(df) & ~sharp(df),
    # --- which existing edge is carrying the hit rate ---
    "bvp_only": lambda df: hot(df) & bvp(df),
    "bvp_only+veto": lambda df: hot(df) & bvp(df) & ~sharp(df),
    "hand_slump_only": lambda df: hot(df) & hand(df) & era_slump(df),
    # --- is the 5-PA BvP floor just noise? ---
    "bvp_pa8+veto": lambda df: hot(df) & bvp(df, pa=8) & ~sharp(df),
    "bvp_pa12+veto": lambda df: hot(df) & bvp(df, pa=12) & ~sharp(df),
    # --- the new edge: hot bat vs. a starter who's been getting hit ---
    "hot_vs_hittable": lambda df: hot(df) & hittable(df),
    "hot_vs_hittable+veto": lambda df: hot(df) & hittable(df) & ~sharp(df),
    "hot_vs_24hits_l3": lambda df: hot(df) & hits_allowed(df, 24),
    "hot_vs_30hits_l3": lambda df: hot(df) & hits_allowed(df, 30),
    "hot330_vs_hittable": lambda df: hot(df, avg=0.330) & hittable(df),
    # --- everything on ---
    "all_edges+veto": lambda df: (
        hot(df) & (bvp(df) | (hand(df) & era_slump(df)) | hittable(df)) & ~sharp(df)
    ),
}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def wilson(wins: int, decided: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval. A 60%-over-40-picks result and a 60%-over-400
    result are not the same claim, and the raw percentage hides that."""
    if decided == 0:
        return (float("nan"), float("nan"))
    p = wins / decided
    denom = 1 + z * z / decided
    center = (p + z * z / (2 * decided)) / denom
    margin = z * math.sqrt(p * (1 - p) / decided + z * z / (4 * decided * decided)) / denom
    return (100 * (center - margin), 100 * (center + margin))


def score(df: pd.DataFrame, mask: pd.Series) -> dict:
    sel = df[mask]
    wins = int((sel["result"] == "WIN").sum())
    losses = int((sel["result"] == "LOSS").sum())
    voids = int((sel["result"] == "VOID").sum())
    decided = wins + losses
    days = sel["pick_date"].nunique() or 1
    lo, hi = wilson(wins, decided)
    return {
        "picks": len(sel), "decided": decided, "wins": wins, "losses": losses,
        "voids": voids, "per_day": round(len(sel) / days, 1),
        "hit_rate": round(100 * wins / decided, 1) if decided else None,
        "ci_lo": round(lo, 1) if decided else None,
        "ci_hi": round(hi, 1) if decided else None,
    }


def _table(rows: list[dict], base_rate: float | None) -> str:
    head = (
        f"{'variant':<24}{'picks':>7}{'dec':>7}{'W':>6}{'L':>6}"
        f"{'void':>6}{'/day':>7}{'hit%':>7}{'95% CI':>16}{'Δ':>7}"
    )
    out = [head, "-" * len(head)]
    for r in rows:
        hr = r["hit_rate"]
        ci = f"{r['ci_lo']}–{r['ci_hi']}" if hr is not None else "—"
        delta = f"{hr - base_rate:+.1f}" if (hr is not None and base_rate) else "—"
        out.append(
            f"{r['variant']:<24}{r['picks']:>7}{r['decided']:>7}{r['wins']:>6}"
            f"{r['losses']:>6}{r['voids']:>6}{r['per_day']:>7}"
            f"{(hr if hr is not None else float('nan')):>7}{ci:>16}{delta:>7}"
        )
    return "\n".join(out)


def evaluate(df: pd.DataFrame, names: list[str] | None = None) -> None:
    names = names or list(VARIANTS)
    rows = []
    for name in names:
        rows.append({"variant": name, **score(df, VARIANTS[name](df))})
    base = next((r["hit_rate"] for r in rows if r["variant"] == "baseline"), None)
    print(f"\nboards: {df['pick_date'].nunique()} days, {len(df):,} batter-games")
    print(f"slate-wide hit rate (every batter, all starters): "
          f"{score(df, pd.Series(True, index=df.index))['hit_rate']}%\n")
    print(_table(rows, base))

    # What the veto actually removed, and how those picks would have done —
    # the only number that says whether the veto is doing its job.
    removed = VARIANTS["baseline"](df) & sharp(df)
    s = score(df, removed)
    print(
        f"\nvetoed by SHARP-SP: {s['picks']} baseline picks, "
        f"{s['decided']} decided, hit rate {s['hit_rate']}% "
        f"(CI {s['ci_lo']}–{s['ci_hi']})"
    )
    print("  → the veto helps only if this is meaningfully below the baseline.\n")

    # Hit rate by the starter's form band, over the whole board. This is the
    # underlying claim — that recent hit suppression predicts hit props —
    # measured without any batter-side filter muddying it.
    print("hit rate by starting-pitcher form band (whole board):")
    for band, sub in df.groupby("p_form"):
        s = score(sub, pd.Series(True, index=sub.index))
        print(f"  {band:<10} {s['decided']:>7} decided  {s['hit_rate']}%  "
              f"(CI {s['ci_lo']}–{s['ci_hi']})")


def sweep(df: pd.DataFrame, param: str, lo: float, hi: float, step: float) -> None:
    rows = []
    v = lo
    while v <= hi + 1e-9:
        if param == "sharp_h9":
            mask = baseline(df) & ~sharp(df, h9=v, baa=-1)
        elif param == "sharp_baa":
            mask = baseline(df) & ~sharp(df, h9=-1, baa=v)
        elif param == "hittable_h9":
            mask = hot(df) & hittable(df, h9=v, baa=99)
        elif param == "hittable_baa":
            mask = hot(df) & hittable(df, h9=99, baa=v)
        elif param == "hits_l3":
            mask = hot(df) & hits_allowed(df, n=v)
        elif param == "bvp_pa":
            mask = hot(df) & bvp(df, pa=v) & ~sharp(df)
        else:
            sys.exit(f"unknown param {param!r}")
        rows.append({"variant": f"{param}={v:g}", **score(df, mask)})
        v += step
    base = score(df, baseline(df))["hit_rate"]
    print(f"\nbaseline hit rate: {base}%\n")
    print(_table(rows, base))


# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="replay dates and cache settled boards")
    b.add_argument("--start", required=True)
    b.add_argument("--end", default=(date.today() - timedelta(days=1)).isoformat())
    b.add_argument("--force", action="store_true", help="rebuild existing dates")

    e = sub.add_parser("eval", help="score rule variants over cached boards")
    e.add_argument("--since")
    e.add_argument("--variants", help="comma-separated subset")

    s = sub.add_parser("sweep", help="scan one threshold")
    s.add_argument("--param", required=True,
                   choices=["sharp_h9", "sharp_baa", "hittable_h9",
                            "hittable_baa", "hits_l3", "bvp_pa"])
    s.add_argument("--lo", type=float, required=True)
    s.add_argument("--hi", type=float, required=True)
    s.add_argument("--step", type=float, required=True)
    s.add_argument("--since")

    args = ap.parse_args()

    if args.cmd == "build":
        build(date.fromisoformat(args.start), date.fromisoformat(args.end),
              args.dir, force=args.force)
    elif args.cmd == "eval":
        df = load_boards(args.dir, args.since)
        names = args.variants.split(",") if args.variants else None
        evaluate(df, names)
    else:
        sweep(load_boards(args.dir, args.since), args.param,
              args.lo, args.hi, args.step)


if __name__ == "__main__":
    main()
