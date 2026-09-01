"""Tests for the NFL prop screen.

Weighted toward the two places this thing can go quietly, catastrophically
wrong rather than toward coverage:

  the name join      a player whose name normalises differently on the odds
                     side and the projection side never appears on the board,
                     and nothing logs an error
  the probability    a missing volume input took the model from a 50/50 line
                     to a 99% read and an 86% "edge"; the anchoring that
                     replaced it has its own degenerate case
"""

from datetime import date, datetime, timezone

import pytest

from sharp_edge.nfl import model, odds, screen
from sharp_edge.nfl.names import norm_name
from sharp_edge.nfl.projections import Week


# ---------------------------------------------------------------------------
# Name joining
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("D.J. Moore", "DJ Moore"),
    ("A.J. Brown", "AJ Brown"),
    ("Michael Pittman Jr.", "Michael Pittman"),
    ("Kyle Pitts Sr.", "Kyle Pitts"),
    ("James Cook III", "James Cook"),
    ("Travis Etienne Jr.", "Travis Etienne"),
    ("Ja'Marr Chase", "JaMarr Chase"),
    ("Amon-Ra St. Brown", "Amon Ra St Brown"),
])
def test_names_that_must_join(a, b):
    assert norm_name(a) == norm_name(b)


def test_suffix_stripping_does_not_eat_real_surnames():
    # "v" and "ii" are only suffixes at the end of the name.
    assert norm_name("Bryan Bresee") != norm_name("Bryan")
    assert norm_name("Jordan Love") == "jordan love"
    assert norm_name("Sam LaPorta") == "sam laporta"


@pytest.mark.parametrize("runner,expected", [
    ("Romeo Doubs Over", "Romeo Doubs"),
    ("Jaxon Smith-Njigba Under", "Jaxon Smith-Njigba"),
    ("Drake Maye 150+ Yards", "Drake Maye"),
    ("Drake Maye 1+ Passing Touchdowns", "Drake Maye"),
    ("AJ Barner 2+ Receptions", "AJ Barner"),
    ("A.J. Brown", "A.J. Brown"),          # anytime-TD runners carry no suffix
])
def test_runner_name_parsing(runner, expected):
    assert odds._player_of(runner) == expected


# ---------------------------------------------------------------------------
# Week window
# ---------------------------------------------------------------------------

def _week(days):
    return Week(season=2026, week=1,
                games=[{"gameday": d, "week": 1} for d in days])


def test_window_covers_the_whole_week():
    lo, hi = _week(["2026-09-10", "2026-09-13", "2026-09-14"]).window()
    assert lo == datetime(2026, 9, 9, tzinfo=timezone.utc)
    assert hi == datetime(2026, 9, 16, tzinfo=timezone.utc)


def test_window_is_schedule_driven_not_date_driven():
    """The regression that motivated the refactor.

    Built from "the Tuesday around today", the week-1 window during the
    preseason gap contains no games at all and the board comes back empty. From
    the schedule it is the same window whatever day you ask on.
    """
    wk = _week(["2026-09-10", "2026-09-14"])
    events = [
        {"event_id": "1", "name": "A @ B", "kickoff": "2026-09-11T00:35:00.000Z"},
        {"event_id": "2", "name": "C @ D", "kickoff": "2026-09-14T00:20:00.000Z"},
        {"event_id": "3", "name": "E @ F", "kickoff": "2026-11-26T21:30:00.000Z"},
    ]
    got = odds.events_in_window(events, wk.window())
    assert [e["event_id"] for e in got] == ["1", "2"]


# ---------------------------------------------------------------------------
# Rescaling the projection onto the market
# ---------------------------------------------------------------------------

def test_calibrate_to_market_recovers_a_known_shrink():
    # projection = 0.6 * line + 12, exactly the shape measured on the live board
    pairs = [(line, 0.6 * line + 12) for line in range(10, 100, 5)]
    slope, intercept = model.calibrate_to_market(pairs)
    assert slope == pytest.approx(0.6, abs=1e-9)
    assert intercept == pytest.approx(12.0, abs=1e-9)


def test_shrunk_projection_produces_no_signal_after_rescaling():
    """A board that is purely shrinkage should fire nothing.

    This is the failure the rescaling exists to prevent: read raw, a 0.6-slope
    board wants the under on every high line and the over on every low one.
    """
    pairs = [(line, 0.6 * line + 12) for line in range(10, 100, 5)]
    fit = model.calibrate_to_market(pairs)
    for line, proj in pairs:
        assert abs(model.market_residual(line, proj, fit)) < 1e-6
        # …while the raw gap is large and signed by the size of the line.
    assert model.market_residual(90, 0.6 * 90 + 12, None) < -20
    assert model.market_residual(10, 0.6 * 10 + 12, None) > 5


def test_calibrate_to_market_refuses_a_thin_board():
    assert model.calibrate_to_market([(10, 20), (20, 30)]) is None


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------

def test_derive_volume_prefers_a_known_count():
    assert model.derive_volume("receptions", 40.0, known=5.0) == 5.0


