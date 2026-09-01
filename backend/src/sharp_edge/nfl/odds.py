"""FanDuel NFL prop prices — the public, unauthenticated odds board.

Same door as ``fanduel/odds.py`` uses for baseball: the JSON that renders the
public board is served to anyone holding the API key from FanDuel's JS bundle,
so nothing here logs in and prices keep working when the session has expired.

Football differs from baseball in two ways that shape this module.

**The props live behind named tabs.** ``event-page`` with no ``tab`` returns
quarter and half markets; the player props are on ``passing-props``,
``receiving-props``, ``rushing-props`` and ``td-scorer-props``. Each is a
separate request, so a full week is 16 games x 4 tabs.

**Every market is two-sided.** A baseball to-record-a-hit market quotes one
runner; a yardage prop quotes Over and Under against a shared handicap. That
is strictly better — a two-sided quote can be devigged properly instead of
having its margin guessed at — and it is why ``Line`` carries both prices.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable, Optional

import httpx

from ..fanduel.client import DEFAULT_HEADERS, FD_API_KEY
from .names import norm_name

logger = logging.getLogger(__name__)

FD_SBAPI_BASE = "https://sbapi.{state}.sportsbook.fanduel.com/api"

PROP_TABS = ("passing-props", "receiving-props", "rushing-props", "td-scorer-props")

# The four over/under markets the screen prices. FanDuel suffixes each with
# _HIGH / _MEDIUM / _LOW — that is a tier of player, not a tier of line, so all
# three are the same market and are folded together here.
MAIN_MARKETS = {
    "PLAYER_X_PASSING_YARDS": "passing_yards",
    "PLAYER_X_RUSHING_YARDS": "rushing_yards",
    "PLAYER_X_RECEIVING_YARDS": "receiving_yards",
    "PLAYER_X_RECEPTIONS": "receptions",
}
# The alternate ladders behind each. Carried through so a leg can be shopped up
# or down the ladder later; the screen itself reads the main line.
ALT_MARKETS = {f"PLAYER_X_ALT_{k.removeprefix('PLAYER_X_')}": v
               for k, v in MAIN_MARKETS.items()}

TD_MARKET = "ANY_TIME_TOUCHDOWN_SCORER"
GAME_MARKETS = {
    "MONEY_LINE": "moneyline",
    "TOTAL_POINTS_(OVER/UNDER)": "total",
    "MATCH_HANDICAP_(2-WAY)": "spread",
}

_TIER_SUFFIX = re.compile(r"_(HIGH|MEDIUM|LOW)$")
# Main lines name their runners "<Player> Over" / "<Player> Under"; an alt rung
# names them "<Player> 25+ Yards" or "<Player> 2+ Receptions", with the number
# repeated in the handicap. Both suffixes have to come off to leave a name that
# joins against a projection.
_SIDE_SUFFIX = re.compile(
    r"\s+(?:Over|Under|\d+\+\s+[A-Za-z ]+)\s*$", re.IGNORECASE
)

# A full week is 16 games; at 4 tabs each that is 64 requests. Five at a time
# keeps it brisk without looking like a scrape.
_MAX_CONCURRENCY = 5


@dataclass
class Line:
    """One two-sided player prop at one number."""
    market: str            # receiving_yards / receptions / …
    player: str            # as FanDuel writes it
    key: str               # normalised, for joining
    line: float
    over: Optional[int] = None
    under: Optional[int] = None
    market_id: Optional[str] = None
    over_selection: Optional[int] = None
    under_selection: Optional[int] = None
    sgm: bool = False
    event_id: Optional[str] = None
    event: Optional[str] = None
    kickoff: Optional[str] = None
    alt: bool = False

    def as_dict(self) -> dict:
        return {
            "market": self.market, "player": self.player, "key": self.key,
            "line": self.line, "over": self.over, "under": self.under,
            "fd_market_id": self.market_id,
            "over_selection_id": self.over_selection,
            "under_selection_id": self.under_selection,
            "sgm": self.sgm, "fd_event_id": self.event_id,
            "event": self.event, "kickoff": self.kickoff, "alt": self.alt,
        }


@dataclass
class Board:
    """Everything the screen needs from one week of the FanDuel NFL board."""
    lines: list[Line] = field(default_factory=list)
    tds: list[dict] = field(default_factory=list)
    games: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    error: Optional[str] = None

    def by_market(self, market: str, alt: bool = False) -> dict[str, Line]:
        """``{normalised player: Line}`` for one market's main (or alt) lines.

        A player has one main line per market, so a flat map is safe. Alt rungs
        are not unique per player, so pass ``alt=True`` only to check existence
        — use ``ladder`` to walk the rungs.
        """
        return {ln.key: ln for ln in self.lines
                if ln.market == market and ln.alt == alt}

    def ladder(self, market: str, key: str) -> list[Line]:
        """Every alternate rung posted for one player in one market, low first."""
        return sorted((ln for ln in self.lines
                       if ln.market == market and ln.key == key and ln.alt),
                      key=lambda ln: ln.line)


def _american(runner: dict) -> Optional[int]:
    odds = ((runner.get("winRunnerOdds") or {})
            .get("americanDisplayOdds") or {}).get("americanOddsInt")
    return int(odds) if odds is not None else None


def _player_of(runner_name: str) -> str:
    return _SIDE_SUFFIX.sub("", runner_name or "").strip()


class FanDuelNFLOdds:
    """Reads the public NFL board. No authentication, no shared state."""

    def __init__(self, state: str = "CO", transport=None, timeout: float = 30.0):
        self.state = state.lower()
        self._base = FD_SBAPI_BASE.format(state=self.state)
        self._transport = transport
        self._timeout = timeout

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict) -> dict:
        resp = await client.get(f"{self._base}/{path}", params={**params, "_ak": FD_API_KEY})
        resp.raise_for_status()
        return resp.json()

    async def fetch_events(self, client: httpx.AsyncClient) -> list[dict]:
        """Every NFL game FanDuel currently lists, soonest first.

        The NFL page also carries futures and award "events" with open dates
        months out; a real game has two teams and an ``@``.
        """
        data = await self._get(
            client, "content-managed-page",
            {"page": "CUSTOM", "customPageId": "nfl", "pbHorizontal": "false",
             "timezone": "America/Denver"},
        )
        out = []
        for eid, ev in (data.get("attachments", {}).get("events", {}) or {}).items():
            name = ev.get("name") or ""
            if "@" not in name or not ev.get("competitionId"):
                continue
            out.append({"event_id": str(eid), "name": name,
                        "kickoff": ev.get("openDate")})
        out.sort(key=lambda e: e["kickoff"] or "")
        return out

    async def fetch_event_markets(
        self, client: httpx.AsyncClient, event: dict, tab: Optional[str]
    ) -> list[dict]:
        params = {"eventId": event["event_id"]}
        if tab:
            params["tab"] = tab
        try:
            data = await self._get(client, "event-page", params)
        except Exception as e:
            logger.warning("[fd-nfl] event %s tab %s: %s", event["event_id"], tab, e)
            return []
        return list((data.get("attachments", {}).get("markets", {}) or {}).values())

    async def fetch_board(self, events: list[dict]) -> Board:
        """Pull every prop tab for every event and fold it into one Board."""
        board = Board(events=events)
        async with httpx.AsyncClient(
            headers=DEFAULT_HEADERS, timeout=self._timeout, transport=self._transport,
        ) as client:
            sem = asyncio.Semaphore(_MAX_CONCURRENCY)

            async def one(ev, tab):
                async with sem:
                    return ev, await self.fetch_event_markets(client, ev, tab)

            # The game markets (moneyline, total, spread) ride along on every
            # prop tab, so no extra request is needed for them.
            jobs = [one(ev, tab) for ev in events for tab in PROP_TABS]
            results = await asyncio.gather(*jobs)

        seen: set = set()
        for ev, markets in results:
            for m in markets:
                mid = m.get("marketId")
                if mid in seen:
                    continue
                seen.add(mid)
                _absorb(board, ev, m)
        return board


def _absorb(board: Board, ev: dict, m: dict) -> None:
    """Fold one FanDuel market into the board, if it's one we price."""
    mt = m.get("marketType") or ""
    if m.get("marketStatus") not in (None, "OPEN"):
        return
    base = _TIER_SUFFIX.sub("", mt)
    # `fd_event_id` rather than `event_id`, because these dicts are handed
    # to the API verbatim and every other FanDuel id on the wire is fd_-
    # prefixed. Getting this wrong is silent: the games table simply renders
    # empty, with no error on either side.
    ctx = {"fd_event_id": ev["event_id"], "event": ev["name"], "kickoff": ev["kickoff"]}

    if base in MAIN_MARKETS or base in ALT_MARKETS:
        alt = base in ALT_MARKETS
        market = (ALT_MARKETS if alt else MAIN_MARKETS)[base]
        # An alt ladder puts several handicaps in one market, so group the
        # runners by their number before pairing over with under.
        rungs: dict[float, Line] = {}
        for r in m.get("runners") or []:
            if r.get("runnerStatus") not in (None, "ACTIVE"):
                continue
            odds = _american(r)
            hcap = r.get("handicap")
            if odds is None or hcap is None:
                continue
            player = _player_of(r.get("runnerName"))
            ln = rungs.get(hcap)
            if ln is None:
                ln = rungs[hcap] = Line(
                    market=market, player=player, key=norm_name(player), line=float(hcap),
                    market_id=m.get("marketId"), sgm=bool(m.get("sgmMarket")),
                    alt=alt, event_id=ctx["fd_event_id"], event=ctx["event"],
                    kickoff=ctx["kickoff"],
                )
            side = (r.get("result") or {}).get("type")
            if side == "UNDER":
                ln.under, ln.under_selection = odds, r.get("selectionId")
            else:
                ln.over, ln.over_selection = odds, r.get("selectionId")
        board.lines.extend(rungs.values())
        return

    if mt == TD_MARKET:
        for r in m.get("runners") or []:
            if r.get("runnerStatus") not in (None, "ACTIVE"):
                continue
            odds = _american(r)
            name = r.get("runnerName")
            if odds is None or not name:
                continue
            board.tds.append({
                "player": name, "key": norm_name(name), "odds": odds,
                "fd_market_id": m.get("marketId"),
                "fd_selection_id": r.get("selectionId"),
                "sgm": bool(m.get("sgmMarket")), **ctx,
            })
        return

    if mt in GAME_MARKETS:
        board.games.append({
            "market": GAME_MARKETS[mt], "fd_market_id": m.get("marketId"), **ctx,
            "runners": [
                {"name": r.get("runnerName"), "odds": _american(r),
                 "handicap": r.get("handicap"),
                 "fd_selection_id": r.get("selectionId")}
                for r in (m.get("runners") or [])
                if r.get("runnerStatus") in (None, "ACTIVE") and _american(r) is not None
            ],
        })


