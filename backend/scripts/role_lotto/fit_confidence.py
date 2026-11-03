import numpy as np, pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss
df=pd.read_parquet('frame.parquet'); df=df[df.hits5>=3].copy()
df["lcush"]=np.log(df.cushion.clip(.05,10)); df["rec"]=(df.market=="receptions").astype(int)
F=["hits5","lcush","rec","tgt_mean","snap_min","ts_mean","tgt_cv"]
tr=df[df.season<=2025]; te=df[df.season==2026].copy()
m=LogisticRegression(max_iter=3000,C=1.0).fit(tr[F],tr.hit)
te["p"]=m.predict_proba(te[F])[:,1]
print("2026 holdout n=%d logloss %.4f"%(len(te),log_loss(te.hit,te.p)))
for a,b in [(.5,.6),(.6,.7),(.7,.8),(.8,.9),(.9,1)]:
    z=te[(te.p>=a)&(te.p<b)]; print(f"  p {a}-{b}: n={len(z)} said {100*z.p.mean():.1f} hit {100*z.hit.mean():.1f}")
role=(te.snap_min>=.5)&(te.ts_mean>=.12)
for lab,z in (("role floor",te[role]),("no role",te[~role])):
    zz=z[(z.p>=.55)&(z.p<.8)]; print(f"{lab}: n={len(zz)} said {100*zz.p.mean():.1f} hit {100*zz.hit.mean():.1f}")
print("intercept",round(m.intercept_[0],4)); print({f:round(c,4) for f,c in zip(F,m.coef_[0])})
