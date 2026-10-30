"""Record NFL suggestions before kickoff, settle them after, and score them.

The MLB side learned this the hard way and the lesson transfers: a screen that
does not write down what it said cannot be evaluated, and by the time you want
the history it is too late to reconstruct — the lines are gone, the projections
have been recomputed, and the board only shows today.

Four jobs:

  ``freeze_week``    write this week's suggestions and card before kickoff
  ``settle_week``    resolve them against nflverse actuals once games are played
  ``track_record``   aggregate: hit rate overall, by market, by side, by week
  ``recommendations`` the ledger: every bet we recommended, graded, in units

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
from ..db.base import iso_utc
from .names import norm_name

logger = logging.getLogger(__name__)

# Which nflverse column settles each market.
SETTLE_COLUMN = {
    "receiving_yards": "receiving_yards",
    "rushing_yards": "rushing_yards",
    "passing_yards": "passing_yards",
    "receptions": "receptions",
}

# Anytime TD settles on every touchdown the player scores himself — rushing,
# receiving or a return — and not on one he throws. Summed rather than one
# column, so it is handled apart from SETTLE_COLUMN.
TD_MARKET = "anytime_td"
TD_COLUMNS = ("rushing_tds", "receiving_tds", "special_teams_tds")

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

    The parse itself lives in ``card`` because the betslip link needs the same
    question answered, and two copies of a time comparison is two chances to
    disagree about what "already started" means.
    """
    return card_mod.kickoff_passed(row, now)


def _is_locked(row: dict, now: Optional[datetime] = None) -> bool:
    """Is this row's bet committed?

    Delegates to ``card`` for the same reason ``_has_kicked_off`` does: the
    deadline and the kickoff test are the same timestamp read two ways, and two
    copies of that arithmetic is two chances to disagree.
    """
    return card_mod.is_locked(row, now)


def _lock_stamp(row: dict) -> Optional[str]:
    """This row's lock deadline, formatted for storage, or None if undatable."""
    when = card_mod.lock_at(row)
    return None if when is None else iso_utc(when)


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
        # The commitment point, computed from this row's own kickoff. Stored so
        # the deadline is answerable from the row and the rule lives in one
        # place; see card.LOCK_LEAD.
        "lock_at": _lock_stamp(r),
    }


def _td_prop(r: dict) -> dict:
    """An anytime-TD board row in the shape of a prop, so it records and
    settles through the same table and the same code. A "yes" is an over 0.5."""
    return {**r, "market": TD_MARKET, "line": 0.5, "side": "OVER",
            "fair_p": r.get("implied_p"), "model_p": r.get("p_cal"),
            "edge_pts": r.get("true_edge_pts"),
            "projection": r.get("projected_tds"),
            "model_p_raw": r.get("model_p_unanchored")}


def _leg_row(r: dict, season: int, week: int, source: str) -> dict:
    """A frozen card leg: the pick row plus the ids the betslip link needs.

    Kept separate from ``_pick_row`` because the two go to different places.
    A pick is a row in a table with fixed columns; a card's legs are a JSON
    blob, so they can carry the three FanDuel ids without a migration — and
    they have to, because **the card that gets bet is the frozen one**. The
    live board rebuilds its card on every request and can quietly pick a
    different pair as lines move, so a link built only from the live board is
    a link to a parlay that is not the one on record.

    Old cards frozen before this existed have no ids and simply get no link.
    """
    return {
        **_pick_row(r, season, week, source),
        "fd_event_id": r.get("fd_event_id"),
        "fd_market_id": r.get("fd_market_id"),
        "over_selection_id": r.get("over_selection_id"),
        "under_selection_id": r.get("under_selection_id"),
    }


