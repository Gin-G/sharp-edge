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

**What is fixed and what still moves — deliberately the same as baseball.**
Picks are replaceable while unresolved, so a re-run before kickoff revises them
and a line move or an injury is picked up. The *card* is written once, the
first time it is built, and then left alone. That is exactly the split
``tracking.freeze_parlay`` uses for the daily parlay, and the incident behind
it is recorded there: on 2026-08-20 a card rebuilt late in the day came back as
a single leg ranked 8th, because the seven names above it had already played
and FanDuel had pulled their markets.

**The asymmetry worth knowing.** Baseball's unit is a day and every game starts
within a few hours, so "frozen at first build" and "frozen shortly before first
pitch" are nearly the same moment. An NFL week runs Thursday to Monday, so a
card frozen on Wednesday is set four or five days before most of its legs kick
off, while every market is still open and the lines still have a long way to
move. The baseball rationale — that the board stops being able to reproduce the
card — does not bite here until each individual game starts.

Kept as-is anyway, because the alternative is worse in a way that matters more
than staleness: a card re-derivable until each leg's kickoff is a card whose
record can improve with hindsight, one leg at a time, and being unable to do
that is the entire point of writing it down. Worth revisiting if the frozen
card starts diverging badly from the Sunday-morning board — the divergence is
measurable, since every pick keeps the price and projection it was recorded at.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
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
    # Every field here exists to answer a question the record will be asked
    # later and cannot reconstruct: which rows carried a matchup adjustment and
    # what it would have been without one, which disagreed with the market
    # about role, what the model said before it was anchored. Adding a field
    # after the fact only works for weeks that have not happened yet.
    keep = ("model_p_raw", "fair_p", "implied_p", "over_odds", "under_odds",
            "raw_gap", "threshold", "prediction_type", "exp_games", "position",
            "kickoff", "sgm", "role_conflict", "role_conflict_with",
            "role_conflict_verdict",
            # What the availability feeds said at the moment of the pick. These
            # cannot be reconstructed afterwards — nflverse overwrites a week's
            # roster and injury rows in place, so by the time a pick settles the
            # feed says what was true on Sunday and not what was true when the
            # bet was recorded. See nfl/availability.py.
            "avail", "avail_reason", "depth_rank", "vacated_share",
            "matchup_factor", "success_rate_factor", "usage_projection")
    return json.dumps({k: row.get(k) for k in keep if row.get(k) is not None})


def _has_kicked_off(row: dict, now: Optional[datetime] = None) -> bool:
    """Has this row's game already started?

    A pick is a prediction, and a prediction made after kickoff is not one. The
    board upserts on every fetch, so without this check a board still serving
    last week's slate keeps writing picks for games that have finished — and
    keeps revising the lines of picks already recorded, replacing the number
    that was actually available with whatever the settled market returns.

    Both happened in week 1: 25 picks were written the morning after the games,
    and D.J. Moore's recorded line moved from 47.5 to an implausible 112.5.

    Unparseable or missing kickoff returns False — a row is only withheld on
    positive evidence that its game has started.
    """
    raw = row.get("kickoff")
    if not raw:
        return False
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= (now or datetime.now(timezone.utc))


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

    # Only rows whose game is still ahead of us. Everything already under way
    # is left exactly as it was recorded before kickoff — see _has_kicked_off.
    picks = [r for r in card_mod.suggestions(payload.get("props") or [])
             if not _has_kicked_off(r)]
    written = await db.upsert_nfl_picks(
        [_pick_row(r, season, week, source) for r in picks]
    )

    await _snapshot_board(payload)

    # The game model's view of the slate, recorded on the same terms as the
    # props: written before kickoff, never revised after. Predictions only —
    # no side is being taken on them.
    try:
        await record_game_predictions(season, week, payload.get("game_model") or [])
    except Exception as exc:                 # must not cost the card its freeze
        logger.warning("[nfl-track] game predictions not recorded: %s", exc)

    the_card = [r for r in (card_mod.build(payload.get("props") or []) or [])
                if not _has_kicked_off(r)]
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


