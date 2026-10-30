"""The alt-line parlays: leg rules, the exact build, recording and grading."""

import asyncio
import json
import math

import pytest

from sharp_edge.nfl import altparlay, tracking

FUTURE = "2099-11-01T17:00:00.000Z"
PAST = "2020-11-01T17:00:00.000Z"


def _rung(**kw):
    base = {"market": "receiving_yards", "player": "A Receiver", "key": "a receiver",
            "team": "AAA", "line": 24.5, "threshold": 25.0, "side": "OVER",
            "odds": -200, "last4": [30.0, 40.0, 26.0, 51.0], "hits4": 4,
            "edge_pts": 5.0, "p_cal": 0.68, "model_p": 0.72, "avail": "ACTIVE",
            "role_conflict_verdict": None, "fd_event_id": "g1", "kickoff": FUTURE,
            "fd_market_id": "m", "over_selection_id": 1}
    base.update(kw)
    return base


# --- pricing ---------------------------------------------------------------

def test_a_rung_is_fair_at_its_quote_less_the_main_lines_vig():
    # Main line -114 both ways: implied .533, fair .500, so the vig ratio is .938.
    fair = altparlay.rung_fair(-300, -114, 0.5)
    assert fair == pytest.approx(0.75 * 0.5 / (114 / 214), abs=1e-6)
    assert altparlay.rung_fair(-300, None, 0.5) is None


def test_the_calibrated_chance_keeps_only_a_fraction_of_the_models_view():
    got = altparlay.price_rung(-200, p_shrunk=0.80, fair=0.62)
    assert got["edge_pts"] == 18.0
    assert 0.62 < got["p_cal"] < 0.66          # 15% of the disagreement, in logits


# --- leg rules -------------------------------------------------------------

@pytest.mark.parametrize("change", [
    {"hits4": 2},                                   # not hitting
    {"last4": [30.0, 40.0, 26.0]},                  # not four games of record
    {"market": "rushing_yards"},                    # not a bettable market
    {"edge_pts": 2.9}, {"edge_pts": 12.1},          # outside the props' over bar
    {"role_conflict_verdict": "market"},            # the depth chart sides with the book
    {"avail": "OUT"},
    {"kickoff": PAST},                              # inside its lock
])
def test_a_leg_must_clear_every_rule(change):
    assert len(altparlay.candidates([_rung()])) == 1
    assert altparlay.candidates([_rung(**change)]) == []


# --- the build -------------------------------------------------------------

def test_the_double_up_is_the_likeliest_ticket_that_pays_even_money():
    # Greedy-by-ratio took the long leg first and overshot to +283. The exact
    # build pairs two short legs that just clear 2.0 and are likelier together.
    legs = [
        _rung(key="long", fd_event_id="g1", odds=104, p_cal=0.472),
        _rung(key="s1", fd_event_id="g2", odds=-114, p_cal=0.513),
        _rung(key="s2", fd_event_id="g3", odds=-450, p_cal=0.786),
        _rung(key="s3", fd_event_id="g4", odds=-154, p_cal=0.582),
    ]
    t = altparlay.build(legs, "double_up")
    assert t["decimal_odds"] >= 2.0
    assert {l["key"] for l in t["legs"]} == {"s2", "s3"}
    best = max(a["p_cal"] * b["p_cal"] for a in legs for b in legs
               if a is not b and a["fd_event_id"] != b["fd_event_id"]
               and altparlay._decimal(a["odds"]) * altparlay._decimal(b["odds"]) >= 2.0)
    assert t["model_p"] == pytest.approx(best, abs=1e-6)


def test_a_ticket_takes_one_leg_per_game():
    legs = [_rung(key=f"p{i}", fd_event_id="g1", odds=-110, p_cal=0.55) for i in range(4)]
    assert altparlay.build(legs, "double_up") is None       # all from one game
    legs[1]["fd_event_id"] = "g2"
    t = altparlay.build(legs, "double_up")
    assert len({l["fd_event_id"] for l in t["legs"]}) == t["leg_count"] == 2


