# Experiments — NFL props

Running log for the football side, in the same spirit as `EXPERIMENTS.md`: what
was tried, what the numbers said, and what shipped as a result.

The NFL screen was built in the week before the 2026 season, so almost
everything here is measured on history or on the live week-1 board rather than
on settled picks of our own. That is the main caveat on all of it, and the
first thing that changes as weeks settle.

---

## What the board is trying to do

The owner's rule, as stated: **if the projection is more than 10 yards above
the Vegas line that's an over, more than 10 below it's an under; receptions
2-3 either way; TDs are probability against the price.**

That rule is what shipped. One thing had to change about *how it is measured*,
and it is the single most important finding here.

---

## Finding 1 — the projections are shrunk, and the raw rule reads that as signal

`nfl_projections` is trained on squared error, so like any such model it
regresses toward the mean. A betting line does not. Fitting projection on line
across the live week-1 board:

| market | correlation | slope | residual sd | mean raw gap |
|---|---|---|---|---|
| passing yards | 0.810 | **1.918** | 29.0 | −31.5 |
| receiving yards | 0.711 | **0.588** | 10.1 | −3.7 |
| receptions | 0.641 | **0.546** | 0.9 | −0.5 |
| rushing yards | 0.792 | **0.776** | 13.8 | −1.2 |

The correlations are high, so the two are measuring the same thing — but none
of the slopes is 1. A slope of 0.59 means the projection understates every
player with a big line and overstates every player with a small one, *purely
as an artefact*.

Read raw, the rule therefore fires like this on week 1:

- **87% of quarterbacks read UNDER.** Deshaun Watson projected 12.4 against a
  178.5 line.
- Every UNDER is a star with a high line: Lamb (76.5), Smith-Njigba (82.5),
  Jefferson (73.5), Nacua (90.5), Gibbs (82.5), Bijan (78.5).
- Every OVER is a backup with a low line: Colby Parkinson (22.5), Woody Marks
  (28.5), Chris Rodriguez (33.5).

That is a portfolio of "fade every good player, back every backup", and it is
not what the rule was meant to express.

**Shipped:** `model.calibrate_to_market` refits the projection→line
relationship every week from that week's own board, and the signal is measured
on the **residual** — how far a player sits from where a projection normally
sits for a line that size. On the same board that takes quarterbacks from 87%
UNDER to 39%, and receiving yards from 63% to 54%. The names that survive are
ones with a role story behind them (Kyle Pitts, Pittman, a holdout back)
rather than just a large number.

Both numbers are on every row in the UI — `raw_gap` and `residual` — and
`raw_signal` is stored next to `signal` so the two rules can be compared on
settled results instead of on this argument.

---

## Finding 2 — how well "clears line X" can be predicted at all

`scripts/calibrate_nfl.py`, one logistic per market taking the line itself as
a feature. Train 2015-2022, test 2023-2025, strict `shift(1)` on every trailing
feature.

| market | test n | AUC | log-loss vs base | worst calibration bucket |
|---|---|---|---|---|
| receptions | 79,888 | 0.9044 | 0.361 vs 0.626 | 3.8 pts |
| rushing yards | 48,510 | 0.8981 | 0.360 vs 0.607 | 1.9 pts |
| receiving yards | 109,846 | 0.8516 | 0.443 vs 0.635 | 2.4 pts |
| passing yards | 10,592 | 0.8105 | 0.531 vs 0.682 | **4.8 pts** |
| anytime TD | 11,422 | 0.7284 | 0.454 vs 0.507 | 6.9 pts (understates) |

Two things to take from this.

**`gap_std` beats `gap` everywhere except passing yards.** The season-to-date
estimate carries roughly 20-40x the coefficient of the trailing-4 one. Recent
form matters much less than the way the market talks about players suggests.

**Passing yards is the one market that is overconfident.** It reads 77.5%
where the outcome is 72.8% and 87.6% where it is 82.7% — 4-5 points hot right
through the range a bet would come from. Every other market misses by under 2
and errs *low*, which is the safe direction. Overstating inflates EV, the
parlay and the Kelly stake at once, so **passing yards ships priced and
displayed but excluded from card selection** (`model.BETTABLE`).

---

## Finding 3 — a Platt correction does not pay for itself

Tried, because 4-5 points of overconfidence on passing yards is worth trying to
fix. Hold the season before the test block out of the fit, refit the model's own
log-odds on it, apply to the test years:

| market | worst bucket, trained through 2022 | held out + Platt |
|---|---|---|
| receiving yards | 1.6 pts | 2.3 pts (correction discarded) |
| receptions | 1.9 pts | 3.7 pts (correction discarded) |
| rushing yards | 1.9 pts | 1.3 pts (a = 1.011 — an identity) |
| passing yards | 4.8 pts | 4.4 pts |

