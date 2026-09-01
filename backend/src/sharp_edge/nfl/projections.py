"""Projections and schedule, read from the NFL-API service.

The projection model already exists — ``nfl-data-py``'s ``nfl_projections``
package, precomputed weekly by NFL-API's ``compute_projections`` job and served
from ``/projections/``. Nothing is refit here; this is a client.

Two things it does on top of a plain HTTP call, and both matter.

**Undo the availability discount.** Every component projection has already been
multiplied by ``exp_games``, the expected fraction of the week the player is
available for. That is right for season-long fantasy and wrong for a prop: a
prop is void if the man does not play, so the number to compare against a line
is the on-field rate, ``component / exp_games``. Skipping this reads every
player about 20% low.

**Say when the projection is preseason.** Week 1 projections are computed in
August off prior seasons and rookie priors, and they sit on a visibly different
scale from the market — see ``screen.calibrate_to_market`` for the measurement
and the correction. ``preseason`` on the response is what lets the screen apply
that correction only where it is needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

import httpx

from ..config import settings
from .names import norm_name

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://nfl-api.nickknows.net"

COMPONENTS = ("passing_yards", "rushing_yards", "receiving_yards", "receptions",
              "passing_tds", "rushing_tds", "receiving_tds")

# Below this, dividing by exp_games turns a rounding error into a projection.
# A player expected to play a fifth of the week is not someone we have a read
# on either way.
_MIN_EXP_GAMES = 0.2

def base_url() -> str:
    return (getattr(settings, "nfl_api_base", None) or DEFAULT_BASE).rstrip("/")


@dataclass
class WeekProjections:
    season: int
    week: int
    rows: list[dict]
    preseason: bool
    computed_at: Optional[str] = None

    def by_key(self) -> dict[str, dict]:
        """``{normalised name: row}``. Later rows lose — the API returns
        best-projected first, so the first row for a name is the one to keep."""
        out: dict[str, dict] = {}
        for r in self.rows:
            out.setdefault(r["key"], r)
        return out


async def fetch_projections(
    season: int, week: int, client: Optional[httpx.AsyncClient] = None,
    timeout: float = 30.0,
) -> WeekProjections:
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        r = await client.get(f"{base_url()}/projections/",
                             params={"season": season, "week": week, "limit": 2000})
        r.raise_for_status()
        data = r.json()
    finally:
        if own:
            await client.aclose()

    if data.get("status") != "success":
        raise RuntimeError(data.get("message") or f"projections unavailable: {data.get('status')}")

    rows = []
    computed = None
    for row in data["data"]:
        eg = row.get("exp_games")
        eg = max(float(eg), _MIN_EXP_GAMES) if eg is not None else 1.0
        out = {
            "player": row.get("player_name"),
            "key": norm_name(row.get("player_name")),
            "player_id": row.get("player_id"),
            "position": row.get("position"),
            "team": row.get("team"),
            "exp_games": row.get("exp_games"),
            "prediction_type": row.get("prediction_type"),
            "projected_points": row.get("projected_points"),
        }
        for c in COMPONENTS:
            v = row.get(c)
            # Per game *played*: undo the availability discount baked into the
            # stored component. See the module docstring.
            out[c] = (float(v) / eg) if v is not None else None
        rows.append(out)
        computed = computed or row.get("computed_at")

    return WeekProjections(
        season=season, week=week, rows=rows,
        preseason=_is_preseason(computed, season), computed_at=computed,
    )


def _is_preseason(computed_at: Optional[str], season: int) -> bool:
    """Was this projection computed before the season kicked off?

    A preseason projection has no in-season usage behind it — it is a prior,
    and it is on a different scale from the market. September 1st is the cut:
    every week-1 board is built before it, and any in-season recompute lands
    after.
    """
    if not computed_at:
        return True
    try:
        dt = datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.date() < date(season, 9, 1)


async def fetch_schedule(
    season: int, week: Optional[int] = None,
    client: Optional[httpx.AsyncClient] = None, timeout: float = 30.0,
) -> list[dict]:
    """The season's games, with the closing lines nflverse carries."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    params = {"season": season}
    if week is not None:
        params["week"] = week
    try:
        r = await client.get(f"{base_url()}/schedules/", params=params)
        r.raise_for_status()
        data = r.json()
    finally:
        if own:
            await client.aclose()
    return data.get("data") or []


@dataclass
class Week:
    """The slate being bet, and the games in it."""
    season: int
    week: int
    games: list[dict]

    @property
    def gamedays(self) -> list[str]:
        return sorted({g["gameday"] for g in self.games if g.get("gameday")})

    def window(self) -> tuple[datetime, datetime]:
        """UTC bracket wide enough to catch every kickoff in the week.

        Derived from the schedule rather than from a rule about today's date,
        which matters more than it sounds: a rule anchored on "the current
        Tuesday" finds nothing at all during the gap between the preseason and
        week 1, which is exactly when the first board goes up. Kickoff times
        are local and the API's gameday is a bare date, so the bracket opens
        the day before the first game and closes two days after the last —
        enough for a Monday night game to land in UTC Tuesday.
        """
        days = self.gamedays
        if not days:
            raise RuntimeError(f"week {self.season}-{self.week} has no gamedays")
        lo = date.fromisoformat(days[0]) - timedelta(days=1)
        hi = date.fromisoformat(days[-1]) + timedelta(days=2)
        return (datetime.combine(lo, time(0, 0), timezone.utc),
                datetime.combine(hi, time(0, 0), timezone.utc))


async def current_week(
    today: Optional[date] = None, client: Optional[httpx.AsyncClient] = None
) -> Week:
    """The week to build a board for.

    Resolved from the schedule rather than from a date rule, because the NFL
    calendar shifts year to year and bye weeks make arithmetic on week numbers
    wrong. The week we want is the earliest one that still has an unplayed
    game; once the last game of a week has been played, the next week is the
    board. In the gap before week 1 that resolves to week 1, which is the
    answer we want and the one a Tuesday-anchored rule gets wrong.
    """
    today = today or date.today()
    # The NFL season is labelled by the year it starts, and it runs into
    # January, so anything before March belongs to the previous season.
    season = today.year if today.month >= 3 else today.year - 1
    games = [g for g in await fetch_schedule(season, client=client)
             if g.get("game_type") == "REG" and g.get("week") is not None]
    if not games:
        raise RuntimeError(f"no schedule for season {season}")

    upcoming = [g for g in games if g.get("gameday") and g["gameday"] >= today.isoformat()]
    # Season over — sit on the last week rather than rolling into a season that
    # has no lines yet.
    week = min(g["week"] for g in upcoming) if upcoming else max(g["week"] for g in games)
    return Week(season=season, week=week,
                games=[g for g in games if g["week"] == week])
