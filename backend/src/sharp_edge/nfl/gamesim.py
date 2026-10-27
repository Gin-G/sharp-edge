"""Game simulation: win, cover and total probabilities, and the one bet it makes.

Every game is run ``SIMS`` times twice over — once centred on our own score
projection (``gamemodel``) and once on FanDuel's own spread and total. Each run
draws a real game's miss from history: how far its final margin and total
landed from the closing spread and total. Drawing the two from the *same* game
keeps their correlation, and drawing real margins rather than a normal curve
keeps the key numbers — three and seven are where NFL games end, and a bell
curve smears them across the whole range.

**What it recommends, and why only that.** Two ideas were tested over 2016-2025
against nflverse closing lines before anything here was allowed to say "bet":

* *Our margin against the spread.* It loses. The points model goes 48.8% ATS
  on 2,572 games and gets worse as its disagreement grows (47.0% at 3+ points).
  An EPA-per-play rating built for the purpose does no better: fitted on
  2016-20 and tested on 2021-25, the closing spread absorbs it entirely
  (coefficient on the spread 1.08, MAE unchanged at 9.83). The model's spread
  and total views are therefore shown as **leans**, recorded and graded, and
  never recommended until the live record says otherwise.

* *The book's moneyline against its own spread.* This one survives. Simulate
  the game from the spread alone, compare the win probability with the
  moneyline's price — vig included, no devig — and back the side whose price is
  at least ``ML_GAP_PTS`` too long. Fitted on 2010-18, tested on 2019-25:
  330 bets, **+8.5% ROI**; at 3+ points, 106 bets at +21%. It is almost all
  underdogs (+13.5% on 258 dogs, -9.6% on 72 favourites), it swings by season
  (-23% in 2022, +63% in 2023) and a bootstrap puts an 18% chance on it being
  worth nothing. A real but noisy edge, so it ships as a recommendation with a
  record next to it, at a size the variance deserves.

The second needs no model at all, which is the point. It does not claim to know
football better than the market; it claims the market's two prices for the same
game sometimes disagree with each other.
"""

from __future__ import annotations

import random
from typing import Optional

from ..fanduel.odds import american_to_implied
from .names import TEAM_ABBR

SIMS = 20_000

# Seasons whose misses make up the draw. 2015 on, which is when the extra point
# moved back and changed how often games land on seven.
RESIDUAL_FROM = 2015

# The moneyline must be at least this many points longer than the spread says.
ML_GAP_PTS = 2.0

# A model lean is shown when our run says a side covers at least this often.
# Display only — see the module docstring for why nothing on the model side is
# recommended.
LEAN_MIN_P = 0.55


def residuals(schedule: list[dict]) -> list[tuple[float, float]]:
    """``(margin miss, total miss)`` for every completed game with closing lines."""
    out = []
    for g in schedule:
        try:
            if (g.get("season") or 0) < RESIDUAL_FROM:
                continue
            hs, as_ = g.get("home_score"), g.get("away_score")
            sp, tl = g.get("spread_line"), g.get("total_line")
            if None in (hs, as_, sp, tl):
                continue
            out.append(((hs - as_) - sp, (hs + as_) - tl))
        except TypeError:
            continue
    return out


def simulate(mean_margin: float, mean_total: float,
             misses: list[tuple[float, float]], seed: str,
             n: int = SIMS) -> list[tuple[float, float]]:
    """``n`` (home margin, total) outcomes around the given centre."""
    if not misses:
        return []
    rng = random.Random(seed)
    k = len(misses)
    out = []
    for _ in range(n):
        dm, dt = misses[rng.randrange(k)]
        out.append((mean_margin + dm, max(0.0, mean_total + dt)))
    return out


def _share(n: int, d: int) -> Optional[float]:
    return round(n / d, 4) if d else None


def outcome_probs(runs: list[tuple[float, float]],
                  home_handicap: Optional[float] = None,
                  total_line: Optional[float] = None) -> dict:
    """Win, cover and over shares. Pushes are taken out, as the book voids them."""
    if not runs:
        return {}
    win = sum(1 for m, _ in runs if m > 0)
    lose = sum(1 for m, _ in runs if m < 0)
    out = {"p_home_win": _share(win, win + lose)}
    if home_handicap is not None:
        cov = sum(1 for m, _ in runs if m + home_handicap > 0)
        miss = sum(1 for m, _ in runs if m + home_handicap < 0)
        out["p_home_cover"] = _share(cov, cov + miss)
    if total_line is not None:
        over = sum(1 for _, t in runs if t > total_line)
        under = sum(1 for _, t in runs if t < total_line)
        out["p_over"] = _share(over, over + under)
    return out


