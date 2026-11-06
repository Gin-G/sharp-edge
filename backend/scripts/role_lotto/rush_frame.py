"""Rushing rung frame: prior-5 role and history -> next game cleared.

The receiving frame (build_frame.py) with carries in place of targets: role is
snap share and the player's share of his team's carries.
"""
import os, sys
import numpy as np, pandas as pd
W="https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{s}.parquet"
S="https://github.com/nflverse/nflverse-data/releases/download/snap_counts/snap_counts_{s}.parquet"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))
from sharp_edge.nfl.names import norm_name
RUNGS=[10,20,25,30,40,50,60,70,80,90,100]
out=sys.argv[1] if len(sys.argv)>1 else "rush_frame.parquet"
frames=[]
for s in (2024,2025,2026):
    ws=pd.read_parquet(W.format(s=s)); ws=ws[ws.season_type=="REG"]
    team_car=ws.groupby(["team","week"]).carries.sum().rename("team_carries").reset_index()
    sc=pd.read_parquet(S.format(s=s)); sc=sc[sc.game_type=="REG"]
    ws["key"]=ws.player_display_name.map(norm_name); sc["key"]=sc.player.map(norm_name)
    sc=sc[sc.position.isin(["RB","QB","WR"])&(sc.offense_snaps>0)]
    g=sc[["key","team","week","position","offense_pct"]].merge(
        ws[["key","week","carries","rushing_yards"]],on=["key","week"],how="left")
    for c in ("carries","rushing_yards"): g[c]=g[c].fillna(0)
    g=g.merge(team_car,on=["team","week"],how="left")
    g["car_share"]=g.carries/g.team_carries.clip(lower=1)
    g["season"]=s; frames.append(g)
g=pd.concat(frames).sort_values(["key","season","week"]).reset_index(drop=True)
rows=[]
for key,x in g.groupby("key"):
    x=x.reset_index(drop=True)
    for i in range(5,len(x)):
        prev=x.iloc[i-5:i]; cur=x.iloc[i]
        if prev.season.min()<cur.season-1 or (prev.team!=cur.team).any(): continue
        base=dict(key=key,season=cur.season,week=cur.week,pos=cur.position,
            car_min=prev.carries.min(),car_mean=prev.carries.mean(),
            car_cv=prev.carries.std()/max(prev.carries.mean(),.1),
            snap_min=prev.offense_pct.min(),cs_mean=prev.car_share.mean())
        v=prev.rushing_yards.values
        for r in RUNGS:
            rows.append({**base,"rung":r,"hits5":int((v>=r).sum()),
                "cushion":float(np.median(v))/r,"hit":int(cur.rushing_yards>=r)})
df=pd.DataFrame(rows); df.to_parquet(out); print(df.shape, df.hit.mean())
