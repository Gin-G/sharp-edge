"""Odds conversion, calibration and EV."""

import pytest

from sharp_edge import pricing
from sharp_edge.fanduel import odds as fd_odds


# --------------------------------------------------------------------------
# Odds conversion
# --------------------------------------------------------------------------

@pytest.mark.parametrize("american,decimal", [
    (-250, 1.40), (-200, 1.50), (-110, 1.909091), (100, 2.0), (150, 2.5), (300, 4.0),
])
def test_american_to_decimal(american, decimal):
    assert fd_odds.american_to_decimal(american) == pytest.approx(decimal, abs=1e-5)


def test_implied_probability_is_the_inverse_of_decimal():
    # -200 is 1.5 decimal, so 66.7% including the vig.
    assert fd_odds.american_to_implied(-200) == pytest.approx(2 / 3, abs=1e-6)
    assert fd_odds.american_to_implied(100) == pytest.approx(0.5)


def test_american_decimal_roundtrip():
    for a in (-500, -250, -110, 105, 250, 900):
        assert fd_odds.decimal_to_american(fd_odds.american_to_decimal(a)) == a


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------

def _row(**kw):
    base = {"vs_hand_avg": 0.260, "recent_ab": 20, "recent_avg": 0.290,
            "p_l3_h9": 9.0, "p_l3_k9": 8.0}
    base.update(kw)
    return base


def test_two_batters_facing_the_same_starter_get_different_numbers():
    """The whole reason the old model had to be replaced. It read the pitcher
    alone, so it gave a star and a .190-hitting backup the same figure — and
    against the backup's honest price that invented an enormous fake edge."""
    star = pricing.model_probability(_row(vs_hand_avg=0.320, recent_ab=28))
    backup = pricing.model_probability(_row(vs_hand_avg=0.195, recent_ab=6))
    assert star > backup
    # And by a margin that matters against a price, not a rounding wobble.
    assert star - backup > 0.05


def test_batter_quality_outweighs_the_starter():
    """Measured over 29,777 settled rows: vs-hand average correlates with a
    hit at r=+0.119, the starter's recent H/9 at r=+0.011."""
    good_bat_sharp_sp = pricing.model_probability(
        _row(vs_hand_avg=0.320, p_l3_h9=5.0))
    weak_bat_battered_sp = pricing.model_probability(
        _row(vs_hand_avg=0.200, p_l3_h9=16.0))
    assert good_bat_sharp_sp > weak_bat_battered_sp


def test_a_more_hittable_starter_still_helps_at_the_margin():
    lo = pricing.model_probability(_row(p_l3_h9=6.0))
    hi = pricing.model_probability(_row(p_l3_h9=16.0))
    assert hi > lo


def test_strikeout_pitchers_suppress_the_probability():
    assert pricing.model_probability(_row(p_l3_k9=12.0)) < \
        pricing.model_probability(_row(p_l3_k9=5.0))


def test_missing_features_are_imputed_not_fatal():
    """A board row can be missing any of these — an unknown career split, a
    game log with no contact line. None of it may raise."""
    for rec in ({}, {"vs_hand_avg": None}, {"p_l3_h9": float("nan")},
                {"recent_ab": "junk"}, None, "nonsense"):
        p = pricing.model_probability(rec)
        assert 0.0 < p < 1.0


def test_predictions_stay_in_a_plausible_range():
    """Even at absurd inputs the model must not claim a certainty it can't
    have — EV multiplies this number by a price."""
    extreme_hi = pricing.model_probability(
        _row(vs_hand_avg=0.500, recent_ab=40, p_l3_h9=20.0, p_l3_k9=2.0))
    extreme_lo = pricing.model_probability(
        _row(vs_hand_avg=0.100, recent_ab=1, p_l3_h9=3.0, p_l3_k9=15.0))
    assert 0.30 < extreme_lo < extreme_hi < 0.95


# --------------------------------------------------------------------------
# EV
# --------------------------------------------------------------------------

