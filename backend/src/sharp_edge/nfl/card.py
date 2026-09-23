"""The week's NFL suggestions, and the card built from them.

The football counterpart of ``bundle.py``. Same objective — legs that are
likely to actually happen, not legs with the flashiest number — but the shape
of the problem differs from baseball in two ways that change the rules.

**There is no daily rhythm.** Baseball gives a fresh slate every day, so a card
that misses costs one day. An NFL week is the unit, and there are eighteen of
them in a season. A rule that produces a five-leg card here is making one bet a
week, which is far too little data to learn from and far too much variance to
survive. So the NFL card is deliberately short — two legs — and the *suggestion
list* underneath it is long. The suggestions are what gets tracked; the card is
what gets bet.

**Both sides of every prop are available, and a short line is dangerous on
both.** A batter prop is one-sided: back him or pass. A yardage line can be bet
either way, and the model is often most confident on the shortest lines — which
is exactly where it should be least trusted, because a short line is the book
pricing an uncertain role and role is the one thing the projection cannot see.
``_plausible`` is where that is refused, in both directions.

**What actually gets picked.** Every row where the model and the market
disagree by more than the stated threshold *and* the disagreement survives the
shrink is a **suggestion**. The card takes the two suggestions with the largest
devigged edge, one per game. That is a much lower bar than baseball's, and
deliberately so: with no track record yet, the point of week 1 is to generate
a measurable set of predictions, not to bet optimally on a model nobody has
scored.
"""

from __future__ import annotations

from typing import Iterable, Optional
from urllib.parse import quote

from . import availability

# Same host and reasoning as bundle.BETSLIP_BASE — the account host opens the
# FanDuel app directly on a phone, where the state subdomain lands on the
# mobile website and makes you press through a second time.
BETSLIP_BASE = "https://account.sportsbook.fanduel.com/sportsbook/addToBetslip"

MIN_LEGS = 2
MAX_LEGS = 2

# Minimum devigged edge, in probability points, for a row to be suggested.
#
# Lower than it will eventually be, and that is a deliberate week-1 choice. The
# model has never been scored on a settled NFL week, so a high bar would be
# tuning on nothing — it would just be my prior about my own prior. Three points
# is the same floor the batter model uses (``pricing.MIN_EDGE_PTS``), chosen
# there because it sits above the model's measured calibration error. Whether
# it is right here is exactly what tracking will tell us.
MIN_EDGE_PTS = 3.0

# An UNDER needs more than an OVER to qualify, and the asymmetry is real rather
# than aesthetic.
#
# The projections are a season-long expectation of a player's rate. When the
# model says "under 19.5 rushing yards" it is often just restating an
# availability risk the market already knows about and has priced, and the
# price is short because everyone knows it. The over at least needs the player
# productive as well as active, so the model's ignorance of workload works
# against it rather than for it — the safer direction to be wrong in.
UNDER_MIN_EDGE_PTS = 6.0

# Below this line we do not bet either side, and this is the most important
# rule in the module.
#
# A low line is not the book being shy about a good player. It is the book
# saying the role is uncertain — WR5, a committee back, someone easing off an
# injury. **Role is precisely what our projection does not model.** It carries
# no snap share and no depth chart; it is a season-long rate for a nominal
# starter. So on a short line the two numbers are not answering the same
# question, and the gap between them measures our ignorance rather than the
# market's.
#
# That failure is symmetric, which the first version of this file got wrong. It
# guarded the under (where you win if the man is inactive) and left the over
# open, and the result was a card of exactly the bets it should have refused:
# Mack Hollins over 8.5 receiving yards on a projection of 33, and Malik Davis
# over 13.5 rushing yards on 44. Sorted by edge, the whole top of the board was
# backups, because the edge was reading role-blindness as value.
#
# Two supporting measurements. On held-out 2023-25 the model is *overconfident*
# exactly here — at lines of 10 or less it reads 68.1% and delivers 65.0% for
# receiving yards, 64.8% against 61.7% for rushing — the only bucket where it
# errs high. And on the live week-1 board edge and projection/line ratio ran
# together almost perfectly through the top fourteen rows.
MIN_LINE = {
    "receiving_yards": 20.0,
    "rushing_yards": 20.0,
    "receptions": 2.5,
    "passing_yards": 175.0,
}