async def freeze_week(payload: dict, source: str = "live",
                      alt_rungs: Optional[list] = None,
                      built_at: Optional[float] = None) -> dict:
    """Persist this week's suggestions and freeze the card.

    Idempotent and safe to call on every board request: picks upsert (revising
    an unresolved row, never a settled one) and the card insert is a no-op once
    the week has one. That is what makes it correct to wire into the screen
    endpoint rather than needing a scheduler — the board is fetched many times
    before kickoff and the first fetch is what sticks.
    """
    db = _require_db()
    season, week = payload["season"], payload["week"]

    # Only rows whose game is still ahead of us, and of those only the ones
    # still open. Everything already under way is left exactly as it was
    # recorded before kickoff (see _has_kicked_off); everything past its lock
    # is left exactly as it was recorded at the lock (see card.LOCK_LEAD).
    #
    # The database enforces the second one too — a locked row fails the
    # upsert's WHERE — because this filter runs on the board's view of a row
    # while the guard that matters is about the row already stored. Belt and
    # braces on the one invariant the whole record now rests on: a bet, once
    # committed, is never rewritten.
    # Touchdowns ride along with the props from here on: same table, same lock,
    # same tightening. See card.td_suggestions for the bar.
    td_board = [_td_prop(r) for r in payload.get("tds") or []]
    td_picked = [_td_prop(r) for r in card_mod.td_suggestions(payload.get("tds") or [])]
    live = [r for r in card_mod.suggestions(payload.get("props") or []) + td_picked
            if not _has_kicked_off(r)]
    picks = [r for r in live if not _is_locked(r)]
    locked = len(live) - len(picks)
    written = await db.upsert_nfl_picks(
        [_pick_row(r, season, week, source) for r in picks]
    )
    # And the other direction: a candidate the board has stopped liking goes,
    # while it is still only a candidate. See _tighten.
    dropped = await _tighten(
        season, week, (payload.get("props") or []) + td_board,
        keep={(r["player_key"], r["market"])
              for r in (_pick_row(x, season, week, source) for x in picks)})

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
            "legs": json.dumps([_leg_row(r, season, week, source) for r in the_card]),
            "leg_count": summary["legs"],
            "american": summary["american"],
            "decimal_odds": summary["decimal"],
            "model_p": summary["model_p"],
        })
    repaired = await _repair_card_ids(season, week, payload.get("props") or [])
    try:
        alt = await record_alt_parlays(season, week, payload.get("alt_parlays") or {})
        alt["prices_archived"] = await archive_alt_prices(season, week, alt_rungs,
                                                          built_at)
    except Exception as exc:                 # must not cost the card its freeze
        logger.warning("[nfl-track] alt parlays not recorded: %s", exc)
        alt = {"error": str(exc)}
    return {"suggestions": len(picks), "written": written, "alt": alt,
            "locked": locked,
            "dropped": len(dropped), "dropped_rows": dropped,
            "card_legs": len(the_card), "card_frozen": frozen,
            "card_ids_repaired": repaired}


async def _tighten(season: int, week: int, props: list,
                   keep: set, now: Optional[datetime] = None) -> list:
    """Drop the candidates this week's board no longer wants.

    ``freeze_week`` was add-only, which made "tighten it up as the week goes
    on" a figure of speech: a row recorded on Tuesday that developed a role
    conflict or an injury flag by Friday stayed on the record and settled
    anyway. This is the other half — the set narrows as the picture firms up.

    **Three things are never dropped, and each for its own reason.** A locked
    pick is a placed bet. A graded pick is history. And a pick whose market is
    not on the board at all is *unexplained*: FanDuel pulls markets routinely,
    and a thin or failed fetch must not be read as "the gate declined these".
    So a row goes only on positive evidence — it is on the board, and the
    board's own gate turned it down. Same rule as ``purge_late_picks`` and
    ``_has_kicked_off``: act on evidence, never on its absence.

    Returns what it removed, with the price it was holding, so a drop is
    reportable rather than silent.
    """
    db = _require_db()
    on_board = {(r.get("key") or norm_name(r.get("player")), r.get("market")): r
                for r in props}
    dropped = []
    for p in await db.list_nfl_picks(season=season, week=week):
        if p.get("result"):
            continue
        ident = (p["player_key"], p["market"])
        if ident in keep or _is_locked(p, now):
            continue
        if ident not in on_board:
            continue        # not offered right now; no evidence either way
        if await db.delete_nfl_pick(season, week, p["player_key"], p["market"]):
            dropped.append({"player": p.get("player"), "market": p["market"],
                            "line": p.get("line"), "side": p.get("side"),
                            "open_line": p.get("open_line"),
                            "lock_at": p.get("lock_at")})
    return dropped