Two of four end up worse, the one clear improvement is a 1% scaling, and
passing yards stays bad either way. **The extra season of training data is
worth more than the recalibration.** Kept behind `--platt`, off by default,
with the table in the script so nobody re-runs the experiment by accident.

---

## Finding 4 — the moneyline is efficient; don't bet it

nflverse carries real closing moneylines, so this one needs no proxy. 2,884
regular-season games, 2015-2025, ties dropped, devigged at a median overround
of 2.7%:

| line says | games | actually won |
|---|---|---|
| 16.3% | 60 | 20.0% |
| 25.5% | 222 | 20.3% |
| 35.3% | 380 | 36.3% |
| 44.8% | 442 | 42.8% |
| 55.6% | 483 | 55.5% |
| 64.8% | 590 | 61.7% |
| 74.9% | 496 | 75.8% |
| 84.7% | 191 | 86.9% |

Calibrated at every level. Flat-stake ROI at the real closing prices: always
home **−5.01%**, always away **−2.41%**, always favourite **−2.90%**, always
underdog **−4.52%** — i.e. everything loses roughly the vig.

**Shipped:** the Games tab lists moneyline, spread and total as context for the
prop board and makes no picks. Beating this needs a model genuinely better than
the market's and we do not have one.

---

## Finding 5 — anytime TD is a field market and must not be devigged like a price

A game's anytime-TD quotes sum to about **399%**. The first instinct is to call
that a 300% margin and normalise it away. That is wrong: roughly four different
players score in an NFL game, so most of that sum is real. Normalising each
game's field to the model's own total halved every fair price and turned the
whole board into a false double-digit edge — Mack Hollins at +900 showed as a
+14 point edge.

**Shipped:** the model is shifted in log-odds until *its* total matches the
book's, so both sides carry the same unknown margin, and the comparison reads
only the disagreement about **which** players score. `price_td` therefore
returns **no EV and no Kelly** — a dollar figure computed from a margin-inflated
probability would read positive across most of the board and would be believed.

A plain Poisson on the trailing TD rate was also tried and rejected: over 39,450
player-weeks `1 − exp(−λ)` reads 54.2% on its top bucket and delivers 39.6%
(ratio 0.73), because a touchdown rate is mostly noise and needs shrinking. The
logistic does that shrinking.

---

## Bugs worth remembering

**Volume defaulting to zero.** The projections carry no carries or attempts, and
`logvol` is the second-largest coefficient in every yardage model. Left empty it
read a starting back as a player with no touches: James Cook's over came back at
1%, which priced as an **86% edge** on the under. Volume is now derived from the
estimate at league-average efficiency (4.3 yds/carry, 7.0 yds/attempt, 11.5
yds/reception). The same bug in the TD path was passing receptions as *touches*,
which put Derrick Henry 40 points below the market.

**The model answers a different question from the price.** It gives
P(actual > L | the player's mean is X). A book does not set L at the mean —
yardage is right-skewed — so feeding an estimate equal to the line returns about
37%, not 50%, and every market tilts under. Fixed by `probability_offset`, which
centres the board's log-odds on the market's.

A two-parameter fit was the obvious version and it collapsed: ten days out
FanDuel quotes essentially every main prop at −114/−114, so the devigged market
probability is exactly 0.500 on every row, the slope fits to zero, and the whole
board flattens to 0.500. An offset has no such failure mode.

---

## Open — what should replace the guesses

**`SHRINK_PRESEASON = 0.25` / `SHRINK_INSEASON = 0.50` are priors, not
measurements.** They exist because a preseason projection of 21.8 rushing yards
for a starting back is a statement about August role uncertainty, and the
market's 74.5 is a statement about Sunday — we are not entitled to a 49-point
edge over the book on that question. Fitting them properly needs settled weeks
paired with the line that was posted at the time. **Nothing like the MLB odds
archive exists for football yet, and building it is the first thing to do.**

**No pick persistence or track record.** The MLB side records every pick and
settles it (`tracking.py`); the NFL side does not yet. Until it does there is no
way to tell whether any of this works. Week 1 is the moment to start recording.

**No card.** MLB freezes a daily parlay and scores it on sweep rate. The NFL
equivalent — a weekly card, one leg per game — is not built, deliberately: it
should not be built before there is a track record to size it from.

**Projections are only scored by us.** `/projections/accuracy` on NFL-API
returns `no_data` for every season, so the model has never been scored
prospectively. Scoring 2025 week 18 by hand against nflverse actuals (n=332)
gave projection MAE 12.03 vs trailing-4's 15.94 on receiving yards — about 25%
skill, with a small negative bias — which is genuinely good, but it is one week.
Running NFL-API's `score_projections` job would replace that with a real record.
