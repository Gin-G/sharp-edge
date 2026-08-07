"""FanDuel prop odds — the public, unauthenticated side of the sportsbook API.

``client.py`` needs a session token because it reads *your* bet history.
Prices are different: the same JSON that renders the public odds board is
served to anyone with the API key that ships in FanDuel's JS bundle, so
nothing here logs in. That matters operationally — odds keep working when the
FanDuel session has expired, which is most of the time.

Two calls per slate:

  content-managed-page  the day's MLB events, with FanDuel's numeric eventId
                        and a name like "Athletics (J Perkins) @ Boston Red
                        Sox (P Tolle)".
  event-page?tab=...    one per game, carrying the batter-prop markets. The
                        default tab returns inning markets instead, so the tab
                        has to be named explicitly.

We want exactly one market type, ``PLAYER_TO_RECORD_A_HIT`` — the thing the
batter screen is predicting.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Optional

import httpx

from .._data import _norm
from .client import DEFAULT_HEADERS, FD_API_KEY

logger = logging.getLogger(__name__)

FD_SBAPI_BASE = "https://sbapi.{state}.sportsbook.fanduel.com/api"

HIT_MARKET = "PLAYER_TO_RECORD_A_HIT"
BATTER_TAB = "batter-props"

# Per-request ceiling. A full slate is ~15 games and each event page is a few
# hundred KB, so this runs concurrently but not so wide that FanDuel notices.
_MAX_CONCURRENCY = 5


def american_to_decimal(american: int) -> float:
    """-250 -> 1.40, +150 -> 2.50."""
    return 1 + (100 / -american if american < 0 else american / 100)


def american_to_implied(american: int) -> float:
    """Implied probability *including* the book's margin (the vig)."""
    return 1 / american_to_decimal(american)


def decimal_to_american(dec: float) -> int:
    if dec >= 2:
        return round((dec - 1) * 100)
    return -round(100 / (dec - 1))


class FanDuelOdds:
    """Reads public prop prices. No authentication, no shared state."""

    def __init__(self, state: str = "CO", transport=None, timeout: float = 30.0):
        self.state = state.lower()
        self._base = FD_SBAPI_BASE.format(state=self.state)
        self._transport = transport
        self._timeout = timeout

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict) -> dict:
        params = {**params, "_ak": FD_API_KEY}
        resp = await client.get(f"{self._base}/{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    async def fetch_events(self, client: httpx.AsyncClient) -> list[dict]:
        """Today's MLB games as {event_id, name, open_date}.

        The MLB page also carries futures and season-award "events" with
        open dates years out; a real game has two teams and an ``@``.
        """
        data = await self._get(
            client,
            "content-managed-page",
            {"page": "CUSTOM", "customPageId": "mlb", "pbHorizontal": "false",
             "timezone": "America/Denver"},
        )
        out = []
        for eid, ev in (data.get("attachments", {}).get("events", {}) or {}).items():
            name = ev.get("name") or ""
            if "@" not in name or not ev.get("competitionId"):
                continue
            out.append({"event_id": str(eid), "name": name,
                        "open_date": ev.get("openDate")})
        return out

    async def fetch_hit_prices(
        self, client: httpx.AsyncClient, event_id: str
    ) -> dict[str, int]:
        """``{normalised batter name: american odds}`` for one game."""
        try:
            data = await self._get(
                client, "event-page", {"eventId": event_id, "tab": BATTER_TAB}
            )
        except Exception as e:
            logger.warning("[fd-odds] event %s: %s", event_id, e)
            return {}

        prices: dict[str, int] = {}
        for market in (data.get("attachments", {}).get("markets", {}) or {}).values():
            if market.get("marketType") != HIT_MARKET:
                continue
            if market.get("marketStatus") not in (None, "OPEN"):
                continue
            for runner in market.get("runners") or []:
                name = runner.get("runnerName")
                odds = (
                    (runner.get("winRunnerOdds") or {})
                    .get("americanDisplayOdds", {})
                    .get("americanOddsInt")
                )
                if name and odds is not None:
                    prices[_norm(name)] = int(odds)
        return prices

    async def hit_odds_for_slate(self, target: Optional[date] = None) -> dict[str, int]:
        """Every batter's to-record-a-hit price across the slate.

        Names are normalised, and a batter appears once — the same player
        can't be in two games on one day, so a flat map is safe and makes the
        join at the call site trivial.
        """
        target = target or date.today()
        async with httpx.AsyncClient(
            headers=DEFAULT_HEADERS, timeout=self._timeout,
            transport=self._transport,
        ) as client:
            events = await self.fetch_events(client)
            events = [e for e in events if _same_day(e.get("open_date"), target)]
            if not events:
                logger.info("[fd-odds] no MLB events listed for %s", target)
                return {}

            sem = asyncio.Semaphore(_MAX_CONCURRENCY)

            async def one(ev):
                async with sem:
                    return await self.fetch_hit_prices(client, ev["event_id"])

            merged: dict[str, int] = {}
            for prices in await asyncio.gather(*(one(e) for e in events)):
                merged.update(prices)
            logger.info(
                "[fd-odds] %d games -> %d batter prices for %s",
                len(events), len(merged), target,
            )
            return merged


# --------------------------------------------------------------------------
# Slate cache
# --------------------------------------------------------------------------
#
# Prices move, so this is a short TTL rather than the screen's day-long cache.
# A failed fetch keeps serving the last good map instead of blanking every
# price on the board — stale odds are far more useful than no odds, and the
# fetched-at stamp lets the UI say how old they are.

_cache: dict = {"odds": {}, "fetched_at": 0.0, "date": None, "error": None}
_CACHE_TTL_SECONDS = 300


async def cached_hit_odds(
    target: Optional[date] = None, state: str = "CO", force: bool = False
) -> dict:
    """``{"odds": {...}, "fetched_at": ts, "age_seconds": n, "error": str|None}``"""
    import time

    target = target or date.today()
    now = time.time()
    fresh = (
        not force
        and _cache["date"] == target
        and now - _cache["fetched_at"] < _CACHE_TTL_SECONDS
        and _cache["odds"]
    )
    if not fresh:
        try:
            odds = await FanDuelOdds(state=state).hit_odds_for_slate(target)
            if odds:
                _cache.update({"odds": odds, "fetched_at": now,
                               "date": target, "error": None})
            else:
                _cache["error"] = "no prices returned"
        except Exception as e:
            logger.warning("[fd-odds] fetch failed, serving cached: %s", e)
            _cache["error"] = str(e)
    return {
        "odds": _cache["odds"] if _cache["date"] == target else {},
        "fetched_at": _cache["fetched_at"],
        "age_seconds": round(now - _cache["fetched_at"]) if _cache["fetched_at"] else None,
        "error": _cache["error"],
    }


def _same_day(open_date: Optional[str], target: date) -> bool:
    """FanDuel stamps openDate in UTC, so a 7pm Denver first pitch lands on
    the following calendar day. Accept the target and the day after and let
    the name join sort out the rest — a batter with a price on the wrong day
    still isn't playing twice."""
    if not open_date:
        return False
    try:
        dt = datetime.fromisoformat(open_date.replace("Z", "+00:00"))
    except ValueError:
        return False
    d = dt.astimezone(timezone.utc).date()
    return (d - target).days in (0, 1)