# A ceiling on how far the projection may sit above the line in relative terms,
# which catches the same failure where MIN_LINE cannot see it.
#
# A projection at twice the line is not a strong opinion, it is a description of
# a different player-week than the one being priced. On the week-1 board the
# median suggestion sat at 1.39x and the 90th percentile at 2.44x, so this trims
# the tail rather than the body — but the tail is where every role failure was.
MAX_PROJECTION_RATIO = 2.0

# How much of a position group's recent production has to belong to players who
# are out before an UNDER on the men who remain is refused.
#
# **This is the one rule here that is about a fact rather than a judgement.**
# Everything above guards against the projection being blind to role. This
# guards against a role we can actually see having changed: when a group has
# lost a third of its carries or targets to injured reserve, the market reprices
# the men left behind within hours and our projection — a season-long rate for
# the committee that no longer exists — does not. The residual is then stale by
# construction and it points one way, down, because the projection describes a
# smaller share than the player now has. That is an UNDER on a man whose role
# just grew.
#
# Minnesota in week 3 is the case: Jordan Mason to reserve/injured takes 44% of
# the backfield's rushing with him, Aaron Jones is the RB1, and the projection
# still has him at 31 yards after he ran for 72 a game beside Mason.
#
# One third, and the number is a judgement rather than a measurement — nothing
# here has been scored on a settled week yet. It is deliberately above the
# reporting floor in ``availability.MIN_VACATED_SHARE`` (0.08), which is set to
# show a vacancy rather than to act on one. The board records the share on
# every row and the track record splits on it, so this is the first constant to
# revisit against results.
VACATED_SHARE_BLOCKS_UNDER = 0.33


def _plausible(row: dict) -> bool:
    """Are the model and the market pricing the same player-week?

    Two ways to fail, both meaning we are describing a role the book is not.
    See MIN_LINE for why this is symmetric across over and under.
    """
    line = row.get("line") or 0.0
    if line < MIN_LINE.get(row.get("market"), 0.0):
        return False
    projected = row.get("adjusted")
    if projected is not None and line > 0 and projected / line > MAX_PROJECTION_RATIO:
        return False
    return True


def _inherited_role(row: dict) -> bool:
    """Is this an UNDER on a man whose position group just lost work?

    Kept apart from ``_plausible`` rather than folded into it because the two
    are different claims. ``_plausible`` says the projection is blind to role
    and the bet should be refused in both directions. This says role has
    visibly changed in one direction, and refuses one side. See
    VACATED_SHARE_BLOCKS_UNDER.
    """
    if row.get("side") != "UNDER":
        return False
    share = row.get("vacated_share")
    return share is not None and share >= VACATED_SHARE_BLOCKS_UNDER


def _available(row: dict) -> bool:
    """Is this man playing?

    Not a modelling question and not a close one. A prop on a player who is on
    injured reserve or listed inactive is void at best, and the model has no
    opinion worth acting on about a player it thinks is taking a full share of
    a game he will not be in.

    Doubtful counts as out; questionable does not. See
    ``availability.PLAYABLE`` for why those two are split rather than grouped.
    A row with no availability at all — the feeds failed, or the player is not
    on any of them — is allowed through, so a broken fetch costs information
    rather than the whole card.
    """
    status = row.get("avail")
    return status is None or status in availability.PLAYABLE


def _edge_bar(row: dict) -> float:
    return UNDER_MIN_EDGE_PTS if row.get("side") == "UNDER" else MIN_EDGE_PTS


def suggestions(props: Iterable[dict]) -> list[dict]:
    """Every row worth recording as a prediction, best edge first.

    This is the tracked set — deliberately wider than the card. A week yields
    on the order of a dozen of these, which is enough to say something about
    the model after a month; a two-leg card alone would take a season to
    produce the same number of settled outcomes.

    Wider, but not blind: a player who is out is dropped here rather than
    recorded and voided later, because a void is not a prediction and a set
    full of them tells the track record nothing.
    """
    out = [r for r in props
           if _qualified(r) and _available(r) and not _inherited_role(r)]
    out.sort(key=lambda r: -(r.get("edge_pts") or 0))
    return out


