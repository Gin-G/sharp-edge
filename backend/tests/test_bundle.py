"""Bundle selection and the FanDuel bet-slip link."""

import pytest

from sharp_edge import bundle

# Fixtures say "a hair above the bar" / "clearly below it", never a literal
# probability. The bar has moved once already — MIN_MODEL_P went 0.72 -> 0.68
# when vs_hand_avg started being regressed by its sample size and the whole
# scale compressed — and every test that hard-coded 0.72 broke without a
# single one of them being about that number.
BAR = bundle.MIN_MODEL_P


def _pick(name, ev, odds, pitcher, market="708.1", selection="1",
          model_p=BAR - 0.02, implied=None, event="e1"):
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
    # All above MIN_MODEL_P, so the bar is not what decides the length here.
    rows = [
        _pick("a", +0.05, -150, pitcher=99, model_p=0.78),
        _pick("b", +0.04, -150, pitcher=99, model_p=0.77),   # same starter
        _pick("c", +0.01, -150, pitcher=77, model_p=0.76),
    ]
    got = bundle.build(rows, max_legs=3)
    assert [r["batter"] for r in got] == ["a", "c"]

    # Without the rule, the two same-game batters both make the card.
    same_game = bundle.build(rows, max_legs=3, cross_game=False)
    assert [r["batter"] for r in same_game] == ["a", "b", "c"]


def test_falls_back_to_the_event_when_the_pitcher_is_unknown():
    rows = [
        _pick("a", +0.05, -200, pitcher=None, event="game1"),
        _pick("b", +0.04, -200, pitcher=None, event="game1"),
        _pick("c", +0.03, -200, pitcher=None, event="game2"),
    ]
    assert [r["batter"] for r in bundle.build(rows)] == ["a", "c"]


def test_length_comes_from_the_bar_not_a_cap():
    """Every batter at or above MIN_MODEL_P goes on, so a strong board makes a
    long card and a thin one makes two."""
    assert bundle.MAX_LEGS is None
    strong = [_pick(f"p{i}", None, -150, pitcher=i, model_p=BAR + 0.08 - i * 0.001)
              for i in range(5)]
    assert len(bundle.build(strong)) == 5
    thin = [_pick(f"q{i}", None, -150, pitcher=i, model_p=BAR - 0.02 - i * 0.001)
            for i in range(5)]
    assert len(bundle.build(thin)) == bundle.MIN_LEGS


def test_a_leg_below_the_bar_never_joins_a_card_that_already_has_two():
    """Below the bar the card just gets longer without getting better, and
    sweep multiplies."""
    rows = [
        _pick("a", None, -150, pitcher=1, model_p=BAR + 0.06),
        _pick("b", None, -150, pitcher=2, model_p=BAR + 0.03),
        _pick("c", None, -150, pitcher=3, model_p=BAR - 0.0001),
    ]
    assert [r["batter"] for r in bundle.build(rows)] == ["a", "b"]
    rows[2]["model_p"] = BAR           # exactly on the bar qualifies
    assert len(bundle.build(rows)) == 3


def test_the_bar_is_overridable():
    rows = [_pick(f"p{i}", None, -150, pitcher=i, model_p=BAR - 0.01 - i * 0.001)
            for i in range(4)]
    assert len(bundle.build(rows)) == bundle.MIN_LEGS
    assert len(bundle.build(rows, min_model_p=BAR - 0.02)) == 4


def test_an_explicit_cap_is_still_honoured():
    """Nothing sets one now, but callers that pass a ceiling get it."""
    rows = [_pick(f"p{i}", None, -150, pitcher=i, model_p=BAR + 0.08 - i * 0.005)
            for i in range(10)]
    assert len(bundle.build(rows, max_legs=3)) == 3
    # ...and a ceiling below the two-leg floor still binds
    assert len(bundle.build(rows, max_legs=1)) == 1


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


def test_betslip_url_uses_the_host_that_opens_the_app():
    """Measured on a phone: this host opens the FanDuel app straight into the
    loaded slip, while the state subdomain lands on the mobile website and
    makes you press a second "open in app" button. No state in the path —
    the session resolves it."""
    legs = [_pick("a", 0.02, -200, 1)]
    url = bundle.betslip_url(legs)
    assert url.startswith("https://account.sportsbook.fanduel.com/sportsbook/addToBetslip?")
    assert "co.sportsbook" not in url


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
            "bvp_edge": False, "p_sharp": False}