async def _snapshot_board(payload: dict) -> None:
    """Record the shape of the whole board, one row per market.

    Picks say what we would bet; this says what we were choosing from, and the
    two answer different questions. The board's over/under tilt cannot be
    diagnosed from the picks alone — you cannot tell whether the residuals were
    already skewed before the threshold, or whether the threshold made them so.
    That distinction is unrecoverable after the week passes, so it is written
    now even though nothing reads it yet.
    """
    db = _require_db()
    season, week = payload["season"], payload["week"]
    props = payload.get("props") or []
    suggested = {(r.get("key"), r.get("market")) for r in card_mod.suggestions(props)}

    by_market: dict = {}
    for r in props:
        by_market.setdefault(r["market"], []).append(r)

    for market, rows in by_market.items():
        residuals = sorted(r["residual"] for r in rows if r.get("residual") is not None)

        def q(p: float):
            if not residuals:
                return None
            i = min(len(residuals) - 1, max(0, int(round(p * (len(residuals) - 1)))))
            return round(residuals[i], 2)

        fit = (payload.get("fits") or {}).get(market) or {}
        pfit = (payload.get("prob_fits") or {}).get(market) or {}
        try:
            await db.upsert_nfl_snapshot({
                "season": season, "week": week, "market": market,
                "rows": len(rows),
                "signal_over": sum(1 for r in rows if r.get("signal") == "OVER"),
                "signal_under": sum(1 for r in rows if r.get("signal") == "UNDER"),
                "suggested_over": sum(
                    1 for r in rows
                    if (r.get("key"), market) in suggested and r.get("side") == "OVER"),
                "suggested_under": sum(
                    1 for r in rows
                    if (r.get("key"), market) in suggested and r.get("side") == "UNDER"),
                "residual_p10": q(0.10), "residual_p50": q(0.50),
                "residual_p90": q(0.90),
                "fit_slope": fit.get("slope"), "prob_offset": pfit.get("offset"),
            })
        except Exception as e:
            # A snapshot is diagnostics. It must never cost us the picks.
            logger.warning("[nfl-track] snapshot %s wk%s %s failed: %s",
                           season, week, market, e)


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

class ActualsNotPublished(Exception):
    """nflverse has no file for this season yet — normal before week 1."""


def _load_actuals(season: int, week: int):
    """nflverse weekly stats for one week, keyed by normalised name.

    nflverse is the settlement source rather than the sportsbook because it is
    the same data the model is calibrated against — settling on one source and
    fitting on another is how a track record quietly stops meaning anything.
    It lands a day or two after the games, which is why settlement is a
    separate pass rather than part of the board.

    A 404 is not a failure. nflverse only publishes a season's file once that
    season has games in it, so every settlement run before the first Sunday
    gets one — and the daily job would otherwise report an error every day of
    the preseason, training whoever reads it to ignore the alert that matters.
    """
    import pandas as pd
    from urllib.error import HTTPError

    try:
        df = pd.read_parquet(NFLVERSE_WEEKLY.format(season=season))
    except HTTPError as e:
        if e.code == 404:
            raise ActualsNotPublished(
                f"nflverse has not published {season} weekly stats yet"
            ) from e
        raise
    df = df[(df.week == week) & (df.season_type == "REG")]
    out = {}
    for _, r in df.iterrows():
        out[norm_name(r.get("player_display_name"))] = r
    # The teams that have actually played this week, which is a different
    # question from which players have a row. nflverse publishes a week
    # incrementally — a Thursday game lands days before Sunday's — so a
    # player with no row is only "did not play" if his *team* is in the file.
    played = {t for t in df.team.dropna().unique() if isinstance(t, str)}
    return out, played


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


