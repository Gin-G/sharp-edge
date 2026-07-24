"""warm_async's intra-day refresh decision, for both daily screens.

A healthy result younger than _WARM_TTL is served as-is; once it ages past the
TTL a background re-warm is kicked so mid-day probable-pitcher / lineup changes
get picked up — and that refresh re-persists with replace=True, so today's
still-pending picks are superseded by the fresh slate rather than piled on top.
"""

import time
from datetime import date

import pytest

from sharp_edge import batters, homers


@pytest.fixture(params=[batters, homers], ids=["batters", "homers"])
def screen(request, monkeypatch):
    mod = request.param
    captured: dict = {}

    class FakeThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            captured["args"] = args
            captured["kwargs"] = kwargs or {}

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(mod.threading, "Thread", FakeThread)
    monkeypatch.setattr(mod, "_warming", False)
    return mod, captured


def _set_cache(mod, monkeypatch, *, have_today, age, stale=False):
    monkeypatch.setattr(mod, "_warm_result", object() if have_today else None)
    monkeypatch.setattr(mod, "_warm_date", date.today() if have_today else None)
    monkeypatch.setattr(mod, "_warm_stale", stale)
    monkeypatch.setattr(mod, "_warming", False)
    monkeypatch.setattr(
        mod, "_warm_started_at", (time.time() - age) if age is not None else None
    )


def test_fresh_cache_serves_without_rewarm(screen, monkeypatch):
    mod, captured = screen
    _set_cache(mod, monkeypatch, have_today=True, age=60)  # 1 min old
    assert mod.warm_async() == {"status": "ready"}
    assert "started" not in captured


def test_aged_cache_triggers_background_refresh_with_replace(screen, monkeypatch):
    mod, captured = screen
    _set_cache(mod, monkeypatch, have_today=True, age=mod._WARM_TTL + 1)
    assert mod.warm_async() == {"status": "warming"}
    assert captured["started"] is True
    # An intra-day refresh replaces the day's still-pending picks.
    assert captured["kwargs"] == {"replace": True}


def test_cold_cache_warms_and_inserts(screen, monkeypatch):
    mod, captured = screen
    _set_cache(mod, monkeypatch, have_today=False, age=None)
    assert mod.warm_async() == {"status": "warming"}
    assert captured["kwargs"] == {"replace": False}


def test_stale_cache_holds_off_until_cooldown(screen, monkeypatch):
    mod, captured = screen
    _set_cache(mod, monkeypatch, have_today=True, age=10, stale=True)
    assert mod.warm_async() == {"status": "ready", "stale": True}
    assert "started" not in captured
