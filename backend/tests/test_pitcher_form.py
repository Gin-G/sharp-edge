"""Starting-pitcher recent-form metrics and the SHARP / HITTABLE banding.

The screen used to read a starter through ERA alone, which misreads the one
profile that beats a "batter records a hit" bet: a pitcher who gives up runs
in chunks (homers, a bad inning) while holding the lineup to a handful of
hits. These cover the hit-based metrics that replace that read, and the
degraded case where the game log carries no contact line at all.
"""

from datetime import date

import pytest

from sharp_edge import _data, batters


def _log(*starts):
    """Build a newest-first game log. Each start: (date, ip, er, hits, ab)."""
    return tuple(
        {"date": d, "ip_str": ip, "er": er, "hr": 0,
         "hits": h, "ab": ab, "bb": 1, "so": 6}
        for d, ip, er, h, ab in starts
    )


@pytest.fixture
def gamelog(monkeypatch):
    def _install(games):
        monkeypatch.setattr(
            _data, "_pitcher_gamelog_starts", lambda pid, season: games
        )
    return _install


# ---------------------------------------------------------------------------
# _pitcher_form
# ---------------------------------------------------------------------------

def test_rates_over_last_three_starts(gamelog):
    # 21 IP, 6 ER, 14 hits, 75 AB
    gamelog(_log(
        ("2026-08-01", "7.0", 2, 5, 25),
        ("2026-07-26", "7.0", 2, 4, 25),
        ("2026-07-20", "7.0", 2, 5, 25),
        ("2026-07-14", "3.0", 9, 12, 20),  # outside the window
    ))
    f = _data._pitcher_form(1, 2026, starts=3)
    assert f["starts"] == 3
    assert f["ip"] == 21.0
    assert f["era"] == pytest.approx(2.57, abs=0.01)
    assert f["hits"] == 14
    assert f["h9"] == pytest.approx(6.0, abs=0.01)
    assert f["baa"] == pytest.approx(0.187, abs=0.001)
    assert f["whip"] == pytest.approx(0.81, abs=0.01)


def test_season_window_uses_every_start(gamelog):
    gamelog(_log(
        ("2026-08-01", "7.0", 2, 5, 25),
        ("2026-07-26", "7.0", 2, 4, 25),
        ("2026-07-20", "7.0", 2, 5, 25),
        ("2026-07-14", "3.0", 9, 12, 20),
    ))
    assert _data._pitcher_form(1, 2026, starts=None)["starts"] == 4
    assert _data._pitcher_form(1, 2026, starts=None)["hits"] == 26


def test_before_excludes_later_starts(gamelog):
    """Historical screens must only see what was known that morning."""
    gamelog(_log(
        ("2026-08-01", "7.0", 0, 1, 24),
        ("2026-07-26", "6.0", 4, 9, 24),
        ("2026-07-20", "6.0", 4, 9, 24),
    ))
    f = _data._pitcher_form(1, 2026, starts=3, before="2026-08-01")
    assert f["starts"] == 2
    assert f["hits"] == 18


def test_missing_contact_line_reads_as_unknown_not_zero(gamelog):
    """A game log without `hits` must not look like a no-hitter."""
    gamelog(tuple(
        {"date": "2026-08-01", "ip_str": "6.0", "er": 3, "hr": 1,
         "hits": None, "ab": None, "bb": None, "so": None}
        for _ in range(3)
    ))
    f = _data._pitcher_form(1, 2026, starts=3)
    assert f["era"] is not None      # ERA still works
    assert f["hits"] is None
    assert f["h9"] is None and f["baa"] is None
    assert batters._sp_band(f) == "UNKNOWN"


def test_partial_contact_line_is_also_unknown(gamelog):
    """One start missing hits makes the window's total unusable, not smaller."""
    gamelog(_log(
        ("2026-08-01", "6.0", 3, 7, 24),
        ("2026-07-26", "6.0", 3, 7, 24),
    ) + ({"date": "2026-07-20", "ip_str": "6.0", "er": 3, "hr": 0,
          "hits": None, "ab": None, "bb": None, "so": None},))
    assert _data._pitcher_form(1, 2026, starts=3)["hits"] is None


def test_no_starts_returns_empty_form(gamelog):
    gamelog(())
    f = _data._pitcher_form(1, 2026, starts=3)
    assert f["starts"] == 0 and f["era"] is None and f["h9"] is None
    assert batters._sp_band(f) == "UNKNOWN"


def test_last_3_alias_matches_three_start_window(gamelog):
    gamelog(_log(("2026-08-01", "6.0", 3, 7, 24)))
    assert _data._pitcher_last_3(1, 2026) == _data._pitcher_form(1, 2026, starts=3)


# ---------------------------------------------------------------------------
# _sp_band
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("h9,baa,starts,expected", [
    (3.5, 0.140, 3, "SHARP"),       # both rates scream sharp
    (6.0, 0.240, 3, "SHARP"),       # H/9 alone qualifies
    (7.5, 0.195, 3, "SHARP"),       # BAA alone qualifies
    (11.0, 0.310, 3, "HITTABLE"),
    (8.4, 0.250, 3, "NEUTRAL"),
    (None, None, 3, "UNKNOWN"),
])
def test_bands(h9, baa, starts, expected):
    form = {"h9": h9, "baa": baa, "starts": starts}
    assert batters._sp_band(form) == expected