def test_the_lotto_is_strict_about_its_target():
    few = [_rung(key=f"p{i}", fd_event_id=f"g{i}", odds=150, p_cal=0.42) for i in range(5)]
    assert altparlay.build(few, "lotto") is None             # 2.5^5 = 98x
    many = [_rung(key=f"p{i}", fd_event_id=f"g{i}", odds=150, p_cal=0.42) for i in range(8)]
    t = altparlay.build(many, "lotto")
    assert t["decimal_odds"] >= 1000 and t["stake"] == 0.1
    assert t["to_win"] == pytest.approx(0.1 * (t["decimal_odds"] - 1), abs=0.01)


# --- grading ---------------------------------------------------------------

def test_a_parlay_grades_like_the_book():
    legs = [_rung(key="a", team="AAA", odds=-200, line=24.5),
            _rung(key="b", team="BBB", odds=150, line=39.5)]
    rows = {"a": {"receiving_yards": 31.0}, "b": {"receiving_yards": 52.0}}
    assert tracking._grade_alt_parlay(legs, rows, {"AAA", "BBB"})[0] == "WIN"
    assert tracking._grade_alt_parlay(legs, rows, {"AAA", "BBB"})[3] == pytest.approx(1.5 * 2.5)
    # One loss decides it, even with the other game still to play.
    lost = {"a": {"receiving_yards": 10.0}}
    assert tracking._grade_alt_parlay(legs, lost, {"AAA"})[0] == "LOSS"
    # A win waits for every game.
    assert tracking._grade_alt_parlay(legs, {"a": rows["a"]}, {"AAA"})[0] is None
    # A leg that did not play drops out and takes its price with it.
    res, won, graded, dec = tracking._grade_alt_parlay(legs, {"a": rows["a"]}, {"AAA", "BBB"})
    assert (res, won, graded, dec) == ("WIN", 1, 1, 1.5)


# --- recording -------------------------------------------------------------

def test_a_parlay_rebuilds_until_its_first_leg_locks_then_freezes(tmp_path):
    from sharp_edge.db.sqlite import SQLiteDatabase

    async def run():
        db = SQLiteDatabase(str(tmp_path / "t.db"))
        await db.connect()
        tracking.configure(db)
        try:
            first = altparlay.summarise([_rung(key="a", odds=-200, p_cal=0.68),
                                         _rung(key="b", fd_event_id="g2", odds=-110,
                                               p_cal=0.54)], "double_up")
            assert (await tracking.record_alt_parlays(2026, 8, {"double_up": first})
                    )["double_up"] == "written"
            second = altparlay.summarise([_rung(key="c", odds=-150, p_cal=0.62),
                                          _rung(key="d", fd_event_id="g2", odds=-120,
                                                p_cal=0.57)], "double_up")
            await tracking.record_alt_parlays(2026, 8, {"double_up": second})
            (row,) = await db.list_nfl_alt_parlays(2026, 8)
            assert [l["key"] for l in row["legs"]] == ["c", "d"] and not row["frozen"]

            # Its first leg's lock passes: frozen, and a new build cannot replace it.
            from datetime import datetime, timezone
            later = datetime(2099, 11, 1, 16, 0, tzinfo=timezone.utc)
            out = await tracking.record_alt_parlays(2026, 8, {"double_up": first}, now=later)
            assert out["double_up"] == "frozen"
            (row,) = await db.list_nfl_alt_parlays(2026, 8)
            assert row["frozen"] and [l["key"] for l in row["legs"]] == ["c", "d"]
            assert await db.upsert_nfl_alt_parlay({
                "season": 2026, "week": 8, "kind": "double_up", "legs": "[]",
                "leg_count": 0, "stake": 1.0}) is False

            # Settled, it joins the ledger at its own stake.
            await db.settle_nfl_alt_parlay(2026, 8, "double_up", "WIN", 2, 2, 2.1)
            led = await tracking.recommendations(2026)
            e = next(x for x in led["ledger"] if x["type"] == "parlay")
            assert e["units"] == pytest.approx(1.1) and e["stake"] == 1.0
            assert led["current"]["by_type"]["parlay"]["roi"] == pytest.approx(110.0)
        finally:
            tracking.configure(None)
            await db.close()

    asyncio.run(run())