async def record_game_predictions(season: int, week: int,
                                  rows: list[dict]) -> int:
    """Persist this week's game predictions, before their games start.

    Same rule as the props: a fixture already under way is left alone, so the
    number on record is the one that was in front of us beforehand.
    """
    db = _require_db()
    fresh = [r for r in rows if not _has_kicked_off(r)]
    if not fresh:
        return 0
    return await db.upsert_nfl_game_predictions(fresh)


async def settle_game_predictions(season: Optional[int] = None,
                                  week: Optional[int] = None) -> dict:
    """Attach final scores to predictions whose games have finished."""
    import asyncio

    db = _require_db()
    rows = [r for r in await db.list_nfl_game_predictions(season, week)
            if r.get("home_score") is None]
    if not rows:
        return {"settled": 0, "message": "nothing pending"}

    try:
        from . import gamemodel
        sched = await asyncio.to_thread(gamemodel._schedule, True)
    except Exception as e:
        return {"settled": 0, "error": str(e)}

    final = {(g["season"], g["week"], g["home_team"], g["away_team"]): g
             for g in sched
             if g.get("home_score") is not None and g.get("away_score") is not None}
    n = 0
    for r in rows:
        g = final.get((r["season"], r["week"], r["home_team"], r["away_team"]))
        if g is None:
            continue
        await db.settle_nfl_game(r["season"], r["week"], r["home_team"],
                                 r["away_team"], int(g["home_score"]),
                                 int(g["away_score"]))
        n += 1
    return {"settled": n, "pending": len(rows) - n}


async def game_model_record(season: Optional[int] = None) -> dict:
    """How the game model is doing, against the result AND against the market.

    Two comparisons, and the second is the one that matters. Beating a naive
    baseline is easy; the market is the standard, and the backtest says we do
    not meet it — margin MAE 10.98 against 9.73 over 2021-25. This exists to
    find out whether that holds live, not to justify a bet.

    No betting signal is derived from any of this. `beat_market` counts games
    where our number landed closer, which is a measurement, not a record.
    """
    db = _require_db()
    rows = [r for r in await db.list_nfl_game_predictions(season)
            if r.get("home_score") is not None]
    if not rows:
        return {"games": 0, "message": "nothing settled yet"}

    def acc(vals):
        return round(sum(vals) / len(vals), 2) if vals else None

    m_err, t_err, mk_m_err, mk_t_err = [], [], [], []
    beat_m = beat_t = mk_games = 0
    wins = correct = 0
    for r in rows:
        margin = r["home_score"] - r["away_score"]
        total = r["home_score"] + r["away_score"]
        if r.get("exp_margin") is not None:
            m_err.append(abs(r["exp_margin"] - margin))
            if r.get("home_win_p") is not None:
                wins += 1
                correct += int((r["home_win_p"] >= 0.5) == (margin > 0))
        if r.get("exp_total") is not None:
            t_err.append(abs(r["exp_total"] - total))
        if r.get("market_spread") is not None and r.get("exp_margin") is not None:
            mk_games += 1
            mk_m_err.append(abs(r["market_spread"] - margin))
            beat_m += int(abs(r["exp_margin"] - margin)
                          < abs(r["market_spread"] - margin))
        if r.get("market_total") is not None and r.get("exp_total") is not None:
            mk_t_err.append(abs(r["market_total"] - total))
            beat_t += int(abs(r["exp_total"] - total)
                          < abs(r["market_total"] - total))
    return {
        "games": len(rows),
        "margin_mae": acc(m_err),
        "total_mae": acc(t_err),
        "market_margin_mae": acc(mk_m_err),
        "market_total_mae": acc(mk_t_err),
        "beat_market_margin": beat_m,
        "beat_market_total": beat_t,
        "compared_with_market": mk_games,
        "winner_called": f"{correct}/{wins}" if wins else None,
        "backtest_reference": {
            "margin_mae": 10.98, "market_margin_mae": 9.73,
            "total_mae": 10.69, "market_total_mae": 10.32,
            "note": ("2021-25, refit weekly. The model is expected to lose to "
                     "the market; this records whether it does."),
        },
    }


