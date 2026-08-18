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


def test_sharp_starter_is_still_banded_and_tagged(slate):
    """A 6.00 ERA over three starts *and* a .164 average against — the Cease
    profile. The banding is the thing under test and it still fires; what it
    no longer does is decide the card."""
    slate(SHARP_FORM)
    res = batters.screen_for_date(TODAY, verbose=False)
    assert set(res.today["p_form"]) == {"SHARP"}
    assert res.today["bvp_edge"].all()
    assert all("SHARP-SP" in t for t in res.today["tags"])


def test_a_sharp_starter_no_longer_vetoes_the_card(slate):
    """This used to return zero picks, and that was the bug in miniature.

    Filtering on the starter cost more than it saved: over 129 days,
    requiring a battered arm (L3 BAA >= .250) dropped the two-leg sweep from
    58.9% to 52.0%. A good hitter against a good pitcher is still one of the
    likeliest bets on the board, and it is priced far better than a good
    hitter against a bad one, because the book prices the bad one too.

    The model already reads the starter — ``p_l3_h9`` and ``p_l3_k9`` are
    terms in it. It weighs him; it doesn't get vetoed by him."""
    slate(SHARP_FORM)
    res = batters.screen_for_date(TODAY, verbose=False)
    assert len(res.picks) == 1          # one per game, not zero
    assert set(res.today["p_form"]) == {"SHARP"}


def test_the_retired_screen_flags_are_accepted_and_inert(slate):
    """``veto_sharp_sp`` and ``include_hittable_edge`` configured the retired
    screen. Callers still pass them, so they must not raise — and they must
    not quietly change the picks either."""
    slate(SHARP_FORM)
    base = batters.screen_for_date(
        TODAY, one_pick_per_game=False, verbose=False
    )
    for kwargs in ({"veto_sharp_sp": False}, {"include_hittable_edge": False},
                   {"veto_sharp_sp": False, "include_hittable_edge": False}):
        res = batters.screen_for_date(
            TODAY, one_pick_per_game=False, verbose=False, **kwargs
        )
        assert list(res.picks["batter"]) == list(base.picks["batter"])


def test_hittable_starter_still_produces_picks(slate):
    """Both fixtures face the *same* starter, so one pick — two batters in one
    game are a single bet on that pitcher having a bad day, not two reads."""
    slate(HITTABLE_FORM)
    res = batters.screen_for_date(TODAY, min_pick_probability=0.0, verbose=False)
    assert len(res.picks) == 1
    assert set(res.today["p_form"]) == {"HITTABLE"}


def test_both_batters_survive_when_one_per_game_is_off(slate):
    slate(HITTABLE_FORM)
    res = batters.screen_for_date(
        TODAY, min_pick_probability=0.0, one_pick_per_game=False, verbose=False
    )
    assert len(res.picks) == 2


def test_the_probability_bar_is_off_but_still_honoured(slate):
    """Off by default: re-measured against the ranked board a gate is at best
    neutral and mostly just skips days (>= 0.72 sat out 38 of 129 for a point
    and a half, with halves disagreeing by twelve). Kept as a knob for anyone
    who wants to sit out thin slates."""
    assert batters.MIN_PICK_PROBABILITY is None
    slate(HITTABLE_FORM)
    # An impossible bar clears the board even though both still qualify.
    none_clear = batters.screen_for_date(
        TODAY, min_pick_probability=0.99, one_pick_per_game=False, verbose=False
    )
    assert len(none_clear.picks) == 0
    assert none_clear.today["hittable_sp_edge"].all()   # still qualified

    both = batters.screen_for_date(
        TODAY, min_pick_probability=0.0, one_pick_per_game=False, verbose=False
    )
    assert len(both.picks) == 2


def test_the_pick_list_is_capped_now_that_it_comes_from_the_board(slate):
    """Uncapped made sense when picks were whatever cleared the filters. The
    board is the whole slate, so an uncapped list would record a couple of
    hundred "picks" a day and drown the track record in bets nobody made."""
    assert batters.MAX_PICKS_PER_DAY == 10
    slate(HITTABLE_FORM)
    res = batters.screen_for_date(
        TODAY, min_pick_probability=0.0, one_pick_per_game=False, verbose=False
    )
    assert len(res.picks) == 2
    capped = batters.screen_for_date(
        TODAY, min_pick_probability=0.0, max_picks=1,
        one_pick_per_game=False, verbose=False
    )
    assert len(capped.picks) == 1


