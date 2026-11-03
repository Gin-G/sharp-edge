"""Rung-level backtest frame: prior-5 features -> next game cleared."""
import numpy as np, pandas as pd
W="https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{s}.parquet"
S="https://github.com/nflverse/nflverse-data/releases/download/snap_counts/snap_counts_{s}.parquet"
import sys; sys.path.insert(0, __import__('os').path.join(__import__('os').path.dirname(__file__), '../../src'))
from sharp_edge.nfl.names import norm_name
RUNGS={"receptions":[2,3,4,5,6],"receiving_yards":[10,20,25,30,40,50,60,70,80]}
frames=[]
for s in (2024,2025,2026):
    ws=pd.read_parquet(W.format(s=s)); ws=ws[ws.season_type=="REG"]
    sc=pd.read_parquet(S.format(s=s)); sc=sc[sc.game_type=="REG"]
    ws["key"]=ws.player_display_name.map(norm_name); sc["key"]=sc.player.map(norm_name)
    sc=sc[sc.position.isin(["WR","TE","RB"])&(sc.offense_snaps>0)]
    g=sc[["key","team","opponent","week","position","offense_pct"]].merge(
        ws[["key","week","targets","receptions","receiving_yards","carries","target_share"]],
        on=["key","week"],how="left")
    for c in ("targets","receptions","receiving_yards","carries","target_share"): g[c]=g[c].fillna(0)
    g["season"]=s; frames.append(g)
g=pd.concat(frames).sort_values(["key","season","week"]).reset_index(drop=True)
# Matchup: receiving yards a defense allowed to the position, prior 5 games,
# relative to the league that season.
d=g.groupby(["season","week","opponent","position"]).receiving_yards.sum().reset_index()
d=d.sort_values(["season","week"])
d["allowed5"]=d.groupby(["season","opponent","position"]).receiving_yards.transform(
    lambda v: v.shift(1).rolling(5,min_periods=3).mean())
lg=d.groupby(["season","position"]).receiving_yards.transform("mean")
d["def_factor"]=d.allowed5/lg
g=g.merge(d[["season","week","opponent","position","def_factor"]],on=["season","week","opponent","position"],how="left")
rows=[]
for key,x in g.groupby("key"):
    x=x.reset_index(drop=True)
    for i in range(5,len(x)):
        prev=x.iloc[i-5:i]; cur=x.iloc[i]
        if prev.season.min()<cur.season-1: continue
        if (prev.team!=cur.team).any(): continue          # same team only
        base=dict(key=key,season=cur.season,week=cur.week,pos=cur.position,
            tgt_min=prev.targets.min(),tgt_mean=prev.targets.mean(),
            tgt_cv=prev.targets.std()/max(prev.targets.mean(),.1),
            snap_min=prev.offense_pct.min(),snap_mean=prev.offense_pct.mean(),
            ts_mean=prev.target_share.mean(),def_factor=cur.def_factor)
        for m,rs in RUNGS.items():
            v=prev[m].values; nxt=cur[m]
            for r in rs:
                rows.append({**base,"market":m,"rung":r,"hits5":int((v>=r).sum()),
                    "cushion":float(np.median(v))/r,"floor":float(v.min())/r,"hit":int(nxt>=r)})
df=pd.DataFrame(rows); df.to_parquet("frame.parquet"); print(df.shape, df.hit.mean())