def test_the_ledger_reads_a_lotto_at_its_tenth_of_a_unit():
    p = {"season": 2026, "week": 8, "kind": "lotto", "stake": 0.1, "result": "LOSS",
         "legs": [], "leg_count": 10, "american": 120000, "decimal_odds": 1201.0}
    e = tracking._alt_entry(p)
    assert e["units"] == -0.1
    b = tracking._ledger_bucket([{**e, "current_rules": True}])
    assert b["staked"] == 0.1 and b["roi"] == -100.0


# --- placed bets -------------------------------------------------------------

def _placed(**kw):
    base = {"season": 2026, "week": 8, "label": "Alt double-up", "source": "alt:double_up",
            "american": 101, "stake": 1.0, "legs": [
                {"player": "Bhayshul Tuten", "key": "bhayshul tuten", "team": "JAX",
                 "market": "receiving_yards", "line": 4.5, "threshold": 5.0,
                 "side": "OVER", "odds": -450},
                {"player": "Rhamondre Stevenson", "key": "rhamondre stevenson",
                 "team": "NE", "market": "receiving_yards", "line": 14.5,
                 "threshold": 15.0, "side": "OVER", "odds": -154}]}
    base.update(kw)
    return base


def test_a_placed_bet_is_kept_as_bet_and_settles_at_the_price_taken(tmp_path):
    from sharp_edge.db.sqlite import SQLiteDatabase

    async def run():
        db = SQLiteDatabase(str(tmp_path / "t.db"))
        await db.connect()
        tracking.configure(db)
        try:
            out = await tracking.record_placed(_placed())
            assert out["american"] == 101 and out["decimal_odds"] == 2.01
            # The board moving on does not touch it.
            await tracking.record_alt_parlays(2026, 8, {"double_up": altparlay.summarise(
                [_rung(key="other", odds=-186, p_cal=0.6),
                 _rung(key="x", fd_event_id="g2", odds=-320, p_cal=0.7)], "double_up")})
            (b,) = await db.list_nfl_placed_bets(2026, 8)
            assert [l["key"] for l in b["legs"]] == ["bhayshul tuten", "rhamondre stevenson"]

            # Both clear: paid at +101, not at the product of the legs (+102).
            rows = {"bhayshul tuten": {"receiving_yards": 22.0},
                    "rhamondre stevenson": {"receiving_yards": 31.0}}
            assert await tracking._settle_placed(2026, 8, rows, {"JAX", "NE"}) == 1
            rec = await tracking.placed_record(2026)
            assert rec["bets"][0]["result"] == "WIN"
            assert rec["bets"][0]["units"] == pytest.approx(1.01)
            assert rec["summary"]["roi"] == pytest.approx(101.0)
            # Settled: no undo, no stake change.
            assert await db.delete_nfl_placed_bet(b["id"]) is False
            assert await db.update_nfl_placed_stake(b["id"], 2.0) is False
        finally:
            tracking.configure(None)
            await db.close()

    asyncio.run(run())


def test_a_placement_that_cannot_settle_is_refused():
    with pytest.raises(ValueError):
        tracking._placed_row(_placed(legs=[]))
    with pytest.raises(ValueError):
        tracking._placed_row(_placed(legs=[{"player": "X", "market": "moneyline", "line": 0}]))
    with pytest.raises(ValueError):
        tracking._placed_row(_placed(stake=0))
    # A price can be left off; the legs supply it.
    row = tracking._placed_row(_placed(american=None))
    assert row["american"] == round((1 + 100 / 450) * (1 + 100 / 154) * 100 - 100)


