"""Game-level score, total and margin predictions."""

import pytest

from sharp_edge.nfl import gamemodel as gm


def _g(h, a, hs, as_):
    return {"home_team": h, "away_team": a, "home_score": hs, "away_score": as_}


def _season(strong="AAA", weak="ZZZ"):
    """A league where one team is plainly good and one plainly bad."""
    mid = ["B", "C", "D", "E"]
    games = []
    for t in mid:
        games += [_g(strong, t, 31, 10), _g(t, strong, 10, 31),
                  _g(weak, t, 10, 27), _g(t, weak, 27, 10)]
    for i, t in enumerate(mid):
        o = mid[(i + 1) % len(mid)]
        games.append(_g(t, o, 21, 20))
    return games


class TestRatings:
    def test_a_good_team_outrates_a_bad_one(self):
        off, dfn, league = gm._ratings(_season())
        assert off["AAA"] > off["ZZZ"]
        assert dfn["AAA"] < dfn["ZZZ"]      # lower points allowed is better
        assert 15 < league < 35

    def test_ratings_are_centred_on_the_league(self):
        off, dfn, _ = gm._ratings(_season())
        assert sum(off.values()) == pytest.approx(0.0, abs=1e-6)
        assert sum(dfn.values()) == pytest.approx(0.0, abs=1e-6)

    def test_no_games_is_empty_not_a_crash(self):
        assert gm._ratings([]) == ({}, {}, 0.0)


class TestBuild:
    def test_the_better_team_is_favoured(self):
        rows = gm.build(_season(), [{"home_team": "AAA", "away_team": "ZZZ",
                                     "event": "ZZZ @ AAA"}])
        assert len(rows) == 1
        r = rows[0]
        assert r["exp_margin"] > 0
        assert r["home_win_p"] > 0.5

    def test_home_field_is_worth_about_two_points(self):
        """Same two teams, sides swapped: the gap between the two margins is
        twice the home-field edge."""
        a = gm.build(_season(), [{"home_team": "B", "away_team": "C"}])[0]
        b = gm.build(_season(), [{"home_team": "C", "away_team": "B"}])[0]
        assert a["exp_margin"] + b["exp_margin"] == pytest.approx(
            2 * gm.HOME_FIELD, abs=0.3)

    def test_the_scores_add_up_to_the_total(self):
        """The total is shrunk and the margin is not, so they are re-split. A
        reader adding the two team scores must get the total back."""
        r = gm.build(_season(), [{"home_team": "AAA", "away_team": "ZZZ"}])[0]
        assert r["exp_home_points"] + r["exp_away_points"] == pytest.approx(
            r["exp_total"], abs=0.11)
        assert r["exp_home_points"] - r["exp_away_points"] == pytest.approx(
            r["exp_margin"], abs=0.11)

    def test_the_total_is_pulled_back_toward_the_league_average(self):
        """Raw ratings predict totals WORSE than a flat constant (11.73 against
        10.81 MAE), so a lopsided matchup must not produce a wild total."""
        rows = gm.build(_season(), [{"home_team": "AAA", "away_team": "ZZZ"}])
        off, dfn, league = gm._ratings(_season())
        raw = 2 * league + off["AAA"] + dfn["ZZZ"] + off["ZZZ"] + dfn["AAA"]
        shrunk = rows[0]["exp_total"]
        assert abs(shrunk - 2 * league) < abs(raw - 2 * league)

    def test_an_unknown_team_is_skipped_not_guessed(self):
        rows = gm.build(_season(), [{"home_team": "AAA", "away_team": "NOPE"}])
        assert rows == []

    def test_no_history_produces_nothing(self):
        assert gm.build([], [{"home_team": "AAA", "away_team": "ZZZ"}]) == []

    def test_a_win_probability_is_not_overstated(self):
        """A three-point edge against a 13.78-point spread of outcomes is a
        coin flip with a lean, not a lock."""
        p = gm._norm_cdf(3.0 / gm.MARGIN_SD)
        assert 0.55 < p < 0.60

    def test_early_season_leans_on_last_year(self):
        """Week 2 fitted on one result a team would swing violently, so the
        prior fades with games played rather than switching off."""
        prior = _season()
        one_week = [_g("AAA", "B", 3, 40)]          # a single freak result
        rows = gm.build(one_week, [{"home_team": "AAA", "away_team": "ZZZ"}],
                        prior_completed=prior)
        assert rows, "a prior must carry the early weeks"
        assert rows[0]["exp_margin"] > 0, "one bad game must not flip a good team"
        assert rows[0]["thin"] is True


def test_no_betting_signal_is_emitted():
    """Not an oversight. Until the live record says the model finds something
    the backtest did not, there is no edge, no EV and no side."""
    r = gm.build(_season(), [{"home_team": "AAA", "away_team": "ZZZ"}])[0]
    for banned in ("ev", "edge_pts", "signal", "side", "kelly", "bet"):
        assert banned not in r


def test_unplayed_games_come_back_as_none_not_nan(monkeypatch):
    """pandas `where(notna(), None)` on a float column substitutes NaN, not
    None. `x is not None` then lets an unplayed game through, and the first
    int() on its score raises — /nfl/games/settle returned a 500 that way
    while the model itself looked fine, because its own week filter happened
    to exclude the bad rows.
    """
    import pandas as pd

    frame = pd.DataFrame([
        {"game_type": "REG", "season": 2026, "week": 1, "gameday": "2026-09-13",
         "home_team": "KC", "away_team": "DEN", "home_score": 27.0,
         "away_score": 20.0, "spread_line": 3.0, "total_line": 44.0},
        {"game_type": "REG", "season": 2026, "week": 2, "gameday": "2026-09-21",
         "home_team": "BUF", "away_team": "NYJ", "home_score": None,
         "away_score": None, "spread_line": 6.5, "total_line": 47.0},
    ])
    monkeypatch.setattr(pd, "read_csv", lambda *a, **k: frame)
    gm._cache.update({"rows": [], "fetched_at": 0.0})

    rows = gm._schedule(force=True)
    unplayed = [r for r in rows if r["week"] == 2][0]
    assert unplayed["home_score"] is None, "NaN would pass an `is not None` check"
    played = [r for r in rows
              if r["home_score"] is not None and r["away_score"] is not None]
    assert len(played) == 1, "an unplayed game must not count as a result"
    assert int(played[0]["home_score"]) == 27
