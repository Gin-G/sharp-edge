"""The rules Finding 14 shipped: the edge cap, the verdict block, calibrated
card order, touchdown picks, and the game simulation."""

from sharp_edge.nfl import card as card_mod, gamesim, model, tracking
from sharp_edge.nfl.names import event_teams


def _prop(**kw):
    base = {
        "market": "receiving_yards", "player": "A Receiver", "key": "a receiver",
        "line": 45.5, "adjusted": 55.0, "side": "OVER", "signal": "OVER", "bettable": True,
        "prior_only": False, "edge_pts": 8.0, "odds": -114, "model_p": 0.58,
        "fair_p": 0.50, "fd_event_id": "e1",
    }
    base.update(kw)
    return base


# --- props -----------------------------------------------------------------

def test_rushing_yards_is_no_longer_bettable():
    assert "rushing_yards" not in model.BETTABLE
    assert set(model.BETTABLE) == {"receiving_yards", "receptions"}


def test_an_edge_past_the_cap_is_refused():
    assert len(card_mod.suggestions([_prop(edge_pts=card_mod.MAX_EDGE_PTS)])) == 1
    assert card_mod.suggestions([_prop(edge_pts=card_mod.MAX_EDGE_PTS + 0.1)]) == []


def test_a_market_verdict_is_refused_and_a_model_verdict_is_not():
    assert card_mod.suggestions([_prop(role_conflict_verdict="market")]) == []
    assert len(card_mod.suggestions([_prop(role_conflict_verdict="model")])) == 1


def test_calibrated_p_keeps_a_fraction_of_the_disagreement():
    p = card_mod.calibrated_p(_prop(model_p=0.70, fair_p=0.50))
    assert 0.50 < p < 0.55
    assert card_mod.calibrated_p(_prop(model_p=None)) is None


def test_the_card_leads_with_the_likeliest_leg_not_the_biggest_edge():
    loud = _prop(key="loud", fd_event_id="g1", edge_pts=11.0, model_p=0.61, fair_p=0.50)
    short = _prop(key="short", fd_event_id="g2", edge_pts=4.0, model_p=0.62, fair_p=0.58)
    other = _prop(key="other", fd_event_id="g3", edge_pts=3.5, model_p=0.555, fair_p=0.52)
    legs = card_mod.build([loud, short, other])
    assert [r["key"] for r in legs] == ["short", "other"]


# --- touchdowns ------------------------------------------------------------

def test_td_calibration_lifts_the_top_more_than_the_bottom():
    assert model.td_calibrated(0.05) - 0.05 < model.td_calibrated(0.45) - 0.45
    assert model.td_calibrated(0.45) > 0.45


def test_price_td_reads_the_edge_against_the_raw_price():
    out = model.price_td(0.30, 200, model_p=0.30, p_cal=0.40)
    assert out["implied_p"] == round(100 / 300, 4)
    assert out["true_edge_pts"] == round(100 * (0.40 - 100 / 300), 1)
    assert out["ev"] > 0
    # Without a calibrated read there is no absolute claim at all.
    assert model.price_td(0.30, 200, model_p=0.30)["ev"] is None


def _td(**kw):
    base = {"player": "A Back", "key": "a back", "odds": 150, "p_cal": 0.48,
            "true_edge_pts": 8.0, "avail": "ACTIVE", "projected_tds": 0.6,
            "implied_p": 0.40, "model_p_unanchored": 0.45, "kickoff": None}
    base.update(kw)
    return base


def test_td_suggestions_need_an_edge_a_role_and_a_player_who_is_playing():
    assert len(card_mod.td_suggestions([_td()])) == 1
    assert card_mod.td_suggestions([_td(true_edge_pts=4.9)]) == []
    assert card_mod.td_suggestions([_td(p_cal=0.15)]) == []
    assert card_mod.td_suggestions([_td(avail="OUT")]) == []
    assert card_mod.td_suggestions([_td(odds=None)]) == []


