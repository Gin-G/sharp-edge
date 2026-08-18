#!/usr/bin/env python
"""Fit the hit-probability model that prices picks, and prove it out of sample.

The model shipped in ``pricing.py`` reads one thing: the starting pitcher's
last-3 H/9, in four coarse buckets. That has two consequences, one of which is
a bug we already had to patch around.

Every batter facing a given starter gets the same number. So the model cannot
tell Jose Ramirez from a .190-hitting backup catcher, and applied to a whole
board it invents enormous edges on weak hitters the market has priced
correctly. ``pricing.is_screen_pick`` existed only to stop that; it is now a
label on the retired screen and gates nothing.

And it leans on the weakest available signal. Over 29,777 settled board rows
the pitcher's H/9 correlates with getting a hit at r=+0.011, while the
batter's career average against the hand correlates at r=+0.119 — an order of
magnitude more. The market weights them roughly that way too, which is why it
prices ~81% of the effect the screen is built on.

So this fits a logistic regression on batter *and* pitcher features over the
whole board, not just picks. Done properly that removes the eligibility
restriction entirely: a model that knows who the batter is can be trusted on
any batter.

Honesty, in the two places it matters:

  * the split is **by date**, first half fits and second half tests. A random
    split would leak — the same batter appears on many days, and a model that
    has memorised him scores well without predicting anything.
  * the incumbent 4-bucket model is scored on the identical test set, so
    "better" means better than what's live rather than better than nothing.

Usage:
    python scripts/calibrate_model.py
    python scripts/calibrate_model.py --emit      # coefficients for pricing.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sharp_edge import pricing  # noqa: E402

DEFAULT_DIR = Path.home() / ".sharp-edge" / "backtest" / "batters"

# Chosen for signal and for being available on essentially every row. BvP is
# deliberately absent: it's missing on 48% of the board, and the backtest
# already found the BvP edge to be worth ~2 points over any hot bat with 8.7
# points of split-half drift. Imputing a noisy feature for half the rows buys
# nothing.
FEATURES = [
    "vs_hand_avg",   # r=+0.119 — batter quality, the dominant term
    "recent_ab",     # r=+0.070 — a regular, not a bench bat; also more PA today
    "p_l3_h9",       # r=+0.011 — the screen's original signal
    "p_l3_k9",       # r=-0.028 — strikeouts suppress contact
]

# recent_avg is deliberately absent. Univariately it points the right way
# (r=+0.015) but in the fit it takes a *negative* coefficient — it is collinear
# with vs_hand_avg and recent_ab, and a sign flip means the model is using it
# to undo those rather than to predict. A feature that only makes sense
# alongside its collaborators is a feature that will mislead on a row where one
# of them is imputed.


def load(outdir: Path) -> pd.DataFrame:
    files = sorted(outdir.glob("board_*.parquet"))
    if not files:
        sys.exit(f"no boards in {outdir} — run backtest_batters.py build first")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    d = df[df["result"].isin(["WIN", "LOSS"])].copy()
    d["y"] = d["result"].eq("WIN").astype(int)
    return d


def design(d: pd.DataFrame, medians: dict | None = None) -> tuple[np.ndarray, dict]:
    """Feature matrix with median imputation and an intercept.

    Medians come from the *training* half when one is supplied — imputing test
    rows with statistics computed over the test set is a quiet form of
    leakage.
    """
    medians = medians or {c: float(d[c].median()) for c in FEATURES}
    cols = [d[c].fillna(medians[c]).astype(float).values for c in FEATURES]
    X = np.column_stack([np.ones(len(d))] + cols)
    return X, medians


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0,
                 iters: int = 50) -> np.ndarray:
    """Newton-Raphson (IRLS) with a light ridge penalty.

    The penalty is there to keep the fit stable rather than to regularise
    hard — with 5 features and ~15k training rows it barely binds, but it
    stops a separated column producing an absurd coefficient.
    """
    n, k = X.shape
    beta = np.zeros(k)
    pen = l2 * np.eye(k)
    pen[0, 0] = 0.0  # never penalise the intercept
    for _ in range(iters):
        eta = np.clip(X @ beta, -30, 30)
        p = 1 / (1 + np.exp(-eta))
        W = np.clip(p * (1 - p), 1e-8, None)
        grad = X.T @ (y - p) - pen @ beta
        H = X.T @ (X * W[:, None]) + pen
        try:
            step = np.linalg.solve(H, grad)
        except np.linalg.LinAlgError:
            break
        beta += step
        if np.max(np.abs(step)) < 1e-8:
            break
    return beta


def predict(X: np.ndarray, beta: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-np.clip(X @ beta, -30, 30)))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def auc(y: np.ndarray, p: np.ndarray) -> float:
    """Rank-based AUC. 0.5 is a coin flip."""
    order = np.argsort(p)
    ranks = np.empty(len(p), float)
    ranks[order] = np.arange(1, len(p) + 1)
    # Average ranks within ties, or a model with few distinct outputs (the
    # incumbent has four) is scored unfairly.
    df = pd.DataFrame({"p": p, "r": ranks})
    ranks = df.groupby("p")["r"].transform("mean").values
    n1 = y.sum()
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    return (ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def log_loss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def calibration_table(y: np.ndarray, p: np.ndarray, bins: int = 8) -> str:
    """Predicted vs actual by predicted-probability bucket.

    A model can rank well and still be miscalibrated, and EV is computed from
    the *level*, not the ranking — so this is the table that decides whether
    the number can be multiplied by a price.
    """
    q = pd.qcut(p, bins, duplicates="drop")
    t = pd.DataFrame({"y": y, "p": p, "q": q}).groupby("q", observed=True).agg(
        n=("y", "size"), predicted=("p", "mean"), actual=("y", "mean")
    )
    lines = [f"{'bucket':<22}{'n':>7}{'pred':>8}{'actual':>8}{'diff':>7}"]
    lines.append("-" * 52)
    for idx, r in t.iterrows():
        lines.append(
            f"{str(idx):<22}{int(r['n']):>7}{100*r['predicted']:>7.1f}%"
            f"{100*r['actual']:>7.1f}%{100*(r['actual']-r['predicted']):>+7.1f}"
        )
    return "\n".join(lines)


def incumbent(d: pd.DataFrame) -> np.ndarray:
    """What ships today: four buckets on the pitcher's H/9."""
    return d["p_l3_h9"].map(pricing.model_probability).astype(float).values


