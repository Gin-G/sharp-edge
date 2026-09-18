"""The day's bets, and a link that loads them into the slip.

**What gets picked.** Every batter the model puts at or above
``MIN_MODEL_P`` to record a hit, one per game, with a two-leg floor — so the
card's length is an output of the board rather than a cap. Not the
best *screen qualifiers*: the filters are gone from selection, because measured
over 129 days they were picking worse bets and paying more for them. See the
ranking block in ``batters.screen_for_date`` for the numbers.

**Price is shown, not obeyed.** Odds and EV ride along on every leg because
they're worth knowing, but they do not decide the card. That now holds for the
whole card rather than just its first two legs: the tail used to be sorted by
``model_p x decimal`` and admitted above a value bar, which made this a
probability card for two legs and an EV card after that. On a parlay those are
opposite objectives — value rewards a longer price, and a longer price is the
book saying the leg is less likely to win. Two-leg cards swept 50% against a
model that said 52%; three-leg cards swept 16.7% against a model that said 45%.

**The bar is a length control, not a quality filter.** Leg quality is flat at
about 67% across the whole top of the board and the model cannot rank within
it; above 0.72 its confidence stops meaning anything and the picks get worse.
See ``MIN_MODEL_P``.

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

MAX_LEGS: Optional[int] = None

# The bar a leg has to clear, and why it is 0.72 and not higher.
#
# The card is every batter the model puts at or above MIN_MODEL_P to record a
# hit, one per game, with the two-leg floor below it. Length is an output of
# the bar rather than a cap: a day with five good bats produces a five-leg
# card and a thin one produces two.
#
# Raising the bar past 0.72 does NOT buy better picks. It buys worse ones.
# Replayed over the 36 days to 2026-09-14:
#
#     bar    days   median legs   leg hit rate   sweep
#     0.68    35        10           67.8%       17.6%
#     0.70    34         7           67.6%       25.8%
#     0.72    33         3           66.7%       44.8%   <- here
#     0.74    27         2           48.7%       33.3%
#     0.76    22         1           54.5%       38.5%
#
# Leg quality is flat at about 67% all the way up to 0.72 and then falls off a
# cliff. That is not noise in the tail, it is the model's calibration: it is
# honest in the bulk (0.675-0.725 predicts 69-71% and delivers 67-71%) and
# overconfident above it — 0.775-0.800 predicts 81.1% and delivers 56.2%. The
# names it is surest about are the ones it is worst about.
#
# Below 0.72 the bar stops binding: the card runs to seven and ten legs, leg
# quality is unchanged, and sweep collapses because sweep multiplies. So 0.72
# is where the card is as long as it can be without either admitting legs that
# are no better or reaching into the range where the model breaks down.
#
# The deeper caveat, which this bar does not fix and should not be read as
# fixing: over 243 graded picks model_p has an AUC of 0.507 and correlates
# -0.026 with getting a hit. It cannot rank the board. Picking the top two by
# probability (65.0%) does not beat two drawn at random off the same board
# (66.2%). The bar works as a LENGTH control and as a guard against the
# overconfident tail, not because 0.73 is a better bet than 0.71 — it isn't.
# Making it a better bet means giving the model something it does not have;
# see IDEA.md for the two features most likely to do that.

MIN_MODEL_P: float = 0.72

# NO LONGER USED BY DEFAULT. Kept because the reasoning is worth having, and
# because `min_edge_pts` still offers a price floor on request. This constant
# described how the card's tail was chosen before selection moved to pure
# probability; see MAX_LEGS for what replaced it and why.
#
# What "qualifies" meant, and why it was not a probability bar.
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
# treats it as a hard ceiling when given, defaulting to MAX_LEGS.
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


# Days back over which a VOID disqualifies a batter from the card.
#
# A VOID means he did not bat — scratched, benched, or platooned out. Measured
# over 44,017 board rows, that is the strongest short-term signal on the board
# and it says two separate things:
#
#     VOIDed in the last 3 days -> VOIDs again today   44.1%  (16.5% otherwise)
#     ...and when he DOES play, he hits                57.4%  (62.7% otherwise)
#
# The second is the one worth having. p = 1.5e-20 over 30,783 graded rows: a
# recently scratched batter is a worse bet even when he makes the lineup,
# because whatever kept him out — a knock, a platoon, a manager's read — is
# still true today and the model cannot see any of it.
#
# On the card it removes wasted slots at no cost to quality: hit rate 80.8%
# against 80.3% (p=0.895, i.e. unchanged) while the VOID rate falls from 39.1%
# to 17.1% (p=3e-08). Three days beats one (79.8%) and seven (78.3%).
#
# This is NOT a sample-size floor on vs_hand_pa. That was tested at the same
# time and rejected: requiring 100+ plate appearances against the hand cut
# VOIDs just as hard but took hit rate from 80.3% to 75.4%, because it promotes
# a worse hitter for the crime of a thin split. A thin split predicts; a man
# who was not in yesterday's lineup is a different problem.
VOID_LOOKBACK_DAYS: int = 3


def build(
    records: list[dict],
    max_legs: Optional[int] = DEFAULT_MAX_LEGS,
    min_edge_pts: float | None = None,
    cross_game: bool = True,
    min_model_p: float | None = None,
    exclude_ids: set | None = None,
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
    min_model_p = MIN_MODEL_P if min_model_p is None else min_model_p
    # Batters who did not bat in the last few days — see VOID_LOOKBACK_DAYS.
    # Dropped before ranking so they cannot take a slot at all, rather than
    # being demoted and still arriving on a thin slate.
    if exclude_ids:
        records = [r for r in records if r.get("batter_id") not in exclude_ids]
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

    # Everything past the floor that clears the bar, most likely first — the
    # same rule that chose the first two. `candidates` is already in
    # probability order, so the first one below the bar ends it.
    #
    # This used to sort the tail by `_value` (model_p x decimal) and admit
    # anything above MIN_LEG_VALUE, which made the card a probability card for
    # two legs and an EV card after that. On a parlay those are opposite
    # objectives: value rewards a longer price, and a longer price is the book
    # saying the leg is less likely to win. It showed up exactly where you
    # would expect — two-leg cards swept 50% against a model that said 52%,
    # three-leg cards swept 16.7% against a model that said 45%.
    #
    # `min_edge_pts`, applied above, is still the way to ask for a price floor.
    for r in rest:
        if not _room():
            break
        if (r.get("model_p") or 0) < min_model_p:
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
