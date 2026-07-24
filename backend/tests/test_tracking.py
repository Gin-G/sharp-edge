"""Hermetic tests for pick persistence + outcome tracking.

Uses a synthetic Statcast frame and a temp SQLite database — no network.
The live-site behavior is covered by the post-deploy smoke workflow.
"""

import asyncio
import json
import threading
from datetime import date, timedelta

import pandas as pd
import pytest

import sharp_edge._data as _data
from sharp_edge import tracking
from sharp_edge.db import create_database

YESTERDAY = date.today() - timedelta(days=1)
TWO_DAYS_AGO = date.today() - timedelta(days=2)


def _synthetic_statcast() -> pd.DataFrame:
    rows = []
    for d, batter, events in [
        (TWO_DAYS_AGO, 111, ["home_run", "strikeout", "single"]),  # HR + hit
        (TWO_DAYS_AGO, 222, ["field_out", "walk"]),                # no hit
        (TWO_DAYS_AGO, 444, ["home_run"]),                         # pinch-hit HR
        (YESTERDAY, 111, ["double", "field_out"]),                 # hit, no HR
    ]:
        for ev in events:
            rows.append({
                "batter": batter, "pitcher": 999, "events": ev,
                "game_date": pd.Timestamp(d), "launch_speed": 90.0,
                "launch_angle": 20.0, "hc_x": 120.0, "hc_y": 120.0,
                "stand": "R", "p_throws": "R", "barrel": 0.0,
            })
    return pd.DataFrame(rows)


# Authoritative box score batting lines, kept consistent with the synthetic
# Statcast frame above. This is the primary settlement source; a batter absent
# from a date's map genuinely took no plate appearance (e.g. 333, who never
# played). Resolution only falls back to Statcast when this is empty.
_SYNTHETIC_BOXSCORE = {
    TWO_DAYS_AGO.isoformat(): {
        111: {"pa": 3, "hits": 2, "hr": 1},
        222: {"pa": 2, "hits": 0, "hr": 0},
        444: {"pa": 1, "hits": 1, "hr": 1},
    },
    YESTERDAY.isoformat(): {
        111: {"pa": 2, "hits": 1, "hr": 0},
    },
}


@pytest.fixture()
def tracked_db(tmp_path, monkeypatch):
    """Tracking configured against a temp SQLite db and a synthetic Statcast
    cache, with the app-style background event loop. Batters 111/222 are
    lineup starters; 444 is a bench player (pinch hitter)."""
    monkeypatch.setattr(_data, "_statcast_cache", _synthetic_statcast())
    monkeypatch.setattr(_data, "_statcast_loaded_on", date.today())
    monkeypatch.setattr(
        tracking, "_starters_for_date", lambda dstr: frozenset({111, 222})
    )
    monkeypatch.setattr(
        tracking, "_boxscore_batting_for_date",
        lambda dstr: dict(_SYNTHETIC_BOXSCORE.get(dstr, {})),
    )

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    db = asyncio.run_coroutine_threadsafe(
        create_database(f"sqlite:///{tmp_path}/test.db"), loop
    ).result(30)
    tracking.configure(db, loop)
    yield db, loop

    asyncio.run_coroutine_threadsafe(db.close(), loop).result(30)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    tracking.configure(None, None)


def _hr_picks() -> pd.DataFrame:
    return pd.DataFrame([
        {"batter": "Test Slugger", "batter_id": 111, "team": "Test Team",
         "opposing_pitcher": "Test Pitcher", "pitcher_id": 999,
         "venue": "Test Park", "hr_score": 25.5,
         "tags": "HOT-POP,BARREL-EDGE", "barrel_pct": 14.0},
        {"batter": "Test Bench", "batter_id": 333, "team": "Test Team",
         "opposing_pitcher": "Test Pitcher", "pitcher_id": 999,
         "venue": "Test Park", "hr_score": 20.0,
         "tags": "HOT-POP,BVP-HR", "barrel_pct": float("nan")},
    ])


def test_persist_is_idempotent(tracked_db):
    assert tracking.persist_screen_result("hr", _hr_picks(), TWO_DAYS_AGO, "backfill") == 2
    # Re-running the same day must not duplicate or clobber rows.
    assert tracking.persist_screen_result("hr", _hr_picks(), TWO_DAYS_AGO, "backfill") == 0


def test_persist_handles_empty_and_missing_ids(tracked_db):
    assert tracking.persist_screen_result("hr", pd.DataFrame(), TWO_DAYS_AGO) == 0
    no_ids = pd.DataFrame([{"batter": "Legacy Row"}])
    assert tracking.persist_screen_result("hr", no_ids, TWO_DAYS_AGO) == 0


