"""The day's bets, and a link that loads them into the slip.

**What gets picked.** The batters most likely to record a hit, ranked by the
model's probability, one per game, capped at a handful. Qualifying for the
screen and being worth betting are different things: everything that clears
the filters hits 67.4% together, while the best 3 a day hit 72.5%.

**Price is shown, not obeyed.** Odds and EV ride along on every leg because
they're worth knowing — a pick at -330 is a poor price whatever the read — but
they don't decide the card. Gating on EV handed selection to the market: legs
vanished when a line moved a few cents, and a slate of short prices produced
"bet nothing" even when the reads were good. It was also a finer distinction
than the model can actually make, its calibration being off by ~1.7 points.

**One leg per game.** Two batters facing the same starter are one bet on that
pitcher having a bad day, not two independent reads. A book prices them as a
same-game parlay, below the product of their legs, for exactly that reason.

**Linking.** A card you have to re-enter by hand isn't much use at 6:50pm with
first pitch at 7:05. FanDuel selections carry a ``marketId`` and
``selectionId``, and its ``addToBetslip`` endpoint takes them as repeated
indexed parameters, so the card can arrive as a loaded slip.
"""

from __future__ import annotations

from typing import Iterable, Optional
from urllib.parse import quote

# FanDuel's deep link. The state subdomain matters — a link built for one
# state bounces a user whose account is registered in another.
BETSLIP_BASE = "https://{state}.sportsbook.fanduel.com/addToBetslip"

# The bundle is a *parlay*, and a parlay wants few legs. That is a different
# objective from the pick list, which is why they now differ: hit rate rewards
# taking every good bet, a parlay punishes it, because every extra leg is
# another chance to lose the whole ticket.
#
# Sweep rate over 129 days — the share of days where every leg won, which is
# the only outcome a parlay pays on:
#
#     all gated picks   3.5 legs   35.9% of days swept
#     best 3            2.5 legs   41.9%
#     best 2            1.8 legs   50.0%     <- here
#     best 1            1.0 legs   73.6%     (not a parlay)
#
# Two legs at around -200 each is roughly +125, so this still clears the
# plus-odds bar while sweeping half the days instead of a third.
#
# Three legs has the higher expected value at those prices (+0.41 per dollar
# against +0.13) because the payout grows faster than the sweep rate falls.
# Two is the choice for cashing more often; three is the choice for making
# more money slowly. Change it here.
DEFAULT_MAX_LEGS: Optional[int] = 2


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
    max_legs: Optional[int] = DEFAULT_MAX_LEGS,
    min_edge_pts: float | None = None,
    cross_game: bool = True,
) -> list[dict]:
    """The day's bets: the picks most likely to record a hit, best first.

    Ranked by ``model_p`` — the probability the batter gets a hit, which is
    the thing being bet. **Price does not gate this.** A pick is a pick
    because we think the man gets a hit; the odds tell you what it pays and
    whether it looks dear, and both are worth seeing, but neither is a good
    enough signal to overrule the read.

    That's a deliberate reversal. Gating on expected value meant the market
    chose the card: a leg the model liked vanished the moment the line moved a
    few cents, and on a slate where every price was short the answer was
    "bet nothing" even when the reads were good. Worse, the gate was only as
    trustworthy as the model's calibration, which is off by ~1.7 points — so
    it was discarding real picks on a distinction it couldn't actually make.

    ``min_edge_pts`` is still honoured when passed explicitly, for anyone who
    does want a price floor. It just isn't the default any more.
    """
    priced = [
        r for r in records
        if r.get("fd_market_id") is not None
        and r.get("fd_selection_id") is not None
        and r.get("model_p") is not None
    ]
    if min_edge_pts is not None:
        priced = [
            r for r in priced
            if r.get("edge_pts") is not None and r["edge_pts"] >= min_edge_pts
        ]
    priced.sort(key=lambda r: (-(r.get("model_p") or 0), -(r.get("ev") or -9)))

    out: list[dict] = []
    seen_games: set = set()
    for r in priced:
        if cross_game:
            # Two batters in one game are one bet on that pitcher having a bad
            # day, not two independent reads. Prefer the pitcher id; fall back
            # to the FanDuel event so a row without board context still can't
            # double up.
            game = r.get("pitcher_id") or r.get("fd_event_id")
            if game is not None and game in seen_games:
                continue
            if game is not None:
                seen_games.add(game)
        out.append(r)
        if max_legs and len(out) >= max_legs:
            break
    return out


def near_misses(board: list[dict], chosen: list[dict], limit: int = 5) -> list[dict]:
    """Batters who qualified but ranked below the cut, best first.

    Pass the whole board, not the picks — the screen already truncates picks
    to the day's best few, so anything it dropped is only visible here. Worth
    showing for two reasons: seeing who just missed, and spotting a name you
    have a read on that the model happened to rank fourth.
    """
    from . import pricing

    taken = {(r.get("batter"), r.get("pitcher_id")) for r in chosen}
    out = [
        {
            "batter": r.get("batter"),
            "opposing_pitcher": r.get("opposing_pitcher"),
            "model_p": r.get("model_p"),
            "fd_odds": r.get("fd_odds"),
            "ev": r.get("ev"),
            "edge_pts": r.get("edge_pts"),
        }
        for r in board
        if pricing.is_screen_pick(r)
        and (r.get("batter"), r.get("pitcher_id")) not in taken
    ]
    out.sort(key=lambda r: -(r["model_p"] if r["model_p"] is not None else 0))
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