async def _repair_card_ids(season: int, week: int, props: list) -> int:
    """Fill FanDuel ids onto a card frozen before the legs carried them.

    Without this the fix only reaches cards frozen from here on, and the week
    already in flight — the one you would actually want to open on the book —
    stays linkless forever, because ``insert_nfl_card`` is a no-op once a week
    has a card.

    **Only the three id fields, and only where they are absent.** Player, line,
    side, price and every model number on a frozen leg are the record of what
    was predicted and must not move; the ids are not a prediction, they are how
    to find the market again. Matching is on (player_key, market), so a leg
    whose market is no longer posted simply keeps no ids and gets no link.

    Returns the number of legs repaired.
    """
    db = _require_db()
    card = await db.get_nfl_card(season, week)
    if not card or card.get("result"):
        return 0
    legs = card.get("legs") or []
    missing = [l for l in legs if not l.get("fd_market_id")]
    if not legs or not missing:
        return 0
    # Only a card that could still be bet. The ids exist to rebuild a betslip
    # link and the link expires at kickoff, so repairing a played week would
    # rewrite the record to produce nothing — and the record is the one thing
    # here worth leaving alone.
    if any(card_mod.kickoff_passed(l) for l in legs):
        return 0

    by_key = {(r.get("key"), r.get("market")): r for r in props}
    repaired = 0
    for leg in missing:
        src = by_key.get((leg.get("player_key"), leg.get("market")))
        if not src or not src.get("fd_market_id"):
            continue
        leg["fd_event_id"] = src.get("fd_event_id")
        leg["fd_market_id"] = src.get("fd_market_id")
        leg["over_selection_id"] = src.get("over_selection_id")
        leg["under_selection_id"] = src.get("under_selection_id")
        repaired += 1

    if repaired and await db.update_nfl_card_legs(season, week, json.dumps(legs)):
        logger.info("[nfl-track] %s wk%s: filled FanDuel ids on %d frozen leg(s)",
                    season, week, repaired)
        return repaired
    return 0


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
    if row is None:
        return None, None
    if pick["market"] == TD_MARKET:
        actual = 0.0
        for c in TD_COLUMNS:
            v = row.get(c)
            actual += float(v) if v is not None and v == v else 0.0
    else:
        col = SETTLE_COLUMN.get(pick["market"])
        if col is None:
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


def _open_result_for(pick: dict, row) -> Optional[str]:
    """The same outcome graded against the line and side we FIRST saw.

    A different bet from the locked one whenever the market moved, and
    sometimes the other side of it: if the line drifts past our projection
    between Tuesday and Sunday, the open was an OVER and the lock is an UNDER.
    Grading both is what answers "would we have been better off firing at
    open?" — a question the record could not previously ask, because the
    opening number was overwritten by the next board refresh.

    None when the pick predates the open columns, which is every pick recorded
    before this shipped. Those grade on the locked side only, and the
    open-versus-lock comparison skips them rather than pretending the one line
    it has was both.
    """
    if pick.get("open_line") is None or not pick.get("open_side"):
        return None
    graded, _ = _result_for(
        {"market": pick["market"], "line": pick["open_line"],
         "side": pick["open_side"]}, row)
    return graded