def screen_picks(t: pd.DataFrame) -> np.ndarray:
    """The retired screen's rows. Kept only to score the old rule for contrast.

    This is no longer the population that gets bet — see ``bet_rows``.
    """
    from sharp_edge import batters as B
    hot = t["recent_avg"].ge(0.300) & t["recent_ab"].ge(10)
    bvp = t["bvp_avg"].ge(0.400) & t["bvp_pa"].ge(5)
    hittable = (
        t["p_l3_h9"].ge(B.MIN_HITTABLE_H9) | t["p_l3_baa"].ge(B.MIN_HITTABLE_BAA)
    ) & t["p_l3_starts"].ge(3)
    sharp = (
        t["p_l3_h9"].le(B.MAX_SHARP_H9) | t["p_l3_baa"].le(B.MAX_SHARP_BAA)
    ) & t["p_l3_starts"].ge(B.MIN_SHARP_STARTS)
    return (hot & (bvp | hittable) & ~sharp).values


def bet_rows(t: pd.DataFrame, p: np.ndarray, legs: int = 2) -> np.ndarray:
    """The rows actually bet: the day's top ``legs``, one batter per game.

    A calibration is only valid for the population that produced it, and that
    population is no longer "everything the screen flagged" — it is the top of
    the board under the model being fitted. Which rows those are depends on the
    model's own scores, so this takes them as an argument rather than
    recomputing a fixed rule.
    """
    from sharp_edge import batters as B
    w = t.assign(_p=p, _i=np.arange(len(t)))
    w = w[w["recent_ab"].fillna(0) >= B.MIN_RECENT_AB_TO_RANK]
    w = w.sort_values(["pick_date", "_p", "vs_hand_pa"],
                      ascending=[True, False, False])
    w = w.groupby(["pick_date", "pitcher_id"], group_keys=False).head(1)
    keep = w.groupby("pick_date", group_keys=False).head(legs)["_i"].values
    m = np.zeros(len(t), dtype=bool)
    m[keep] = True
    return m