def test_a_td_pick_records_as_an_over_half_and_settles_on_his_own_scores():
    row = tracking._td_prop(_td())
    assert (row["market"], row["line"], row["side"]) == ("anytime_td", 0.5, "OVER")
    assert row["model_p"] == 0.48 and row["edge_pts"] == 8.0
    pick = {"market": "anytime_td", "line": 0.5, "side": "OVER"}
    nan = float("nan")
    assert tracking._result_for(pick, {"rushing_tds": 0, "receiving_tds": 1,
                                       "special_teams_tds": nan}) == ("WIN", 1.0)
    # A thrown touchdown is not his.
    assert tracking._result_for(pick, {"rushing_tds": 0, "receiving_tds": 0,
                                       "passing_tds": 3}) == ("LOSS", 0.0)


# --- games -----------------------------------------------------------------

def test_event_teams_joins_fanduel_names_to_nflverse_codes():
    assert event_teams("Dallas Cowboys @ Philadelphia Eagles") == ("DAL", "PHI")
    assert event_teams("Los Angeles Rams @ Seattle Seahawks") == ("LA", "SEA")
    assert event_teams(None) is None


def _markets(home_ml, away_ml, home_hcp, total=44.5):
    return {
        "moneyline": {"runners": [
            {"name": "Philadelphia Eagles", "odds": home_ml},
            {"name": "Dallas Cowboys", "odds": away_ml}]},
        "spread": {"runners": [
            {"name": "Philadelphia Eagles", "odds": -110, "handicap": home_hcp},
            {"name": "Dallas Cowboys", "odds": -110, "handicap": -home_hcp}]},
        "total": {"runners": [
            {"name": "Over", "odds": -110, "handicap": total},
            {"name": "Under", "odds": -110, "handicap": total}]},
    }


def test_fd_lines_reads_every_side():
    fd = gamesim.fd_lines(_markets(-150, 130, -3.0), "PHI", "DAL")
    assert fd["home_ml"] == -150 and fd["away_ml"] == 130
    assert fd["home_spread"] == -3.0 and fd["total"] == 44.5


def test_outcome_probs_drop_pushes():
    runs = [(3.0, 44.0), (-3.0, 45.0), (0.0, 44.5), (7.0, 50.0)]
    got = gamesim.outcome_probs(runs, home_handicap=-3.0, total_line=44.5)
    assert got["p_home_win"] == round(2 / 3, 4)
    assert got["p_home_cover"] == round(1 / 3, 4)    # 7 covers; 3 pushes
    assert got["p_over"] == round(2 / 3, 4)          # 45, 50 over; 44.5 pushes


MISSES = [(d, t) for d in range(-14, 15) for t in (-10, 0, 10)]


def test_a_moneyline_too_long_for_its_own_spread_is_a_pick():
    # Home a 3-point favourite but priced as an underdog: the sim must back it.
    sim = gamesim.price_game("PHI", "DAL", _markets(110, -130, -3.0), None,
                             MISSES, seed="t")
    assert [p["side"] for p in sim["picks"]] == ["home"]
    assert sim["picks"][0]["edge_pts"] >= gamesim.ML_GAP_PTS
    # Priced in line with the spread, nothing.
    fair = gamesim.price_game("PHI", "DAL", _markets(-160, 135, -3.0), None,
                              MISSES, seed="t")
    assert fair["picks"] == []


def test_model_leans_come_from_our_projection_not_the_market():
    pred = {"exp_margin": 10.0, "exp_total": 55.0, "exp_home_points": 32.5,
            "exp_away_points": 22.5, "thin": False}
    sim = gamesim.price_game("PHI", "DAL", _markets(-150, 130, -3.0), pred,
                             MISSES, seed="t")
    kinds = {(l["market"], l["side"]) for l in sim["leans"]}
    assert ("spread", "home") in kinds and ("total", "over") in kinds
    assert sim["model"]["p_home_win"] > 0.6


def test_sim_picks_and_leans_grade_at_their_recorded_prices():
    row = {"home_score": 24, "away_score": 20, "sim": {
        "picks": [{"side": "home", "odds": 120}],
        "leans": [{"market": "spread", "side": "home", "line": -3.0, "odds": -110},
                  {"market": "total", "side": "over", "line": 44.0, "odds": -110}]}}
    picks, leans = tracking._grade_sims([row])
    assert picks["wins"] == 1 and picks["profit"] == 1.2
    assert leans["spread"]["wins"] == 1
    assert leans["total"]["pushes"] == 1 and leans["total"]["roi"] is None
