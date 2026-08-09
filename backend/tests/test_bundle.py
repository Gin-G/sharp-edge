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

def test_negative_ev_picks_are_dropped_however_good_the_matchup():
    """The 2026-08-07 lesson: the two best matchups on the board were priced
    -290 and -280 and were both losing bets."""
    rows = [
        _pick("great matchup bad price", None, -290, 1, model_p=0.715),
        _pick("ordinary matchup fair price", None, -185, 2, model_p=0.715),
    ]
    got = bundle.build(rows)
    assert [r["batter"] for r in got] == ["ordinary matchup fair price"]


def test_ranked_by_ev_not_by_probability():
    """Both clear the edge gate; the one with the *lower* win probability is
    the better bet because its price is longer."""
    rows = [
        _pick("high p", None, -280, 1, model_p=0.79),   # edge 5.3, EV +0.072
        _pick("high ev", None, -220, 2, model_p=0.75),  # edge 6.3, EV +0.091
    ]
    got = bundle.build(rows)
    assert [r["batter"] for r in got] == ["high ev", "high p"]
    assert got[0]["model_p"] < got[1]["model_p"]


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


# --------------------------------------------------------------------------
# Minimum edge threshold
# --------------------------------------------------------------------------

def _edged(name, edge_pts, ev, odds, pitcher, model_p=0.70):
    """A pick with an exact edge, by choosing the implied probability."""
    imp = model_p - edge_pts / 100.0
    r = _pick(name, ev, odds, pitcher, model_p=model_p, implied=imp)
    return r


def test_barely_positive_edges_are_rejected():
    """The model's level is off by ~1.7 points on held-out picks, so a
    half-point edge is indistinguishable from zero — betting it means paying
    the vig to act on rounding."""
    from sharp_edge import pricing
    rows = [
        _edged("rounding error", 0.5, +0.008, -200, 1),
        _edged("real edge", 4.0, +0.060, -200, 2),
    ]
    got = bundle.build(rows)
    assert [r["batter"] for r in got] == ["real edge"]
    assert pricing.MIN_EDGE_PTS == 3.0


def test_the_threshold_is_on_edge_not_ev():
    """EV and edge agree on sign but not on magnitude — the same EV is a
    different edge at a different price, and the calibration error is
    denominated in edge."""
    # A long price turns a small edge into a big EV; the gate must still bite.
    small_edge_big_ev = _edged("longshot", 1.5, +0.30, 400, 1, model_p=0.25)
    assert bundle.build([small_edge_big_ev]) == []


def test_threshold_is_overridable():
    rows = [_edged("marginal", 1.0, +0.02, -200, 1)]
    assert bundle.build(rows) == []
    assert len(bundle.build(rows, min_edge_pts=0.5)) == 1


def test_near_misses_report_the_price_that_would_clear_the_threshold():
    """Break-even isn't the useful number once the gate is 3 points above it."""
    rows = [_edged("close", 2.0, None, -250, 1, model_p=0.70)]
    misses = bundle.near_misses(rows, [])
    assert len(misses) == 1
    m = misses[0]
    assert m["short_by"] == 1.0
    # A 3-point edge needs implied down to 67%, which is -203. For a
    # favourite that is a *longer* price than the -250 on offer, i.e. nearer
    # zero.
    assert m["needs"] == -203
    assert m["needs"] > -250


def test_near_misses_are_ordered_by_how_close_they_came():
    rows = [
        _edged("far", -4.0, -0.05, -300, 1),
        _edged("closest", 2.5, +0.04, -200, 2),
        _edged("middling", 0.0, 0.0, -220, 3),
    ]
    assert [m["batter"] for m in bundle.near_misses(rows, [])] == \
        ["closest", "middling", "far"]