def test_near_misses_come_from_the_board_not_the_picks():
    """The card is two legs, so everything else is only visible on the board."""
    board = [_board_row("chosen", 0.75, 1), _board_row("third", 0.70, 2),
             _board_row("fourth", 0.68, 3)]
    chosen = [board[0]]
    misses = bundle.near_misses(board, chosen)
    assert [m["batter"] for m in misses] == ["third", "fourth"]


def test_near_misses_ignore_the_retired_screen_tags():
    """The tags no longer select anything, so an untagged row is a legitimate
    runner-up — and used to be invisible here."""
    board = [_board_row("chosen", 0.75, 1),
             _board_row("untagged but likelier", 0.90, 2, is_pick=False),
             _board_row("tagged", 0.68, 3)]
    misses = bundle.near_misses(board, [board[0]])
    assert [m["batter"] for m in misses] == ["untagged but likelier", "tagged"]


def test_near_misses_take_one_batter_per_game():
    """Same rule that chose the card, so these are the actual runners-up: a
    second batter off a game already on the ticket isn't an alternative to it."""
    board = [_board_row("chosen", 0.75, 1),
             _board_row("same game as chosen", 0.74, 1),
             _board_row("other game", 0.70, 2),
             _board_row("also other game", 0.69, 2)]
    misses = bundle.near_misses(board, [board[0]])
    assert [m["batter"] for m in misses] == ["other game"]


def test_near_misses_are_ranked_by_probability():
    board = [_board_row("low", 0.60, 1), _board_row("high", 0.72, 2),
             _board_row("mid", 0.66, 3)]
    assert [m["batter"] for m in bundle.near_misses(board, [])] == \
        ["high", "mid", "low"]


def test_the_first_two_legs_are_the_two_most_likely():
    """Ranks 1 and 2 are the only ones the model can actually separate — 78.9%
    and 75.8% over 129 days, against a flat ~70% below them — so they are
    taken on probability and taken first."""
    rows = [_pick(f"p{i}", None, -200, pitcher=i, model_p=BAR - i * 0.001)
            for i in range(8)]
    got = bundle.build(rows)
    assert [r["batter"] for r in got][:2] == ["p0", "p1"]
    assert bundle.MIN_LEGS == 2


def test_the_floor_holds_when_nothing_else_qualifies():
    """A thin slate still produces a parlay, not a single."""
    rows = [
        _pick("top1", None, -200, pitcher=1, model_p=BAR),
        _pick("top2", None, -200, pitcher=2, model_p=BAR - 0.01),
        _pick("dear", None, -600, pitcher=3, model_p=BAR - 0.02),
    ]
    got = bundle.build(rows)
    assert [r["batter"] for r in got] == ["top1", "top2"]


def test_a_short_minus_money_card_is_allowed():
    """Two legs at -300 is -128, and the only other name is a -475 scoring
    0.87. Padding the card with it would buy a plus sign by making the bet
    worse, so the card stays short and the price stays visible."""
    rows = [
        _pick("top1", None, -300, pitcher=1, model_p=BAR),
        _pick("top2", None, -300, pitcher=2, model_p=BAR - 0.001),
        _pick("dear", None, -475, pitcher=3, model_p=BAR - 0.02),
    ]
    got = bundle.build(rows)
    assert [r["batter"] for r in got] == ["top1", "top2"]
    assert bundle.summarise(got)["american"] < 100


def test_no_leg_is_ever_chosen_on_price():
    """The tail used to be sorted by model_p x decimal, which rewards a longer
    price — and a longer price is the book saying the leg is less likely to
    win. Measured over 36 days it cost about eleven points of hit rate: the
    card's legs hit 56.0% against 67.1% for the screen they came from.

    With the cap at two this is the whole card, so `best_priced` must not
    displace a more likely batter however cheap it is.
    """
    rows = [
        _pick("top1", None, -300, pitcher=1, model_p=BAR),
        _pick("top2", None, -300, pitcher=2, model_p=BAR - 0.001),
        _pick("best_priced", None, -110, pitcher=4, model_p=BAR - 0.01),
    ]
    assert [r["batter"] for r in bundle.build(rows)] == ["top1", "top2"]
    # And where a third leg does qualify, it is the likelier one that goes on,
    # not the cheaper one.
    rows = [
        _pick("top1", None, -300, pitcher=1, model_p=BAR + 0.04),
        _pick("top2", None, -300, pitcher=2, model_p=BAR + 0.03),
        _pick("best_priced", None, -110, pitcher=4, model_p=BAR + 0.01),
        _pick("likelier", None, -260, pitcher=5, model_p=BAR + 0.02),
    ]
    got = [r["batter"] for r in bundle.build(rows)]
    assert got[2] == "likelier", "price must not outrank probability"


