"""Slate cache — the thing standing between a page refresh and the month's quota.

``fanduel/odds.py`` caches for five minutes because its only cost is politeness
to FanDuel. Here every miss spends real credits (one per market per event), so
the cache is doing two jobs at once: keeping the board quick, and keeping a
free tier alive for a month. The second is the harder constraint, which is why
a failed fetch keeps serving the last good slate rather than blanking the
board — the same call ``fanduel/odds.py`` made, for a stronger reason.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Optional

from ..config import settings
from .client import CACHE_TTL_SECONDS, OddsAPI, OddsAPIError
from .props import mlb_slate, nfl_slate

logger = logging.getLogger(__name__)

# One entry per sport. A slate is per-day, so the stamped date is what makes a
# stale entry detectable rather than merely old.
_cache: dict[str, dict] = {}

_FETCHERS = {"mlb": mlb_slate, "nfl": nfl_slate}


def configured() -> bool:
    """Is there a key to spend? Every call site should ask before assuming."""
    return bool(settings.odds_api_key)


async def cached_slate(
    sport: str = "mlb", target: Optional[date] = None, force: bool = False
) -> dict:
    """``{"slate": {...}, "age_seconds": n, "error": str|None, "quota": {...}}``

    ``force`` bypasses the TTL but not the quota floor — the floor exists
    precisely to survive someone holding down refresh.
    """
    if sport not in _FETCHERS:
        raise ValueError(f"unknown sport {sport!r}")

    target = target or date.today()
    now = time.time()
    entry = _cache.setdefault(
        sport, {"slate": {}, "fetched_at": 0.0, "date": None, "error": None}
    )

    fresh = (
        not force
        and entry["date"] == target
        and now - entry["fetched_at"] < CACHE_TTL_SECONDS
        and entry["slate"]
    )
    if not fresh:
        if not configured():
            entry["error"] = "no Odds API key configured"
        else:
            try:
                api = OddsAPI(
                    settings.odds_api_key, books=settings.odds_api_books_list
                )
                slate = await _FETCHERS[sport](api, target)
                # A slate that is nothing but _meta means every event failed or
                # the quota floor stopped the run; keep the previous one.
                if [k for k in slate if k != "_meta"]:
                    entry.update({
                        "slate": slate, "fetched_at": now,
                        "date": target, "error": None,
                    })
                else:
                    entry["error"] = (
                        (slate.get("_meta") or {}).get("quota_exhausted")
                        and "Odds API quota floor reached — serving cached prices"
                        or "no prices returned"
                    )
            except OddsAPIError as e:
                logger.warning("[odds-api] %s fetch failed, serving cache: %s", sport, e)
                entry["error"] = str(e)
            except Exception as e:
                logger.warning("[odds-api] %s fetch failed: %r", sport, e)
                entry["error"] = str(e)

    slate = entry["slate"] if entry["date"] == target else {}
    return {
        "slate": slate,
        "fetched_at": entry["fetched_at"],
        "age_seconds": round(now - entry["fetched_at"]) if entry["fetched_at"] else None,
        "error": entry["error"],
        "quota": (slate.get("_meta") or {}) if slate else {},
    }