async def purge_late_picks(season: Optional[int] = None,
                           week: Optional[int] = None,
                           apply: bool = False) -> dict:
    """Remove picks that were recorded after their own game had started.

    These are not predictions. Before the kickoff guard existed, the board
    upserted on every fetch and kept writing a week that had already been
    played: 25 of week 1's 106 picks were created after their kickoff, went
    19-6 (76.0%), and took the recorded hit rate from 53.4% to 59.2%. Leaving
    them in means every number computed from the track record is wrong.

    The rule is the filter — a row qualifies only when its own ``created_at``
    is later than its own ``kickoff``. There is no way to ask this to delete
    anything else, which is the point: it cannot be pointed at a legitimate
    pick by getting an argument wrong.

    Dry by default. ``apply=True`` performs the deletes and is the only thing
    that writes. Rows with an unparseable or missing timestamp are left alone —
    a pick is removed on positive evidence that it postdates its kickoff, never
    on the absence of evidence that it does not.
    """
    db = _require_db()
    rows = await db.list_nfl_picks(season=season, week=week)

    # Both timestamps must be readable. _has_kicked_off falls back to "now"
    # when handed None, which for a finished game is always true — so passing
    # an unparsed created_at straight through would delete the entire week.
    late = []
    for p in rows:
        made = _parsed(p.get("created_at"))
        if made is None:
            continue
        if _has_kicked_off(p, made):
            late.append(p)
    out = {
        "scanned": len(rows),
        "late": len(late),
        "applied": bool(apply),
        "picks": [
            {k: p.get(k) for k in
             ("season", "week", "player_key", "player", "market", "side",
              "line", "result", "created_at", "kickoff")}
            for p in late
        ],
    }
    if not apply:
        out["deleted"] = 0
        out["message"] = "dry run — pass apply=true to delete"
        return out

    deleted = 0
    for p in late:
        deleted += await db.delete_nfl_pick(
            p["season"], p["week"], p["player_key"], p["market"])
    out["deleted"] = deleted
    logger.warning("[nfl-track] purged %d picks recorded after kickoff", deleted)
    return out


def _parsed(stamp) -> Optional[datetime]:
    """A timestamp as an aware datetime, or None if it cannot be read."""
    if not stamp:
        return None
    if isinstance(stamp, datetime):
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


async def settle_week(season: int, week: int, regrade: bool = False) -> dict:
    """Resolve every unsettled pick for one week against nflverse actuals.

    ``regrade`` re-scores an already-settled card. The picks are left alone —
    they are graded at their own line and are right — but a card written before
    a line moved can hold a result that its frozen legs do not support, and
    without this there is no way to correct one.
    """
    db = _require_db()
    picks = [p for p in await db.list_nfl_picks(season=season, week=week)
             if not p.get("result")]
    if not picks:
        # Still re-score the card: every pick being settled is exactly when a
        # card most needs grading, not a reason to skip it.
        await _settle_card(season, week, regrade=regrade)
        return {"season": season, "week": week, "settled": 0,
                "message": "nothing pending"}

    import asyncio
    try:
        actuals, played = await asyncio.to_thread(_load_actuals, season, week)
    except ActualsNotPublished as e:
        # Expected before the season's first games — a status, not a failure.
        return {"season": season, "week": week, "settled": 0,
                "pending": len(picks), "message": str(e)}
    except Exception as e:
        logger.warning("[nfl-track] actuals unavailable for %s wk%s: %s", season, week, e)
        return {"season": season, "week": week, "settled": 0, "error": str(e)}
    if not actuals:
        return {"season": season, "week": week, "settled": 0,
                "pending": len(picks),
                "message": f"week {week} not in the published data yet"}

    counts: dict = {"WIN": 0, "LOSS": 0, "PUSH": 0, "VOID": 0}
    waiting = 0
    for p in picks:
        # Leave a pick alone until its own game is in the file. Settling the
        # whole week the moment *any* of it publishes is how 40 of 45 week-1
        # picks were voided on a Friday, with Sunday's games still to come:
        # "no row for this player" was read as "did not play" when it meant
        # "has not kicked off".
        if p.get("team") not in played:
            waiting += 1
            continue
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

    # Scored against the card's OWN legs, not the week's. Gating this on the
    # whole week meant a parlay whose legs had both finished on Sunday sat
    # unresolved because unrelated picks were waiting on Monday night.
    await _settle_card(season, week, regrade=regrade)
    return {"season": season, "week": week,
            "settled": sum(counts.values()), "waiting_on_kickoff": waiting,
            **counts}


