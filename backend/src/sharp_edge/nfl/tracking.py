"""Record NFL suggestions before kickoff, settle them after, and score them.

The MLB side learned this the hard way and the lesson transfers: a screen that
does not write down what it said cannot be evaluated, and by the time you want
the history it is too late to reconstruct — the lines are gone, the projections
have been recomputed, and the board only shows today.

Three jobs:

  ``freeze_week``    write this week's suggestions and card before kickoff
  ``settle_week``    resolve them against nflverse actuals once games are played
  ``track_record``   aggregate: hit rate overall, by market, by side, by week

**Everything here is async and called from the request path**, unlike
``tracking.py``, which runs in warm-up threads and has to hop back onto the
event loop through ``run_coroutine_threadsafe``. The NFL board is cheap enough
to build inside a request, so none of that machinery is needed, and its absence
is worth keeping.

**Why freezing matters more here than in baseball.** A batter prop that is not
frozen can be re-derived from the next morning's board and will be roughly
right. An NFL line moves all week and FanDuel pulls each market at kickoff, so
a card re-derived on Monday is made of whichever games had not started —
usually one Monday-night game. The record has to be written before the first
kickoff or it is not a record of anything.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Optional

from . import card as card_mod
from .names import norm_name

logger = logging.getLogger(__name__)

# Which nflverse column settles each market.
SETTLE_COLUMN = {
    "receiving_yards": "receiving_yards",
    "rushing_yards": "rushing_yards",
    "passing_yards": "passing_yards",
    "receptions": "receptions",
}

NFLVERSE_WEEKLY = (
    "https://github.com/nflverse/nflverse-data/releases/download/stats_player/"
    "stats_player_week_{season}.parquet"
)

_db = None


def configure(db) -> None:
    """Wire in the app's database. Called from the FastAPI lifespan."""
    global _db
    _db = db


def _require_db():
    if _db is None:
        raise RuntimeError("nfl tracking not configured (no db)")
    return _db


def _metrics(row: dict) -> str:
    """The context worth keeping for a post-mortem but not worth a column.

    Chosen for one purpose: when a pick loses, these are the fields that say
    *why the model thought what it did*, so a bad week can be diagnosed rather
    than just counted.
    """
    keep = ("model_p_raw", "fair_p", "implied_p", "over_odds", "under_odds",
            "raw_gap", "threshold", "prediction_type", "exp_games", "position",
            "kickoff", "sgm")
    return json.dumps({k: row.get(k) for k in keep if row.get(k) is not None})


def _pick_row(r: dict, season: int, week: int, source: str) -> dict:
    return {
        "season": season, "week": week,
        "player_key": r.get("key") or norm_name(r.get("player")),
        "market": r["market"],
        "player": r.get("player"),
        "player_id": r.get("player_id"),
        "position": r.get("position"),
        "team": r.get("team"),
        "event": r.get("event"),
        "kickoff": r.get("kickoff"),
        "line": r["line"],
        "side": r["side"],
        "fd_odds": r.get("odds"),
        "model_p": r.get("model_p"),
        "edge_pts": r.get("edge_pts"),
        "residual": r.get("residual"),
        "projection": r.get("projection"),
        "adjusted": r.get("adjusted"),
        "metrics": _metrics(r),
        "source": source,
    }


async def freeze_week(payload: dict, source: str = "live") -> dict:
    """Persist this week's suggestions and freeze the card.

    Idempotent and safe to call on every board request: picks upsert (revising
    an unresolved row, never a settled one) and the card insert is a no-op once
    the week has one. That is what makes it correct to wire into the screen
    endpoint rather than needing a scheduler — the board is fetched many times
    before kickoff and the first fetch is what sticks.
    """
    db = _require_db()
    season, week = payload["season"], payload["week"]

    picks = card_mod.suggestions(payload.get("props") or [])
    written = await db.upsert_nfl_picks(
        [_pick_row(r, season, week, source) for r in picks]
    )

    the_card = card_mod.build(payload.get("props") or [])
    frozen = False
    if the_card:
        summary = card_mod.summarise(the_card)
        frozen = await db.insert_nfl_card({
            "season": season, "week": week,
            "legs": json.dumps([_pick_row(r, season, week, source) for r in the_card]),
            "leg_count": summary["legs"],
            "american": summary["american"],
            "decimal_odds": summary["decimal"],
            "model_p": summary["model_p"],
        })
    return {"suggestions": len(picks), "written": written,
            "card_legs": len(the_card), "card_frozen": frozen}


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

def _load_actuals(season: int, week: int):
    """nflverse weekly stats for one week, keyed by normalised name.

    nflverse is the settlement source rather than the sportsbook because it is
    the same data the model is calibrated against — settling on one source and
    fitting on another is how a track record quietly stops meaning anything.
    It lands a day or two after the games, which is why settlement is a
    separate pass rather than part of the board.
    """
    import pandas as pd

    df = pd.read_parquet(NFLVERSE_WEEKLY.format(season=season))
    df = df[(df.week == week) & (df.season_type == "REG")]
    out = {}
    for _, r in df.iterrows():
        out[norm_name(r.get("player_display_name"))] = r
    return out


