"""Where a batter hits, and how many swings that buys him.

A hit prop is decided by one thing more than any other: how many official
at-bats the man gets. Measured over 7,308 starter-games across 30 slates
(Sept 2026):

    at-bats     1       2       3       4       5+
    P(hit)    21.1%   36.1%   50.7%   68.9%   83.9%

And at-bats are set by where he hits and whether he walks:

    slot        1      2      3      4      5      6      7      8      9
    PA        4.46   4.41   4.30   4.20   4.08   3.89   3.73   3.59   3.45
    AB        3.97   3.88   3.79   3.70   3.66   3.50   3.33   3.19   3.07
    P(hit)   72.6%  64.5%  62.9%  66.1%  55.9%  57.0%  57.0%  55.4%  54.3%

Leadoff against ninth is +1.07 plate appearances and +18.3 points of hit
probability; slots 1-2 against 8-9 is 68.5% against 54.8%, Fisher p = 1.6e-04.

**The lineup is not out when the board is built.** MLB posts it a couple of
hours before first pitch, and a scheduled game's boxscore carries no batting
order at all — checked on 2026-09-14 across ten games, zero slots. So the slot
is assumed from where the batter has been hitting, which is a good assumption
because lineups are sticky: 57.3% of starts are in the identical slot to the
previous one, the mean move is 0.82 slots, and only 8.5% move more than two.

Sticky, but drifting — a batter going well moves up and one going badly moves
down — so the estimator is deliberately short. Predicting the next start's slot
over 5,934 starter-games:

    estimator        exact    within 1
    last start       58.0%     81.5%
    mode of 3        59.4%     82.2%    <- here
    mode of 5        59.0%     82.5%
    mode of 10       58.3%     82.0%
    mode of all      56.7%     80.7%

The mode of the last three starts wins, and "mode of all" losing to "last
start" is the drift showing: a long window remembers a role the batter has
already moved out of.

**What this is worth, honestly.** Assumed slot alone scores AUC 0.545 against
whether the batter recorded a hit, over those 5,934 starter-games. That is a
real signal and a modest one. It should not be compared with the shipped
model's AUC of 0.507, which is measured on its own 243 selected picks — a
narrow, already-filtered population where the spread is compressed. Different
populations, and the comparison would flatter this module.

Projecting the workload from the assumed slot beats a league-mean baseline on
at-bats by 7.0% MAE, and does **not** beat it on plate appearances (-1.3%).
That asymmetry is the point: the slot tells you about swings, not trips, and
at-bats are what the prop settles on.

The batter's own walk rate is not the missing piece — on its own it scores AUC
0.506 against the hit, and combined with slot it moves 0.545 to 0.549. The walk
effect is large after the fact and hard to forecast from the batter's history.
The opposing starter's and the bullpen's walk rates are the untested candidate
and the more likely one; see IDEA.md.
"""

from __future__ import annotations

import collections
from typing import Iterable, Optional

# Plate appearances, at-bats and hit rate by lineup slot, from the 7,308
# starter-games above. Medians for the workload numbers, because these are used
# as point estimates under absolute error and the median is what minimises it —
# using the means made the plate-appearance projection worse than the league
# baseline rather than better.
SLOT_PA = {1: 4.46, 2: 4.41, 3: 4.30, 4: 4.20, 5: 4.08,
           6: 3.89, 7: 3.73, 8: 3.59, 9: 3.45}
SLOT_AB = {1: 3.97, 2: 3.88, 3: 3.79, 4: 3.70, 5: 3.66,
           6: 3.50, 7: 3.33, 8: 3.19, 9: 3.07}
SLOT_P_HIT = {1: 0.726, 2: 0.645, 3: 0.629, 4: 0.661, 5: 0.559,
              6: 0.570, 7: 0.570, 8: 0.554, 9: 0.543}

# Starts to read back over. Three, because the slot drifts with form and a
# longer window remembers a role the batter has left.
RECENT_STARTS = 3

# Used when a batter has no recorded start — a call-up, or someone back from
# the injured list. The bottom third is the honest prior for a man with no
# established place in the order, not the league average.
DEFAULT_SLOT = 7


def expected_slot(recent_slots: Iterable[int]) -> Optional[int]:
    """The slot to assume, from a batter's recent starts **oldest first**.

    The mode of the last ``RECENT_STARTS``, breaking a tie toward the most
    recent — a batter who has hit 2nd, 5th, 2nd is assumed 2nd, and one who has
    hit 5th, 2nd, 5th, 2nd is assumed 2nd rather than being frozen at the older
    habit.

    ``None`` when there is nothing to go on, which the caller should read as
    "no established slot" rather than substituting a number silently.
    """
    slots = [s for s in recent_slots if s and 1 <= s <= 9]
    if not slots:
        return None
    window = slots[-RECENT_STARTS:]
    counts = collections.Counter(window)
    top = max(counts.values())
    for s in reversed(window):          # most recent among the tied
        if counts[s] == top:
            return s
    return window[-1]


def projected_ab(slot: Optional[int], walk_rate: Optional[float] = None) -> float:
    """Expected official at-bats for a batter hitting in ``slot``.

    ``walk_rate`` is walks plus hit-by-pitch per plate appearance. When given,
    the batter's own tendency replaces the league-average walk already baked
    into ``SLOT_AB``, since the whole reason at-bats trail plate appearances is
    walks and a batter who walks twice as often loses twice the swings.

    It is a small correction and it is not yet shown to predict the hit — see
    the module docstring — so it is applied only when a rate is supplied.
    """
    if slot is None:
        slot = DEFAULT_SLOT
    slot = min(max(int(slot), 1), 9)
    if walk_rate is None:
        # SLOT_AB already carries the league-average walk rate for that slot.
        return SLOT_AB[slot]
    # Swap the league's walk rate for this batter's: every trip he does not
    # walk in is a swing at a hit. Clamped because a short sample can produce
    # a rate of 1.0, which would project zero at-bats off a handful of games.
    rate = min(max(walk_rate, 0.0), 0.5)
    return round(SLOT_PA[slot] * (1 - rate), 3)


def slots_from_boxscores(summaries: Iterable[dict], player_id: int) -> list[int]:
    """One batter's slots across a run of ``_boxscore_summary`` results.

    Summaries are expected **oldest first**, and games he did not start are
    skipped rather than recorded as a gap — a bench day says nothing about
    where he hits when he plays.
    """
    out: list[int] = []
    for summary in summaries:
        for side in ("away", "home"):
            slot = ((summary.get(side) or {}).get("slots") or {}).get(player_id)
            if slot:
                out.append(int(slot))
    return out