def fair_american(p: Optional[float]) -> Optional[int]:
    if p is None or not (0 < p < 1):
        return None
    return -round(100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def fd_lines(markets: dict, home: str, away: str) -> dict:
    """FanDuel's three markets for one game, keyed by side.

    ``markets`` is ``{market name: board row}``. Runners are named by full
    club name for moneyline and spread, Over/Under for the total.
    """
    def runner(m: str, team: str):
        for r in (markets.get(m) or {}).get("runners") or []:
            if TEAM_ABBR.get(r.get("name"), r.get("name")) == team:
                return r
        return None

    out: dict = {}
    h, a = runner("moneyline", home), runner("moneyline", away)
    out["home_ml"] = h.get("odds") if h else None
    out["away_ml"] = a.get("odds") if a else None
    hs, as_ = runner("spread", home), runner("spread", away)
    out["home_spread"] = hs.get("handicap") if hs else None
    out["home_spread_odds"] = hs.get("odds") if hs else None
    out["away_spread_odds"] = as_.get("odds") if as_ else None
    for r in (markets.get("total") or {}).get("runners") or []:
        name = (r.get("name") or "").lower()
        if name.startswith("over"):
            out["total"] = r.get("handicap")
            out["over_odds"] = r.get("odds")
        elif name.startswith("under"):
            out["under_odds"] = r.get("odds")
    return out


def _ev(p: float, american: int) -> float:
    dec = 1 + (100 / -american if american < 0 else american / 100)
    return round(p * (dec - 1) - (1 - p), 4)


def price_game(home: str, away: str, markets: dict, pred: Optional[dict],
               misses: list[tuple[float, float]], seed: str) -> dict:
    """Both runs for one game, the moneyline picks, and the model's leans."""
    fd = fd_lines(markets, home, away)
    hh, tl = fd.get("home_spread"), fd.get("total")
    out: dict = {"home_team": home, "away_team": away, "fd": fd,
                 "sims": SIMS, "market": None, "model": None,
                 "picks": [], "leans": []}

    # The market's own run: centred on FanDuel's spread and total.
    if hh is not None:
        runs = simulate(-hh, tl if tl is not None else 44.0, misses, seed + ":mkt")
        mk = outcome_probs(runs, hh, tl)
        out["market"] = mk
        p_home = mk.get("p_home_win")
        for side, team, odds, p in (
            ("home", home, fd.get("home_ml"), p_home),
            ("away", away, fd.get("away_ml"), None if p_home is None else 1 - p_home),
        ):
            if odds is None or p is None:
                continue
            gap = 100 * (p - american_to_implied(odds))
            if gap >= ML_GAP_PTS:
                out["picks"].append({
                    "market": "moneyline", "side": side, "team": team,
                    "odds": odds, "p": round(p, 4), "edge_pts": round(gap, 1),
                    "ev": _ev(p, odds), "fair": fair_american(p),
                })

    # Our run: centred on the game model's projection.
    if pred and pred.get("exp_margin") is not None and pred.get("exp_total") is not None:
        runs = simulate(pred["exp_margin"], pred["exp_total"], misses, seed + ":model")
        md = outcome_probs(runs, hh, tl)
        md.update({
            "exp_home_points": pred.get("exp_home_points"),
            "exp_away_points": pred.get("exp_away_points"),
            "exp_margin": pred.get("exp_margin"),
            "exp_total": pred.get("exp_total"),
            "fair_home_ml": fair_american(md.get("p_home_win")),
            "fair_away_ml": fair_american(
                None if md.get("p_home_win") is None else 1 - md["p_home_win"]),
            "thin": pred.get("thin"),
        })
        out["model"] = md
        pc = md.get("p_home_cover")
        if pc is not None and hh is not None:
            if pc >= LEAN_MIN_P:
                out["leans"].append({"market": "spread", "side": "home", "team": home,
                                     "line": hh, "odds": fd.get("home_spread_odds"),
                                     "p": pc})
            elif 1 - pc >= LEAN_MIN_P:
                out["leans"].append({"market": "spread", "side": "away", "team": away,
                                     "line": -hh, "odds": fd.get("away_spread_odds"),
                                     "p": round(1 - pc, 4)})
        po = md.get("p_over")
        if po is not None and tl is not None:
            if po >= LEAN_MIN_P:
                out["leans"].append({"market": "total", "side": "over", "line": tl,
                                     "odds": fd.get("over_odds"), "p": po})
            elif 1 - po >= LEAN_MIN_P:
                out["leans"].append({"market": "total", "side": "under", "line": tl,
                                     "odds": fd.get("under_odds"), "p": round(1 - po, 4)})
    return out
