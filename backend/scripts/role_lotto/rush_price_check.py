"""The rushing model against week-8 FanDuel alt rushing prices."""
import sys
import numpy as np, pandas as pd
from sklearn.linear_model import LogisticRegression
frame, prices = sys.argv[1], sys.argv[2]
df=pd.read_parquet(frame); df=df[df.hits5>=3].copy()
df["lcush"]=np.log(df.cushion.clip(.05,10)); df["qb"]=(df.pos=="QB").astype(int)
F=["hits5","lcush","car_mean","snap_min","cs_mean","car_cv","qb"]
m=LogisticRegression(max_iter=3000).fit(df[df.season<=2025][F],df[df.season<=2025].hit)
x=df[df.season==2026].copy(); x["p"]=m.predict_proba(x[F])[:,1]
pr=pd.read_csv(prices); pr["rung"]=(pr.line+0.5).round().astype(int)
j=pr.merge(x,left_on=["week","player_key","rung"],right_on=["week","key","rung"])
imp=lambda o: np.where(o>0,100/(o+100),-o/(-o+100)); dec=lambda o: np.where(o>0,1+o/100,1+100/-o)
j["implied"]=imp(j.odds); j["fair"]=j.implied*0.95; j["edge"]=j.p-j.fair
role=(j.cs_mean>=.4)
print("rungs matched",len(j),"players",j.key.nunique())
for lab,z in [("all",j),("-300..+150",j[(j.odds>=-300)&(j.odds<=150)]),
              ("role",j[role]),("role & -300..+150",j[role&(j.odds>=-300)&(j.odds<=150)]),
              ("role & edge>=3 & -300..+150",j[role&(j.edge>=.03)&(j.odds>=-300)&(j.odds<=150)]),
              ("no role & -300..+150",j[~role&(j.odds>=-300)&(j.odds<=150)])]:
    roi=(np.where(z.hit==1,dec(z.odds)-1,-1)).mean()*100 if len(z) else float('nan')
    print(f"{lab:30s} n={len(z):4d} model {100*z.p.mean():5.1f} implied {100*z.implied.mean():5.1f} hit {100*z.hit.mean():5.1f} flat ROI {roi:+.1f}%")