def test_one_start_is_not_enough_evidence_to_call_a_pitcher_sharp():
    """A single dominant outing shouldn't veto a whole slate of picks."""
    form = {"h9": 2.0, "baa": 0.100, "starts": 1}
    assert batters._sp_band(form) == "NEUTRAL"


def test_era_blowup_while_suppressing_hits_is_not_hittable():
    """The failure this change exists to fix: a 6.00 ERA over three starts
    built on homers, with the lineup managing five hits a night. The old
    ERA-only read called that a slumping pitcher to attack."""
    form = {"h9": 5.4, "baa": 0.170, "starts": 3, "era": 6.00}
    assert batters._sp_band(form) == "SHARP"


# ---------------------------------------------------------------------------
# End-to-end pick selection
# ---------------------------------------------------------------------------

@pytest.fixture
def slate(monkeypatch):
    """A one-game slate: two hot bats with a strong BvP line, facing one
    starter whose recent form the test dictates."""
    import pandas as pd

    def _install(form: dict):
        monkeypatch.setattr(batters, "fetch_schedule", lambda d: [{
            "gamePk": 1,
            "gameDate": "2026-08-06T23:05:00Z",
            "status": {"detailedState": "Scheduled"},
            "teams": {
                "away": {"team": {"id": 112, "name": "Chicago Cubs"}},
                "home": {"team": {"id": 141, "name": "Toronto Blue Jays"},
                         "probablePitcher": {"id": 656302, "fullName": "Test Starter"}},
            },
        }])
        monkeypatch.setattr(batters, "_load_statcast", lambda: pd.DataFrame({
            "batter": [], "pitcher": [], "events": [],
            "game_date": pd.to_datetime([]),
        }))
        monkeypatch.setattr(batters, "_batting_stats_range", lambda s, e: pd.DataFrame([
            {"Name": "Nico Hoerner", "Tm": "CHC", "BA": 0.350, "AB": 20,
             "H": 7, "HR": 0, "OBP": 0.400, "OPS": 0.850},
            {"Name": "Pete Crow-Armstrong", "Tm": "CHC", "BA": 0.320, "AB": 25,
             "H": 8, "HR": 2, "OBP": 0.360, "OPS": 0.900},
        ]))
        monkeypatch.setattr(batters, "_roster_batters", lambda tid: [
            (1, "Nico Hoerner"), (2, "Pete Crow-Armstrong"),
        ])
        monkeypatch.setattr(batters, "_handedness_splits", lambda bid: {
            "vs_R_avg": 0.280, "vs_R_pa": 400, "vs_L_avg": 0.270, "vs_L_pa": 200,
        })
        monkeypatch.setattr(batters, "_pitcher_info",
                            lambda pid: {"hand": "R", "name": "Test Starter"})
        monkeypatch.setattr(batters, "_pitcher_form",
                            lambda pid, season, starts=3, before=None: form)
        # Both batters are 3-for-6 lifetime off this starter: a textbook BvP
        # edge, and the only thing the old rules looked at.
        monkeypatch.setattr(batters, "_bvp", lambda bid, pid, df=None: {
            "pa": 6, "ab": 6, "hits": 3, "avg": 0.500,
        })
    return _install


# A starter whose ERA says "attack him" and whose contact line says the
# opposite — the Cease profile the old rules had no way to see.
SHARP_FORM = {"era": 6.00, "ip": 19.0, "er": 12, "starts": 3,
              "hits": 11, "h9": 5.21, "baa": 0.164, "whip": 0.95, "k9": 11.0}
HITTABLE_FORM = {"era": 6.00, "ip": 16.0, "er": 11, "starts": 3,
                 "hits": 24, "h9": 13.5, "baa": 0.343, "whip": 1.75, "k9": 5.0}

TODAY = date.today()


def test_sharp_starter_vetoes_bvp_picks(slate):
    """A 6.00 ERA over three starts *and* a .164 average against: the old
    rules saw a slumping pitcher plus a .500 BvP line and picked both bats.
    The BvP edge is still flagged on the board — it just isn't a pick."""
    slate(SHARP_FORM)
    res = batters.screen_for_date(TODAY, verbose=False)
    assert len(res.picks) == 0
    assert set(res.today["p_form"]) == {"SHARP"}
    assert res.today["bvp_edge"].all()
    assert all("SHARP-SP" in t for t in res.today["tags"])


def test_veto_can_be_switched_off_for_backtesting(slate):
    slate(SHARP_FORM)
    res = batters.screen_for_date(TODAY, veto_sharp_sp=False, verbose=False)
    assert len(res.picks) == 2


def test_hittable_starter_still_produces_picks(slate):
    slate(HITTABLE_FORM)
    res = batters.screen_for_date(TODAY, verbose=False)
    assert len(res.picks) == 2
    assert set(res.today["p_form"]) == {"HITTABLE"}


def test_hittable_edge_is_off_by_default(slate, monkeypatch):
    """A hot bat with no BvP history vs. a hittable starter is a candidate,
    not a pick, until the backtest says otherwise."""
    slate(HITTABLE_FORM)
    # Strip the BvP edge so hittable_sp_edge is the only thing left.
    monkeypatch.setattr(batters, "_bvp", lambda bid, pid, df=None: None)

    res = batters.screen_for_date(TODAY, verbose=False)
    assert res.today["hittable_sp_edge"].all()
    assert len(res.picks) == 0

    res_on = batters.screen_for_date(
        TODAY, include_hittable_edge=True, verbose=False
    )
    assert len(res_on.picks) == 2
