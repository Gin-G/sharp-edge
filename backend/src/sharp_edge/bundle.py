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
    min_ev: float = 0.0,
    cross_game: bool = True,
) -> list[dict]:
    """The day's bundle: priced, +EV picks ranked by EV, best first.

    ``min_ev=0`` keeps only bets that are actually positive at the quoted
    price. That is the whole point of having odds — on 2026-08-07 it cut a
    9-pick board to 3, and the two it dropped hardest were the *best*
    matchups on the board, priced at -290 and -280.
    """
    priced = [
        r for r in records
        if r.get("fd_odds") is not None
        and r.get("ev") is not None
        and r.get("fd_market_id") is not None
        and r.get("fd_selection_id") is not None
        and r["ev"] >= min_ev
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
    """Priced picks that didn't clear the bar, best first.

    A one- or zero-leg bundle is frequently the honest answer — the market
    prices most of the screen's edge already — but "nothing today" is a much
    more useful message when you can see what was close and by how much.
    ``needs`` is the price each would have to reach to become a bet.
    """
    taken = {id(r) for r in chosen}
    out = [
        {
            "batter": r.get("batter"),
            "opposing_pitcher": r.get("opposing_pitcher"),
            "fd_odds": r.get("fd_odds"),
            "ev": r.get("ev"),
            "edge_pts": r.get("edge_pts"),
            "needs": r.get("breakeven_odds"),
        }
        for r in records
        if id(r) not in taken and r.get("ev") is not None
    ]
    out.sort(key=lambda r: -(r["ev"] or 0))
    return out[:limit]


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
