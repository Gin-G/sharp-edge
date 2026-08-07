"""Bundle selection and the FanDuel bet-slip link."""

import pytest

from sharp_edge import bundle


def _pick(name, ev, odds, pitcher, market="708.1", selection="1",
          model_p=0.70, implied=0.66, event="e1"):
    return {
        "batter": name, "ev": ev, "fd_odds": odds, "pitcher_id": pitcher,
        "fd_market_id": market, "fd_selection_id": selection,
        "fd_event_id": event, "model_p": model_p, "implied_p": implied,
    }


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

def test_negative_ev_picks_are_dropped_however_good_the_matchup():
    """The 2026-08-07 lesson: the two best matchups on the board were priced
    -290 and -280 and were both losing bets."""
    rows = [
        _pick("great matchup bad price", -0.038, -290, 1, model_p=0.715),
        _pick("ordinary matchup fair price", +0.025, -185, 2, model_p=0.665),
    ]
    got = bundle.build(rows)
    assert [r["batter"] for r in got] == ["ordinary matchup fair price"]


def test_ranked_by_ev_not_by_probability():
    """A 71.5% pick at -290 is a worse bet than a 66.5% pick at -185."""
    rows = [
        _pick("high p", +0.005, -280, 1, model_p=0.72),
        _pick("high ev", +0.040, -220, 2, model_p=0.66),
    ]
    assert [r["batter"] for r in bundle.build(rows)] == ["high ev", "high p"]


def test_one_leg_per_game_by_default():
    """57% of naive top-2 bundles were two batters facing the same starter —
    a same-game parlay, which a book prices below the product of its legs."""
    rows = [
        _pick("a", +0.05, -200, pitcher=99),
        _pick("b", +0.04, -200, pitcher=99),   # same starter
        _pick("c", +0.01, -200, pitcher=77),
    ]
    got = bundle.build(rows, max_legs=3)
    assert [r["batter"] for r in got] == ["a", "c"]

    same_game = bundle.build(rows, max_legs=3, cross_game=False)
    assert [r["batter"] for r in same_game] == ["a", "b", "c"]


def test_falls_back_to_the_event_when_the_pitcher_is_unknown():
    rows = [
        _pick("a", +0.05, -200, pitcher=None, event="game1"),
        _pick("b", +0.04, -200, pitcher=None, event="game1"),
        _pick("c", +0.03, -200, pitcher=None, event="game2"),
    ]
    assert [r["batter"] for r in bundle.build(rows)] == ["a", "c"]


def test_max_legs_is_respected():
    rows = [_pick(f"p{i}", 0.05 - i * 0.001, -200, pitcher=i) for i in range(10)]
    assert len(bundle.build(rows, max_legs=3)) == 3


def test_picks_without_a_market_cannot_enter_a_bundle():
    """A leg with no FanDuel ids can't be put on the slip, so it must not be
    counted in the parlay maths either."""
    rows = [
        {"batter": "no market", "ev": None, "fd_odds": None, "pitcher_id": 1,
         "fd_market_id": None, "fd_selection_id": None},
        _pick("priced", +0.02, -200, 2),
    ]
    assert [r["batter"] for r in bundle.build(rows)] == ["priced"]


# --------------------------------------------------------------------------
# Bet-slip link
# --------------------------------------------------------------------------

def test_betslip_url_uses_literal_brackets():
    """Percent-encoded brackets are the difference between a loaded slip and
    a shrug."""
    url = bundle.betslip_url([_pick("a", 0.02, -200, 1, "708.42", "999")])
    assert "marketId[0]=708.42" in url
    assert "selectionId[0]=999" in url
    assert "%5B" not in url


def test_betslip_url_indexes_each_leg():
    legs = [
        _pick("a", 0.03, -200, 1, "708.1", "11"),
        _pick("b", 0.02, -200, 2, "708.2", "22"),
    ]
    url = bundle.betslip_url(legs)
    for frag in ("marketId[0]=708.1", "selectionId[0]=11",
                 "marketId[1]=708.2", "selectionId[1]=22"):
        assert frag in url


def test_betslip_url_is_state_specific():
    """A link built for the wrong state bounces the user."""
    legs = [_pick("a", 0.02, -200, 1)]
    assert bundle.betslip_url(legs, state="NJ").startswith(
        "https://nj.sportsbook.fanduel.com/addToBetslip?"
    )


def test_no_link_rather_than_a_broken_one():
    assert bundle.betslip_url([]) is None
    assert bundle.betslip_url([{"batter": "x"}]) is None


# --------------------------------------------------------------------------
# Parlay maths
# --------------------------------------------------------------------------

def test_summarise_multiplies_legs():
    legs = [
        _pick("a", 0.02, -200, 1, model_p=0.70, implied=0.667),
        _pick("b", 0.02, -200, 2, model_p=0.70, implied=0.667),
    ]
    s = bundle.summarise(legs)
    assert s["legs"] == 2
    assert s["decimal"] == pytest.approx(2.25, abs=1e-3)   # 1.5 * 1.5
    assert s["model_p"] == pytest.approx(0.49, abs=1e-3)   # 0.7 * 0.7
    # 0.49 * 1.25 - 0.51
    assert s["ev"] == pytest.approx(0.1025, abs=1e-3)
    assert s["american"] == 125


def test_summarise_of_an_empty_bundle_is_not_a_crash():
    s = bundle.summarise([])
    assert s["legs"] == 0 and s["ev"] is None
