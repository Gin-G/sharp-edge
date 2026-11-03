import numpy as np, pandas as pd
from sklearn.linear_model import LogisticRegression
df=pd.read_parquet('frame.parquet'); df=df[df.hits5>=3].copy()
df["lcush"]=np.log(df.cushion.clip(.05,10)); df["rec"]=(df.market=="receptions").astype(int)
F=["hits5","lcush","rec","tgt_mean","snap_min","ts_mean","tgt_cv"]
m=LogisticRegression(max_iter=3000).fit(df[(df.season<=2025)][F],df[df.season<=2025].hit)
pr=pd.read_csv('prices.csv'); pr["rung"]=(pr.line+0.5).round().astype(int)
x=df[df.season==2026].copy(); x["p"]=m.predict_proba(x[F])[:,1]
j=pr.merge(x,left_on=["week","player_key","market","rung"],right_on=["week","key","market","rung"])
imp=lambda o: np.where(o>0,100/(o+100),-o/(-o+100))
j["implied"]=imp(j.odds); j["fair"]=j.implied*0.95
j["edge"]=j.p-j.fair
print("weeks",sorted(j.week.unique()),"rungs matched",len(j))
role=(j.snap_min>=.5)&(j.ts_mean>=.12)
dec=lambda o: np.where(o>0,1+o/100,1+100/-o)
for lab,z in [("all",j),("edge>=+5",j[j.edge>=.05]),("edge>=+5 & role",j[(j.edge>=.05)&role]),
              ("edge>=+5 & -250..+150 & role",j[(j.edge>=.05)&role&(j.odds>=-250)&(j.odds<=150)]),
              ("edge<0",j[j.edge<0]),("no role",j[~role])]:
    roi=(np.where(z.hit==1,dec(z.odds)-1,-1)).mean()*100 if len(z) else float('nan')
    print(f"{lab:32s} n={len(z):4d} model {100*z.p.mean():5.1f} implied {100*z.implied.mean():5.1f} hit {100*z.hit.mean():5.1f}  flat ROI {roi:+.1f}%  players {z.key.nunique()}")