async def record_game_predictions(season: int, week: int,
                                  rows: list[dict]) -> int:
    """Persist this week's game predictions, before their games start.

    Same rule as the props: a fixture already under way is left alone, so the
    number on record is the one that was in front of us beforehand.
    """
    db = _require_db()
    fresh = [{**r, "sim": json.dumps(r["sim"]) if r.get("sim") is not None else None}
             for r in rows if not _has_kicked_off(r)]
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
    picks, leans = _grade_sims(rows)
    return {
        "games": len(rows),
        # The moneyline recommendations (gamesim: the book's moneyline against
        # its own spread) and the model's spread/total leans, graded at the
        # FanDuel price recorded with them. Leans are not recommendations; this
        # is where they would earn the right to become one.
        "ml_picks": picks,
        "leans": leans,
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


def _grade_sims(rows: list[dict]) -> tuple[dict, dict]:
    """W-L and ROI for every recorded sim pick and lean, at its recorded price."""
    def bucket():
        return {"bets": 0, "wins": 0, "losses": 0, "pushes": 0, "profit": 0.0}

    picks = bucket()
    leans = {"spread": bucket(), "total": bucket()}
    for r in rows:
        sim = r.get("sim")
        if isinstance(sim, str):
            try:
                sim = json.loads(sim)
            except (TypeError, ValueError):
                sim = None
        if not isinstance(sim, dict):
            continue
        margin = r["home_score"] - r["away_score"]
        total = r["home_score"] + r["away_score"]

        def settle(b: dict, edge: float, odds) -> None:
            b["bets"] += 1
            if edge == 0:
                b["pushes"] += 1
            elif edge > 0:
                b["wins"] += 1
                if odds:
                    b["profit"] += 100 / -odds if odds < 0 else odds / 100
                else:
                    b["profit"] += 100 / 110
            else:
                b["losses"] += 1
                b["profit"] -= 1.0

        for pk in sim.get("picks") or []:
            settle(picks, margin if pk.get("side") == "home" else -margin, pk.get("odds"))
        for ln in sim.get("leans") or []:
            line = ln.get("line")
            if line is None:
                continue
            if ln.get("market") == "spread":
                m = margin if ln.get("side") == "home" else -margin
                settle(leans["spread"], m + line, ln.get("odds"))
            elif ln.get("market") == "total":
                settle(leans["total"],
                       (total - line) if ln.get("side") == "over" else (line - total),
                       ln.get("odds"))

    def finish(b: dict) -> dict:
        decided = b["wins"] + b["losses"]
        b["hit_rate"] = round(100 * b["wins"] / decided, 1) if decided else None
        b["roi"] = round(100 * b["profit"] / decided, 1) if decided else None
        b["profit"] = round(b["profit"], 2)
        return b

    return finish(picks), {k: finish(v) for k, v in leans.items()}


# --- alt-line parlays (nfl/altparlay.py) -----------------------------------

_archived: dict = {"key": None}


async def record_alt_parlays(season: int, week: int, parlays: dict,
                             now: Optional[datetime] = None) -> dict:
    """Write this week's alt parlays, rebuilding each until its first leg locks.

    The card is frozen at first build; these are not, because a lotto drawn
    from Thursday to Monday cannot be built on Tuesday — half the ladders are
    not up yet. So each is rewritten with the board while every leg is still
    open, and frozen the moment its earliest leg passes its lock (kickoff less
    card.LOCK_LEAD). From then on it is a placed bet and the database refuses
    to rewrite it. A week the board can no longer build drops its unfrozen
    ticket rather than leaving a stale one on the record.
    """
    db = _require_db()
    stored = {r["kind"]: r for r in await db.list_nfl_alt_parlays(season, week)}
    out = {}
    for kind, p in parlays.items():
        cur = stored.get(kind)
        if cur and not cur.get("frozen") and not cur.get("result") and any(
                _is_locked(leg, now) for leg in cur.get("legs") or []):
            await db.freeze_nfl_alt_parlay(season, week, kind)
            out[kind] = "frozen"
            continue
        if cur and (cur.get("frozen") or cur.get("result")):
            out[kind] = "frozen"
            continue
        if not p or any(_is_locked(leg, now) for leg in p.get("legs") or []):
            if cur and await db.delete_nfl_alt_parlay(season, week, kind):
                out[kind] = "dropped"
            else:
                out[kind] = "none"
            continue
        legs = [{**{k: leg.get(k) for k in (
                    "player", "key", "team", "position", "market", "line",
                    "threshold", "side", "odds", "implied", "fair", "model_p",
                    "p_cal", "edge_pts", "hits4", "last4", "event", "fd_event_id",
                    "kickoff", "fd_market_id", "over_selection_id")},
                 "lock_at": _lock_stamp(leg)} for leg in p["legs"]]
        await db.upsert_nfl_alt_parlay({
            "season": season, "week": week, "kind": kind,
            "legs": json.dumps(legs), "leg_count": len(legs),
            "american": p.get("american"), "decimal_odds": p.get("decimal_odds"),
            "model_p": p.get("model_p"), "stake": p.get("stake"),
        })
        out[kind] = "written"
    return out


async def archive_alt_prices(season: int, week: int, rungs: Optional[list],
                             built_at: Optional[float]) -> int:
    """Every priced rung, once per board build. See upsert_nfl_alt_prices."""
    if not rungs:
        return 0
    key = (season, week, built_at)
    if built_at is not None and _archived["key"] == key:
        return 0
    rows = [{"season": season, "week": week, "player_key": r.get("key"),
             **{k: r.get(k) for k in ("market", "line", "player", "team", "event",
                                      "kickoff", "odds", "model_p", "p_cal",
                                      "edge_pts", "hits4")}}
            for r in rungs if r.get("key") and r.get("odds") is not None
            and not _has_kicked_off(r)]
    n = await _require_db().upsert_nfl_alt_prices(rows)
    _archived["key"] = key
    return n


def _grade_alt_parlay(legs: list[dict], actuals: dict, played: set) -> tuple:
    """``(result | None, legs won, legs graded, decimal over the legs that stood)``.

    The card's rules: one lost leg decides it, a void leg drops out and takes
    its price with it, and a win needs every leg in.
    """
    won = graded = 0
    pending = False
    dec = 1.0
    for leg in legs:
        if leg.get("team") not in played:
            pending = True
            continue
        row = actuals.get(leg.get("key"))
        if row is None:
            continue                        # did not play: void, as the book does
        res, actual = _result_for({**leg, "side": leg.get("side") or "OVER"}, row)
        if res is None:
            pending = True
            continue
        graded += 1
        if res == "WIN":
            won += 1
            o = leg.get("odds")
            dec *= 1 + (o / 100 if o > 0 else 100 / -o) if o else 1.0
        elif res == "LOSS":
            return "LOSS", won, graded, None
    if pending:
        return None, won, graded, None
    if graded == 0:
        return "VOID", 0, 0, 1.0
    return "WIN", won, graded, round(dec, 4)


async def _settle_alt_parlays(season: int, week: int, actuals: dict,
                              played: set) -> int:
    db = _require_db()
    n = 0
    for p in await db.list_nfl_alt_parlays(season, week):
        if p.get("result") or not p.get("frozen"):
            continue
        res, won, graded, dec = _grade_alt_parlay(p.get("legs") or [], actuals, played)
        if res is None:
            continue
        await db.settle_nfl_alt_parlay(season, week, p["kind"], res, won, graded, dec)
        n += 1
    return n


# --- placed bets ------------------------------------------------------------
#
# What the owner actually bet, as opposed to what the board recommended. The
# alt parlays rebuild with the board until their first leg locks, so the
# ticket on screen when a bet is placed is often not the one the record ends
# up holding: week 8's double-up was bet as Tuten 5+ / Stevenson 15+ at +101,
# the page later showed Emmett Johnson 5+ / Stevenson 10+, and the record froze
# a third ticket nobody saw. A placed bet is written once, from the screen,
# and never rebuilt.

PLACED_LEG_FIELDS = ("player", "key", "team", "market", "line", "threshold", "side",
                     "odds", "p_cal", "event", "fd_event_id", "kickoff",
                     "fd_market_id", "over_selection_id", "under_selection_id")


def _placed_row(bet: dict) -> dict:
    """Validate and normalise a placement. Raises ValueError on a bad one."""
    legs = bet.get("legs") or []
    if not legs:
        raise ValueError("a placed bet needs at least one leg")
    clean = []
    for leg in legs:
        market = leg.get("market")
        if market not in SETTLE_COLUMN and market != TD_MARKET:
            raise ValueError(f"cannot settle a {market!r} leg")
        if leg.get("line") is None or not (leg.get("key") or leg.get("player")):
            raise ValueError("every leg needs a player and a line")
        out = {k: leg.get(k) for k in PLACED_LEG_FIELDS}
        out["key"] = out["key"] or norm_name(out["player"])
        out["side"] = (out["side"] or "OVER").upper()
        clean.append(out)
    american = bet.get("american")
    dec = bet.get("decimal_odds")
    if dec is None and american is not None:
        a = int(american)
        dec = 1 + (a / 100 if a > 0 else 100 / -a)
    if dec is None:
        dec = 1.0
        for leg in clean:
            o = leg.get("odds")
            dec *= 1 + (o / 100 if o > 0 else 100 / -o) if o else 1.0
    if american is None:
        american = round((dec - 1) * 100) if dec >= 2 else round(-100 / (dec - 1))
    stake = float(bet.get("stake") if bet.get("stake") is not None else 1.0)
    if stake <= 0:
        raise ValueError("stake must be positive")
    return {"season": int(bet["season"]), "week": int(bet["week"]),
            "label": bet.get("label"), "source": bet.get("source") or "manual",
            "legs": json.dumps(clean), "leg_count": len(clean),
            "american": int(american), "decimal_odds": round(float(dec), 4),
            "model_p": bet.get("model_p"), "stake": stake}


async def record_placed(bet: dict) -> dict:
    row = _placed_row(bet)
    bet_id = await _require_db().insert_nfl_placed_bet(row)
    return {"id": bet_id, **{k: row[k] for k in ("season", "week", "label", "american",
                                                 "decimal_odds", "stake", "leg_count")}}


async def _settle_placed(season: int, week: int, actuals: dict, played: set) -> int:
    db = _require_db()
    n = 0
    for b in await db.list_nfl_placed_bets(season, week):
        if b.get("result"):
            continue
        legs = b.get("legs") or []
        res, won, graded, dec = _grade_alt_parlay(legs, actuals, played)
        if res is None:
            continue
        # The price taken stands unless a leg voided; then the book reprices
        # the ticket on the legs that remained.
        if res == "WIN" and graded == len(legs):
            dec = b.get("decimal_odds") or dec
        await db.settle_nfl_placed_bet(b["id"], res, won, graded, dec)
        n += 1
    return n


def _placed_entry(b: dict) -> dict:
    stake = float(b.get("stake") or 1.0)
    res = b.get("result")
    if res == "WIN":
        units = round(stake * ((b.get("settled_decimal") or b.get("decimal_odds") or 1) - 1), 3)
    elif res == "LOSS":
        units = -stake
    elif res in ("VOID", "PUSH"):
        units = 0.0
    else:
        units = None
    legs = b.get("legs") or []
    return {"id": b.get("id"), "season": b["season"], "week": b["week"],
            "placed_at": b.get("placed_at"), "label": b.get("label"),
            "source": b.get("source"), "odds": b.get("american"), "stake": stake,
            "result": res, "units": units, "model_p": b.get("model_p"),
            "actual": (f"{b.get('legs_won')}/{b.get('legs_settled')} legs"
                       if b.get("legs_settled") is not None else None),
            "legs": [{k: l.get(k) for k in ("player", "market", "line", "threshold",
                                           "side", "odds", "event", "kickoff")}
                     for l in legs]}


async def placed_record(season: Optional[int] = None) -> dict:
    rows = [_placed_entry(b) for b in await _require_db().list_nfl_placed_bets(season)]
    return {"summary": _ledger_bucket([{**r, "type": "placed"} for r in rows]),
            "bets": rows}


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
    alt_pending = [p for p in await db.list_nfl_alt_parlays(season, week)
                   if p.get("frozen") and not p.get("result")]
    alt_pending += [b for b in await db.list_nfl_placed_bets(season, week)
                    if not b.get("result")]
    if not picks and not alt_pending:
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

    alt_settled = await _settle_alt_parlays(season, week, actuals, played)
    placed_settled = await _settle_placed(season, week, actuals, played)
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
            result, actual, open_result = "VOID", None, "VOID"
        else:
            result, actual = _result_for(p, row)
            if result is None:
                continue
            open_result = _open_result_for(p, row)
        await db.settle_nfl_pick(season, week, p["player_key"], p["market"],
                                 result, actual, open_result)
        counts[result] += 1

    # Scored against the card's OWN legs, not the week's. Gating this on the
    # whole week meant a parlay whose legs had both finished on Sunday sat
    # unresolved because unrelated picks were waiting on Monday night.
    await _settle_card(season, week, regrade=regrade)
    return {"season": season, "week": week,
            "settled": sum(counts.values()), "waiting_on_kickoff": waiting,
            "alt_parlays_settled": alt_settled, "placed_settled": placed_settled,
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


def _open_vs_lock(rows: list[dict]) -> dict:
    """Did waiting for the lock help, on the picks where it can be compared.

    The two bets being compared are the SAME selection at two moments: the line
    and price we first saw, and the line and price it had when the bet
    committed. Only rows carrying both and graded on both count, so everything
    recorded before the open columns existed drops out rather than being
    counted as a wash.

    ``moved_to_us`` is the one to watch. It is the share of picks where the
    market came toward our side between open and lock — the line moving in the
    direction we had already taken. That is closing line value measured against
    our own commitment point rather than against the close, and it is the
    nearest thing to an honest read on whether a bet was good that does not
    depend on whether it won.
    """
    pairs = [r for r in rows
             if r.get("open_result") in ("WIN", "LOSS")
             and r.get("result") in ("WIN", "LOSS")
             and r.get("open_line") is not None and r.get("line") is not None]
    if not pairs:
        return {"comparable": 0,
                "note": "no pick yet carries both an opening and a locked line"}

    at_open = [{**r, "result": r["open_result"],
                "fd_odds": r.get("open_odds")} for r in pairs]
    moved = 0
    for r in pairs:
        delta = float(r["line"]) - float(r["open_line"])
        if delta == 0:
            continue
        # Measured against the side taken at OPEN, because the open bet is the
        # one whose value this is asking about. An OVER wants the line to come
        # down afterwards, an UNDER wants it to go up.
        moved += 1 if (delta < 0) == (r.get("open_side") == "OVER") else 0
    flipped = sum(1 for r in pairs if r.get("open_side") != r.get("side"))
    changed = sum(1 for r in pairs
                  if float(r["line"]) != float(r["open_line"]))
    return {
        "comparable": len(pairs),
        "at_open": _bucket(at_open),
        "at_lock": _bucket(pairs),
        "line_moved": changed,
        "side_flipped": flipped,
        "moved_to_us": round(100 * moved / changed, 1) if changed else None,
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

    # A link back to FanDuel for the parlay actually on record, not the one the
    # live board would rebuild now — and only while it can still be placed.
    # ``betslip_url`` drops it at the first leg's kickoff, which is the moment
    # the markets go, rather than at settlement a day or two later. The legs,
    # their lines and the prices that were available stay on the record either
    # way; it is only the link that expires.
    # Both guards, and neither is redundant. Kickoff is the one that matters —
    # it drops the link at the moment FanDuel pulls the markets, which is the
    # whole weekend before a result exists. The result check behind it catches
    # what the kickoff parse cannot see: a leg frozen without a kickoff, or one
    # whose timestamp will not parse, on a week that has already graded.
    for c in cards:
        c["betslip_url"] = (
            None if c.get("result")
            else card_mod.betslip_url(c.get("legs") or [])
        )

    graded_cards = [c for c in cards if c.get("result")]
    # Touchdowns are a different bet — plus money, a yes-only market — and
    # folding them into the prop totals would make every split below answer
    # two questions at once. They get their own block.
    tds = [r for r in rows if r.get("market") == TD_MARKET]
    rows = [r for r in rows if r.get("market") != TD_MARKET]
    return {
        "season": season,
        "overall": _bucket(rows),
        "touchdowns": {
            "overall": _bucket(tds),
            "by_edge": [
                {"edge": k, **_bucket(v)}
                for k, v in sorted(_split(tds, lambda r: _td_edge_bucket(
                    r.get("edge_pts"))).items())
            ],
            "by_week": sorted(
                ({"week": k, **_bucket(v)} for k, v in _by_week(tds).items()),
                key=lambda d: d["week"], reverse=True),
            "picks": tds[:200],
        },
        # Whether a pick sits under card.MAX_EDGE_PTS. Every pick recorded
        # since the cap shipped is below it by construction; the split is here
        # so the history the cap was fitted on stays visible beside it.
        "by_edge": [
            {"edge": k, **_bucket(v)}
            for k, v in sorted(_split(rows, lambda r: (
                "over cap" if (r.get("edge_pts") or 0) > card_mod.MAX_EDGE_PTS
                else "under cap")).items())
        ],
        # The same picks graded at the line we first saw against the line they
        # locked at. See _open_vs_lock, and card.LOCK_LEAD for the deadline.
        "open_vs_lock": _open_vs_lock(rows),
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


def _td_edge_bucket(edge) -> str:
    if edge is None:
        return "unknown"
    return "5-8" if edge < 8 else "8-12" if edge < 12 else "12+"


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


# --- the ledger ------------------------------------------------------------
#
# Every recommendation in one place, whatever produced it: the prop and
# touchdown picks from nfl_picks and the moneyline picks stored inside each
# game prediction's sim. One row per bet, one unit flat stake, graded at the
# price recorded with it. Model leans and the parlay card are not in here —
# leans were never recommended, and the card is built from picks that already
# are, so counting it would count them twice.

# The first week bet under the Finding 14 rules (edge cap, no rushing, no
# "market" verdict, TD and moneyline picks). Everything before it is the
# record those rules were written to fix, kept beside the new one rather than
# blended into it.
RULES_SINCE = (2026, 8)

MARKET_LABEL = {
    "receiving_yards": "rec yds", "receptions": "receptions",
    "rushing_yards": "rush yds", "passing_yards": "pass yds",
}


def _units(odds, result) -> Optional[float]:
    if result == "WIN":
        if not odds:
            return round(100 / 110, 3)
        return round(100 / -odds if odds < 0 else odds / 100, 3)
    if result == "LOSS":
        return -1.0
    if result in ("PUSH", "VOID"):
        return 0.0
    return None


def _prop_entry(r: dict) -> dict:
    td = r.get("market") == TD_MARKET
    kind = "touchdown" if td else "prop"
    label = (f"{r.get('player')} anytime TD" if td else
             f"{r.get('player')} {(r.get('side') or '').title()} {r.get('line')} "
             f"{MARKET_LABEL.get(r.get('market'), r.get('market'))}")
    return {
        "season": r["season"], "week": r["week"], "kickoff": r.get("kickoff"),
        "type": kind, "pick": label, "team": r.get("team"),
        "event": r.get("event"), "market": r.get("market"),
        "odds": r.get("fd_odds"), "model_p": r.get("model_p"),
        "edge_pts": r.get("edge_pts"), "result": r.get("result"),
        "actual": r.get("actual"),
    }


def _ml_entries(g: dict) -> list[dict]:
    sim = g.get("sim")
    if isinstance(sim, str):
        try:
            sim = json.loads(sim)
        except (TypeError, ValueError):
            sim = None
    if not isinstance(sim, dict):
        return []
    settled = g.get("home_score") is not None and g.get("away_score") is not None
    out = []
    for pk in sim.get("picks") or []:
        result = actual = None
        if settled:
            m = g["home_score"] - g["away_score"]
            m = m if pk.get("side") == "home" else -m
            result = "WIN" if m > 0 else "LOSS" if m < 0 else "PUSH"
            actual = f"{g['away_team']} {g['away_score']} – {g['home_score']} {g['home_team']}"
        out.append({
            "season": g["season"], "week": g["week"], "kickoff": g.get("kickoff"),
            "type": "moneyline", "pick": f"{pk.get('team')} ML",
            "team": pk.get("team"),
            "event": f"{g['away_team']} @ {g['home_team']}", "market": "moneyline",
            "odds": pk.get("odds"), "model_p": pk.get("p"),
            "edge_pts": pk.get("edge_pts"), "result": result, "actual": actual,
        })
    return out


def _ledger_bucket(rows: list[dict]) -> dict:
    """Record and units. ROI is on what was staked, so a 0.1u lotto counts as
    0.1u risked, not a full unit."""
    w = sum(1 for r in rows if r["result"] == "WIN")
    lo = sum(1 for r in rows if r["result"] == "LOSS")
    profit = sum(r["units"] for r in rows if r["units"] is not None)
    staked = sum(r.get("stake", 1.0) for r in rows if r["result"] in ("WIN", "LOSS"))
    return {
        "bets": len(rows), "wins": w, "losses": lo,
        "pushes": sum(1 for r in rows if r["result"] in ("PUSH", "VOID")),
        "pending": sum(1 for r in rows if not r["result"]),
        "hit_rate": round(100 * w / (w + lo), 1) if w + lo else None,
        "units": round(profit, 2),
        "staked": round(staked, 2),
        "roi": round(100 * profit / staked, 1) if staked else None,
    }


def _alt_entry(p: dict) -> dict:
    """One alt parlay as a ledger row: a single bet at its own stake."""
    name = {"double_up": "Alt double-up", "lotto": "Alt lotto"}.get(p["kind"], p["kind"])
    stake = float(p.get("stake") or 1.0)
    res = p.get("result")
    if res == "WIN":
        units = round(stake * ((p.get("settled_decimal") or p.get("decimal_odds") or 1) - 1), 3)
    elif res == "LOSS":
        units = -stake
    elif res in ("VOID", "PUSH"):
        units = 0.0
    else:
        units = None
    legs = p.get("legs") or []
    return {
        "season": p["season"], "week": p["week"],
        "kickoff": min((l.get("kickoff") or "" for l in legs), default=None) or None,
        "type": "parlay", "pick": f"{name}: {p.get('leg_count')} legs",
        "team": None, "event": None, "market": p["kind"],
        "odds": p.get("american"), "model_p": p.get("model_p"), "edge_pts": None,
        "result": res,
        "actual": (f"{p.get('legs_won')}/{p.get('legs_settled')} legs"
                   if p.get("legs_settled") is not None else None),
        "units": units, "stake": stake, "frozen": bool(p.get("frozen")),
        "legs": [{k: l.get(k) for k in ("player", "market", "threshold", "odds",
                                       "p_cal", "event", "kickoff")} for l in legs],
    }


async def recommendations(season: Optional[int] = None) -> dict:
    """Every recommendation, its result, and the record and ROI they add up to."""
    db = _require_db()
    entries = [_prop_entry(r) for r in await db.list_nfl_picks(season=season)]
    for g in await db.list_nfl_game_predictions(season):
        entries.extend(_ml_entries(g))
    for e in entries:
        e["units"] = _units(e["odds"], e["result"])
        e["current_rules"] = (e["season"], e["week"]) >= RULES_SINCE
    # Alt parlays carry their own stake and settled payout, so they join after
    # the flat-stake grading above rather than through it.
    for p in await db.list_nfl_alt_parlays(season):
        e = _alt_entry(p)
        e["current_rules"] = True
        entries.append(e)
    entries.sort(key=lambda e: (e["season"], e["week"], e.get("kickoff") or "",
                                e["pick"]), reverse=True)

    def summary(rows: list[dict]) -> dict:
        return {
            "overall": _ledger_bucket(rows),
            "by_type": {t: _ledger_bucket([r for r in rows if r["type"] == t])
                        for t in ("prop", "touchdown", "moneyline", "parlay")},
        }

    current = [e for e in entries if e["current_rules"]]
    weeks: dict = {}
    for e in entries:
        weeks.setdefault((e["season"], e["week"]), []).append(e)
    return {
        "season": season,
        "rules_since": {"season": RULES_SINCE[0], "week": RULES_SINCE[1]},
        "current": summary(current),
        "before": summary([e for e in entries if not e["current_rules"]]),
        "all_time": summary(entries),
        "by_week": [
            {"season": s, "week": w, "current_rules": (s, w) >= RULES_SINCE,
             **_ledger_bucket(rows),
             "by_type": {t: _ledger_bucket([r for r in rows if r["type"] == t])
                         for t in ("prop", "touchdown", "moneyline", "parlay")}}
            for (s, w), rows in sorted(weeks.items(), reverse=True)
        ],
        "ledger": entries,
    }
