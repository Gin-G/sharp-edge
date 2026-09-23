"""The Odds API — every book's price for a prop, including books we can't scrape.

``fanduel/odds.py`` reads FanDuel's own board directly, and that works because
FanDuel serves its public JSON to anyone holding the key from its JS bundle.
DraftKings does not: every host and path variant answers ``403`` from
``AkamaiGHost`` before the request reaches DraftKings at all — the anonymous
board, the per-state hosts, and the newer sportscontent endpoint alike, from a
dev machine and from a datacenter egress both. That is bot protection on the
edge, and it is not negotiable from an HTTP client.

So DraftKings prices arrive the supported way instead, through an aggregator
that licenses them. Three things fall out of that which are worth stating,
because they shape the whole module:

**It is not a DraftKings client.** One call returns *every* book's price for a
market, so the same request that prices a leg at DraftKings also prices it at
FanDuel, BetMGM and Caesars. That makes line shopping free — the expensive part
was always the round trip — and it is why nothing here is named after a book.

**Two-sided quotes make the devig honest.** ``pricing.devig_probability``
carries an ``overround`` argument defaulting to 1.0, and a comment admitting
that a one-sided FanDuel runner can't be devigged properly. Over/under props
come back with both sides, so the margin can be measured instead of assumed.

**Quota is the scarce resource, not latency.** Player props are per-event: a
15-game MLB slate is 15 requests plus one to list the events. The free tier is
500 a month. At ``fanduel/odds.py``'s 5-minute TTL that is gone inside a day,
so the cache here is deliberately slower, the remaining balance is read back
off every response, and a floor stops the fetcher dead before it spends the
last of the month. See ``QUOTA_FLOOR``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timezone
from typing import Iterable, Optional

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.the-odds-api.com/v4"

SPORT_MLB = "baseball_mlb"
SPORT_NFL = "americanfootball_nfl"

# The Odds API's market keys, mapped to the names the screens already use.
# The MLB pair are over/under 0.5, so "Over" is the to-record-a-hit / to-homer
# side the batter and homer screens are predicting.
MLB_MARKETS = {
    "batter_hits": "hits",
    "batter_home_runs": "home_runs",
}
NFL_MARKETS = {
    "player_pass_yds": "passing_yards",
    "player_rush_yds": "rushing_yards",
    "player_reception_yds": "receiving_yards",
    "player_receptions": "receptions",
    "player_anytime_td": "anytime_td",
}

# Books worth asking for. Restricting the request to named books rather than a
# whole region keeps the response small and the cost predictable; adding one
# here is free at the call site.
DEFAULT_BOOKS = ("draftkings", "fanduel", "betmgm", "caesars")

# Stop fetching with this many credits left in the month.
#
# Not paranoia — the failure it prevents is silent and total. Player props cost
# one credit per market per event, so a single careless refresh of a 15-game
# slate across two markets is 30 credits, and a loop that refetches on every
# page load empties a 500-credit month in an afternoon. When that happens the
# API starts returning 401 and every price on every board goes blank at once,
# which looks like an outage rather than a bill.
#
# Holding a reserve means the board degrades to "prices are N hours old"
# instead, which is the failure mode ``fanduel/odds.py`` already chose when it
# decided stale odds beat no odds.
QUOTA_FLOOR = 25

# Fifteen minutes, against FanDuel's five.
#
# The TTL is a spending rate, not a freshness preference. One MLB refresh is
# ~16 credits, so 15 minutes caps a day at roughly 1,500 — still past a free
# month, which is why QUOTA_FLOOR exists as the real backstop and this is only
# the first line of defence. Raise it, or narrow the books, before raising the
# refresh rate.
CACHE_TTL_SECONDS = 900

# A slate is fetched a game at a time; this is how many go at once.
_MAX_CONCURRENCY = 5


class OddsAPIError(Exception):
    """The aggregator refused the request — bad key, or quota exhausted."""


class OddsAPIQuotaExhausted(OddsAPIError):
    """No credits left, or too few to spend. Serve the cache."""


class OddsAPI:
    """Reads prop prices for every book at once. One API key, no login."""

    def __init__(
        self,
        api_key: str,
        books: Iterable[str] = DEFAULT_BOOKS,
        transport=None,
        timeout: float = 30.0,
    ):
        if not api_key:
            raise OddsAPIError("No Odds API key configured (ODDS_API_KEY)")
        self._key = api_key
        self.books = tuple(books)
        self._transport = transport
        self._timeout = timeout
        # Read back off every response. None until the first call lands —
        # distinct from 0, which is a real and much worse number.
        self.remaining: Optional[int] = None
        self.used: Optional[int] = None

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict) -> list | dict:
        """One call, with the quota headers harvested on the way through.

        Every response carries the running balance, so the ledger stays
        current without a dedicated usage call — which would itself cost a
        request on some plans.
        """
        resp = await client.get(
            f"{API_BASE}{path}", params={**params, "apiKey": self._key}
        )
        self._read_quota(resp)

        if resp.status_code == 401:
            raise OddsAPIError(
                "Odds API rejected the key (401). Either it is wrong, or the "
                "month's quota is spent — check the balance at the-odds-api.com."
            )
        if resp.status_code == 422:
            # Almost always a market this plan or this sport doesn't carry.
            raise OddsAPIError(f"Odds API rejected the request (422): {resp.text[:200]}")
        if not resp.is_success:
            raise OddsAPIError(f"Odds API error ({resp.status_code}): {resp.text[:200]}")
        return resp.json()

    def _read_quota(self, resp: httpx.Response) -> None:
        for header, attr in (
            ("x-requests-remaining", "remaining"),
            ("x-requests-used", "used"),
        ):
            raw = resp.headers.get(header)
            if raw is None:
                continue
            try:
                setattr(self, attr, int(float(raw)))
            except (TypeError, ValueError):
                pass

    def _check_quota(self) -> None:
        if self.remaining is not None and self.remaining <= QUOTA_FLOOR:
            raise OddsAPIQuotaExhausted(
                f"Only {self.remaining} Odds API credits left (floor is "
                f"{QUOTA_FLOOR}) — serving cached prices instead of spending them."
            )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def fetch_events(
        self, client: httpx.AsyncClient, sport: str, target: Optional[date] = None
    ) -> list[dict]:
        """The sport's upcoming fixtures as ``{id, home, away, commence}``.

        Listing events is the cheap call — it carries no prices, so it costs
        nothing on current plans — and it is what turns one slate into the set
        of per-event prop requests that do cost.
        """
        data = await self._get(client, f"/sports/{sport}/events", {})
        out = []
        for ev in data or []:
            if target and not _same_day(ev.get("commence_time"), target):
                continue
            out.append({
                "id": ev.get("id"),
                "home": ev.get("home_team"),
                "away": ev.get("away_team"),
                "commence": ev.get("commence_time"),
            })
        return out

    async def fetch_event_props(
        self, client: httpx.AsyncClient, sport: str, event_id: str, markets: Iterable[str]
    ) -> dict:
        """Raw per-event odds for the named markets, every requested book.

        A failure here is logged and swallowed rather than raised: one game's
        props going missing should cost that game's prices, not the slate's.
        Quota exhaustion is the exception — it will fail identically for every
        remaining event, so it stops the run.
        """
        try:
            return await self._get(
                client,
                f"/sports/{sport}/events/{event_id}/odds",
                {
                    "markets": ",".join(markets),
                    "bookmakers": ",".join(self.books),
                    "oddsFormat": "american",
                },
            )
        except OddsAPIQuotaExhausted:
            raise
        except OddsAPIError as e:
            logger.warning("[odds-api] event %s: %s", event_id, e)
            return {}


def _same_day(commence: Optional[str], target: date) -> bool:
    """Is this fixture on the target date?

    Commence times are UTC, so a 7pm Denver first pitch stamps as the next
    calendar day. Accept the target and the day after, exactly as
    ``fanduel/odds.py`` does, and let the name join settle the rest.
    """
    if not commence:
        return False
    try:
        dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (dt.astimezone(timezone.utc).date() - target).days in (0, 1)
