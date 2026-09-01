"""The week's NFL board: projections against posted lines, priced and ranked.

One pass per week, cached, warmed in the background — the same shape as
``batters.py``, but a week's slate rather than a day's, so the cache lives
longer and the refresh is cheaper.

What it does, in order:

  1. Resolve the season and week from the NFL-API schedule.
  2. Pull that week's projections and FanDuel's posted lines.
  3. Rescale each market's projections onto the market's scale (this is the
     load-bearing step — see ``model.calibrate_to_market``).
  4. Price both sides of every prop and every anytime-TD quote.
  5. Rank, and flag the ones that clear the owner's divergence thresholds.

**The thresholds are the owner's rule, kept as stated but measured against the
residual rather than the raw gap.** "Ten yards clear of the line" fires on 45%
of the board when read raw, almost all of it UNDER on good players, because the
projections are shrunk (see ``model``). Against the residual the same number
fires on about a quarter of the board and splits roughly evenly between over
and under, which is what a disagreement with the market should look like. Both
numbers are on every row so the difference is visible rather than asserted.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import httpx

from ..fanduel.odds import american_to_implied
from . import model, odds as nfl_odds, projections as nfl_proj

logger = logging.getLogger(__name__)

# The owner's rule, in the stat's own units: how far the projection has to sit
# from the line before the row is called a bet. Applied to the market-adjusted
# residual, not the raw difference.
THRESHOLDS = {
    "receiving_yards": 10.0,
    "rushing_yards": 10.0,
    "passing_yards": 10.0,
    "receptions": 2.0,
}

# Which projected component feeds each market, and which projected component
# (if any) gives its volume driver directly. Where there is none — rushing and
# passing carry no carries/attempts projection — ``model.derive_volume`` infers
# it from the estimate at league-average efficiency.
_MARKET_INPUTS = {
    "receiving_yards": ("receiving_yards", "receptions"),
    "receptions": ("receptions", "receptions"),
    "rushing_yards": ("rushing_yards", None),
    "passing_yards": ("passing_yards", None),
}

# An anytime-TD quote is only interesting where the model has something to say;
# below this the read is a base rate rather than an opinion.
TD_MIN_PROB = 0.08

# League catch rate, for turning projected receptions back into the targets the
# TD model was trained on. Stable around this for a decade.
CATCH_RATE = 0.65


@dataclass
class NFLBoard:
    season: int
    week: int
    props: list[dict] = field(default_factory=list)
    tds: list[dict] = field(default_factory=list)
    games: list[dict] = field(default_factory=list)
    fits: dict = field(default_factory=dict)
    prob_fits: dict = field(default_factory=dict)
    preseason: bool = False
    odds_age: Optional[int] = None
    odds_error: Optional[str] = None
    unmatched: list[str] = field(default_factory=list)
    built_at: float = 0.0


async def build_board(today: Optional[date] = None, state: str = "CO",
                      force: bool = False) -> NFLBoard:
    today = today or date.today()
    async with httpx.AsyncClient(timeout=30.0) as client:
        wk = await nfl_proj.current_week(today, client=client)
        proj = await nfl_proj.fetch_projections(wk.season, wk.week, client=client)

    season, week = wk.season, wk.week
    got = await nfl_odds.cached_board(wk.window(), state=state, force=force)
    fd = got["board"]

    board = NFLBoard(season=season, week=week, preseason=proj.preseason,
                     odds_age=got["age_seconds"], odds_error=got["error"],
                     built_at=time.time())
    if fd is None:
        return board

    by_key = proj.by_key()
    board.games = fd.games

    # How much of any disagreement with the market to keep. Set once for the
    # whole board: it is a statement about the projections, not about a market.
    shrink = (model.SHRINK_PRESEASON if proj.preseason else model.SHRINK_INSEASON)

    matched: set[str] = set()
    for market in model.MARKETS:
        component, volume_key = _MARKET_INPUTS[market]
        lines = fd.by_market(market)

        # Rescale on this week's own board: every player who has both a posted
        # line and a projection contributes one point to the fit.
        pairs = [(ln.line, by_key[k][component])
                 for k, ln in lines.items()
                 if k in by_key and by_key[k].get(component) is not None]
        fit = model.calibrate_to_market(pairs)
        board.fits[market] = (
            {"slope": round(fit[0], 4), "intercept": round(fit[1], 2), "n": len(pairs)}
            if fit else {"slope": None, "intercept": None, "n": len(pairs)}
        )

        # Pass one: the model's own read on every row in this market.
        raw_rows = []
        for key, ln in lines.items():
            p = by_key.get(key)
            if p is None or p.get(component) is None:
                continue
            matched.add(key)
            raw = float(p[component])
            adjusted = model.adjusted_projection(ln.line, raw, fit)
            volume = model.derive_volume(
                market, adjusted, p.get(volume_key) if volume_key else None
            )
            p_over = model.prop_probability(
                market, adjusted, ln.line, est_season=adjusted, volume=volume
            )
            fair_over, _ = model.devig_two_way(ln.over, ln.under)
            raw_rows.append((key, ln, p, raw, adjusted, volume, p_over, fair_over))

        # Pass two: anchor the model's level to the market's, then price the
        # disagreement that survives. Without this the model prices its own
        # answer to a different question — see model.probability_offset.
        offset = model.probability_offset(
            [(r[6], r[7]) for r in raw_rows if r[7] is not None]
        )
        board.prob_fits[market] = {
            "offset": round(offset, 4) if offset is not None else None,
            "shrink": shrink,
            "n": sum(1 for r in raw_rows if r[7] is not None),
        }

        for key, ln, p, raw, adjusted, volume, p_raw, fair in raw_rows:
            p_over = model.anchor_probability(p_raw, offset, fair, shrink)
            priced = model.price_side(p_over, ln.over, ln.under)
            residual = model.market_residual(ln.line, raw, fit)
            threshold = THRESHOLDS[market]
            board.props.append({
                "market": market,
                "player": ln.player,
                "key": key,
                "player_id": p.get("player_id"),
                "position": p.get("position"),
                "team": p.get("team"),
                "event": ln.event,
                "fd_event_id": ln.event_id,
                "kickoff": ln.kickoff,
                "line": ln.line,
                "projection": round(raw, 1),
                "adjusted": round(adjusted, 1),
                "raw_gap": round(raw - ln.line, 1),
                "residual": round(residual, 1),
                "model_p_raw": round(p_raw, 4),
                # The owner's rule, on the residual. `raw_signal` is the same
                # rule on the unadjusted gap, kept so the two can be compared
                # on live results rather than on the argument in model.py.
                "signal": _signal(residual, threshold),
                "raw_signal": _signal(raw - ln.line, threshold),
                "threshold": threshold,
                "bettable": market in model.BETTABLE,
                "prediction_type": p.get("prediction_type"),
                "exp_games": p.get("exp_games"),
                "fd_market_id": ln.market_id,
                "over_selection_id": ln.over_selection,
                "under_selection_id": ln.under_selection,
                "sgm": ln.sgm,
                **priced,
            })

    # Anytime TD. The projections carry expected touchdowns directly, so the
    # rate feature is the projected total rather than a trailing one.
    #
    # Priced per event rather than per player, because the market is a field:
    # the quoted probabilities across a game sum to about 400%, and an edge
    # measured against a raw one-sided price is mostly margin. See
    # model.devig_field.
    td_rows: list[tuple[dict, dict, float]] = []
    for q in fd.tds:
        p = by_key.get(q["key"])
        if p is None:
            continue
        matched.add(q["key"])
        td_rate = sum(v for v in (p.get("rushing_tds"), p.get("receiving_tds"))
                      if v is not None) or None
        # Touches is carries plus receptions, and the projections carry no
        # carry count — so infer it from projected rushing yards, the same way
        # the prop models do. Passing receptions alone was a real bug: it read
        # a feature-back as a player with one touch a game and put Derrick
        # Henry 40 points below the market.
        receptions = p.get("receptions") or 0.0
        carries = model.derive_volume("rushing_yards", p.get("rushing_yards") or 0.0)
        prob = model.td_probability(
            td_rate=td_rate,
            touches=receptions + (carries or 0.0),
            # Targets, not catches. The model was trained on targets, and a
            # projection only gives receptions; the league catch rate converts.
            targets=receptions / CATCH_RATE,
            position=p.get("position"),
        )
        td_rows.append((q, p, prob))

    by_event: dict[str, list[int]] = {}
    for i, (q, _p, _prob) in enumerate(td_rows):
        by_event.setdefault(q["fd_event_id"], []).append(i)

    anchored: dict[int, float] = {}
    for idxs in by_event.values():
        quoted = [american_to_implied(td_rows[i][0]["odds"]) for i in idxs]
        shifted = model.anchor_field([td_rows[i][2] for i in idxs], quoted)
        for i, a in zip(idxs, shifted):
            anchored[i] = a

    for i, (q, p, prob) in enumerate(td_rows):
        # Same shrink as the props, for the same reason: these are preseason
        # priors in week 1, and a 25-point disagreement with the book about who
        # scores is not something an August projection has earned.
        a = model.anchor_probability(
            anchored.get(i, prob), 0.0,
            american_to_implied(q["odds"]) if q["odds"] is not None else None,
            shrink,
        )
        priced = model.price_td(a, q["odds"], model_p=prob)
        td_rate = (p.get("rushing_tds") or 0) + (p.get("receiving_tds") or 0)
        board.tds.append({
            "player": q["player"], "key": q["key"],
            "player_id": p.get("player_id"), "position": p.get("position"),
            "team": p.get("team"), "event": q["event"],
            "fd_event_id": q["fd_event_id"], "kickoff": q["kickoff"],
            "projected_tds": round(td_rate, 3) if td_rate else None,
            "fd_market_id": q["fd_market_id"],
            "fd_selection_id": q["fd_selection_id"],
            "sgm": q["sgm"],
            # Thinness is judged on the unanchored model read — it is a
            # statement about the player, not about the book's margin.
            "thin": prob < TD_MIN_PROB,
            **priced,
        })

    board.props.sort(key=_prop_rank)
    board.tds.sort(key=lambda r: -(r.get("edge_pts") if r.get("edge_pts") is not None else -99))
    board.unmatched = sorted({ln.key for ln in fd.lines if ln.key not in matched})
    return board


def _signal(residual: float, threshold: float) -> str:
    if residual >= threshold:
        return "OVER"
    if residual <= -threshold:
        return "UNDER"
    return ""


def _prop_rank(r: dict) -> tuple:
    """Signals first, then by how far past the threshold they sit.

    Distance past the threshold rather than probability, because the
    probability model reads the top of every board at much the same level —
    the same flatness the batter card ran into — while the residual is the
    thing the rule is actually stated in.
    """
    fired = 0 if r.get("signal") else 1
    scale = r.get("threshold") or 1.0
    return (fired, -abs(r.get("residual") or 0) / scale)


# ---------------------------------------------------------------------------
# Per-week cache + background warm-up
# ---------------------------------------------------------------------------
#
# Mirrors batters.py: the first request triggers a build and gets a 503 with a
# Retry-After, and every request after that is instant. A week's board is far
# cheaper than a Statcast scrape — 64 FanDuel calls and two NFL-API calls, a
# few seconds — but the prices move, so the TTL is short and a refresh runs in
# the background rather than blocking a reader.

_cache: dict = {"board": None, "key": None}
_state: dict = {"running": False, "started_at": None, "last_error": None,
                "finished_at": None}
_lock = threading.Lock()
_TTL_SECONDS = 600


def _week_key(board: NFLBoard) -> tuple:
    return (board.season, board.week)


def get_cached(max_age: Optional[float] = None) -> Optional[NFLBoard]:
    board = _cache["board"]
    if board is None:
        return None
    if max_age is not None and time.time() - board.built_at > max_age:
        return None
    return board


def warm_status() -> dict:
    board = _cache["board"]
    return {
        "warming": _state["running"],
        "has_cache": board is not None,
        "season": board.season if board else None,
        "week": board.week if board else None,
        "age_seconds": round(time.time() - board.built_at) if board else None,
        "stale": bool(board and time.time() - board.built_at > _TTL_SECONDS),
        "elapsed_seconds": (round(time.time() - _state["started_at"], 1)
                            if _state["started_at"] and _state["running"] else None),
        "last_error": _state["last_error"],
    }


def warm_async(force: bool = False) -> dict:
    """Kick off a build if one is warranted, without blocking the caller.

    A no-op when the cache is fresh and nothing is already running, so it is
    safe to call on every request — which is how the endpoint keeps mid-week
    line moves flowing in without ever making a reader wait for them.
    """
    with _lock:
        if _state["running"]:
            return {"status": "warming"}
        board = _cache["board"]
        fresh = board is not None and time.time() - board.built_at < _TTL_SECONDS
        if fresh and not force:
            return {"status": "fresh"}
        _state.update({"running": True, "started_at": time.time(), "last_error": None})

    def run():
        try:
            board = asyncio.run(build_board(force=force))
            _cache.update({"board": board, "key": _week_key(board)})
            logger.info("[nfl] week %s-%s board: %d props, %d TD prices",
                        board.season, board.week, len(board.props), len(board.tds))
        except Exception as e:
            logger.warning("[nfl] board build failed: %r", e)
            _state["last_error"] = str(e)
        finally:
            _state.update({"running": False, "finished_at": time.time(),
                           "started_at": None})

    threading.Thread(target=run, name="nfl-warm", daemon=True).start()
    return {"status": "warming"}


def as_payload(board: NFLBoard) -> dict:
    """The board as the API serves it."""
    signals = [r for r in board.props if r["signal"] and r["bettable"]]
    return {
        "season": board.season,
        "week": board.week,
        "preseason": board.preseason,
        "props": board.props,
        "signals": signals,
        "tds": board.tds,
        "games": board.games,
        "fits": board.fits,
        "prob_fits": board.prob_fits,
        "thresholds": THRESHOLDS,
        "bettable": list(model.BETTABLE),
        "passing_yards_caveat": model.PASSING_YARDS_CAVEAT,
        "odds": {"age_seconds": board.odds_age, "error": board.odds_error},
        "unmatched": board.unmatched[:25],
        "built_at": board.built_at,
    }