def test_resolve_outcomes(tracked_db):
    db, loop = tracked_db
    tracking.persist_screen_result("hr", _hr_picks(), TWO_DAYS_AGO, "backfill")
    bat_picks = pd.DataFrame([
        {"batter": "Test Slugger", "batter_id": 111, "team": "Test Team",
         "opposing_pitcher": "Test Pitcher", "pitcher_id": 999,
         "tags": "BvP", "recent_avg": 0.350},
    ])
    tracking.persist_screen_result("batter", bat_picks, YESTERDAY)

    tracking.resolve_pending()

    hr_rows = asyncio.run_coroutine_threadsafe(db.list_picks("hr"), loop).result(30)
    by_id = {r["batter_id"]: r for r in hr_rows}
    assert by_id[111]["result"] == "WIN" and by_id[111]["hr_actual"] == 1
    assert by_id[333]["result"] == "VOID" and by_id[333]["pa_actual"] == 0

    bat_rows = asyncio.run_coroutine_threadsafe(db.list_picks("batter"), loop).result(30)
    assert bat_rows[0]["result"] == "WIN" and bat_rows[0]["hits_actual"] == 1

    # The default projection omits the metrics blob — nothing in the app reads
    # it back, and it dominates row size over a full season.
    assert "metrics" not in hr_rows[0]

    # NaN metrics must survive the JSON round-trip as null.
    with_metrics = asyncio.run_coroutine_threadsafe(
        db.list_picks("hr", include_metrics=True), loop
    ).result(30)
    blob = {r["batter_id"]: r["metrics"] for r in with_metrics}
    assert json.loads(blob[333])["barrel_pct"] is None


def test_non_starter_voids_even_with_events(tracked_db):
    """Sportsbook rule: player not in the starting lineup → bet void, even if
    they pinch-hit a home run."""
    db, loop = tracked_db
    picks = pd.DataFrame([
        {"batter": "Pinch Hitter", "batter_id": 444, "team": "Test Team",
         "opposing_pitcher": "Test Pitcher", "pitcher_id": 999,
         "venue": "Test Park", "hr_score": 18.0, "tags": "HOT-POP"},
    ])
    tracking.persist_screen_result("hr", picks, TWO_DAYS_AGO)
    tracking.resolve_pending()

    rows = asyncio.run_coroutine_threadsafe(db.list_picks("hr"), loop).result(30)
    assert rows[0]["result"] == "VOID"
    assert rows[0]["hr_actual"] == 1  # the HR is still recorded for reference


def test_pa_fallback_when_no_lineup_data(tracked_db, monkeypatch):
    """If lineup data is unavailable, 'recorded a PA' stands in for
    'started' instead of voiding the whole slate."""
    db, loop = tracked_db
    monkeypatch.setattr(tracking, "_starters_for_date", lambda dstr: frozenset())
    picks = pd.DataFrame([
        {"batter": "Pinch Hitter", "batter_id": 444, "team": "Test Team",
         "opposing_pitcher": "Test Pitcher", "pitcher_id": 999,
         "venue": "Test Park", "hr_score": 18.0, "tags": "HOT-POP"},
    ])
    tracking.persist_screen_result("hr", picks, TWO_DAYS_AGO)
    tracking.resolve_pending()

    rows = asyncio.run_coroutine_threadsafe(db.list_picks("hr"), loop).result(30)
    assert rows[0]["result"] == "WIN"


def test_no_boxscore_leaves_zero_pa_pending(tracked_db, monkeypatch):
    """Without a box score, a pick with no Statcast PA can't be told apart from
    data that simply hasn't published yet — so it stays pending rather than
    being frozen as a wrong VOID. This is the regression that made real hits
    (Clement, Springer) show up as void."""
    db, loop = tracked_db
    monkeypatch.setattr(tracking, "_boxscore_batting_for_date", lambda dstr: {})
    tracking.persist_screen_result("hr", _hr_picks(), TWO_DAYS_AGO, "backfill")
    tracking.resolve_pending()

    rows = asyncio.run_coroutine_threadsafe(db.list_picks("hr"), loop).result(30)
    by_id = {r["batter_id"]: r for r in rows}
    # 111 has Statcast events, so it still settles from the fallback.
    assert by_id[111]["result"] == "WIN"
    # 333 has neither box score nor Statcast PA — left pending, not VOID.
    assert by_id[333]["result"] is None


def test_reresolve_fixes_premature_void(tracked_db):
    """A pick frozen VOID (Statcast lag) is corrected to its real WIN/LOSS once
    the box score is available."""
    db, loop = tracked_db
    picks = pd.DataFrame([
        {"batter": "Test Slugger", "batter_id": 111, "team": "Test Team",
         "opposing_pitcher": "Test Pitcher", "pitcher_id": 999,
         "tags": "BvP", "recent_avg": 0.350},
    ])
    tracking.persist_screen_result("batter", picks, YESTERDAY)
    # Simulate the premature void: settled before the data published.
    asyncio.run_coroutine_threadsafe(
        db.update_pick_results("batter", YESTERDAY.isoformat(), [
            {"batter_id": 111, "result": "VOID",
             "hr_actual": 0, "hits_actual": 0, "pa_actual": 0},
        ]), loop,
    ).result(30)

    # Dry run reports the fix without writing it.
    preview = tracking.reresolve_voids(dry_run=True)
    assert preview["batter"]["fixed"] == 1
    still_void = asyncio.run_coroutine_threadsafe(
        db.list_picks("batter"), loop).result(30)
    assert still_void[0]["result"] == "VOID"

    # Real run rewrites it to the actual outcome.
    summary = tracking.reresolve_voids()
    assert summary["batter"]["fixed"] == 1
    rows = asyncio.run_coroutine_threadsafe(db.list_picks("batter"), loop).result(30)
    assert rows[0]["result"] == "WIN" and rows[0]["hits_actual"] == 1