# ---------------------------------------------------------------------------
# Week selection
# ---------------------------------------------------------------------------

def events_in_window(events: Iterable[dict],
                     window: tuple[datetime, datetime]) -> list[dict]:
    """The listed events that kick off inside ``window``.

    The window comes from the schedule (``projections.Week.window``) rather
    than from a rule about today's date. A date rule looks fine in-season and
    breaks in the one week that matters most — the gap before week 1, where
    "the current Tuesday to the next" contains no games at all.
    """
    lo, hi = window
    out = []
    for ev in events:
        k = ev.get("kickoff")
        if not k:
            continue
        try:
            dt = datetime.fromisoformat(k.replace("Z", "+00:00"))
        except ValueError:
            continue
        if lo <= dt < hi:
            out.append(ev)
    return out


# ---------------------------------------------------------------------------
# Slate cache
# ---------------------------------------------------------------------------
#
# Prices move, so this is a short TTL — but a week's board is 64 requests
# rather than baseball's 15, so it is a longer one than the MLB cache uses. A
# failed refresh keeps serving the last good board: stale prices beat none, and
# the fetched-at stamp lets the UI say how old they are.

_cache: dict = {"board": None, "fetched_at": 0.0, "window": None, "error": None}
_CACHE_TTL_SECONDS = 600
_lock = asyncio.Lock()


