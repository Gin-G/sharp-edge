"""Turn aggregator payloads into the shape the screens already read.

The wire format nests book inside event and market inside book:

    event -> bookmakers[] -> markets[] -> outcomes[]

and an outcome names the *side* while describing the *player*:

    {"name": "Over", "description": "Rafael Devers", "price": -180, "point": 0.5}

which is inside out from what a screen wants. A screen holds a player and asks
what he costs; this module pivots the payload to ``book -> market -> player``
so that question is a dict lookup.

Anytime-touchdown and to-record-a-hit are the same question asked twice in two
notations — ``{"name": "Yes"}`` with no handicap, and ``{"name": "Over",
"point": 0.5}``. Both fold to the over side here, because the screens predict
an event happening rather than a number being cleared.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Callable, Iterable, Optional

import httpx

from .client import (
    MLB_MARKETS,
    NFL_MARKETS,
    SPORT_MLB,
    SPORT_NFL,
    OddsAPI,
    OddsAPIQuotaExhausted,
    _MAX_CONCURRENCY,
)

logger = logging.getLogger(__name__)

# The outcome names that mean "the thing happened". Yes/No is how anytime-TD
# and to-record-a-hit are written; Over/Under is how a yardage line is.
_OVER_NAMES = {"over", "yes"}
_UNDER_NAMES = {"under", "no"}


def two_way_devig(over: Optional[int], under: Optional[int]) -> Optional[dict]:
    """Strip the margin from a two-sided quote.

    This is the thing a single FanDuel runner could never support.
    ``pricing.devig_probability`` takes an ``overround`` argument defaulting to
    1.0 and its docstring concedes it is "honest about not knowing"; with both
    sides quoted the margin is measurable rather than assumed.

    Proportional (multiplicative) devig: both implied probabilities are scaled
    by the same factor until they sum to one. It is the standard choice and the
    right default for a two-way market near even money. It does bias the
    favourite slightly on a lopsided line, where a shin/power devig is better —
    worth revisiting if these prices ever drive selection rather than describe
    it.

    Returns ``None`` unless both sides are present, because one side alone is
    exactly the situation this exists to improve on.
    """
    if over is None or under is None:
        return None
    from ..fanduel.odds import american_to_implied

    p_over = american_to_implied(over)
    p_under = american_to_implied(under)
    total = p_over + p_under
    if total <= 0:
        return None
    return {
        "p_over": round(p_over / total, 4),
        "p_under": round(p_under / total, 4),
        # 1.045 means the book is holding 4.5 points on this market.
        "overround": round(total, 4),
    }


def parse_event(payload: dict, markets: dict[str, str], norm: Callable[[str], str]) -> dict:
    """Pivot one event's payload to ``{book: {market: {player_key: entry}}}``.

    ``markets`` maps the aggregator's key to ours, and doubles as the filter —
    a market we didn't ask about is ignored rather than carried.
    """
    event_meta = {
        "event_id": payload.get("id"),
        "event": _event_name(payload),
        "event_start": payload.get("commence_time"),
    }

    out: dict[str, dict[str, dict]] = {}
    for book in payload.get("bookmakers") or []:
        book_key = book.get("key")
        if not book_key:
            continue
        for market in book.get("markets") or []:
            our_market = markets.get(market.get("key"))
            if not our_market:
                continue
            bucket = out.setdefault(book_key, {}).setdefault(our_market, {})
            for outcome in market.get("outcomes") or []:
                player = outcome.get("description")
                if not player:
                    continue
                key = norm(player)
                if not key:
                    continue
                entry = bucket.setdefault(key, {
                    "player": player, "line": outcome.get("point"),
                    "over": None, "under": None,
                    "book": book_key, "market": our_market, **event_meta,
                })
                side = (outcome.get("name") or "").strip().lower()
                price = outcome.get("price")
                if side in _OVER_NAMES:
                    entry["over"] = price
                elif side in _UNDER_NAMES:
                    entry["under"] = price

    # Devig once, at the end, so a market quoted in either notation gets the
    # same treatment and a one-sided quote is left honestly undevigged.
    for book_markets in out.values():
        for bucket in book_markets.values():
            for entry in bucket.values():
                fair = two_way_devig(entry["over"], entry["under"])
                if fair:
                    entry.update(fair)
    return out


def _event_name(payload: dict) -> str:
    away, home = payload.get("away_team"), payload.get("home_team")
    return f"{away} @ {home}" if away and home else (home or away or "")


def _merge(into: dict, add: dict) -> None:
    """Fold one event's pivot into the slate's, in place."""
    for book, book_markets in add.items():
        dest_book = into.setdefault(book, {})
        for market, bucket in book_markets.items():
            dest_book.setdefault(market, {}).update(bucket)


async def slate_props(
    api: OddsAPI,
    sport: str,
    markets: dict[str, str],
    norm: Callable[[str], str],
    target: Optional[date] = None,
    max_events: Optional[int] = None,
) -> dict:
    """Every requested book's prices for one day's games.

    Returns ``{book: {market: {player_key: entry}}}`` plus a ``_meta`` entry
    carrying the quota balance and how much of the slate actually came back —
    a partial answer is normal (a game whose props are not posted yet) and the
    caller deserves to know it was partial.
    """
    target = target or date.today()
    async with httpx.AsyncClient(timeout=api._timeout, transport=api._transport) as client:
        events = await api.fetch_events(client, sport, target)
        if max_events is not None:
            events = events[:max_events]
        if not events:
            logger.info("[odds-api] no %s events for %s", sport, target)
            return {"_meta": _meta(api, 0, 0)}

        sem = asyncio.Semaphore(_MAX_CONCURRENCY)
        quota_hit = False

        async def one(ev: dict) -> dict:
            nonlocal quota_hit
            async with sem:
                if quota_hit:
                    return {}
                try:
                    api._check_quota()
                    return await api.fetch_event_props(
                        client, sport, ev["id"], markets.keys()
                    )
                except OddsAPIQuotaExhausted as e:
                    # Stop the rest of the slate rather than failing 15 times.
                    if not quota_hit:
                        logger.warning("[odds-api] %s", e)
                    quota_hit = True
                    return {}

        payloads = await asyncio.gather(*(one(e) for e in events))

        merged: dict = {}
        fetched = 0
        for payload in payloads:
            if not payload:
                continue
            fetched += 1
            _merge(merged, parse_event(payload, markets, norm))

        merged["_meta"] = _meta(api, fetched, len(events), quota_hit)
        logger.info(
            "[odds-api] %s %s: %d/%d events, %d books, %s credits left",
            sport, target, fetched, len(events),
            len([k for k in merged if k != "_meta"]), api.remaining,
        )
        return merged


def _meta(api: OddsAPI, fetched: int, total: int, quota_hit: bool = False) -> dict:
    return {
        "events_fetched": fetched,
        "events_total": total,
        "partial": fetched < total,
        "quota_remaining": api.remaining,
        "quota_used": api.used,
        "quota_exhausted": quota_hit,
    }


# --------------------------------------------------------------------------
# Screen-facing adapters
# --------------------------------------------------------------------------

async def mlb_slate(api: OddsAPI, target: Optional[date] = None) -> dict:
    """Batter hit and home-run prices, every book."""
    from .._data import _norm

    return await slate_props(api, SPORT_MLB, MLB_MARKETS, _norm, target)


async def nfl_slate(api: OddsAPI, target: Optional[date] = None) -> dict:
    """This week's player props, every book."""
    from ..nfl.names import norm_name

    return await slate_props(api, SPORT_NFL, NFL_MARKETS, norm_name, target)


def hit_odds(slate: dict, book: str) -> dict[str, dict]:
    """One book's to-record-a-hit prices, shaped like ``cached_hit_odds``.

    The batter screen joins on ``{normalised name: {"odds": american, ...}}``,
    so this projects the slate onto that and nothing more. The over side is the
    hit — ``batter_hits`` is quoted at 0.5.
    """
    bucket = (slate.get(book) or {}).get("hits") or {}
    return {
        key: {
            "odds": entry["over"],
            "line": entry.get("line"),
            "devig_p": entry.get("p_over"),
            "overround": entry.get("overround"),
            "event_id": entry.get("event_id"),
            "event_name": entry.get("event"),
            "event_start": entry.get("event_start"),
            # No market/selection ids: an aggregator quote carries a price, not
            # the book's internal handles, so no bet-slip link can be built
            # from it. Spelled out because pricing.enrich_records reads these.
            "market_id": None,
            "selection_id": None,
        }
        for key, entry in bucket.items()
        if entry.get("over") is not None
    }


def books_in(slate: dict) -> list[str]:
    """Which books actually returned prices, in the slate's own order."""
    return [k for k in slate if k != "_meta"]
