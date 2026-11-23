"""Does anything we know predict a rung beyond its own price?

    python backend/scripts/rung_study/beyond_price.py <rungs.parquet>

Logistic regression of the result on logit(implied) plus one candidate set,
fitted on three weeks and scored on the fourth (each week held out in turn).
The bar is the price-only fit on the same held-out rows: a candidate that
cannot lower held-out log-loss below it carries no information the book did
not already price. Then, for the best candidate, the held-out ROI of the rungs
it rates as beating the price by 5+ points. Standard errors by player-week
bootstrap.
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from compare import boot, load  # noqa: E402


def fit(X, y, l2=1.0, iters=50):
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (p - y) + l2 * np.r_[0, w[1:]]
        H = (X * (p * (1 - p))[:, None]).T @ X + l2 * np.diag(np.r_[0, np.ones(len(w) - 1)])
        w -= np.linalg.solve(H, g)
    return w


def design(d, cols):
    li = np.log(d.implied / (1 - d.implied))
    parts = [np.ones(len(d)), li.to_numpy()]
    for c in cols:
        v = d[c].astype(float).fillna(d[c].astype(float).median())
        parts.append(((v - v.mean()) / (v.std() or 1)).to_numpy())
    return np.column_stack(parts)


def main(path):
    d = load(path)
    d = d[(d.implied > 0.02) & (d.implied < 0.98)].copy()
    d["lcush"] = np.log(d.cushion.clip(0.05, 10))
    d["hit_rate5"] = d.hits5 / 5
    d["model_li"] = np.log(d.model_p.clip(.01, .99) / (1 - d.model_p.clip(.01, .99)))
    d["move"] = d.implied - d.open_implied
    d["rec"] = (d.market == "receptions").astype(float)
    d["rush"] = (d.market == "rushing_yards").astype(float)
    d["steady_f"] = d.steady.astype(float)
    sets = {
        "price only": [],
        "cushion + streak": ["lcush", "hit_rate5"],
        "role": ["snap_min", "ts_mean", "tgt_mean", "tgt_cv", "cs_mean", "car_cv", "steady_f"],
        "cushion + streak + role": ["lcush", "hit_rate5", "snap_min", "ts_mean", "tgt_cv",
                                    "cs_mean", "steady_f"],
        "props model": ["model_li"],
        "line move since open": ["move"],
        "market": ["rec", "rush"],
        "everything": ["lcush", "hit_rate5", "snap_min", "ts_mean", "tgt_cv", "cs_mean",
                       "steady_f", "model_li", "move", "rec", "rush"],
    }
    weeks = sorted(d.week.unique())
    held = {}
    for name, cols in sets.items():
        p = np.empty(len(d))
        for w in weeks:
            tr, te = (d.week != w).to_numpy(), (d.week == w).to_numpy()
            X = design(d, cols)
            coef = fit(X[tr], d.y.to_numpy()[tr])
            p[te] = 1 / (1 + np.exp(-X[te] @ coef))
        held[name] = p
    y = d.y.to_numpy()
    ll = lambda p: -(y * np.log(p) + (1 - y) * np.log(1 - p))
    base = ll(held["price only"])
    raw = ll(d.implied.to_numpy())
    print(f"held-out log-loss, raw price {raw.mean():.4f}; price refit {base.mean():.4f}")
    rng = np.random.default_rng(0)
    pw = d.pw.to_numpy()
    groups = pd.Series(np.arange(len(d))).groupby(pw).apply(np.array).to_list()
    for name, p in held.items():
        diff = ll(p) - base
        sums = np.array([diff[g].sum() for g in groups]); cnt = np.array([len(g) for g in groups])
        idx = rng.integers(0, len(groups), size=(1000, len(groups)))
        bs = sums[idx].sum(1) / cnt[idx].sum(1)
        lo, hi = np.percentile(bs, [2.5, 97.5])
        print(f"  {name:26s} {ll(p).mean():.4f}  vs price refit {diff.mean():+.5f} [{lo:+.5f}, {hi:+.5f}]")
    for name in ("cushion + streak + role", "everything"):
        d["p_hat"] = held[name]
        for edge in (0.03, 0.05, 0.08):
            pick = d[d.p_hat - d.implied >= edge]
            if len(pick) < 30:
                continue
            lo, hi = boot(pick)
            print(f"  {name:26s} held-out edge {edge:.2f}+: {len(pick):5d} rungs, hit {pick.y.mean():.1%} "
                  f"vs implied {pick.implied.mean():.1%}, ROI {pick.ret.mean():+.1%} [{lo:+.1%}, {hi:+.1%}]")


if __name__ == "__main__":
    main(sys.argv[1])