def test_derive_volume_infers_touches_from_yardage():
    assert model.derive_volume("rushing_yards", 86.0) == pytest.approx(20.0, abs=0.1)
    assert model.derive_volume("passing_yards", 280.0) == pytest.approx(40.0, abs=0.1)


def test_missing_volume_no_longer_reads_as_zero_touches():
    """The bug that produced a 99% read on a -114 line.

    With volume left empty the model saw a starting back as a player with no
    carries and returned 1% for the over, which priced as an 86% edge on the
    under. Derived volume has to move the probability materially back.
    """
    line = 74.5
    est = 70.0
    zero = model.prop_probability("rushing_yards", est, line, volume=0.0)
    derived = model.prop_probability(
        "rushing_yards", est, line, volume=model.derive_volume("rushing_yards", est)
    )
    assert zero < 0.10
    assert derived > 0.30


# ---------------------------------------------------------------------------
# Anchoring
# ---------------------------------------------------------------------------

def test_offset_centres_the_board_on_the_market():
    pairs = [(0.30, 0.50), (0.35, 0.50), (0.40, 0.50)]
    offset = model.probability_offset(pairs, min_n=3)
    centred = [model._sigmoid(model._logit(m) + offset) for m, _ in pairs]
    assert sum(centred) / len(centred) == pytest.approx(0.5, abs=0.02)


def test_offset_survives_a_market_with_no_price_variance():
    """The degenerate case that broke the two-parameter version.

    Ten days out every main prop is -114/-114, so the devigged market
    probability is exactly 0.5 on every row. A slope fit collapses to zero and
    flattens the board; an offset must not.
    """
    pairs = [(p, 0.5) for p in (0.10, 0.30, 0.50, 0.70, 0.90)]
    offset = model.probability_offset(pairs, min_n=3)
    out = [model.anchor_probability(p, offset, 0.5, shrink=0.5) for p, _ in pairs]
    assert out[0] < out[2] < out[-1], "ordering must survive anchoring"
    assert len(set(round(o, 4) for o in out)) == len(out), "board must not flatten"


def test_shrink_pulls_toward_the_market():
    p_raw, fair = 0.95, 0.50
    hard = model.anchor_probability(p_raw, 0.0, fair, shrink=0.25)
    soft = model.anchor_probability(p_raw, 0.0, fair, shrink=0.75)
    assert fair < hard < soft < p_raw


def test_anchor_without_an_offset_returns_the_raw_model():
    assert model.anchor_probability(0.73, None) == 0.73


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def test_two_way_devig_sums_to_one():
    over, under = model.devig_two_way(-114, -114)
    assert over + under == pytest.approx(1.0)
    assert over == pytest.approx(0.5)


def test_one_sided_quote_is_not_devigged():
    over, under = model.devig_two_way(-114, None)
    assert under is None
    assert over > 0.5  # raw implied, margin left in and flagged by fair_p_under


def test_price_side_takes_the_side_we_disagree_on():
    priced = model.price_side(0.62, over=-114, under=-114)
    assert priced["side"] == "OVER"
    assert priced["edge_pts"] == pytest.approx(12.0, abs=0.1)
    assert priced["ev"] > 0

    priced = model.price_side(0.38, over=-114, under=-114)
    assert priced["side"] == "UNDER"
    assert priced["ev"] > 0


def test_price_side_with_no_quote_is_inert():
    priced = model.price_side(0.62, over=None, under=None)
    assert priced["side"] is None and priced["ev"] is None


def test_passing_yards_is_priced_but_not_bettable():
    assert "passing_yards" in model.MARKETS
    assert "passing_yards" not in model.BETTABLE


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("residual,threshold,expected", [
    (12.0, 10.0, "OVER"),
    (-12.0, 10.0, "UNDER"),
    (9.9, 10.0, ""),
    (-9.9, 10.0, ""),
    (2.0, 2.0, "OVER"),
])
def test_signal_thresholds(residual, threshold, expected):
    assert screen._signal(residual, threshold) == expected


def test_thresholds_match_the_stated_rule():
    assert screen.THRESHOLDS["receiving_yards"] == 10.0
    assert screen.THRESHOLDS["rushing_yards"] == 10.0
    assert screen.THRESHOLDS["receptions"] == 2.0


def test_board_ranks_signals_first():
    rows = [
        {"signal": "", "residual": 40.0, "threshold": 10.0},
        {"signal": "OVER", "residual": 11.0, "threshold": 10.0},
        {"signal": "UNDER", "residual": -25.0, "threshold": 10.0},
    ]
    rows.sort(key=screen._prop_rank)
    assert [r["signal"] for r in rows] == ["UNDER", "OVER", ""]


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------

def _market(mtype, runners, **kw):
    return {"marketId": "708.1", "marketType": mtype, "marketStatus": "OPEN",
            "runners": runners, **kw}


def _runner(name, odds, handicap=None, result=None, sel=1):
    return {"runnerName": name, "runnerStatus": "ACTIVE", "selectionId": sel,
            "handicap": handicap, "result": {"type": result} if result else None,
            "winRunnerOdds": {"americanDisplayOdds": {"americanOddsInt": odds}}}