def _result_for(pick: dict, row) -> tuple[Optional[str], Optional[float]]:
    """WIN / LOSS / PUSH for one pick, and the actual value.

    A player with no row for the week did not record a stat — inactive, or
    never targeted. That is a VOID rather than a LOSS: the sportsbook voids the
    prop, so counting it as a loss would make the record worse than the bet
    actually was. It is handled by the caller, which knows the row is missing.
    """
    col = SETTLE_COLUMN.get(pick["market"])
    if col is None or row is None:
        return None, None
    actual = row.get(col)
    if actual is None or actual != actual:  # NaN
        actual = 0.0
    actual = float(actual)

    line = float(pick["line"])
    if actual == line:
        return "PUSH", actual
    over = actual > line
    won = over if pick["side"] == "OVER" else not over
    return ("WIN" if won else "LOSS"), actual


async def settle_week(season: int, week: int) -> dict:
    """Resolve every unsettled pick for one week against nflverse actuals."""
    db = _require_db()
    picks = [p for p in await db.list_nfl_picks(season=season, week=week)
             if not p.get("result")]
    if not picks:
        return {"season": season, "week": week, "settled": 0,
                "message": "nothing pending"}

    import asyncio
    try:
        actuals = await asyncio.to_thread(_load_actuals, season, week)
    except Exception as e:
        logger.warning("[nfl-track] actuals unavailable for %s wk%s: %s", season, week, e)
        return {"season": season, "week": week, "settled": 0, "error": str(e)}
    if not actuals:
        return {"season": season, "week": week, "settled": 0,
                "message": "no actuals published yet"}

    counts: dict = {"WIN": 0, "LOSS": 0, "PUSH": 0, "VOID": 0}
    for p in picks:
        row = actuals.get(p["player_key"])
        if row is None:
            result, actual = "VOID", None
        else:
            result, actual = _result_for(p, row)
            if result is None:
                continue
        await db.settle_nfl_pick(season, week, p["player_key"], p["market"],
                                 result, actual)
        counts[result] += 1

    await _settle_card(season, week)
    return {"season": season, "week": week,
            "settled": sum(counts.values()), **counts}


async def _settle_card(season: int, week: int) -> None:
    """Score the frozen card off its legs' settled results.

    A card wins only if every leg does — it is one parlay. A VOID leg drops out
    rather than killing the ticket, which is how the sportsbook treats it.
    """
    db = _require_db()
    the_card = await db.get_nfl_card(season, week)
    if not the_card or the_card.get("result"):
        return
    settled = {(p["player_key"], p["market"]): p
               for p in await db.list_nfl_picks(season=season, week=week)}

    won = graded = 0
    for leg in the_card.get("legs") or []:
        p = settled.get((leg["player_key"], leg["market"]))
        if not p or not p.get("result") or p["result"] == "VOID":
            continue
        graded += 1
        if p["result"] in ("WIN", "PUSH"):
            won += 1
    if graded == 0:
        return
    result = "WIN" if won == graded else "LOSS"
    await db.settle_nfl_card(season, week, result, won, graded)


# ---------------------------------------------------------------------------
# Track record
# ---------------------------------------------------------------------------

def _bucket(rows: list[dict]) -> dict:
    """Wins, losses and hit rate. Voids and pushes leave the denominator.

    Same convention as the batter track record: a bet that could not lose is
    not evidence the model was right.
    """
    w = sum(1 for r in rows if r.get("result") == "WIN")
    losses = sum(1 for r in rows if r.get("result") == "LOSS")
    graded = w + losses
    return {
        "picks": len(rows),
        "wins": w, "losses": losses,
        "voids": sum(1 for r in rows if r.get("result") == "VOID"),
        "pushes": sum(1 for r in rows if r.get("result") == "PUSH"),
        "pending": sum(1 for r in rows if not r.get("result")),
        "hit_rate": round(100 * w / graded, 1) if graded else None,
        # Flat-stake ROI at the price we recorded. Hit rate alone is the thing
        # that misled the baseball screen for months — 64.8% looked good until
        # the median price turned out to be -260.
        "roi": _roi(rows),
    }


def _roi(rows: list[dict]) -> Optional[float]:
    staked = profit = 0.0
    for r in rows:
        odds, res = r.get("fd_odds"), r.get("result")
        if odds is None or res not in ("WIN", "LOSS"):
            continue
        staked += 1.0
        profit += (100 / -odds if odds < 0 else odds / 100) if res == "WIN" else -1.0
    return round(100 * profit / staked, 1) if staked else None


def _group(rows: list[dict], key: str) -> list[dict]:
    out: dict = {}
    for r in rows:
        out.setdefault(r.get(key) or "—", []).append(r)
    return [{key: k, **_bucket(v)} for k, v in sorted(out.items())]


async def track_record(season: Optional[int] = None) -> dict:
    """Everything the UI needs to say whether this is working.

    Split by market and by side deliberately: the two are where the model is
    most likely to be wrong in a way an overall number would hide. The UNDER
    bar in ``card.py`` is a guess, and this is what will confirm or kill it.
    """
    db = _require_db()
    rows = await db.list_nfl_picks(season=season)
    cards = await db.list_nfl_cards(season=season)

    graded_cards = [c for c in cards if c.get("result")]
    return {
        "season": season,
        "overall": _bucket(rows),
        "by_market": _group(rows, "market"),
        "by_side": _group(rows, "side"),
        "by_week": sorted(
            ({"week": k, **_bucket(v)} for k, v in
             _by_week(rows).items()), key=lambda d: d["week"], reverse=True,
        ),
        "cards": {
            "played": len(graded_cards),
            "won": sum(1 for c in graded_cards if c["result"] == "WIN"),
            "rows": cards[:20],
        },
        "picks": rows[:400],
    }


def _by_week(rows: list[dict]) -> dict:
    out: dict = {}
    for r in rows:
        out.setdefault(r.get("week"), []).append(r)
    return out