def test_a_bench_bat_cannot_be_a_pick(slate, monkeypatch):
    """The live board is the whole active roster, not the lineup, so without a
    playing-time floor the top of it can be a backup with a flattering career
    split who isn't starting. A backtest can never surface this: on a past
    date the board is built from the boxscore, so every row already played."""
    import pandas as pd
    slate(HITTABLE_FORM)
    # Hoerner has barely played this week; Crow-Armstrong is a regular.
    monkeypatch.setattr(batters, "_batting_stats_range", lambda s, e: pd.DataFrame([
        {"Name": "Nico Hoerner", "Tm": "CHC", "BA": 0.500, "AB": 4,
         "H": 2, "HR": 0, "OBP": 0.500, "OPS": 1.100},
        {"Name": "Pete Crow-Armstrong", "Tm": "CHC", "BA": 0.320, "AB": 25,
         "H": 8, "HR": 2, "OBP": 0.360, "OPS": 0.900},
    ]))
    res = batters.screen_for_date(
        TODAY, one_pick_per_game=False, verbose=False
    )
    assert list(res.picks["batter"]) == ["Pete Crow-Armstrong"]
    # He is still on the board — the floor decides bets, not visibility.
    assert "Nico Hoerner" in set(res.today["batter"])


def test_the_floor_yields_rather_than_hand_back_an_empty_card(slate, monkeypatch):
    """A thin or broken stats feed should not silently cancel the day."""
    import pandas as pd
    slate(HITTABLE_FORM)
    monkeypatch.setattr(batters, "_batting_stats_range", lambda s, e: pd.DataFrame([
        {"Name": "Nico Hoerner", "Tm": "CHC", "BA": 0.500, "AB": 2,
         "H": 1, "HR": 0, "OBP": 0.500, "OPS": 1.100},
    ]))
    res = batters.screen_for_date(
        TODAY, one_pick_per_game=False, verbose=False
    )
    assert len(res.picks) == 2


def test_picks_are_ranked_by_probability_of_a_hit(slate):
    slate(HITTABLE_FORM)
    res = batters.screen_for_date(
        TODAY, min_pick_probability=0.0, one_pick_per_game=False, verbose=False
    )
    ps = list(res.picks["model_p"])
    assert ps == sorted(ps, reverse=True)


def test_a_batter_with_no_edge_tag_at_all_can_still_be_a_pick(slate, monkeypatch):
    """The heart of the change. Picks are the top of the board by probability,
    so carrying no tag is not disqualifying — over 129 days the tagged pool
    swept 49.6% of two-leg days and the untagged board swept 58.1%."""
    slate(MIDDLING_FORM)
    monkeypatch.setattr(batters, "_bvp", lambda bid, pid, df=None: None)

    res = batters.screen_for_date(TODAY, verbose=False)
    assert not res.today["bvp_edge"].any()
    assert not res.today["hittable_sp_edge"].any()
    assert not res.today["hand_slump_edge"].any()
    assert len(res.picks) == 1


# Between the old 9.50 bar and the new 11.00 one. Run 1 found hot bats facing
# starters in this range hit within noise of hot bats facing anyone — the band
# was spending most of its volume on a flat stretch of the curve.
MIDDLING_FORM = {"era": 5.40, "ip": 18.0, "er": 11, "starts": 3,
                 "hits": 20, "h9": 10.0, "baa": 0.286, "whip": 1.50, "k9": 6.0}


def test_middling_starter_is_no_longer_hittable(slate, monkeypatch):
    """10.0 H/9 / .286 BAA cleared the original 9.50 / .270 bars. It doesn't
    clear 11.00 / .310, so it produces no hittable-edge pick."""
    slate(MIDDLING_FORM)
    monkeypatch.setattr(batters, "_bvp", lambda bid, pid, df=None: None)

    res = batters.screen_for_date(TODAY, verbose=False)
    assert set(res.today["p_form"]) == {"NEUTRAL"}
    assert not res.today["hittable_sp_edge"].any()


def test_baa_arm_alone_cannot_reopen_the_band(slate, monkeypatch):
    """The bands are an OR, and h9 >= 11.0 is a strict subset of baa >= .270.
    Raising only H/9 would have left the BAA arm binding and changed almost
    nothing, so .310 has to hold the line on its own."""
    baa_only = dict(MIDDLING_FORM, h9=9.9, baa=0.300)
    slate(baa_only)
    monkeypatch.setattr(batters, "_bvp", lambda bid, pid, df=None: None)

    res = batters.screen_for_date(TODAY, verbose=False)
    assert set(res.today["p_form"]) == {"NEUTRAL"}


def test_one_start_does_not_brand_a_starter_hittable():
    """HITTABLE used to be ungated on starts while SHARP was, so a single bad
    outing banded a pitcher — 15% of HITTABLE rows in the run-1 boards came off
    one or two starts. Picks were never affected (hittable_sp_edge checks
    starts >= 3 itself) but the label was wrong on the board and in
    GET /batters/pitcher-form."""
    one_start = dict(HITTABLE_FORM, starts=1, ip=5.0, hits=9)
    assert batters._sp_band(one_start) == "NEUTRAL"
    assert batters._sp_band(dict(one_start, starts=3)) == "HITTABLE"

    # The same gate that already applied to SHARP.
    lone_gem = dict(SHARP_FORM, starts=1)
    assert batters._sp_band(lone_gem) == "NEUTRAL"