EV = {"event_id": "999", "name": "A @ B", "kickoff": "2026-09-14T00:20:00.000Z"}


def test_game_markets_use_the_wire_id_name():
    """The bug that rendered an empty games table.

    These dicts go to the API verbatim, so the key has to be `fd_event_id` like
    every other FanDuel id on the wire. When it was `event_id` the frontend
    filtered every row out and neither side logged anything.
    """
    board = odds.Board()
    odds._absorb(board, EV, _market("MONEY_LINE", [_runner("A", -180), _runner("B", 150)]))
    assert len(board.games) == 1
    assert board.games[0]["fd_event_id"] == "999"
    assert "event_id" not in board.games[0]


def test_td_and_prop_rows_carry_the_same_id_name():
    board = odds.Board()
    odds._absorb(board, EV, _market("ANY_TIME_TOUCHDOWN_SCORER", [_runner("A.J. Brown", 120)]))
    odds._absorb(board, EV, _market(
        "PLAYER_X_RECEPTIONS_HIGH",
        [_runner("A.J. Brown Over", -114, 4.5, "OVER", 11),
         _runner("A.J. Brown Under", -106, 4.5, "UNDER", 12)],
    ))
    assert board.tds[0]["fd_event_id"] == "999"
    assert board.lines[0].event_id == "999"


def test_two_sided_prop_pairs_over_with_under():
    board = odds.Board()
    odds._absorb(board, EV, _market(
        "PLAYER_X_RECEIVING_YARDS_MEDIUM",
        [_runner("Romeo Doubs Over", -114, 36.5, "OVER", 11),
         _runner("Romeo Doubs Under", -106, 36.5, "UNDER", 12)],
    ))
    ln = board.lines[0]
    assert (ln.market, ln.line, ln.over, ln.under) == ("receiving_yards", 36.5, -114, -106)
    assert (ln.over_selection, ln.under_selection) == (11, 12)


def test_alt_ladder_splits_into_one_line_per_rung():
    board = odds.Board()
    odds._absorb(board, EV, _market(
        "PLAYER_X_ALT_RUSHING_YARDS_HIGH",
        [_runner("Woody Marks 25+ Yards", -200, 24.5, "OVER", 11),
         _runner("Woody Marks 50+ Yards", 150, 49.5, "OVER", 12),
         _runner("Woody Marks 75+ Yards", 400, 74.5, "OVER", 13)],
    ))
    rungs = board.ladder("rushing_yards", norm_name("Woody Marks"))
    assert [r.line for r in rungs] == [24.5, 49.5, 74.5]
    assert all(r.alt for r in rungs)
    # …and the alt rungs stay out of the main-line map.
    assert board.by_market("rushing_yards") == {}


def test_suspended_markets_and_scratched_runners_are_skipped():
    board = odds.Board()
    odds._absorb(board, EV, _market(
        "PLAYER_X_RECEPTIONS_HIGH",
        [_runner("X Over", -114, 4.5, "OVER")], marketStatus="SUSPENDED",
    ))
    assert board.lines == []

    m = _market("ANY_TIME_TOUCHDOWN_SCORER", [_runner("Scratched", 120)])
    m["runners"][0]["runnerStatus"] = "REMOVED"
    odds._absorb(board, EV, m)
    assert board.tds == []


# ---------------------------------------------------------------------------
# Anytime TD is a field market, not a two-way one
# ---------------------------------------------------------------------------

def test_anchor_field_matches_the_market_total():
    """Both sides must carry the same margin for the comparison to mean anything.

    Normalising the field to the model's own total was tried and is wrong: about
    four players score in a game, so a 400% field is mostly real, and scaling it
    away turned the entire board into a false double-digit edge.
    """
    model_ps = [0.30, 0.25, 0.20, 0.15, 0.10]        # sums to 1.00
    implied = [0.35, 0.30, 0.28, 0.25, 0.22]          # sums to 1.40
    out = model.anchor_field(model_ps, implied)
    assert sum(out) == pytest.approx(sum(implied), abs=1e-4)
    # Ordering survives — anchoring shifts the level, it does not re-rank.
    assert out == sorted(out, reverse=True)


def test_anchor_field_preserves_disagreement():
    """A player the model likes more than the field must still stand out."""
    model_ps = [0.40, 0.10, 0.10, 0.10]
    implied = [0.25, 0.25, 0.25, 0.25]
    out = model.anchor_field(model_ps, implied)
    edges = [a - i for a, i in zip(out, implied)]
    assert edges[0] > 0.10
    assert all(e < 0 for e in edges[1:])


def test_anchor_field_is_inert_on_empty_input():
    assert model.anchor_field([], [0.5]) == []


def test_td_price_reports_no_ev():
    """The margin cannot be stripped from this market, so a dollar EV would be
    a confidently wrong number on most of the board."""
    priced = model.price_td(0.28, 220, model_p=0.31)
    assert priced["ev"] is None and priced["kelly"] is None
    assert priced["edge_pts"] == pytest.approx(100 * (0.28 - 0.3125), abs=0.2)
    assert priced["model_p_unanchored"] == 0.31