def test_reresolve_leaves_genuine_voids(tracked_db):
    """A pick on a player who truly never took a PA stays VOID."""
    db, loop = tracked_db
    tracking.persist_screen_result("hr", _hr_picks(), TWO_DAYS_AGO, "backfill")
    tracking.resolve_pending()  # 333 (never played) -> VOID

    summary = tracking.reresolve_voids()
    assert summary["hr"]["fixed"] == 0
    rows = asyncio.run_coroutine_threadsafe(db.list_picks("hr"), loop).result(30)
    by_id = {r["batter_id"]: r for r in rows}
    assert by_id[333]["result"] == "VOID"


def test_track_record_aggregation(tracked_db):
    db, loop = tracked_db
    tracking.persist_screen_result("hr", _hr_picks(), TWO_DAYS_AGO, "backfill")
    tracking.resolve_pending()

    rows = asyncio.run_coroutine_threadsafe(db.list_picks("hr"), loop).result(30)
    tr = tracking.build_track_record("hr", rows)

    assert tr["overall"] == {
        "picks": 2, "wins": 1, "losses": 0, "voids": 1,
        "pending": 0, "decided": 1, "hit_rate": 100.0,
    }
    assert {t["tag"] for t in tr["by_tag"]} == {"HOT-POP", "BARREL-EDGE", "BVP-HR"}
    assert tr["daily"][0]["date"] == TWO_DAYS_AGO.isoformat()
    # metrics blob stays out of the API payload
    assert len(tr["picks"]) == 2 and "metrics" not in tr["picks"][0]


def test_catchup_plan_skips_days_already_recorded(tracked_db):
    """The startup catch-up only regenerates (day, screen) pairs that have no
    picks yet, so a pod restart costs one query instead of a rescreen."""
    tracking.persist_screen_result("hr", _hr_picks(), TWO_DAYS_AGO, "backfill")

    plan = tracking._missing_plan(TWO_DAYS_AGO, YESTERDAY, ["hr", "batter"])
    assert plan == {
        TWO_DAYS_AGO: ["batter"],
        YESTERDAY: ["hr", "batter"],
    }


def test_catchup_skips_days_that_screened_to_nothing(tracked_db):
    """A day with no games writes no picks. It still has to count as done —
    otherwise every restart re-screens the pre-season window, the All-Star
    break, and every empty slate, and the catch-up never converges."""
    tracking.persist_screen_result("hr", pd.DataFrame(), TWO_DAYS_AGO, "backfill")

    plan = tracking._missing_plan(TWO_DAYS_AGO, TWO_DAYS_AGO, ["hr", "batter"])
    assert plan == {TWO_DAYS_AGO: ["batter"]}  # hr is done, despite zero rows


def test_catchup_reports_up_to_date_when_nothing_missing(tracked_db):
    tracking.persist_screen_result("hr", _hr_picks(), YESTERDAY, "backfill")
    result = tracking.start_catchup(YESTERDAY, YESTERDAY, ["hr"])
    assert result["status"] == "up-to-date"


def test_batting_stats_range_tolerates_a_window_with_no_games(monkeypatch):
    """pybaseball indexes [0] into a parsed HTML table, so a window with no
    games raises IndexError instead of returning an empty frame. Every date
    in the season's first week has that problem — its trailing window sits in
    the pre-season — and an unhandled raise means the day never records a
    run, so the catch-up retries it on every restart forever."""
    from sharp_edge import batters

    def boom(*a, **kw):
        raise IndexError("list index out of range")

    monkeypatch.setattr(batters.pb, "batting_stats_range", boom)
    out = batters._batting_stats_range(TWO_DAYS_AGO, YESTERDAY)
    assert out.empty and "AB" in out.columns  # filterable, not fatal


def test_screen_rows_carry_ids():
    """The persistence layer keys on batter_id/pitcher_id — both screens must
    emit them. Guards against a refactor dropping the columns silently."""
    import inspect
    from sharp_edge import batters, homers
    hr_src = inspect.getsource(homers.screen_hr_for_date)
    bat_src = inspect.getsource(batters.screen_for_date)
    for src in (hr_src, bat_src):
        assert '"batter_id"' in src and '"pitcher_id"' in src