def test_an_under_leg_settles_as_an_under():
    legs = [_rung(key="u", team="AAA", side="UNDER", line=40.5, odds=-110)]
    assert tracking._grade_alt_parlay(legs, {"u": {"receiving_yards": 22.0}}, {"AAA"})[0] == "WIN"


# --- paper strategies ---------------------------------------------------------

def _anchor(**kw):
    base = {"anchor": "td", "market": "anytime_td", "player": "A Scorer",
            "key": "a scorer", "team": "AAA", "line": 0.5, "threshold": 1.0,
            "side": "OVER", "odds": 300, "p_cal": 0.30, "fd_event_id": "g1",
            "kickoff": FUTURE}
    base.update(kw)
    return base


def test_the_short_stack_takes_every_games_likeliest_short_rung():
    legs = [_rung(key=f"p{i}", fd_event_id=f"g{i}", odds=-150, p_cal=0.6) for i in range(5)]
    legs += [_rung(key="better", fd_event_id="g0", odds=-200, p_cal=0.7),
             _rung(key="too_long", fd_event_id="g9", odds=150, p_cal=0.45)]
    t = altparlay.stack(legs)
    assert t["leg_count"] == 5 and "better" in {l["key"] for l in t["legs"]}
    assert "too_long" not in {l["key"] for l in t["legs"]} and t["stake"] == 0.1
    assert altparlay.stack(legs[:3]) is None                # under four games


def test_the_anchored_lotto_is_short_legs_with_at_most_two_capped_anchors():
    short = [_rung(key=f"s{i}", fd_event_id=f"g{i}", odds=-140, p_cal=0.6) for i in range(12)]
    anchors = [_anchor(key=f"a{i}", fd_event_id=f"g{i}", odds=400, p_cal=0.25)
               for i in range(4)]
    anchors.append(_anchor(key="moon", fd_event_id="g5", odds=1600, p_cal=0.2))
    t = altparlay.build(short, "anchored", anchors)
    assert t["decimal_odds"] >= 1000
    used = [l for l in t["legs"] if l.get("anchor")]
    assert len(used) <= 2 and all(100 <= l["odds"] <= 400 for l in used)
    assert len({l["fd_event_id"] for l in t["legs"]}) == t["leg_count"]
    # The official lotto never takes an anchor: on these rungs alone it cannot
    # reach 1000x, which the anchors would have carried it to.
    assert altparlay.build(short, "lotto", anchors) is None


def test_a_moneyline_anchor_finds_its_game_through_either_team():
    gm = [{"home_team": "BBB", "away_team": "AAA", "event": "AAA @ BBB", "kickoff": FUTURE,
           "sim": {"picks": [{"market": "moneyline", "side": "away", "team": "AAA",
                              "odds": 172, "p": 0.39, "edge_pts": 2.3}]}},
          {"home_team": "CCC", "away_team": "DDD", "kickoff": FUTURE,
           "sim": {"picks": [{"market": "moneyline", "side": "home", "team": "CCC",
                              "odds": -126, "p": 0.58}]}}]          # short: not an anchor
    (leg,) = altparlay.anchor_legs([], gm, {"BBB": "g7"})
    assert leg["fd_event_id"] == "g7" and leg["anchor"] == "ml" and leg["p_cal"] == 0.39


