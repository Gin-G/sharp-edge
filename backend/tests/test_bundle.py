"""Bundle selection and the FanDuel bet-slip link."""

import pytest

from sharp_edge import bundle


def _pick(name, ev, odds, pitcher, market="708.1", selection="1",
          model_p=0.70, implied=None, event="e1"):
    """A priced pick, with implied/edge/EV derived from the odds.

    Deriving rather than passing them keeps fixtures self-consistent — an edge
    and an EV that disagree on sign is not a state the real pipeline can
    produce, and a test built on one proves nothing. ``ev`` is accepted and
    ignored for call-site readability.
    """
    from sharp_edge import pricing
    from sharp_edge.fanduel.odds import american_to_implied
    imp = american_to_implied(odds) if implied is None else implied
    return {
        "batter": name, "fd_odds": odds, "pitcher_id": pitcher,
        "fd_market_id": market, "fd_selection_id": selection,
        "fd_event_id": event, "model_p": model_p, "implied_p": round(imp, 4),
        "edge_pts": round(100 * (model_p - imp), 1),
        "ev": round(pricing.expected_value(model_p, odds), 4),
    }


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

def test_price_does_not_veto_a_pick():
    """The reversal. A short price is worth seeing, but a pick is a pick
    because we think the man gets a hit — the market doesn't get to choose
    the card, and EV gating meant a leg vanished when a line moved a cent."""
    rows = [
        _pick("dear but likely", None, -330, 1, model_p=0.72),
        _pick("cheap and likely", None, -150, 2, model_p=0.72),
    ]
    got = bundle.build(rows)
    assert {r["batter"] for r in got} == {"dear but likely", "cheap and likely"}
    # The -330 leg is plainly -EV and still made the card.
    assert min(r["ev"] for r in got) < 0


def test_ranked_by_probability_of_a_hit():
    """Ranked by the thing being bet, not by what it pays."""
    rows = [
        _pick("most likely", None, -280, 1, model_p=0.79),
        _pick("better priced", None, -150, 2, model_p=0.68),
    ]
    got = bundle.build(rows)
    assert [r["batter"] for r in got] == ["most likely", "better priced"]
    # ...even though the second is the better price by EV.
    assert got[1]["ev"] > got[0]["ev"]


def test_a_price_floor_is_still_available_on_request():
    """Not the default any more, but the machinery remains for anyone who
    wants it."""
    rows = [
        _pick("dear", None, -330, 1, model_p=0.72),
        _pick("cheap", None, -150, 2, model_p=0.72),
    ]
    assert len(bundle.build(rows)) == 2
    assert [r["batter"] for r in bundle.build(rows, min_edge_pts=3.0)] == ["cheap"]


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
    rows = [_pick(f"p{i}", None, -200, pitcher=i, model_p=0.7 - i * 0.01)
            for i in range(10)]
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


# --------------------------------------------------------------------------
# Near misses
# --------------------------------------------------------------------------

def _board_row(name, model_p, pitcher, is_pick=True):
    return {"batter": name, "opposing_pitcher": f"sp{pitcher}",
            "pitcher_id": pitcher, "model_p": model_p, "fd_odds": -200,
            "ev": 0.05, "edge_pts": 3.3,
            "is_hot": is_pick, "hittable_sp_edge": is_pick,
            "bvp_edge": False, "hand_slump_edge": False, "p_sharp": False}


def test_near_misses_come_from_the_board_not_the_picks():
    """The screen truncates picks to the day's best few, so anything it
    dropped is only visible on the board."""
    board = [_board_row("chosen", 0.75, 1), _board_row("fourth", 0.70, 2),
             _board_row("fifth", 0.68, 3), _board_row("not a pick", 0.9, 4,
                                                      is_pick=False)]
    chosen = [board[0]]
    misses = bundle.near_misses(board, chosen)
    assert [m["batter"] for m in misses] == ["fourth", "fifth"]


def test_near_misses_are_ranked_by_probability():
    board = [_board_row("low", 0.60, 1), _board_row("high", 0.72, 2),
             _board_row("mid", 0.66, 3)]
    assert [m["batter"] for m in bundle.near_misses(board, [])] == \
        ["high", "mid", "low"]
