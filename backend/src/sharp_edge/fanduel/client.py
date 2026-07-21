"""FanDuel Sportsbook API client.

Auth flow and response parsing are stubbed until we capture actual
request headers and response JSON from the browser Network tab.

Discovered endpoints from browser network capture:
  - Bet history:  api.sportsbook.fanduel.com/sbapi/fetch-my-bets
  - Sessions:     api.fanduel.com/sessions
  - User:         api.fanduel.com/users/current?product=SB
  - Wallet:       api.fanduel.com/account/wallet
  - Market data:  smp.co.sportsbook.fanduel.com/api/sports/fixedodds/readonly/v1/getMarketPrices
  - Live data:    api.sportsbook.fanduel.com/ips/inplayservice/v1.0/livedata
"""

import json
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Public API key embedded in FanDuel's JS bundles
FD_API_KEY = "FhMFpcPWXMeyZxOx"

# Base URLs
FD_API_BASE = "https://api.fanduel.com"
FD_SB_API_BASE = "https://api.sportsbook.fanduel.com"
FD_SMP_BASE = "https://smp.co.sportsbook.fanduel.com"

# Default headers observed in browser requests
DEFAULT_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Origin": "https://co.sportsbook.fanduel.com",
    "Referer": "https://co.sportsbook.fanduel.com/",
}

PAGE_SIZE = 20


class FanDuelAuthError(Exception):
    pass


