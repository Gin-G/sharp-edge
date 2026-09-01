#!/usr/bin/env python
"""Fit the NFL prop models against nflverse history and print the coefficients.

The MLB side has ``calibrate_model.py``; this is the same idea for football,
and it is offline by design — it downloads a decade of weekly player stats,
fits, prints, and exits. Paste the output into ``sharp_edge.nfl.model``.

Two models come out of it:

``prop_over``  P(a player clears line L) for receiving yards, receptions,
               rushing yards and passing yards. One logistic per market,
               taking the line itself as a feature, so a single fit covers
               every alt line the book posts rather than needing one model
               per rung of the ladder.

``anytime_td`` P(a player scores). A plain Poisson on the trailing TD rate is
               not good enough — measured over 39,450 player-weeks it reads
               54% on the top bucket and delivers 39.6% — because a TD rate
               is noisy and regresses hard. The logistic does the shrinking.

**On what stands in for the line.** There is no public archive of historical
NFL prop lines, so the models are trained with the player's trailing-4 average
where the live screen will feed a projection. That is the honest substitute:
it is the same quantity (an estimate of this week's production formed before
the game), just a weaker one. A weaker input at fit time makes the shipped
model *under*confident when handed a better one, which is the safe direction —
the same argument ``pricing.py`` makes for the batter model.

Usage:
    python scripts/calibrate_nfl.py                 # fit everything
    python scripts/calibrate_nfl.py --market receptions
    python scripts/calibrate_nfl.py --cache ~/.sharp-edge/nfl   # reuse downloads
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from calibrate_model import fit_logistic, predict, auc, log_loss  # noqa: E402

NFLVERSE_WEEKLY = (
    "https://github.com/nflverse/nflverse-data/releases/download/stats_player/"
    "stats_player_week_{season}.parquet"
)

# Markets, and the alt-line ladders FanDuel actually posts for each. The lines
# are part of the training set, not just the eval: the model learns how the
# outcome distribution tightens or spreads as the bar moves.
MARKETS = {
    "receiving_yards": {
        "stat": "receiving_yards", "volume": "targets",
        "positions": ["WR", "TE", "RB"],
        "lines": [9.5, 14.5, 19.5, 24.5, 29.5, 34.5, 39.5, 49.5, 59.5, 69.5, 79.5],
    },
    "receptions": {
        "stat": "receptions", "volume": "targets",
        "positions": ["WR", "TE", "RB"],
        "lines": [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5],
    },
    "rushing_yards": {
        "stat": "rushing_yards", "volume": "carries",
        "positions": ["RB", "QB", "WR"],
        "lines": [9.5, 14.5, 19.5, 29.5, 39.5, 49.5, 59.5, 69.5, 79.5, 99.5],
    },
    "passing_yards": {
        "stat": "passing_yards", "volume": "attempts",
        "positions": ["QB"],
        "lines": [149.5, 174.5, 199.5, 224.5, 249.5, 274.5, 299.5, 324.5],
    },
}

FEATURES = ["gap", "gap_std", "logvol", "logline"]
TD_FEATURES = ["log_td_rate", "log_touches", "log_tgt", "is_rb", "is_te"]

TRAIN = range(2015, 2023)
TEST = range(2023, 2026)
MIN_GAMES_PRIOR = 3

# A Platt correction — refitting the model's own log-odds on a season it never
# saw — is available behind --platt but is OFF, because it was tried and it
# does not pay for itself. Holding 2022 out of the fit to have something to
# correct on costs more calibration than the correction returns:
#
#   market            worst bucket, trained thru 2022   held out + Platt
#   receiving_yards              1.6pts                  2.3pts (correction discarded)
#   receptions                  1.9pts                  3.7pts (correction discarded)
#   rushing_yards               1.9pts                  1.3pts (a=1.011 — identity)
#   passing_yards               4.8pts                  4.4pts
#
# Two of four were left worse, the one clear win is a correction of 1%, and
# passing yards stays bad either way. So the extra season of training data is
# worth more than the recalibration, and passing yards is handled by keeping it
# off the card rather than by pretending a scaling fixed it.
PLATT_TRIGGER_PTS = 2.0


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def load_weekly(seasons, cache: Path | None) -> pd.DataFrame:
    frames = []
    for s in seasons:
        cached = cache / f"week_{s}.parquet" if cache else None
        if cached and cached.exists():
            frames.append(pd.read_parquet(cached))
            continue
        print(f"  downloading {s}…", flush=True)
        df = pd.read_parquet(NFLVERSE_WEEKLY.format(season=s))
        if cached:
            cache.mkdir(parents=True, exist_ok=True)
            df.to_parquet(cached)
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


STATS = ["receiving_yards", "receptions", "targets", "rushing_yards", "carries",
         "passing_yards", "attempts", "rushing_tds", "receiving_tds"]


def prepare(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw[raw.season_type == "REG"].copy()
    df = df.sort_values(["player_id", "season", "week"]).reset_index(drop=True)
    for c in STATS:
        df[c] = df[c].fillna(0.0) if c in df.columns else 0.0
    df["tds"] = df.rushing_tds + df.receiving_tds
    df["touches"] = df.carries + df.receptions
    df["scored"] = (df.tds > 0).astype(int)

    # Trailing form, strictly shifted so nothing from the week being predicted
    # leaks in. Grouped by (player, season): a new season does not inherit last
    # year's usage, which is also why week 1 needs a projection rather than
    # this — see sharp_edge.nfl.screen.
    g = df.groupby(["player_id", "season"], sort=False)
    for c in STATS + ["tds", "touches"]:
        df[f"t4_{c}"] = g[c].transform(lambda s: s.shift(1).rolling(4, min_periods=1).mean())
        df[f"std_{c}"] = g[c].transform(lambda s: s.shift(1).expanding().mean())
    df["t8_tds"] = g.tds.transform(lambda s: s.shift(1).rolling(8, min_periods=3).mean())
    df["games_prior"] = g.cumcount()
    return df


# ---------------------------------------------------------------------------
# Feature construction
# ---------------------------------------------------------------------------

def prop_frame(df: pd.DataFrame, market: str) -> pd.DataFrame:
    spec = MARKETS[market]
    stat, vol = spec["stat"], spec["volume"]
    d = df[df.position.isin(spec["positions"]) & (df.games_prior >= MIN_GAMES_PRIOR)]
    # A market only exists for a player the offence actually uses; one carry a
    # month is not a posted line.
    d = d[d[f"t4_{vol}"] > 0.5]

    frames = []
    for line in spec["lines"]:
        frames.append(pd.DataFrame({
            "season": d.season.values, "week": d.week.values,
            "player_id": d.player_id.values, "position": d.position.values,
            "line": line,
            "est": d[f"t4_{stat}"].values, "est_std": d[f"std_{stat}"].values,
            "vol": d[f"t4_{vol}"].values,
            "y": (d[stat].values > line).astype(float),
        }))
    long = pd.concat(frames, ignore_index=True)
    return add_prop_features(long)


def add_prop_features(long: pd.DataFrame) -> pd.DataFrame:
    """Shared with the live screen — see ``sharp_edge.nfl.model.features``.

    ``gap`` is the whole model: how far the estimate sits above the line, on a
    log scale so the same coefficient works at o9.5 and o79.5. Rushing and
    receiving yards go negative on a bad day, so clip before log1p — log1p(-1)
    is -inf and anything below it is NaN, which silently poisons the fit.
    """
    out = long.copy()
    out["gap"] = np.log1p(out.est.clip(lower=0)) - np.log1p(out.line)
    out["gap_std"] = np.log1p(out.est_std.clip(lower=0)) - np.log1p(out.line)
    out["logvol"] = np.log1p(out.vol.clip(lower=0))
    out["logline"] = np.log1p(out.line)
    return out


def td_frame(df: pd.DataFrame) -> pd.DataFrame:
    d = df[df.t8_tds.notna() & df.position.isin(["RB", "WR", "TE"])
           & (df.games_prior >= MIN_GAMES_PRIOR)].copy()
    out = pd.DataFrame({
        "season": d.season.values, "week": d.week.values,
        "position": d.position.values,
        "td_rate": d.t8_tds.values, "touches": d.t4_touches.values,
        "tgt": d.t4_targets.values,
        "y": d.scored.values.astype(float),
    })
    return add_td_features(out)


def add_td_features(d: pd.DataFrame) -> pd.DataFrame:
    out = d.copy()
    out["log_td_rate"] = np.log1p(out.td_rate.clip(lower=0))
    out["log_touches"] = np.log1p(out.touches.clip(lower=0))
    out["log_tgt"] = np.log1p(out.tgt.clip(lower=0))
    out["is_rb"] = (out.position == "RB").astype(float)
    out["is_te"] = (out.position == "TE").astype(float)
    return out


# ---------------------------------------------------------------------------
# Fitting / reporting
# ---------------------------------------------------------------------------

def design(d: pd.DataFrame, features: list[str]) -> np.ndarray:
    return np.column_stack([np.ones(len(d))] + [d[c].astype(float).values for c in features])


def fit_platt(p: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """One-dimensional logistic of the outcome on the model's own log-odds.

    Returns (a, b) for ``sigmoid(a * logit(p) + b)``. a < 1 shrinks the model
    toward its base rate, which is what an overconfident fit needs. Fit on a
    season the model never saw, so it is a correction rather than hindsight.
    """
    z = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
    X = np.column_stack([np.ones(len(z)), z])
    beta = fit_logistic(X, y, l2=1e-6)
    return float(beta[1]), float(beta[0])


def apply_platt(p: np.ndarray, a: float, b: float) -> np.ndarray:
    z = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
    return 1 / (1 + np.exp(-np.clip(a * z + b, -30, 30)))


def calibration(te: pd.DataFrame, p: np.ndarray) -> tuple[pd.DataFrame, float]:
    edges = [0, .3, .5, .6, .7, .75, .8, .85, .9, 1.0]
    cal = (te.assign(p=p, bucket=pd.cut(p, edges))
             .groupby("bucket", observed=True)
             .agg(n=("y", "size"), pred=("p", "mean"), actual=("y", "mean")))
    # Ignore buckets too thin to say anything — a two-row bucket that misses by
    # 39 points is noise, not miscalibration.
    solid = cal[cal.n >= 100]
    worst = float((100 * (solid.actual - solid.pred)).abs().max()) if len(solid) else 0.0
    return cal, worst


def report(name: str, te: pd.DataFrame, p: np.ndarray, features: list[str],
           beta: np.ndarray, va: pd.DataFrame | None = None,
           p_va: np.ndarray | None = None) -> dict:
    y = te.y.values
    a = auc(y, p)
    ll = log_loss(y, p)
    ll_base = log_loss(y, np.full(len(y), y.mean()))
    print(f"\n=== {name} ===")
    print(f"  test n={len(te):,}  base rate {100*y.mean():.1f}%")
    print(f"  AUC {a:.4f}   log-loss {ll:.4f} vs {ll_base:.4f} for the base rate")
    print("  intercept %.6f" % beta[0])
    for f, b in zip(features, beta[1:]):
        print(f"    {f:<14} {b:>11.6f}")

    cal, worst = calibration(te, p)
    print("  calibration (predicted vs actual):")
    for b_, r in cal.iterrows():
        thin = "" if r.n >= 100 else "   (thin)"
        print(f"    {str(b_):13s} n={int(r.n):7,}  pred {100*r.pred:5.1f}%  "
              f"actual {100*r.actual:5.1f}%  {100*(r.actual-r.pred):+5.1f}pts{thin}")
    print(f"  worst bucket miss (n>=100): {worst:.1f}pts")

    out = {"auc": float(a), "intercept": float(beta[0]),
           "coef": {f: float(b) for f, b in zip(features, beta[1:])},
           "worst_pts": worst}

    if worst > PLATT_TRIGGER_PTS and va is not None and p_va is not None and len(va) > 500:
        pa, pb = fit_platt(p_va, va.y.values)
        pc = apply_platt(p, pa, pb)
        cal_c, worst_c = calibration(te, pc)
        print(f"  -> miss exceeds {PLATT_TRIGGER_PTS}pts; Platt fit on {VALIDATE}: "
              f"a={pa:.4f} b={pb:.4f}")
        for b_, r in cal_c.iterrows():
            if r.n < 100:
                continue
            print(f"     corrected {str(b_):13s} pred {100*r.pred:5.1f}%  "
                  f"actual {100*r.actual:5.1f}%  {100*(r.actual-r.pred):+5.1f}pts")
        print(f"     worst after correction: {worst_c:.1f}pts "
              f"({'kept' if worst_c < worst else 'DISCARDED — no better'})")
        if worst_c < worst:
            out["platt"] = {"a": pa, "b": pb, "worst_pts": worst_c}
    return out


def emit(results: dict) -> None:
    """Print the block to paste into sharp_edge/nfl/model.py."""
    print("\n" + "=" * 72)
    print("# Paste into sharp_edge/nfl/model.py")
    print("=" * 72)
    print("_PROP_COEF = {")
    for market, r in results.items():
        if market == "anytime_td":
            continue
        print(f'    "{market}": {{')
        print(f'        "intercept": {r["intercept"]:.6f},')
        for f, b in r["coef"].items():
            print(f'        "{f}": {b:.6f},')
        if "platt" in r:
            print(f'        "platt_a": {r["platt"]["a"]:.6f},')
            print(f'        "platt_b": {r["platt"]["b"]:.6f},')
        print(f'    }},  # AUC {r["auc"]:.4f}, worst bucket '
              f'{r.get("platt", r)["worst_pts"]:.1f}pts')
    print("}")
    if "anytime_td" in results:
        r = results["anytime_td"]
        print("\n_TD_COEF = {")
        print(f'    "intercept": {r["intercept"]:.6f},')
        for f, b in r["coef"].items():
            print(f'    "{f}": {b:.6f},')
        print(f'}}  # AUC {r["auc"]:.4f}')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market", action="append",
                    help="fit only this market (repeatable); default is all + TDs")
    ap.add_argument("--cache", type=Path, default=Path.home() / ".sharp-edge" / "nfl",
                    help="directory for downloaded nflverse parquet")
    ap.add_argument("--from-season", type=int, default=2015)
    ap.add_argument("--test-from", type=int, default=2023)
    ap.add_argument("--platt", action="store_true",
                    help="hold the season before the test block out of the fit "
                         "and use it for a Platt correction (measured worse — "
                         "see PLATT_TRIGGER_PTS)")
    args = ap.parse_args()

    seasons = list(range(args.from_season, 2026))
    # With --platt the season before the test block is held out of the fit, so
    # a correction is never fit on the rows it is scored against. Without it
    # that season trains like any other, which measures better.
    train = range(args.from_season, args.test_from - (1 if args.platt else 0))
    validate = args.test_from - 1 if args.platt else None
    test = range(args.test_from, 2026)

    print(f"Loading nflverse weekly stats {seasons[0]}-{seasons[-1]}…")
    df = prepare(load_weekly(seasons, args.cache))
    print(f"  {len(df):,} player-weeks")
    print(f"  train {train.start}-{train.stop-1}"
          + (f", validate {validate}" if validate else "")
          + f", test {test.start}-{test.stop-1}")

    wanted = args.market or list(MARKETS) + ["anytime_td"]
    results: dict = {}

    for market in wanted:
        if market == "anytime_td":
            long = td_frame(df)
            features = TD_FEATURES
        else:
            long = prop_frame(df, market)
            features = FEATURES
        tr = long[long.season.isin(train)]
        va = long[long.season == validate] if validate else None
        te = long[long.season.isin(test)]
        if len(tr) < 500:
            print(f"\n{market}: only {len(tr)} training rows, skipping")
            continue
        beta = fit_logistic(design(tr, features), tr.y.values)
        p = predict(design(te, features), beta)
        p_va = predict(design(va, features), beta) if va is not None and len(va) else None
        results[market] = report(market, te, p, features, beta, va, p_va)

    emit(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
