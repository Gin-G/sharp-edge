"""Rushing confidence model: fit 2024-25, hold out 2026."""
import sys
import numpy as np, pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss, roc_auc_score
df=pd.read_parquet(sys.argv[1]); df=df[df.hits5>=3].copy()
df["lcush"]=np.log(df.cushion.clip(.05,10)); df["qb"]=(df.pos=="QB").astype(int)
base=["hits5","lcush"]; F=base+["car_mean","snap_min","cs_mean","car_cv","qb"]
tr=df[df.season<=2025]; te=df[df.season==2026].copy()
for lab,f in (("history only",base),("+role",F)):
    m=LogisticRegression(max_iter=3000).fit(tr[f],tr.hit); p=m.predict_proba(te[f])[:,1]
    print(f"{lab:13s} n={len(te)} logloss {log_loss(te.hit,p):.4f} auc {roc_auc_score(te.hit,p):.4f}")
te["p"]=p
for a,b in [(.4,.5),(.5,.6),(.6,.7),(.7,.8),(.8,.9),(.9,1)]:
    z=te[(te.p>=a)&(te.p<b)]; print(f"  p {a}-{b}: n={len(z)} said {100*z.p.mean():.1f} hit {100*z.hit.mean():.1f}")
print("by role (legs the model rates 55-80%):")
for sm in (0,.4,.5):
    for cs in (0,.3,.4,.5):
        z=te[(te.snap_min>=sm)&(te.cs_mean>=cs)&(te.p>=.55)&(te.p<.8)]
        if len(z): print(f"  snap_min>={sm} carry_share>={cs}: n={len(z)} said {100*z.p.mean():.1f} hit {100*z.hit.mean():.1f} players {z.key.nunique()}")
print("intercept",round(m.intercept_[0],4)); print({f:round(c,4) for f,c in zip(F,m.coef_[0])})