class FanDuelClient:
    """HTTP client for FanDuel's sportsbook API.

    Authentication: FanDuel uses session-based auth via api.fanduel.com/sessions.
    The browser sends credentials and receives a session token that is passed
    as either a Bearer token or X-Auth-Token header on subsequent requests.

    TODO: Once we capture the actual auth headers from the browser, wire up
    the token injection here. For now, pass the token via constructor.
    """

    def __init__(self, auth_token: str, state: str = "CO", auth=None, transport=None):
        """
        Args:
            auth_token: Session token from FanDuel login (the x-authentication
                        value) — produced by FanDuelAuth.login() or captured
                        manually from browser DevTools.
            state: Two-letter state code (affects which API subdomain/config).
            auth:  Optional FanDuelAuth with stored credentials. When given,
                   a 401 mid-request triggers one transparent re-login and
                   retry instead of failing the sync.
            transport: httpx transport override (tests).
        """
        self.auth_token = auth_token
        self.state = state
        self._auth = auth
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {
                **DEFAULT_HEADERS,
                "x-authentication": self.auth_token,
                "x-application": FD_API_KEY,
                "x-app-version": "2.140.0",
                "x-sportsbook-region": self.state,
            }
            self._client = httpx.AsyncClient(
                headers=headers, timeout=30.0, transport=self._transport
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, url: str, params: dict) -> httpx.Response:
        """GET with one transparent re-login + retry on 401 when the bound
        FanDuelAuth has credentials — a token expiring mid-sync just renews."""
        client = await self._get_client()
        resp = await client.get(url, params=params)
        if (
            resp.status_code == 401
            and self._auth is not None
            and self._auth.can_relogin
        ):
            logger.info("FanDuel 401 — re-logging in and retrying once")
            self.auth_token = await self._auth.ensure_token(force=True)
            await self.close()  # rebuild client with the fresh token
            client = await self._get_client()
            resp = await client.get(url, params=params)
        return resp

    # ------------------------------------------------------------------
    # Bet History
    # ------------------------------------------------------------------

    async def fetch_bets(
        self,
        settled: bool = True,
        from_record: int = 1,
        to_record: int = 20,
    ) -> dict:
        """Fetch bet history page from FanDuel.

        Endpoint: /sbapi/fetch-my-bets
        Params observed in browser:
            isSettled, fromRecord, toRecord, sortDir, sortParam,
            adaptiveTokenEnabled, rewardsClubEnabled, _ak
        """
        params = {
            "isSettled": str(settled).lower(),
            "fromRecord": from_record,
            "toRecord": to_record,
            "sortDir": "DESC",
            "sortParam": "SETTLEMENT_DATE" if settled else "PLACEMENT_DATE",
            "adaptiveTokenEnabled": "false",
            "rewardsClubEnabled": "false",
            "_ak": FD_API_KEY,
        }
        resp = await self._get(f"{FD_SB_API_BASE}/sbapi/fetch-my-bets", params=params)
        resp.raise_for_status()
        return resp.json()

    async def fetch_all_settled_bets(self, max_pages: int = 100) -> list[dict]:
        """Paginate through all settled bet history."""
        all_bets = []
        for page in range(max_pages):
            from_rec = 1 + page * PAGE_SIZE
            to_rec = from_rec + PAGE_SIZE - 1
            data = await self.fetch_bets(
                settled=True, from_record=from_rec, to_record=to_rec
            )
            bets = self._extract_bets_from_response(data)
            if not bets:
                break
            all_bets.extend(bets)
            logger.info(f"Fetched page {page + 1}: {len(bets)} bets (total: {len(all_bets)})")
            if not self.has_more_bets(data):
                break
        return all_bets

    async def fetch_open_bets(self) -> list[dict]:
        """Fetch currently open/pending bets."""
        data = await self.fetch_bets(settled=False, from_record=1, to_record=50)
        return self._extract_bets_from_response(data)

    def _extract_bets_from_response(self, data: dict) -> list[dict]:
        """Extract bet list from API response.

        Response shape:
            {"moreAvailable": bool, "numberOfMatches": int, "bets": [...]}
        """
        return data.get("bets", [])

    def has_more_bets(self, data: dict) -> bool:
        """Check if there are more pages of results."""
        return data.get("moreAvailable", False)

    def normalize_bet(self, raw: dict) -> dict:
        """Convert a FanDuel API bet object to our canonical schema.

        FanDuel response field mapping:
            betId           -> bet_id
            betReceiptId    -> (alt id, stored in raw)
            betType         -> SGL, DBL, TBL, ACC4, ACC9, etc.
            currentSize     -> stake
            betPrice        -> decimal odds (from betPrices.betPrice.decimalPrice or top-level)
            pandl           -> profit (0 for losses, positive number for wins)
            result          -> WON, LOST
            placedDate      -> time_placed
            settledDate     -> time_settled
            legs[].parts[]  -> individual selections with full detail
            sameGameMultis  -> whether it's an SGM
            rewardUsed      -> promo/boost info
        """
        legs_raw = raw.get("legs", [])
        legs_info = []
        sports = set()
        leagues = set()

        for leg in legs_raw:
            for part in leg.get("parts", []):
                leg_data = {
                    "event": part.get("eventDescription", ""),
                    "market": part.get("eventMarketDescription", ""),
                    "market_type": part.get("marketType", ""),
                    "selection": part.get("selectionName", ""),
                    "odds_decimal": float(part.get("price", 0) or 0),
                    "odds_american": part.get("americanPrice", 0),
                    "result": part.get("result", leg.get("result", "")),
                    "competition": part.get("competitionName", ""),
                    "event_id": part.get("eventId", ""),
                    "handicap": part.get("parsedHandicap"),
                    "is_player": part.get("isPlayerSelection", False),
                }
                legs_info.append(leg_data)

                # Map competition to sport/league
                comp = part.get("competitionName", "")
                if comp:
                    mapped = self._map_competition(comp)
                    sports.add(mapped["sport"])
                    leagues.add(mapped["league"])

        # Build bet_info string matching Pikkit format
        bet_info = " | ".join(
            f"{l['selection']} {l['market']} {l['event']}" for l in legs_info
        )

        # Get decimal odds — prefer betPrices.betPrice.decimalPrice
        bet_prices = raw.get("betPrices", {})
        odds = (
            bet_prices.get("betPrice", {}).get("decimalPrice")
            or raw.get("betPrice")
        )

        # Calculate profit
        result = raw.get("result", "")
        pandl = raw.get("pandl", 0) or 0
        stake = raw.get("currentSize", 0) or 0
        if result == "LOST":
            profit = -float(stake)
        elif result == "WON":
            profit = float(pandl)
        else:
            profit = 0.0

        return {
            "bet_id": str(raw.get("betReceiptId", raw.get("betId", ""))),
            "sportsbook": "Fanduel Sportsbook",
            "bet_type": self._map_bet_type(raw.get("betType", "SGL")),
            "status": self._map_status(result),
            "odds": float(odds) if odds else None,
            "closing_line": None,
            "ev": None,
            "stake": float(stake),
            "profit": profit,
            "time_placed": raw.get("placedDate"),
            "time_settled": raw.get("settledDate"),
            "sport": " | ".join(sorted(sports)) if sports else "",
            "league": " | ".join(sorted(leagues)) if leagues else "",
            "bet_info": bet_info,
            "legs": json.dumps(legs_info),
            "leg_count": len(legs_info),
            "tags": self._extract_tags(raw),
            "source": "fanduel",
            "raw_json": json.dumps(raw),
        }

    @staticmethod
    def _map_competition(comp: str) -> dict:
        """Map FanDuel competitionName to sport/league."""
        mapping = {
            "Major League Baseball": {"sport": "Baseball", "league": "MLB"},
            "NBA": {"sport": "Basketball", "league": "NBA"},
            "NFL": {"sport": "American Football", "league": "NFL"},
            "NHL": {"sport": "Ice Hockey", "league": "NHL"},
            "WNBA": {"sport": "Basketball", "league": "WNBA"},
            "NCAA Football": {"sport": "American Football", "league": "NCAAFB"},
            "NCAA Basketball": {"sport": "Basketball", "league": "NCAAM"},
            "NCAA Women's Basketball": {"sport": "Basketball", "league": "NCAAW"},
        }
        return mapping.get(comp, {"sport": comp, "league": comp})

    @staticmethod
    def _map_bet_type(fd_type: str) -> str:
        """Map FanDuel betType codes to canonical types.

        FanDuel codes: SGL, DBL, TBL, ACC4-ACC20+
        """
        mapping = {
            "SGL": "straight",
            "DBL": "parlay",
            "TBL": "parlay",
        }
        if fd_type.startswith("ACC"):
            return "parlay"
        return mapping.get(fd_type.upper(), fd_type.lower())

    @staticmethod
    def _map_status(fd_status: str) -> str:
        mapping = {
            "WON": "SETTLED_WIN",
            "LOST": "SETTLED_LOSS",
            "OPEN": "PLACED",
            "PENDING": "PLACED",
            "CASHED_OUT": "CASHED_OUT",
            "VOID": "VOID",
        }
        return mapping.get(fd_status.upper(), fd_status.upper())

    @staticmethod
    def _extract_tags(raw: dict) -> str:
        """Extract tags from promo/boost features."""
        tags = []
        if raw.get("sameGameMultis"):
            tags.append("SGM")
        reward = raw.get("rewardUsed", {})
        if reward:
            rtype = reward.get("type", "")
            if rtype == "PROFIT_BOOST":
                pct = raw.get("features", {}).get("priceBoost", {}).get("generosityPercentage", "")
                tags.append(f"boost_{pct}pct" if pct else "boost")
            elif rtype == "NO_SWEAT":
                tags.append("no_sweat")
            elif rtype:
                tags.append(rtype.lower())
        return ",".join(tags)

    # ------------------------------------------------------------------
    # Market Data (for future model integration)
    # ------------------------------------------------------------------

    async def get_market_prices(self, market_ids: list[str]) -> dict:
        """Fetch current market prices.

        Endpoint: smp.co.sportsbook.fanduel.com/api/sports/fixedodds/readonly/v1/getMarketPrices
        POST body observed in browser (sent as form data with priceHistory=1).
        """
        client = await self._get_client()
        resp = await client.get(
            f"{FD_SMP_BASE}/api/sports/fixedodds/readonly/v1/getMarketPrices",
            params={"priceHistory": 1},
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    async def get_balance(self) -> dict:
        """Fetch current wallet balance."""
        client = await self._get_client()
        resp = await client.get(f"{FD_API_BASE}/account/wallet")
        resp.raise_for_status()
        return resp.json()