def test_paper_tickets_are_one_per_strategy_per_day_and_settle_leg_by_leg(tmp_path):
    from datetime import datetime, timezone

    from sharp_edge.db.sqlite import SQLiteDatabase

    async def run():
        db = SQLiteDatabase(str(tmp_path / "t.db"))
        await db.connect()
        tracking.configure(db)
        try:
            ml = {**_anchor(anchor="ml", market="moneyline", player="BBB", key="ml:BBB",
                            team="BBB", side="home", odds=150, p_cal=0.45,
                            fd_event_id="g2"), "line": 0.0}
            first = altparlay.summarise([_rung(key="a", odds=-200, p_cal=0.68), ml],
                                        "anchored")
            second = altparlay.summarise([_rung(key="c", odds=-150, p_cal=0.62), ml],
                                         "anchored")
            fri = datetime(2026, 10, 30, 18, 0, tzinfo=timezone.utc)
            sat = datetime(2026, 10, 31, 18, 0, tzinfo=timezone.utc)
            out = await tracking.record_paper(2026, 8, {"anchored": first}, now=fri)
            assert out["anchored"] == "written" and out["short_stack"] == "none"
            # Later the same day the board moves: that day's ticket follows it.
            await tracking.record_paper(2026, 8, {"anchored": second}, now=fri)
            # A new day is a new ticket; Friday's stands as it was last shown.
            await tracking.record_paper(2026, 8, {"anchored": first}, now=sat)
            rows = await db.list_nfl_paper_tickets(2026, 8)
            assert [(r["day"], r["legs"][0]["key"]) for r in rows] == [
                ("2026-10-31", "a"), ("2026-10-30", "c")]

            # Player legs are graded; the moneyline waits for the final score.
            actuals = {"a": {"receiving_yards": 10.0}, "c": {"receiving_yards": 30.0}}
            assert await tracking._settle_paper(2026, 8, actuals, {"AAA", "BBB"}) == 0
            await db.upsert_nfl_game_predictions([{
                "season": 2026, "week": 8, "home_team": "BBB", "away_team": "ZZZ"}])
            await db.settle_nfl_game(2026, 8, "BBB", "ZZZ", 27, 20)
            assert await tracking._settle_paper(2026, 8, actuals, {"AAA", "BBB"}) == 2

            rec = await tracking.paper_record(2026)
            s = next(x for x in rec["strategies"] if x["strategy"] == "anchored")
            assert (s["summary"]["wins"], s["summary"]["losses"]) == (1, 1)
            fri_t = next(t for t in s["tickets"] if t["day"] == "2026-10-30")
            assert fri_t["result"] == "WIN"
            assert fri_t["units"] == pytest.approx(0.1 * (1.6667 * 2.5 - 1), abs=0.01)
            assert [l["result"] for l in fri_t["legs"]] == ["WIN", "WIN"]
            assert s["legs"]["ml"]["won"] == 2 and s["legs"]["rung"]["hit_rate"] == 50.0
        finally:
            tracking.configure(None)
            await db.close()

    asyncio.run(run())


def test_a_days_ticket_stands_once_a_leg_has_locked(tmp_path):
    from datetime import datetime, timezone

    from sharp_edge.db.sqlite import SQLiteDatabase

    async def run():
        db = SQLiteDatabase(str(tmp_path / "t.db"))
        await db.connect()
        tracking.configure(db)
        try:
            early = "2026-11-01T18:00:00.000Z"
            first = altparlay.summarise([_rung(key="a", kickoff=early, p_cal=0.6),
                                         _rung(key="b", fd_event_id="g2", p_cal=0.6)],
                                        "short_stack")
            morning = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
            await tracking.record_paper(2026, 8, {"short_stack": first}, now=morning)
            later = datetime(2026, 11, 1, 18, 30, tzinfo=timezone.utc)
            out = await tracking.record_paper(2026, 8, {"short_stack": altparlay.summarise(
                [_rung(key="z", p_cal=0.6)], "short_stack")}, now=later)
            assert out["short_stack"] == "kept"
            (row,) = await db.list_nfl_paper_tickets(2026, 8)
            assert [l["key"] for l in row["legs"]] == ["a", "b"]
        finally:
            tracking.configure(None)
            await db.close()

    asyncio.run(run())
