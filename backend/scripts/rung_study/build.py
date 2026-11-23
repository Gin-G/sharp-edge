"""Every archived alt rung, graded, with the player's role going in.

    python backend/scripts/rung_study/build.py <alt-prices.json> <out.parquet>

<alt-prices.json> is GET /nfl/alt-prices?season=2026. For each rung: the
result (void when he took no offensive snap), the book's implied chance at the
latest and opening quote, and his last five games before that week —
the same `recent_values` the live lotto reads, so the features are what the
board could have known. Finding 16.
"""
import json
import sys

import pandas as pd

from sharp_edge.nfl.altparlay import recent_values
from sharp_edge.nfl.names import norm_name

SEASON = 2026
STAT = {"receiving_yards": "receiving_yards", "receptions": "receptions",
        "rushing_yards": "rushing_yards"}


def implied(o):
    if o is None or o != o:
        return None
    return 100 / (o + 100) if o > 0 else -o / (-o + 100)


def main(src, out):
    rows = pd.DataFrame(json.load(open(src))["rows"])
    rows = rows[rows.market.isin(STAT)]
    wk = pd.read_parquet(f"https://github.com/nflverse/nflverse-data/releases/download/"
                         f"stats_player/stats_player_week_{SEASON}.parquet")
    wk = wk[wk.season_type == "REG"].assign(key=lambda d: d.player_display_name.map(norm_name))
    sn = pd.read_parquet(f"https://github.com/nflverse/nflverse-data/releases/download/"
                         f"snap_counts/snap_counts_{SEASON}.parquet")
    sn = sn[(sn.game_type == "REG") & (sn.offense_snaps.fillna(0) > 0)]
    sn = sn.assign(key=sn.player.map(norm_name))
    played_weeks = set(wk.week.unique())
    out_rows = []
    for week, g in rows.groupby("week"):
        if week not in played_weeks:
            continue
        rv = recent_values(SEASON, int(week), force=True)
        stats = wk[wk.week == week].set_index("key")
        snaps = set(sn[sn.week == week].key)
        teams_played = set(wk[wk.week == week].team)
        for r in g.itertuples():
            thr = r.line + 0.5
            if r.team not in teams_played:
                continue                # his game is not in the box scores yet
            if r.player_key not in snaps:
                res, actual = "VOID", None
            else:
                v = stats[STAT[r.market]].get(r.player_key, 0.0) if r.player_key in stats.index else 0.0
                if isinstance(v, pd.Series):
                    v = v.iloc[0]
                actual = float(v) if v == v else 0.0
                res = "WIN" if actual >= thr else "LOSS"
            e = rv.get(r.player_key) or {}
            last5 = (e.get(r.market) or [])[-5:]
            role = e.get("_role") or {}
            med = sorted(last5)[len(last5) // 2] if len(last5) == 5 else None
            out_rows.append({
                "week": week, "player_key": r.player_key, "player": r.player, "team": r.team,
                "market": r.market, "threshold": thr, "odds": r.odds, "open_odds": r.open_odds,
                "implied": implied(r.odds), "open_implied": implied(r.open_odds),
                "model_p": r.model_p, "p_cal": r.p_cal, "edge_pts": r.edge_pts, "hits4": r.hits4,
                "result": res, "actual": actual,
                "n_last": len(last5), "hits5": sum(v >= thr for v in last5) if len(last5) == 5 else None,
                "med5": med, "mean5": sum(last5) / len(last5) if last5 else None,
                "cushion": (med / thr) if med is not None else None,
                **{k: role.get(k) for k in ("snap_min", "ts_mean", "tgt_mean", "tgt_cv",
                                            "cs_mean", "car_mean", "car_cv")}})
    df = pd.DataFrame(out_rows)
    df.to_parquet(out)
    print(df.result.value_counts(dropna=False).to_dict(), "weeks", sorted(df.week.unique()))


if __name__ == "__main__":
    main(*sys.argv[1:3])