def _leg_result(leg: dict, actual: Optional[float]) -> Optional[str]:
    """Grade one frozen card leg against the actual, at the leg's OWN line.

    A card cannot inherit its legs' results from the picks table. A pick is
    upserted with the current line for as long as it is unresolved, while the
    card is frozen once, before kickoff — so by settlement the two can hold
    different lines for the same player. Week 1: the Malik Willis pick had
    moved to under 39.5 and settled a win on 39.0 rushing yards, while the card
    had been frozen at under 38.5, which that same 39.0 loses.

    The frozen line is the one that matters: it is the bet the card actually
    represents, and the betslip link it was written with.
    """
    if actual is None or leg.get("line") is None or not leg.get("side"):
        return None
    line = float(leg["line"])
    actual = float(actual)
    if actual == line:
        return "PUSH"
    over = actual > line
    return "WIN" if (over if leg["side"] == "OVER" else not over) else "LOSS"


async def _settle_card(season: int, week: int, regrade: bool = False) -> None:
    """Score the frozen card off its legs' settled results.

    A card wins only if every leg does — it is one parlay. A VOID leg drops out
    rather than killing the ticket, which is how the sportsbook treats it.
    """
    db = _require_db()
    the_card = await db.get_nfl_card(season, week)
    if not the_card or (the_card.get("result") and not regrade):
        return
    settled = {(p["player_key"], p["market"]): p
               for p in await db.list_nfl_picks(season=season, week=week)}

    legs = the_card.get("legs") or []
    if not legs:
        return

    won = graded = 0
    pending = False
    for leg in legs:
        p = settled.get((leg["player_key"], leg["market"]))
        if not p or not p.get("result"):
            pending = True
            continue
        if p["result"] == "VOID":
            continue        # a voided leg drops out, as the book treats it
        # Graded at the leg's frozen line, not by inheriting p["result"] —
        # the pick's line may have moved after the card was written.
        res = _leg_result(leg, p.get("actual"))
        if res is None:
            pending = True
            continue
        graded += 1
        if res in ("WIN", "PUSH"):
            won += 1

    # One lost leg kills a parlay, so the card is decided the moment any leg
    # loses — no reason to wait on the rest. Otherwise every leg has to be in
    # before it can be called a win, or a two-legger would be graded a winner
    # off the one leg that happened to finish first.
    lost = graded > won
    if not lost and pending:
        return
    if graded == 0:
        return
    await db.settle_nfl_card(season, week, "LOSS" if lost else "WIN", won, graded)


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