def test_expected_value_sign_matches_the_break_even_price():
    p = 0.70
    be = pricing._breakeven(p)          # -233
    assert pricing.expected_value(p, be) == pytest.approx(0.0, abs=1e-3)
    assert pricing.expected_value(p, be + 50) > 0   # shorter favourite...
    assert pricing.expected_value(p, be - 50) < 0


def test_fair_coin_at_even_money_is_zero_ev():
    assert pricing.expected_value(0.5, 100) == pytest.approx(0.0)


def test_kelly_is_zero_when_there_is_no_edge():
    assert pricing.kelly_fraction(0.5, -110) == 0.0
    assert pricing.kelly_fraction(0.60, 100) > 0


def test_price_pick_without_a_market_still_reports_what_it_needs():
    q = pricing.price_pick(_row(vs_hand_avg=0.310, p_l3_h9=16.2), None)
    assert q["fd_odds"] is None and q["ev"] is None
    assert 0.5 < q["model_p"] < 0.95
    # The useful half: the price at which this would become a bet.
    assert q["breakeven_odds"] < -100


def test_price_pick_flags_a_negative_edge():
    """Junior Caminero's shape on 2026-08-07: an ordinary starter, priced
    like a good matchup."""
    q = pricing.price_pick(6.75, -250)
    assert q["edge_pts"] < 0
    assert q["ev"] < 0


def test_enrich_records_joins_on_normalised_names():
    from sharp_edge._data import _norm

    def _pick(name, h9):
        return {"batter": name, "p_l3_h9": h9, "is_hot": True,
                "bvp_edge": False, "hand_slump_edge": False,
                "hittable_sp_edge": True, "p_sharp": False}

    records = [_pick("José Ramírez", 16.2), _pick("Nobody At All", 12.0)]
    pricing.enrich_records(records, {_norm("Jose Ramirez"): -150})
    assert records[0]["fd_odds"] == -150      # accents normalised away
    assert records[0]["ev"] > 0
    assert records[1]["fd_odds"] is None      # no market posted
    assert records[1]["model_p"] > 0


# --------------------------------------------------------------------------
# Market parsing
# --------------------------------------------------------------------------

def test_only_open_to_record_a_hit_runners_are_read():
    """The batter-props tab carries ~25 market types; we want one, and we
    don't want a suspended market quoting a stale price."""
    import asyncio

    payload = {"attachments": {"markets": {
        "1": {"marketType": "PLAYER_TO_RECORD_A_HIT", "marketStatus": "OPEN",
              "runners": [{"runnerName": "Blaze Jordan", "winRunnerOdds": {
                  "americanDisplayOdds": {"americanOddsInt": -220}}}]},
        "2": {"marketType": "PLAYER_TO_RECORD_2+_HITS", "marketStatus": "OPEN",
              "runners": [{"runnerName": "Blaze Jordan", "winRunnerOdds": {
                  "americanDisplayOdds": {"americanOddsInt": 450}}}]},
        "3": {"marketType": "PLAYER_TO_RECORD_A_HIT", "marketStatus": "SUSPENDED",
              "runners": [{"runnerName": "Someone Hurt", "winRunnerOdds": {
                  "americanDisplayOdds": {"americanOddsInt": -300}}}]},
    }}}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return payload

    class _Client:
        async def get(self, *a, **k): return _Resp()

    got = asyncio.run(fd_odds.FanDuelOdds().fetch_hit_prices(_Client(), "1"))
    assert got == {"blaze jordan": -220}


def test_a_failed_event_page_yields_no_prices_rather_than_raising():
    """One bad game must not take the slate's odds down with it."""
    import asyncio

    class _Client:
        async def get(self, *a, **k): raise RuntimeError("502")

    got = asyncio.run(fd_odds.FanDuelOdds().fetch_hit_prices(_Client(), "1"))
    assert got == {}


# --------------------------------------------------------------------------
# Closing prices
# --------------------------------------------------------------------------

