"""Pick the bets worth making, and build a link that loads them.

Two jobs the screen didn't previously do.

**Selecting.** The board is ~12 picks a day now, and the backtest is blunt
about what that's worth: all picks together hit 67.4%, which at -207 is
break-even. Ranking by calibrated probability and keeping only the top of the
board hit 74.0% at 1/day and 73.5% at 2/day, and those held up across halves
of the season (drift +0.9 and +4.5) where taking three or more did not (+7.7).
Now that prices are attached, the ranking is by **expected value** rather than
probability — a 71.5% pick at -290 is a worse bet than a 66.5% pick at -185,
and only EV says so.

**Linking.** A bundle you have to re-enter by hand isn't much use at 6:50pm
with first pitch at 7:05. FanDuel selections carry a ``marketId`` and
``selectionId``, and its ``addToBetslip`` endpoint takes them as repeated
indexed parameters, so a bundle can arrive as a loaded slip.

One rule shapes the default: **one leg per game.** 57% of naive top-2 bundles
were two batters facing the same starter, which a book prices as a same-game
parlay well below the product of its legs, precisely because it knows the
outcomes are correlated. Cross-game legs are priced independently and are the
ones you can actually get down at the quoted number.
"""

from __future__ import annotations

from typing import Iterable, Optional
from urllib.parse import quote

# FanDuel's deep link. The state subdomain matters — a link built for one
# state bounces a user whose account is registered in another.
BETSLIP_BASE = "https://{state}.sportsbook.fanduel.com/addToBetslip"

DEFAULT_MAX_LEGS = 3


def betslip_url(selections: Iterable[dict], state: str = "co") -> Optional[str]:
    """Build an addToBetslip link from rows carrying FanDuel ids.

    Each selection needs ``fd_market_id`` and ``fd_selection_id``; rows
    missing either are skipped, since a half-built link is worse than none.
    Returns ``None`` when nothing usable is left.
    """
    parts: list[str] = []
    i = 0
    for sel in selections:
        market = sel.get("fd_market_id")
        selection = sel.get("fd_selection_id")
        if market is None or selection is None:
            continue
        # Brackets stay literal. FanDuel's own links are written that way and
        # percent-encoding them (%5B/%5D) is the difference between a loaded
        # slip and a shrug. Ids are still escaped — a market id is a dotted
        # decimal, not something to trust unquoted.
        parts.append(f"marketId[{i}]={quote(str(market), safe='')}")
        parts.append(f"selectionId[{i}]={quote(str(selection), safe='')}")
        i += 1
    if not parts:
        return None
    return f"{BETSLIP_BASE.format(state=state.lower())}?{'&'.join(parts)}"


def build(
    records: list[dict],
    max_legs: int = DEFAULT_MAX_LEGS,
    min_edge_pts: float | None = None,
    cross_game: bool = True,
) -> list[dict]:
    """The day's bundle: picks clearing the edge threshold, ranked by EV.

    The gate is a minimum model-vs-market gap, defaulting to
    ``pricing.MIN_EDGE_PTS`` (3 points). Merely positive isn't enough: the
    model's level is off by ~1.7 points on held-out picks, so a half-point
    edge is indistinguishable from zero and betting it means paying the vig to
    act on rounding.

    Ranking stays on EV even though the gate is on edge — once a pick has
    cleared, the question is how much it returns per dollar, and that depends
    on the price as well as the gap.
    """
    from . import pricing

    threshold = pricing.MIN_EDGE_PTS if min_edge_pts is None else min_edge_pts
    priced = [
        r for r in records
        if r.get("fd_odds") is not None
        and r.get("ev") is not None
        and r.get("edge_pts") is not None
        and r.get("fd_market_id") is not None
        and r.get("fd_selection_id") is not None
        and r["edge_pts"] >= threshold
    ]
    priced.sort(key=lambda r: (-r["ev"], -(r.get("model_p") or 0)))

    out: list[dict] = []
    seen_games: set = set()
    for r in priced:
        if cross_game:
            # Prefer the pitcher id; fall back to the FanDuel event so a row
            # without board context still can't double up on one game.
            game = r.get("pitcher_id") or r.get("fd_event_id")
            if game is not None and game in seen_games:
                continue
            if game is not None:
                seen_games.add(game)
        out.append(r)
        if len(out) >= max_legs:
            break
    return out


def near_misses(records: list[dict], chosen: list[dict], limit: int = 4) -> list[dict]:
    """Priced picks that didn't clear the threshold, closest first.

    A short or empty bundle is frequently the honest answer — the market
    prices most of the screen's edge already — but "nothing today" is far more
    useful when you can see what was close and by how much.

    ``needs`` is the price at which the pick would clear, which is the
    actionable number: the threshold is on edge, so it's the price that buys
    the missing points, not merely break-even.
    """
    from . import pricing

    taken = {id(r) for r in chosen}
    out = []
    for r in records:
        if id(r) in taken or r.get("edge_pts") is None:
            continue
        p = r.get("model_p")
        out.append({
            "batter": r.get("batter"),
            "opposing_pitcher": r.get("opposing_pitcher"),
            "fd_odds": r.get("fd_odds"),
            "ev": r.get("ev"),
            "edge_pts": r.get("edge_pts"),
            "short_by": round(pricing.MIN_EDGE_PTS - r["edge_pts"], 1),
            "needs": _price_for_edge(p, pricing.MIN_EDGE_PTS) if p else None,
        })
    # Explicit None check: an edge of exactly 0.0 is falsy, and `or` would
    # sort a dead-level pick below one that missed by four points.
    out.sort(key=lambda r: -(r["edge_pts"] if r["edge_pts"] is not None else -99))
    return out[:limit]


def _price_for_edge(model_p: float, edge_pts: float) -> Optional[int]:
    """The price at which ``model_p`` would carry ``edge_pts`` of edge.

    Implied probability has to fall to ``model_p - edge``, so this is the
    longer price you'd need to see quoted.
    """
    target = model_p - edge_pts / 100.0
    if target <= 0:
        return None
    dec = 1 / target
    return round((dec - 1) * 100) if dec >= 2 else -round(100 / (dec - 1))


def summarise(bundle: list[dict]) -> dict:
    """Combined odds and EV for the bundle taken as a parlay.

    Legs are treated as independent, which is what cross-game selection is
    for. If ``cross_game`` was turned off this overstates both the payout and
    the probability, because the book will price the correlation and the
    outcomes really are correlated.
    """
    if not bundle:
        return {"legs": 0, "decimal": None, "american": None,
                "model_p": None, "ev": None, "implied_p": None}

    dec = 1.0
    p = 1.0
    implied = 1.0
    for r in bundle:
        dec *= 1 + (100 / -r["fd_odds"] if r["fd_odds"] < 0 else r["fd_odds"] / 100)
        p *= r.get("model_p") or 0.0
        implied *= r.get("implied_p") or 0.0
    american = round((dec - 1) * 100) if dec >= 2 else -round(100 / (dec - 1))
    return {
        "legs": len(bundle),
        "decimal": round(dec, 4),
        "american": american,
        "model_p": round(p, 4),
        "implied_p": round(implied, 4),
        "ev": round(p * (dec - 1) - (1 - p), 4),
    }
