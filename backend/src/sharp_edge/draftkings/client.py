"""DraftKings bet history, read through the authenticated browser.

``fanduel/client.py`` can name its endpoint because it was captured:
``/sbapi/fetch-my-bets``, with its paging params and response shape written
down. The DraftKings equivalent has not been captured, and guessing a URL
would produce a client that 404s with no explanation.

So this doesn't guess. It opens the My Bets page in the session the login
already established and **listens to what the page fetches itself**, keeping
any JSON response that looks like bet history. The endpoint is whatever
DraftKings is using today, discovered rather than declared, and the first sync
logs what it found so the mapping below can be tightened against something
real.

Reads go through the browser for the same reason the login does: the bet
endpoints sit behind the same Akamai policy as ``/v1/auth/*``, so an httpx
call carrying the session cookies would very likely be refused where the page
itself is not. Playwright's context shares the validated cookie state, so a
request issued by the page is indistinguishable from any other.

``normalize_bet`` is written defensively across plausible field names — the
same approach ``FanDuelAuth._extract_tokens`` takes, and for the same reason.
Every raw payload is kept in ``raw_json`` regardless, so nothing captured is
lost to a mapping that turns out wrong.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from . import browser as _browser

logger = logging.getLogger(__name__)

MY_BETS_URL = "https://sportsbook.draftkings.com/mybets"

# A response worth keeping: JSON, from DraftKings, with a bets-ish path.
_BETS_URL = re.compile(r"draftkings\.com/.*(bet|wager|ticket)", re.I)

# How many times to reach the bottom before giving up on more history. My Bets
# is an infinite scroll, so this is the paging control.
_MAX_SCROLLS = 25
_SCROLL_PAUSE_MS = 900

_SETTLED_STATUSES = {
    "won": "SETTLED_WIN", "win": "SETTLED_WIN", "lost": "SETTLED_LOSS",
    "loss": "SETTLED_LOSS", "lose": "SETTLED_LOSS", "push": "VOID",
    "void": "VOID", "voided": "VOID", "cancelled": "VOID", "canceled": "VOID",
    "cashedout": "CASHED_OUT", "cashed_out": "CASHED_OUT",
    "open": "PLACED", "pending": "PLACED", "live": "PLACED",
}


class DraftKingsClient:
    """Reads bet history in the browser context the login produced."""

    def __init__(self, storage_state: Optional[dict] = None, auth=None,
                 session_key: str = "dk:sync"):
        self._storage = storage_state
        self._auth = auth
        self._session_key = session_key
        self._session = None
        # Every JSON payload the page fetched that looked like bet history.
        self._captured: list[Any] = []

    async def _open(self):
        if self._session is None:
            self._session = await _browser.open_session(
                self._session_key, storage_state=self._storage
            )
            self._session.page.on("response", self._on_response)
        return self._session

    def _on_response(self, response) -> None:
        """Keep bets-shaped JSON as the page loads it.

        Synchronous by necessity — Playwright's response event handler cannot
        await — so the body is pulled in a task and failures are swallowed:
        a response that has already been discarded is normal during
        navigation and is not worth failing a sync over.
        """
        url = response.url or ""
        if not _BETS_URL.search(url):
            return

        async def grab():
            try:
                if "json" not in (response.headers.get("content-type") or ""):
                    return
                payload = await response.json()
            except Exception:
                return
            self._captured.append(payload)
            logger.info("draftkings: captured bets payload from %s", url)

        import asyncio

        asyncio.create_task(grab())

    async def fetch_all_settled_bets(self, max_pages: int = _MAX_SCROLLS) -> list[dict]:
        """Every settled bet the My Bets page will surrender.

        ``max_pages`` is scroll rounds rather than API pages, since the page
        loads history by infinite scroll.
        """
        session = await self._open()
        page = session.page
        from ..config import settings

        await page.goto(
            MY_BETS_URL, wait_until="domcontentloaded",
            timeout=settings.draftkings_timeout_ms,
        )
        await page.wait_for_timeout(2000)
        await self._select_settled(page)

        seen = 0
        for _ in range(max_pages):
            await page.mouse.wheel(0, 20000)
            await page.wait_for_timeout(_SCROLL_PAUSE_MS)
            if len(self._captured) == seen:
                # Nothing new arrived on that scroll; assume the end.
                break
            seen = len(self._captured)

        bets = []
        for payload in self._captured:
            bets.extend(_harvest_bets(payload))
        deduped = _dedupe(bets)
        logger.info(
            "draftkings: %d payloads -> %d bets (%d after dedupe)",
            len(self._captured), len(bets), len(deduped),
        )
        if deduped:
            logger.info(
                "draftkings: first bet keys = %s", sorted(deduped[0].keys())[:40]
            )
        else:
            logger.warning(
                "draftkings: no bets recognised. Captured %d payloads; if My "
                "Bets showed history, _harvest_bets needs the real shape.",
                len(self._captured),
            )
        return deduped

    @staticmethod
    async def _select_settled(page) -> None:
        """Switch to the Settled tab when there is one.

        Best-effort: the default view is usually Open bets, and settled ones
        are what the track record needs. A layout without the tab just leaves
        whatever the page showed.
        """
        for sel in ('button:has-text("Settled")', '[role="tab"]:has-text("Settled")',
                    'a:has-text("Settled")'):
            try:
                tab = page.locator(sel).first
                if await tab.is_visible(timeout=1500):
                    await tab.click()
                    await page.wait_for_timeout(1500)
                    return
            except Exception:
                continue

    async def fetch_open_bets(self) -> list[dict]:
        session = await self._open()
        from ..config import settings

        await session.page.goto(
            MY_BETS_URL, wait_until="domcontentloaded",
            timeout=settings.draftkings_timeout_ms,
        )
        await session.page.wait_for_timeout(2500)
        bets = []
        for payload in self._captured:
            bets.extend(_harvest_bets(payload))
        return _dedupe(bets)

    def normalize_bet(self, raw: dict) -> dict:
        """Map one DraftKings bet onto the canonical schema.

        Field names are tried across the variants DraftKings and community
        clients have used. Anything unmatched still reaches the database in
        ``raw_json``, so a wrong guess here costs a column rather than the row.
        """
        legs_raw = _first(raw, "legs", "selections", "outcomes", "picks") or []
        legs_info = []
        leagues, sports = set(), set()

        for leg in legs_raw if isinstance(legs_raw, list) else []:
            if not isinstance(leg, dict):
                continue
            league = _first(leg, "league", "leagueName", "competition",
                            "competitionName", "sport") or ""
            info = {
                "event": _first(leg, "event", "eventName", "eventDescription",
                                "gameName") or "",
                "market": _first(leg, "market", "marketName",
                                 "marketDescription") or "",
                "selection": _first(leg, "selection", "selectionName",
                                    "outcomeName", "displayName") or "",
                "odds_american": _to_num(_first(leg, "americanOdds", "oddsAmerican",
                                                "displayOdds", "odds")),
                "result": str(_first(leg, "result", "status", "settlementStatus")
                              or ""),
                "competition": league,
            }
            legs_info.append(info)
            if league:
                mapped = self._map_league(str(league))
                sports.add(mapped["sport"])
                leagues.add(mapped["league"])

        stake = _to_num(_first(raw, "stake", "wagerAmount", "betAmount", "risk")) or 0.0
        status_raw = str(_first(raw, "status", "result", "settlementStatus",
                                "betStatus") or "")
        status = _SETTLED_STATUSES.get(
            status_raw.lower().replace(" ", "").replace("-", "_"),
            status_raw.upper(),
        )
        payout = _to_num(_first(raw, "payout", "potentialReturn", "returnAmount",
                                "winAmount", "totalPayout"))
        profit = _profit(status, stake, payout)

        return {
            "bet_id": str(_first(raw, "betId", "id", "wagerId", "ticketId",
                                 "betReceiptId") or ""),
            "sportsbook": "DraftKings",
            "bet_type": _bet_type(raw, len(legs_info)),
            "status": status,
            "odds": _to_num(_first(raw, "americanOdds", "oddsAmerican",
                                   "displayOdds", "odds", "price")),
            "closing_line": None,
            "ev": None,
            "stake": float(stake),
            "profit": profit,
            "time_placed": _first(raw, "placedDate", "placedAt", "createdAt",
                                  "timePlaced", "datePlaced"),
            "time_settled": _first(raw, "settledDate", "settledAt",
                                   "timeSettled", "dateSettled"),
            "sport": " | ".join(sorted(sports)) if sports else "",
            "league": " | ".join(sorted(leagues)) if leagues else "",
            "bet_info": " | ".join(
                f"{l['selection']} {l['market']} {l['event']}".strip()
                for l in legs_info
            ),
            "legs": json.dumps(legs_info),
            "leg_count": len(legs_info),
            "tags": "",
            "source": "draftkings",
            "raw_json": json.dumps(raw, default=str),
        }

    @staticmethod
    def _map_league(name: str) -> dict:
        mapping = {
            "MLB": {"sport": "Baseball", "league": "MLB"},
            "NFL": {"sport": "American Football", "league": "NFL"},
            "NBA": {"sport": "Basketball", "league": "NBA"},
            "NHL": {"sport": "Ice Hockey", "league": "NHL"},
            "WNBA": {"sport": "Basketball", "league": "WNBA"},
            "NCAAF": {"sport": "American Football", "league": "NCAAFB"},
            "NCAAB": {"sport": "Basketball", "league": "NCAAM"},
        }
        key = name.strip().upper()
        for token, mapped in mapping.items():
            if token in key:
                return mapped
        return {"sport": name, "league": name}

    async def close(self) -> None:
        await _browser.close_session(self._session_key)
        self._session = None


# --------------------------------------------------------------------------
# Payload walking
# --------------------------------------------------------------------------

def _harvest_bets(payload: Any) -> list[dict]:
    """Find the bet objects anywhere in a captured response.

    The envelope is unknown and has no reason to be stable, so this walks for
    objects that *look* like a bet — an identifier alongside a stake, odds or
    a settlement status — rather than reaching down a guessed path. Over-
    matching is cheap (duplicates are dropped and raw payloads are kept);
    missing the array entirely is not.
    """
    found: list[dict] = []
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if _looks_like_bet(node):
                found.append(node)
                continue  # don't also harvest its legs as bets
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(n for n in node if isinstance(n, (dict, list)))
    return found


_ID_KEYS = ("betid", "wagerid", "ticketid", "betreceiptid")
_BET_KEYS = ("stake", "wageramount", "betamount", "risk", "payout",
             "potentialreturn", "settlementstatus", "betstatus", "legs",
             "selections")


def _looks_like_bet(node: dict) -> bool:
    flat = {k.lower().replace("_", ""): v for k, v in node.items()}
    has_id = any(k in flat for k in _ID_KEYS) or (
        "id" in flat and any(k in flat for k in _BET_KEYS)
    )
    return has_id and any(k in flat for k in _BET_KEYS)


def _dedupe(bets: list[dict]) -> list[dict]:
    """One row per bet id. The same payload is often fetched twice as the
    page re-renders, and scrolling re-requests overlapping windows."""
    out, seen = [], set()
    for bet in bets:
        key = str(_first(bet, "betId", "id", "wagerId", "ticketId") or id(bet))
        if key in seen:
            continue
        seen.add(key)
        out.append(bet)
    return out


def _first(node: dict, *names):
    """First present, non-empty value among several candidate key names,
    matched case- and underscore-insensitively."""
    flat = {k.lower().replace("_", ""): v for k, v in node.items()}
    for name in names:
        value = flat.get(name.lower().replace("_", ""))
        if value not in (None, "", []):
            return value
    return None


def _to_num(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("$", "").replace(",", "").replace("+", ""))
    except (TypeError, ValueError):
        return None


def _bet_type(raw: dict, leg_count: int) -> str:
    declared = str(_first(raw, "betType", "type", "wagerType") or "").lower()
    if "parlay" in declared or "accumulator" in declared:
        return "parlay"
    if "teaser" in declared:
        return "teaser"
    if "round" in declared:
        return "round_robin"
    if "straight" in declared or "single" in declared:
        return "straight"
    return "parlay" if leg_count > 1 else "straight"


def _profit(status: str, stake: float, payout: Optional[float]) -> float:
    """Profit, not return. A loss is the stake back out.

    DraftKings reports a payout that includes the stake, so the profit on a
    win is payout minus stake — subtracting it is the whole point, and
    forgetting to would overstate every winning bet by its own stake.
    """
    if status == "SETTLED_LOSS":
        return -float(stake)
    if status == "SETTLED_WIN" and payout is not None:
        return float(payout) - float(stake)
    return 0.0