def _snap():
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "scripts" / "snapshot_odds.py"
    spec = importlib.util.spec_from_file_location("snap", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_closing_price_is_the_last_quote_before_that_batters_own_first_pitch():
    """The card runs from lunchtime to late evening, so one pass is near the
    bell for a few games and hours early for the rest. Each batter's close is
    relative to his own game."""
    import pandas as pd
    snap = _snap()

    df = pd.DataFrame([
        # Day game: the 15:00 pass is 80 min out, the later pass is past start.
        {"batter": "day guy", "fd_odds": -200, "mins_to_start": 80.0,
         "captured_at": "T1"},
        {"batter": "day guy", "fd_odds": -260, "mins_to_start": -200.0,
         "captured_at": "T2"},
        # Night game: both passes pre-game, the later one is the close.
        {"batter": "night guy", "fd_odds": -180, "mins_to_start": 500.0,
         "captured_at": "T1"},
        {"batter": "night guy", "fd_odds": -215, "mins_to_start": 25.0,
         "captured_at": "T2"},
    ])
    close = snap.closing_prices(df).set_index("batter")["fd_odds"].to_dict()

    # The day game's in-play quote must not become its "close".
    assert close["day guy"] == -200
    assert close["night guy"] == -215


def test_a_batter_whose_game_already_started_keeps_his_pre_game_close():
    import pandas as pd
    snap = _snap()
    df = pd.DataFrame([
        {"batter": "x", "fd_odds": -150, "mins_to_start": 10.0, "captured_at": "T1"},
        {"batter": "x", "fd_odds": -400, "mins_to_start": -30.0, "captured_at": "T2"},
    ])
    assert snap.closing_prices(df)["fd_odds"].tolist() == [-150]


def test_unknown_game_time_falls_back_to_the_last_capture():
    import pandas as pd
    snap = _snap()
    df = pd.DataFrame([
        {"batter": "x", "fd_odds": -150, "mins_to_start": None, "captured_at": "T1"},
        {"batter": "x", "fd_odds": -170, "mins_to_start": None, "captured_at": "T2"},
    ])
    got = snap.closing_prices(df)
    assert len(got) == 1 and got["fd_odds"].iloc[0] in (-150, -170)


def test_mins_to_start_goes_negative_once_underway():
    from datetime import datetime, timezone
    snap = _snap()
    now = datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc)
    assert snap._mins_to_start("2026-08-07T23:00:00.000Z", now) == 300.0
    assert snap._mins_to_start("2026-08-07T17:00:00.000Z", now) == -60.0
    assert snap._mins_to_start(None, now) is None
    assert snap._mins_to_start("not a date", now) is None


# --------------------------------------------------------------------------
# Every row is priced now
# --------------------------------------------------------------------------

def test_a_weak_hitter_is_priced_near_his_market_not_far_above_it():
    """The bug this model exists to fix. The old one gave every batter the
    pitcher-derived ~66%, so a .190 hitter honestly priced at -125 (55.6%)
    showed a 10-point edge. 55% of the board looked +EV on that error."""
    backup = {"batter": "Weak Hitting Catcher", "vs_hand_avg": 0.195,
              "recent_ab": 6, "recent_avg": 0.190, "p_l3_h9": 9.0,
              "p_l3_k9": 8.0}
    pricing.enrich_records([backup], {"weak hitting catcher": -125})
    assert backup["model_p"] is not None       # no gate any more
    assert backup["edge_pts"] < 5.0            # and no fabricated edge


def test_a_strong_hitter_is_still_allowed_an_edge():
    star = {"batter": "Star Bat", "vs_hand_avg": 0.330, "recent_ab": 30,
            "recent_avg": 0.340, "p_l3_h9": 16.0, "p_l3_k9": 5.0}
    pricing.enrich_records([star], {"star bat": -150})
    assert star["model_p"] > 0.60
    assert star["ev"] is not None



