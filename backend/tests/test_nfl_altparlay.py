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
