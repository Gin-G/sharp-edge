"""The day's bets, and a link that loads them into the slip.

**What gets picked.** The two batters most likely to record a hit, taken off
the top of the board by model probability, one per game. Not the two best
*screen qualifiers* — the filters are gone from selection, because measured
over 129 days they were picking worse bets and paying more for them. See the
ranking block in ``batters.screen_for_date`` for the numbers.

**Price is shown, not obeyed — and that finally costs nothing.** Odds and EV
ride along on every leg because they're worth knowing, but they don't decide
the card; gating on EV handed selection to the market, and legs vanished when
a line moved a few cents. The reason that used to hurt was that the old picks
were genuinely badly priced: median leg -260, a 72% break-even against a 70.5%
read. Betting the board instead moves the median leg to -185, so the card
clears its own price without the price having to choose it.

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

# The card is two legs, and grows only to reach a plus price.
#
# Sweep rate — the share of days every leg won, over 129 days of settled
# boards — against the median parlay actually quoted over 12 days of closing
# snapshots:
#
#     legs   sweep rate   median parlay   EV per $1
#      1        80.6%         -177          +0.26
#      2        58.9%         +130          +0.36     <- the floor
#      3        41.7%         +216          +0.32
#      4        29.9%         +370          +0.41
#
# Two is the shortest card that usually pays plus money. But *usually* is the
# problem: on real closing prices the two-leg card came back plus on 8 of 12
# days and minus (-102 to -129) on the other 4, because some days the two best
# batters are both priced -240 or shorter. On those days a third leg is what
# makes it a bet worth placing, and a third leg always got there.
MIN_LEGS: int = 2

# The ceiling. Reached only if the price needs that many, never as a target.
MAX_LEGS: int = 6

# Grow the card until it pays at least this. 2.0 decimal is +100.
#
# This is the knob that decides card size. Raise it for longer cards and
# longer payouts at a lower sweep rate; the frontier above is the trade.
TARGET_DECIMAL: float = 2.0

# Why extra legs are chosen on **price** rather than on the next-best
# probability, which is the part that isn't obvious.
#
# The model cannot tell the top of the board apart. On a real slate its top ten
# span 71.5% down to 70.0% — a point and a half — and measured over 129 days
# the ranks below the top two are indistinguishable from each other:
#
#     rank 1   78.9%      rank 5   72.2%
#     rank 2   75.8%      rank 6   69.0%
#     rank 3   69.8%      rank 7   68.8%
#     rank 4   70.6%      rank 8   69.6%
#
# Ranks 1 and 2 are genuinely better and are always taken. From rank 3 down it
# is a flat 69-72% whichever name you pick, so ordering that pool by
# probability is sorting on noise — while their prices on the same slate ran
# -105 to -425, which is a payout multiplier of 1.95 against 1.24. Picking on
# price there is free.
#
# The test each candidate has to pass is ``model_p * decimal > 1``, which is
# just "this leg is +EV" written so the reason is visible: a 70% leg at -475
# multiplies the payout by 1.24 while costing 30% of the ticket, and 0.70 x
# 1.24 = 0.87 says plainly that it takes more than it gives. A 70% leg at -105
# scores 1.38. Same risk, and the second one is worth adding.
MIN_LEG_VALUE: float = 1.0

# Retained so callers that passed an explicit cap keep working; ``build``
# treats it as the ceiling when given.
DEFAULT_MAX_LEGS: Optional[int] = MAX_LEGS


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
    "bet nothing" even when the reads were good.

    What made the old card lose was never the missing price gate — it was
    where the picks came from. Filtered on hot bats and battered starters, they
    came back at a median -260 against a 70.5% read, and no gate saves a bet
    that dear. Drawn off the board they come back at -185 against 77%. Fix the
    selection and the price stops needing to be a veto.

    The card is built in two stages, because the two stages are answering
    different questions. Legs 1 and 2 are the two most likely batters, taken on
    probability alone. Any leg past that is added **only to lift the card to a
    plus price**, and is chosen on price rather than on rank — see
    ``MIN_LEG_VALUE`` for why that choice is free.

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

    ceiling = max_legs if max_legs else MAX_LEGS
    floor = min(MIN_LEGS, ceiling)

    candidates: list[dict] = []
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
        candidates.append(r)

    out = candidates[:floor]
    rest = candidates[floor:]

    # Grow only while the card is short of a plus price, and only with legs
    # that pay for the risk they add.
    while len(out) < ceiling and _decimal(out) < TARGET_DECIMAL and rest:
        best, best_value = None, MIN_LEG_VALUE
        for r in rest:
            value = (r.get("model_p") or 0) * _leg_decimal(r)
            if value > best_value:
                best, best_value = r, value
        if best is None:
            # Nothing left that gives more than it takes. A card below the
            # target is the honest answer here — padding it with a -400 leg
            # would buy a plus sign by making the bet worse.
            break
        out.append(best)
        rest.remove(best)
    return out


def _leg_decimal(r: dict) -> float:
    """Decimal price of one leg; 1.0 (adds nothing) when it has no price."""
    odds = r.get("fd_odds")
    if not odds:
        return 1.0
    return 1 + (100 / -odds if odds < 0 else odds / 100)


def _decimal(legs: list[dict]) -> float:
    dec = 1.0
    for r in legs:
        dec *= _leg_decimal(r)
    return dec


def near_misses(board: list[dict], chosen: list[dict], limit: int = 5) -> list[dict]:
    """The next-best batters below the cut, best first.

    Pass the whole board, not the picks — the card is a few legs at most, so
    everything else is visible only here. Worth showing for two reasons:
    seeing who just missed, and spotting a name you have a read on that the
    model happened to rank fourth.

    This used to filter on ``is_screen_pick``, which now would show the wrong
    list entirely: those tags no longer decide anything, so a row carrying
    them isn't a near miss and a row without them isn't excluded. Ranking by
    probability, one per game, is the same rule that chose the card — which is
    what makes these the actual runners-up.
    """
    taken = {(r.get("batter"), r.get("pitcher_id")) for r in chosen}
    taken_games = {r.get("pitcher_id") for r in chosen if r.get("pitcher_id") is not None}
    rows = sorted(
        (r for r in board if r.get("model_p") is not None),
        key=lambda r: -r["model_p"],
    )
    out: list[dict] = []
    seen_games = set(taken_games)
    for r in rows:
        if (r.get("batter"), r.get("pitcher_id")) in taken:
            continue
        game = r.get("pitcher_id") or r.get("fd_event_id")
        if game is not None:
            if game in seen_games:
                continue
            seen_games.add(game)
        out.append({
            "batter": r.get("batter"),
            "opposing_pitcher": r.get("opposing_pitcher"),
            "model_p": r.get("model_p"),
            "fd_odds": r.get("fd_odds"),
            "ev": r.get("ev"),
            "edge_pts": r.get("edge_pts"),
        })
        if len(out) >= limit:
            break
    return out


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
        dec *= _leg_decimal(r)
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
