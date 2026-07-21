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


@pytest.fixture()
def tracked_db(tmp_path, monkeypatch):
    """Tracking configured against a temp SQLite db and a synthetic Statcast
    cache, with the app-style background event loop."""
    monkeypatch.setattr(_data, "_statcast_cache", _synthetic_statcast())
    monkeypatch.setattr(_data, "_statcast_loaded_on", date.today())

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

    # NaN metrics must survive the JSON round-trip as null.
    assert json.loads(by_id[333]["metrics"])["barrel_pct"] is None


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


def test_screen_rows_carry_ids():
    """The persistence layer keys on batter_id/pitcher_id — both screens must
    emit them. Guards against a refactor dropping the columns silently."""
    import inspect
    from sharp_edge import batters, homers
    hr_src = inspect.getsource(homers.screen_hr_for_date)
    bat_src = inspect.getsource(batters.screen_for_date)
    for src in (hr_src, bat_src):
        assert '"batter_id"' in src and '"pitcher_id"' in src
