"""Which kinds of alt rung beat their price? Reads build.py's output.

    python backend/scripts/rung_study/compare.py <rungs.parquet>

ROI is per one-unit flat bet at the latest quote. The CI is a bootstrap over
player-weeks (a player's rungs in one week rise and fall together), 2,000 draws.
"""
import sys

import numpy as np
import pandas as pd


def decimal(o):
    return np.where(o > 0, 1 + o / 100, 1 + 100 / -o)


def load(path):
    d = pd.read_parquet(path)
    d = d[d.result.isin(["WIN", "LOSS"]) & d.odds.notna()].copy()
    d["y"] = (d.result == "WIN").astype(int)
    d["ret"] = np.where(d.y == 1, decimal(d.odds.astype(float)) - 1, -1.0)
    d["pw"] = d.week.astype(str) + ":" + d.player_key
    rec = d.market != "rushing_yards"
    d["steady"] = np.where(rec, (d.snap_min >= 0.5) & (d.ts_mean >= 0.12), d.cs_mean >= 0.40)
    d.loc[d.snap_min.isna(), "steady"] = False
    d["cush_band"] = pd.cut(d.cushion, [0, 0.8, 1.0, 1.25, 1.6, 2.0, 3.0, 100],
                            labels=["<0.8", "0.8-1", "1-1.25", "1.25-1.6", "1.6-2", "2-3", "3+"],
                            right=False)
    d["price"] = pd.cut(d.odds, [-100000, -500, -300, -200, -150, -100, 100, 200, 100000],
                        labels=["<=-500", "-500..-301", "-300..-201", "-200..-151",
                                "-150..-101", "-100..+99", "+100..+199", "+200+"], right=False)
    return d


def boot(g, n=2000, seed=0):
    rng = np.random.default_rng(seed)
    by = g.groupby("pw").ret.agg(["sum", "count"])
    s, c = by["sum"].to_numpy(), by["count"].to_numpy()
    idx = rng.integers(0, len(s), size=(n, len(s)))
    r = s[idx].sum(1) / c[idx].sum(1)
    return np.percentile(r, [2.5, 97.5])


def table(d, by, title, min_n=40):
    print(f"\n-- {title}")
    print(f"  {'group':28s} {'rungs':>6s} {'pw':>5s} {'hit':>6s} {'implied':>8s} {'ROI':>7s}  95% CI")
    for k, g in d.groupby(by, observed=True):
        if len(g) < min_n:
            continue
        lo, hi = boot(g)
        print(f"  {str(k):28s} {len(g):6d} {g.pw.nunique():5d} {g.y.mean():6.1%} "
              f"{g.implied.mean():8.1%} {g.ret.mean():+7.1%}  [{lo:+.1%}, {hi:+.1%}]")


def main(path):
    d = load(path)
    table(d, lambda _: "all", "every graded rung")
    table(d, "market", "by market")
    table(d, "price", "by price")
    table(d, "cush_band", "by cushion (median of last five / rung)")
    table(d, ["steady", "cush_band"], "steady role x cushion")
    table(d, ["market", "steady"], "market x steady role")
    deep = d[(d.cushion >= 1.6)]
    table(deep, ["market", "steady"], "cushion 1.6+ only: market x steady")
    table(deep[deep.steady], "price", "cushion 1.6+, steady: by price")
    table(d, "week", "by week")


if __name__ == "__main__":
    main(sys.argv[1])
