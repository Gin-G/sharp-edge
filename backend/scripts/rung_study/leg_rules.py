"""The parlays' leg rules, today's and proposed, on the graded archive.

    python backend/scripts/rung_study/leg_rules.py <rungs.parquet>

cost/payout is the margin a leg costs per unit of log payout it adds:
-log(1 + ROI) / log(decimal). A parlay to a fixed payout pays the sum of its
legs' costs, so the lower it is the less a ticket gives up to reach its target.
"""
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from compare import boot, load  # noqa: E402


def main(path):
    d = load(path)
    d["lpay"] = np.log(np.where(d.odds > 0, 1 + d.odds / 100, 1 + 100 / -d.odds))
    rec = d.market != "receptions"
    S = {
        "double-up today (3/4, edge 4-12, -500..+150)":
            (d.hits4 >= 3) & d.edge_pts.between(4, 12) & d.odds.between(-500, 150),
        "steady, cushion 1.6+, -500..-150": d.steady & (d.cushion >= 1.6) & d.odds.between(-500, -150),
        "  same, no receptions": d.steady & (d.cushion >= 1.6) & d.odds.between(-500, -150) & rec,
        "steady, cushion 1.6+, any price": d.steady & (d.cushion >= 1.6),
        "lotto today (steady, 4/5, -300..+150)": d.steady & (d.hits5 >= 4) & d.odds.between(-300, 150),
        "+100..+199, all": d.odds.between(100, 199),
        "+100..+199, steady": d.steady & d.odds.between(100, 199),
        "+100..+199, steady, no receptions": d.steady & d.odds.between(100, 199) & rec,
        "+200 and longer": d.odds >= 200,
        "-150..-101, all": d.odds.between(-150, -101),
        "-500 and shorter, all": d.odds <= -500,
    }
    print(f"{'slice':46s} {'rungs':>6s} {'hit':>6s} {'impl':>6s} {'ROI':>7s}  {'95% CI':18s} cost/payout")
    for k, m in S.items():
        g = d[m]
        lo, hi = boot(g)
        eff = -np.log1p(g.ret.mean()) / g.lpay.mean()
        print(f"{k:46s} {len(g):6d} {g.y.mean():6.1%} {g.implied.mean():6.1%} "
              f"{g.ret.mean():+7.1%}  [{lo:+.1%}, {hi:+.1%}]  {eff:.2f}")


if __name__ == "__main__":
    main(sys.argv[1])