async def cached_board(
    window: tuple[datetime, datetime], state: str = "CO", force: bool = False
) -> dict:
    """``{"board": Board|None, "age_seconds": n, "error": str|None}``.

    ``window`` is the week's UTC bracket, from ``projections.Week.window``. It
    doubles as the cache key, so rolling into a new week invalidates the board
    without any explicit expiry.
    """
    import time as _time

    key = window[0].isoformat()
    now = _time.time()

    async with _lock:
        fresh = (
            not force
            and _cache["window"] == key
            and now - _cache["fetched_at"] < _CACHE_TTL_SECONDS
            and _cache["board"] is not None
        )
        if not fresh:
            try:
                odds = FanDuelNFLOdds(state=state)
                async with httpx.AsyncClient(
                    headers=DEFAULT_HEADERS, timeout=odds._timeout
                ) as client:
                    events = await odds.fetch_events(client)
                week = events_in_window(events, window)
                if not week:
                    _cache["error"] = "no NFL events listed for this week"
                else:
                    board = await odds.fetch_board(week)
                    _cache.update({"board": board, "fetched_at": now,
                                   "window": key, "error": None})
                    logger.info("[fd-nfl] %d games -> %d lines, %d TD prices",
                                len(week), len(board.lines), len(board.tds))
            except Exception as e:
                logger.warning("[fd-nfl] board fetch failed, serving cached: %s", e)
                _cache["error"] = str(e)

    board = _cache["board"] if _cache["window"] == key else None
    return {
        "board": board,
        "fetched_at": _cache["fetched_at"],
        "age_seconds": round(now - _cache["fetched_at"]) if _cache["fetched_at"] else None,
        "error": _cache["error"],
    }