# The Platt helpers that used to live here are gone, deliberately.
#
# They fit a two-parameter correction on the screen's pick population, which
# ran 4.7 points above what the features explained. That selection effect went
# away with the screen: the card is now the top of the board, and the logistic
# is fit on and honest across the board. Refitting a correction for the new
# population was measured and came out *worse* — over the top 8 of each day it
# returns (0.4849, 0.4913), which shrinks toward that pool's mean and drags the
# top-2 estimate down to 73.3% against a 76.7% outcome.
#
# Leaving the helpers here would invite exactly that experiment being redone by
# someone who assumed a missing correction was an oversight. See run 3 in
# EXPERIMENTS.md.


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--emit", action="store_true",
                    help="print coefficients ready to paste into pricing.py")
    args = ap.parse_args()

    d = load(args.dir)
    dates = sorted(d["pick_date"].unique())
    cut = dates[len(dates) // 2]
    train, test = d[d["pick_date"] < cut], d[d["pick_date"] >= cut]

    print(f"boards: {len(d):,} decided rows over {len(dates)} days")
    print(f"base rate: {100*d['y'].mean():.1f}%")
    print(f"split by date at {cut}: {len(train):,} train / {len(test):,} test\n")

    Xtr, med = design(train)
    Xte, _ = design(test, med)
    ytr, yte = train["y"].values, test["y"].values

    beta = fit_logistic(Xtr, ytr)
    p_new = predict(Xte, beta)
    p_old = incumbent(test)
    p_base = np.full(len(test), ytr.mean())

    print(f"{'model':<28}{'AUC':>8}{'log-loss':>11}")
    print("-" * 47)
    for name, p in (("base rate (no features)", p_base),
                    ("incumbent (4 H/9 buckets)", p_old),
                    ("logistic (batter+pitcher)", p_new)):
        print(f"{name:<28}{auc(yte, p):>8.4f}{log_loss(yte, p):>11.5f}")

    print("\ncoefficients (log-odds per unit):")
    for name, b in zip(["intercept"] + FEATURES, beta):
        print(f"  {name:<14}{b:>+10.4f}")

    print("\ncalibration of the new model, held-out half:")
    print(calibration_table(yte, p_new))

    print("\nspread of predictions (the incumbent's whole problem):")
    print(f"  incumbent: {len(np.unique(p_old))} distinct values, "
          f"range {100*p_old.min():.1f}%–{100*p_old.max():.1f}%")
    print(f"  logistic:  {len(np.unique(p_new.round(4)))} distinct values, "
          f"range {100*p_new.min():.1f}%–{100*p_new.max():.1f}%")

    # The level, on the population that actually gets bet: the top two of the
    # board each day, one batter per game. Not the retired screen — a
    # calibration is only valid for the population that produced it, and that
    # is the one this model will be asked about.
    mte = bet_rows(test, p_new)
    ym = yte[mte]
    print(f"\n=== held-out BET rows (n={mte.sum()}, actual {100*ym.mean():.1f}%) ===")
    print(f"{'model':<26}{'AUC':>8}{'log-loss':>11}{'mean pred':>11}")
    print("-" * 56)
    for nm, p in (("incumbent", p_old[mte]), ("logistic", p_new[mte])):
        print(f"{nm:<26}{auc(ym, p):>8.4f}{log_loss(ym, p):>11.5f}{100*p.mean():>10.1f}%")
    print("\ncalibration on held-out bet rows:")
    print(calibration_table(ym, p_new[mte], bins=5))
    resid = 100 * (ym.mean() - p_new[mte].mean())
    print(f"\nlevel error on bet rows: {resid:+.1f} points. Negative means the "
          f"model talks the card up, which is the direction to worry about; a "
          f"small positive number is conservative and safe.")

    # For contrast only: what the retired screen's population looked like.
    ms = screen_picks(test)
    if ms.sum():
        print(f"\n(retired screen's rows, for contrast: n={ms.sum()}, "
              f"actual {100*yte[ms].mean():.1f}%)")

    if args.emit:
        full_X, full_med = design(d)
        full_beta = fit_logistic(full_X, d["y"].values)
        print("\n# --- paste into pricing.py, refit on the full history ---")
        print("_COEF = {")
        print(f'    "intercept": {full_beta[0]:.6f},')
        for name, b in zip(FEATURES, full_beta[1:]):
            print(f'    "{name}": {b:.6f},')
        print("}")
        print("_MEDIANS = {")
        for k, v in full_med.items():
            print(f'    "{k}": {v:.4f},')
        print("}")
        # No _PLATT line. The correction was removed from pricing.py when the
        # card stopped being drawn from the screen: it corrected a selection
        # effect in that sub-population, and fitting a fresh one on the top of
        # the board measured *worse* than leaving the logistic alone. Emitting
        # a pair here would invite pasting a constant the module no longer has.


if __name__ == "__main__":
    main()