def test_a_dear_leg_no_longer_decides_anything():
    """Price is out of selection entirely, so a -475 third leg is excluded by
    the two-leg cap rather than by its price. The card stays short either way;
    what changed is the reason."""
    rows = [
        _pick("top1", None, -300, pitcher=1, model_p=BAR),
        _pick("top2", None, -300, pitcher=2, model_p=BAR - 0.01),
        _pick("dear", None, -475, pitcher=3, model_p=BAR - 0.02),
    ]
    got = bundle.build(rows)
    assert [r["batter"] for r in got] == ["top1", "top2"]
    assert bundle.summarise(got)["american"] < 100


def test_dear_legs_never_pad_the_card_however_many_there_are():
    """A board of -900 favourites below the bar is a board with nothing worth
    adding: twelve of them add exactly nothing and the card stays at the
    floor."""
    rows = [_pick(f"p{i}", None, -900, pitcher=i, model_p=BAR - 0.02)
            for i in range(12)]
    assert len(bundle.build(rows)) == bundle.MIN_LEGS


def test_summarise_sizes_the_stake_at_quarter_kelly():
    """Full Kelly on these cards routinely computes above 30% of bankroll,
    which is not a stake. The quarter is returned already divided rather than
    left as a note nobody applies."""
    legs = [_pick("a", None, -160, pitcher=1, model_p=BAR),
            _pick("b", None, -115, pitcher=2, model_p=BAR - 0.01),
            _pick("c", None, -160, pitcher=3, model_p=BAR - 0.02)]
    s = bundle.summarise(legs)
    assert s["kelly"] > 0
    assert s["kelly_quarter"] == pytest.approx(s["kelly"] / 4, abs=1e-4)
    assert s["kelly_quarter"] < s["kelly"]


def test_a_card_with_no_edge_is_sized_at_zero():
    """Kelly goes negative when the price is worse than the read; the helper
    floors it, so a bad card asks for no money rather than a short position."""
    legs = [_pick("a", None, -400, pitcher=1, model_p=0.60),
            _pick("b", None, -400, pitcher=2, model_p=0.60)]
    s = bundle.summarise(legs)
    assert s["ev"] < 0
    assert s["kelly"] == 0 and s["kelly_quarter"] == 0


def test_an_empty_bundle_reports_no_stake():
    s = bundle.summarise([])
    assert s["kelly"] is None and s["kelly_quarter"] is None


# --------------------------------------------------------------------------
# A batter who was not in yesterday's lineup
# --------------------------------------------------------------------------

def test_a_recently_scratched_batter_is_dropped_before_ranking():
    """A VOID says he did not bat. Over 44,017 board rows that batter VOIDs
    again 44.1% of the time against 16.5%, and hits 57.4% against 62.7% when
    he does play — so he is removed, not demoted."""
    rows = [
        _pick("scratched", None, -150, pitcher=1, model_p=0.90),
        _pick("fit1", None, -150, pitcher=2, model_p=0.78),
        _pick("fit2", None, -150, pitcher=3, model_p=0.77),
    ]
    rows[0]["batter_id"] = 111
    got = [r["batter"] for r in bundle.build(rows, exclude_ids={111})]
    assert "scratched" not in got, "the top name must not survive on probability"
    assert got == ["fit1", "fit2"]


def test_no_exclusions_changes_nothing():
    rows = [_pick(f"p{i}", None, -150, pitcher=i, model_p=0.80 - i * 0.01)
            for i in range(3)]
    for r in rows:
        r["batter_id"] = int(r["batter"][1:])
    assert bundle.build(rows) == bundle.build(rows, exclude_ids=set())
    assert bundle.build(rows, exclude_ids=None) == bundle.build(rows)


def test_a_thin_split_is_not_excluded():
    """The sample-size floor was tested alongside this and rejected: requiring
    100+ PA against the hand took card hit rate from 80.3% to 75.4%. Only the
    not-in-the-lineup signal is acted on."""
    rows = [
        _pick("thin", None, -150, pitcher=1, model_p=0.78),
        _pick("deep", None, -150, pitcher=2, model_p=0.77),
    ]
    rows[0]["vs_hand_pa"] = 9
    rows[1]["vs_hand_pa"] = 1900
    assert [r["batter"] for r in bundle.build(rows)] == ["thin", "deep"]
