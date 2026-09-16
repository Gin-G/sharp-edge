#!/usr/bin/env python
"""Do recent vs-hand splits predict a hit, beyond the career split?

Builds rolling 7- and 30-day batting averages against the handedness of the
day's starter, from the backtest boards themselves — each board row records
batter, date, opposing hand, hits and plate appearances, which is a near
complete daily log (341 rows a day against ~270 daily starters).

Answer: they carry signal, and it is far weaker than the career split, and
filtering on them makes the card worse. See EXPERIMENTS.md.

Usage:
    python scripts/hand_split_test.py       # writes /tmp/hand_splits.parquet
"""
import pandas as pd, numpy as np, glob, collections, sys
sys.path.insert(0,"src"); sys.path.insert(0,"scripts")

fs=sorted(glob.glob('/home/gin-g/.sharp-edge/backtest/batters/board_*.parquet'))
df=pd.concat([pd.read_parquet(f) for f in fs],ignore_index=True)
df["pick_date"]=pd.to_datetime(df.pick_date)
df=df[df.p_hand.isin(["L","R"])].copy()

# A batter's daily log: what he did, against which hand. Only rows where he
# actually batted count as a game.
log=df[df.pa_actual.fillna(0)>0][["batter_id","pick_date","p_hand","hits_actual","pa_actual"]]
log=log.sort_values("pick_date")
print(f"daily batting log: {len(log):,} player-games, "
      f"{log.batter_id.nunique()} batters, {log.pick_date.nunique()} dates")

by=collections.defaultdict(list)          # (batter, hand) -> [(date, h, pa)]
for r in log.itertuples():
    by[(r.batter_id, r.p_hand)].append((r.pick_date, r.hits_actual or 0, r.pa_actual or 0))
allb=collections.defaultdict(list)        # batter -> [(date, h, pa)] any hand
for r in log.itertuples():
    allb[r.batter_id].append((r.pick_date, r.hits_actual or 0, r.pa_actual or 0))

def window(rows, upto, days):
    """(hits, pa) strictly before `upto`, within `days`."""
    lo = upto - pd.Timedelta(days=days)
    h=pa=0
    for d,hh,p in rows:
        if lo <= d < upto:
            h+=hh; pa+=p
    return h, pa

graded=df[df.result.isin(["WIN","LOSS"])].copy()
out=[]
for r in graded.itertuples():
    key=(r.batter_id, r.p_hand)
    h7,pa7   = window(by.get(key,[]), r.pick_date, 7)
    h30,pa30 = window(by.get(key,[]), r.pick_date, 30)
    a30,ap30 = window(allb.get(r.batter_id,[]), r.pick_date, 30)
    out.append((r.pick_date, r.batter_id, r.vs_hand_avg, r.vs_hand_pa,
                r.recent_ab, r.p_l3_h9, r.p_l3_k9, r.result=="WIN",
                h7,pa7,h30,pa30,a30,ap30))
c=pd.DataFrame(out, columns=["pick_date","batter_id","vs_hand_avg","vs_hand_pa",
    "recent_ab","p_l3_h9","p_l3_k9","y","h7","pa7","h30","pa30","a30","apa30"])
c.to_parquet("/tmp/claude-1000/hand_splits.parquet")
print(f"\nbuilt {len(c):,} graded rows")
print(f"  with any 7d vs-hand sample:  {100*(c.pa7>0).mean():.1f}%   median PA {c[c.pa7>0].pa7.median():.0f}")
print(f"  with >=5 PA in 7d vs hand:   {100*(c.pa7>=5).mean():.1f}%")
print(f"  with any 30d vs-hand sample: {100*(c.pa30>0).mean():.1f}%  median PA {c[c.pa30>0].pa30.median():.0f}")
print(f"  with >=15 PA in 30d vs hand: {100*(c.pa30>=15).mean():.1f}%")