def _qualified(row: dict) -> bool:
    """Everything a row must clear *except* the two availability guards.

    Split out so ``withheld`` can say what those two guards actually cost,
    rather than listing every out player who happened to carry a posted line.
    """
    return bool(
        row.get("bettable")
        and row.get("signal")
        and not row.get("prior_only")
        and row.get("side")
        and row.get("edge_pts") is not None
        and row["edge_pts"] >= _edge_bar(row)
        and _plausible(row)
    )


def withheld(props: Iterable[dict]) -> dict:
    """The rows the availability guards took, and why.

    Reported on the page rather than only applied: a guard nobody can see is a
    guard nobody revisits, and one of these two is a judgement call with no
    settled result behind it yet.

    Only rows that would otherwise have been suggested. An out player with a
    posted line and a half-point edge was never going to be bet, and listing
    him would make the guard look like it was doing work it was not.
    """
    rows = [r for r in props if _qualified(r)]
    return {
        "unavailable": [r for r in rows if not _available(r)],
        "vacated_under": [r for r in rows
                          if _available(r) and _inherited_role(r)],
    }


def build(props: Iterable[dict], max_legs: int = MAX_LEGS) -> list[dict]:
    """The week's card: the best suggestions, one leg per game.

    One per game for the same reason baseball takes one per pitcher — two props
    from the same game are one bet on that game going a particular way, and a
    book prices them as a same-game parlay precisely because they correlate.
    Here the correlation is even stronger: two receivers on the same offence
    are close to the same bet on whether that offence moves the ball.
    """
    picked: list[dict] = []
    seen_games: set = set()
    for r in suggestions(props):
        game = r.get("fd_event_id")
        if game is not None and game in seen_games:
            continue
        if game is not None:
            seen_games.add(game)
        picked.append(r)
        if len(picked) >= max_legs:
            break
    return picked if len(picked) >= MIN_LEGS else []


def _leg_decimal(r: dict) -> float:
    odds = r.get("odds")
    if not odds:
        return 1.0
    return 1 + (100 / -odds if odds < 0 else odds / 100)


def summarise(card: list[dict]) -> dict:
    """Combined price and probability for the card taken as one parlay."""
    if not card:
        return {"legs": 0, "decimal": None, "american": None, "model_p": None,
                "implied_p": None, "ev": None, "kelly_quarter": None}

    dec, p, implied = 1.0, 1.0, 1.0
    for r in card:
        dec *= _leg_decimal(r)
        p *= r.get("model_p") or 0.0
        implied *= r.get("fair_p") or r.get("implied_p") or 0.0
    american = round((dec - 1) * 100) if dec >= 2 else -round(100 / (dec - 1))

    from ..pricing import kelly_fraction

    return {
        "legs": len(card),
        "decimal": round(dec, 4),
        "american": american,
        "model_p": round(p, 4),
        "implied_p": round(implied, 4),
        "ev": round(p * (dec - 1) - (1 - p), 4),
        # Quarter-Kelly, already divided — same caveat as the baseball card:
        # a parlay compounds each leg's probability error, and Kelly is
        # asymmetric about overstating p. Here it deserves even less trust,
        # since the probabilities have never been scored on a settled week.
        "kelly_quarter": round(kelly_fraction(p, american) / 4, 4),
    }


def betslip_url(card: Iterable[dict]) -> Optional[str]:
    """An addToBetslip link for the card.

    Which selection id depends on the side — a prop market has one for the
    over and one for the under, and sending the wrong one loads the opposite
    bet, which is the worst possible failure for a convenience feature.
    """
    parts: list[str] = []
    i = 0
    for r in card:
        market = r.get("fd_market_id")
        sel = (r.get("under_selection_id") if r.get("side") == "UNDER"
               else r.get("over_selection_id"))
        if market is None or sel is None:
            continue
        parts.append(f"marketId[{i}]={quote(str(market), safe='')}")
        parts.append(f"selectionId[{i}]={quote(str(sel), safe='')}")
        i += 1
    return f"{BETSLIP_BASE}?{'&'.join(parts)}" if parts else None