def _metric(row: dict, key: str, default=None):
    """Read one field back out of a pick's stored ``metrics`` blob.

    SQLite hands it back as a string, Postgres as a dict, so both are handled
    rather than pushed onto every caller.
    """
    m = row.get("metrics")
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except (TypeError, ValueError):
            return default
    return (m or {}).get(key, default) if isinstance(m, dict) else default


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

    # The legs are the frozen snapshot taken before kickoff, so they carry no
    # result — that lives on the pick. Join them here rather than in the UI:
    # "what was the parlay and did it hit" is one question and should not need
    # the client to re-derive half of it.
    by_pick = {(q["season"], q["week"], q["player_key"], q["market"]): q
               for q in rows}
    for c in cards:
        for leg in c.get("legs") or []:
            q = by_pick.get((c["season"], c["week"],
                             leg.get("player_key"), leg.get("market")))
            if q:
                leg["actual"] = q.get("actual")
                # At the leg's own frozen line — see _leg_result. The pick's
                # result can disagree when the line moved after the freeze.
                leg["result"] = ("VOID" if q.get("result") == "VOID"
                                 else _leg_result(leg, q.get("actual")))

    graded_cards = [c for c in cards if c.get("result")]
    return {
        "season": season,
        "overall": _bucket(rows),
        "by_market": _group(rows, "market"),
        "by_side": _group(rows, "side"),
        # The open question this season: we rank roughly half the board's
        # players differently from the market within their own team, because
        # the projection reads last year's usage and cannot see a role change.
        # Those rows are flagged rather than dropped so this split can answer
        # whether they actually lose. See screen.flag_role_conflicts.
        "by_role_conflict": [
            {"role_conflict": k, **_bucket(v)}
            for k, v in sorted(
                _split(rows, lambda r: bool(_metric(r, "role_conflict"))).items(),
                key=lambda kv: kv[0],
            )
        ],
        # Whether the depth chart picked the winner of a role conflict. The
        # flag above says we disagreed with the market about who plays; this
        # says who a third source sided with, and whether that was worth
        # knowing. See screen._depth_verdict.
        "by_role_verdict": [
            {"verdict": k, **_bucket(v)}
            for k, v in sorted(
                _split([r for r in rows if _metric(r, "role_conflict")],
                       lambda r: _metric(r, "role_conflict_verdict") or "unknown"
                       ).items(),
                key=lambda kv: str(kv[0]),
            )
        ],
        # Picks made on a player whose position group had lost work to an
        # injury. ``card.VACATED_SHARE_BLOCKS_UNDER`` refuses the unders above
        # a third on the argument that the projection is stale-low there; this
        # is the split that says whether the argument holds, and it needs the
        # overs — which are not refused — to say anything at all.
        "by_vacated": [
            {"vacated": k, **_bucket(v)}
            for k, v in sorted(
                _split(rows, lambda r: _vacated_bucket(
                    _metric(r, "vacated_share"))).items(),
                key=lambda kv: str(kv[0]),
            )
        ],
        # Picks recorded on a player carrying an injury designation. Only
        # questionable survives the card guard, so this is really asking one
        # question: does the market price a questionable tag correctly, or does
        # backing through one cost us?
        "by_availability": [
            {"avail": k, **_bucket(v)}
            for k, v in sorted(
                _split(rows, lambda r: _metric(r, "avail") or "unknown").items(),
                key=lambda kv: str(kv[0]),
            )
        ],
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


def _vacated_bucket(share) -> str:
    """Coarse buckets, because the sample will never support fine ones.

    Eighteen weeks of a dozen suggestions is a couple of hundred picks in a
    season, and only a handful carry a vacancy at all. Three buckets is already
    optimistic; splitting on the exact share would produce cells of one.
    """
    if share is None:
        return "none"
    try:
        v = float(share)
    except (TypeError, ValueError):
        return "none"
    if v <= 0:
        return "none"
    from .card import VACATED_SHARE_BLOCKS_UNDER
    return "major" if v >= VACATED_SHARE_BLOCKS_UNDER else "minor"


def _split(rows: list[dict], key) -> dict:
    out: dict = {}
    for r in rows:
        out.setdefault(key(r), []).append(r)
    return out


def _by_week(rows: list[dict]) -> dict:
    out: dict = {}
    for r in rows:
        out.setdefault(r.get("week"), []).append(r)
    return out
