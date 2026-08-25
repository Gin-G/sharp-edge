"""The day's bets, and a link that loads them into the slip.

**What gets picked.** The two batters most likely to record a hit, taken off
the top of the board by model probability, one per game — plus every other leg
that pays for the risk it adds, with no cap. Not the best *screen qualifiers*:
the filters are gone from selection, because measured over 129 days they were
picking worse bets and paying more for them. See the ranking block in
``batters.screen_for_date`` for the numbers.

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
# FanDuel's deep link.
#
# The account host with its /sportsbook prefix, not the state subdomain, and
# the difference is not cosmetic: tapped on a phone this one opens the FanDuel
# app directly with the slip loaded, while
# co.sportsbook.fanduel.com/addToBetslip lands on the mobile website and makes
# you press a second "open in app" button. Both hosts serve the same
# apple-app-site-association claiming /* for the sportsbook apps, so the reason
# is FanDuel's own routing rather than anything about app-link eligibility —
# but the behaviour is what matters and it was measured, not reasoned.
#
# No state in the URL, which is a bonus: the old link had to be built for the
# state the account is registered in or it bounced the user, and this one is
# resolved from the session instead.
BETSLIP_BASE = "https://account.sportsbook.fanduel.com/sportsbook/addToBetslip"

MIN_LEGS: int = 2

# No ceiling. Every pick that qualifies goes on the card, so a slate with five
# good bets produces a five-leg card.
#
# This is a deliberate choice against the sweep numbers, and the trade should
# be visible to whoever reads this next. Sweep rate — the share of days every
# leg won, over 129 days — falls hard with each leg, because below the top two
# the board is flat at about 70% and every addition multiplies by roughly that:
#
#     legs      1      2      3      4      5      6      7
#     sweep   80.6%  58.9%  41.7%  29.9%  ~21%   ~15%   ~10%
#
# A five-leg card sweeps something like one day in five. That is the cost of
# listing every qualifier, and it is the owner's call: the upside is that the
# days it does land pay several times what a two-leg card pays.
MAX_LEGS: Optional[int] = None

# What "qualifies" means, and why it is not a probability bar.
#
# The obvious rule would be "model_p above some threshold", and it does not
# work. The model cannot tell the top of the board apart — its top ten span
# 71.5% to 70.0% — so a bar is a knife edge rather than a filter: at 0.70 it
# admits six names a day, at 0.72 it admits two, and measured over 129 days
# the ranks below the top two are indistinguishable from one another (69.8,
# 70.6, 72.2, 69.0, 68.8, 69.6 against 78.9% and 75.8% for ranks 1 and 2).
# Sorting that pool by probability is sorting on noise.
#
# What does separate them is price, which on the same slate ran -105 to -425.
# So a leg qualifies when it pays for the risk it adds: ``model_p * decimal``
# above 1, which is the leg being +EV, written so the reason shows. A 70% read
# at -475 multiplies the payout by 1.24 while costing 30% of the ticket —
# 0.70 x 1.24 = 0.87, it takes more than it gives — while the same read at
# -105 scores 1.38. That is the Pages-at-475 test, generalised.
#
# The bar sits above 1.0, and that is the load-bearing part. Break-even alone
# is far too loose: the model reads most of the board at about 70% while the
# market prices those names nearer 63%, so on a full slate ten to fourteen
# legs clear 1.0 and the "card" becomes the whole board. Measured over the
# priced days, with the two-leg floor always applied:
#
#     bar    median legs   range    est. sweep
#     1.00        10        3-14       ~3%
#     1.05         5        2-7       ~21%
#     1.10         3        2-5       ~42%     <- here
#     1.15         2        2-4       ~60%
#     1.25         2        2-2       ~60%
#
# 1.10 is the setting where a genuinely good slate produces the five-leg card
# and a thin one still produces two — the range is 2 to 5, which is the shape
# asked for. Sweep is estimated, not measured: 78.9% x 75.8% for the top two
# and ~70% per leg after, since the board below rank 2 is flat.
#
# Lower it to 1.05 for longer cards, raise it to 1.15 to sit near the two-leg
# sweep rate. This is the knob.
MIN_LEG_VALUE: float = 1.10

# There is deliberately no "pad the card until it reaches +100" rule.
#
# An earlier version had one, and it was wrong in a way worth recording: on a
# slate where the two best batters are both -300, nothing qualifies, the card
# is -128, and a backstop would reach for the next leg to manufacture a plus
# sign — which on those slates means a -475 leg scoring 0.87. It would buy the
# plus sign by making the bet worse, which is the exact trade this module
# refuses everywhere else.
#
# So a short, minus-money card is an allowed outcome and an honest one: it is
# the board saying today is not a good day to play. The price is on the card
# in the UI, so it is visible rather than silently padded.

# Retained so callers that passed an explicit cap keep working; ``build``
# treats it as a hard ceiling when given, and there is none by default.
DEFAULT_MAX_LEGS: Optional[int] = MAX_LEGS


def betslip_url(selections: Iterable[dict]) -> Optional[str]:
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
    return f"{BETSLIP_BASE}?{'&'.join(parts)}"


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

    The card is built in two stages, because the two stages answer different
    questions. Legs 1 and 2 are the two most likely batters, taken on
    probability alone — those two ranks are the only ones the model can
    actually separate. Everything past them is every remaining leg that pays
    for the risk it adds, ordered by how well it pays; see ``MIN_LEG_VALUE``
    for why that pool is sorted on price rather than probability.

    There is no cap: five qualifying picks make a five-leg card. What that
    costs in sweep rate is documented on ``MAX_LEGS``.

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

    ceiling = max_legs if max_legs is not None else MAX_LEGS
    floor = MIN_LEGS if ceiling is None else min(MIN_LEGS, ceiling)

    def _room() -> bool:
        return ceiling is None or len(out) < ceiling

    def _value(r: dict) -> float:
        return (r.get("model_p") or 0) * _leg_decimal(r)

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

    # Every remaining leg that pays for the risk it adds, best value first.
    # Five qualifiers means a five-leg card; see MAX_LEGS for what that costs.
    for r in sorted(rest, key=_value, reverse=True):
        if not _room():
            break
        if _value(r) <= MIN_LEG_VALUE:
            # Sorted by value, so nothing after this one qualifies either.
            break
        out.append(r)
    return out


def _leg_decimal(r: dict) -> float:
    """Decimal price of one leg; 1.0 (adds nothing) when it has no price."""
    odds = r.get("fd_odds")
    if not odds:
        return 1.0
    return 1 + (100 / -odds if odds < 0 else odds / 100)


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
                "model_p": None, "ev": None, "implied_p": None,
                "kelly": None, "kelly_quarter": None}

    from . import pricing

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
        # Stake sizing, and it wants reading with the caveats attached.
        #
        # Kelly is only ever as good as the probability fed to it, and this one
        # is a product of per-leg estimates, so two errors compound. The legs
        # come from different games, which is what cross-game selection buys,
        # but "different game" is not quite "independent" — a cold night moves
        # every bat on the slate together. Both effects push ``model_p`` above
        # the truth, and Kelly is asymmetric about that: overstating p
        # overstakes fast, understating it merely leaves money on the table.
        #
        # So the quarter is the number to use, and it is returned already
        # divided rather than left as a note nobody applies. Full Kelly on a
        # three-leg card at these prices routinely computes above 30% of
        # bankroll, which is not a stake, it is a coin flip with extra steps.
        "kelly": round(pricing.kelly_fraction(p, american), 4),
        "kelly_quarter": round(pricing.kelly_fraction(p, american) / 4, 4),
    }
