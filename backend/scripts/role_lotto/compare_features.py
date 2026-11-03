import numpy as np, pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss, roc_auc_score
df=pd.read_parquet('frame.parquet')
df=df[df.hits5>=3].copy()
df["lcush"]=np.log(df.cushion.clip(.05,10)); df["lfloor"]=np.log(df.floor.clip(.02,10))
df["rec"]=(df.market=="receptions").astype(int)
df["def"]=np.log(df.def_factor.fillna(1).clip(.3,3))
df["lowts"]=(df.ts_mean<.12).astype(int)
tr=df[df.season==2024]; te=df[df.season>=2025].copy()
base=["hits5","lcush","lfloor","rec"]
opp=["tgt_mean","tgt_min","snap_min","ts_mean","tgt_cv","lowts"]
sets={"streak+cushion":base,"+opportunity":base+opp,"+matchup":base+["def"],"+both":base+opp+["def"]}
fits={}
for k,f in sets.items():
    m=LogisticRegression(max_iter=3000).fit(tr[f],tr.hit); p=m.predict_proba(te[f])[:,1]; fits[k]=p
    print(f"{k:16s} test logloss {log_loss(te.hit,p):.4f}  auc {roc_auc_score(te.hit,p):.4f}")
    if k=="+both": print("   coefs", dict(zip(f,np.round(m.coef_[0],3))))
te["p"]=fits["+both"]; te["p0"]=fits["streak+cushion"]
print("\ncalibration (full model), test:")
for a,b in [(.5,.6),(.6,.7),(.7,.8),(.8,.9),(.9,1)]:
    z=te[(te.p>=a)&(te.p<b)]; print(f"  p {a}-{b}: n={len(z)} said {100*z.p.mean():.1f} hit {100*z.hit.mean():.1f}")
z=te[(te.p0>=.6)&(te.p0<.75)].copy()
z["q"]=pd.qcut(z.p,4,labels=False)
print("\nlegs the streak+cushion model rates 60-75%, split by the full model:")
for q,w in z.groupby("q"):
    print(f"  quartile {q+1}: n={len(w)} baseline {100*w.p0.mean():.1f} full {100*w.p.mean():.1f} actual {100*w.hit.mean():.1f}  snap_min {w.snap_min.mean():.2f} tgt_mean {w.tgt_mean.mean():.1f} def {w.def_factor.mean():.2f}")
