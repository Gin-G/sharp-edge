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

## Finding 6 — the projections mislabelled every suffixed veteran as a rookie

Noticed on the week-1 board: James Cook projected for 21.3 rushing yards and
Travis Etienne 20.9, both starting backs. Not low — wrong.

The tell was `prediction_type`. Both came back `rookie_prior`, meaning the
engine had no history for them and fell back to draft capital run through a
positional curve. Cross-referencing the whole board against nflverse 2025, ten
players with real history were typed `rookie_prior`, and **all ten are name
normalisation failures**:

| player | 2025 | projected |
|---|---|---|
| James Cook III | 1,621 rush yds, 17 g | 18.8 |
| Travis Etienne Jr. | 1,107 rush yds, 17 g | 18.5 |
| Kyle Pitts Sr. | 928 rec yds, 17 g | 0.0 rec |
| Michael Pittman Jr. | 784 rec yds, 17 g | 0.7 rec |
| Aaron Jones Sr. | 548 rush yds, 12 g | 17.3 |
| Brian Robinson Jr. | 400 rush yds, 17 g | 15.5 |
| Tre' Harris | 324 rec yds, 16 g | 0.6 rec |
| Audric Estimé | 198 rush yds, 5 g | 12.8 |
| David Sills V | 191 rec yds, 12 g | 0.6 rec |
| Gardner Minshew II | 4 g | 0.2 |

Eight generational suffixes, one apostrophe, one accent.

**Cause.** `PlayerPredictor._predict_one` looked history up with

```python
self.history["player_display_name"].str.contains(player_name, case=False)
```

nflverse stores names *without* suffixes ("Travis Etienne"); rosters keep them
("Travis Etienne Jr."). Asking whether the stored name **contains** the roster
name is false in that direction, so the player's entire history came back
empty. There was no `player_id` branch on that lookup at all. `features.is_rookie`
had the same containment bug, with an id branch that didn't save it.

Two lesser faults rode along: the pattern was interpreted as a **regex**, so the
dots in "A.J. Brown" matched any character; and substring matching lets a short
name collide with a longer one.

**Fixed upstream** in `nfl-data-py` — match on `player_id` first, then on an
exact normalised name, using the `utils.normalize_player_name` helper that
already existed in that repo and strips exactly these suffixes. Verified against
2024-25 nflverse: all eight suffixed players recover their history (33-38 games
each), unsuffixed players are unchanged.

**Guarded here too**, because sharp-edge cannot control when NFL-API recomputes.
A `rookie_prior` row is a positional prior with no player-specific information
in it, so differencing it against a line measures the prior's distance from the
market rather than anything about the player. Those rows are now priced and
displayed but never fire (`screen.PRIOR_ONLY_TYPES`), and the ones held back are
reported in `held_prior_only` so a stale upstream table is visible instead of
silent. That guard is correct for genuine rookies on its own terms.

**How much it mattered.** 9 of 67 signals came from these players — including
three of the top seven and the single strongest pick on the board (James Cook
UNDER at −43.2). Every held-back row was an UNDER, which is the direction the
bug necessarily pushes: a prior sits far below a starter's line. Any NFL result
recorded before this date should be treated as contaminated.

---

## Finding 7 — a short line is where the model is least trustworthy, both ways

The first card the screen produced was **Mack Hollins over 8.5 receiving yards**
and **Malik Davis over 13.5 rushing yards**. Sorted by edge, the entire top of
the board was backups on short lines. The correlation was almost perfect:

| player | line | projection | ratio | "edge" |
|---|---|---|---|---|
| Mack Hollins | 8.5 | 33.3 | 3.92x | +28.2 |
| Malik Davis | 13.5 | 44.2 | 3.27x | +26.7 |
| Corey Kiner | 8.5 | 26.7 | 3.14x | +25.6 |
| Tyler Higbee | 14.5 | 36.3 | 2.50x | +20.0 |
| RJ Harvey | 18.5 | 45.2 | 2.44x | +21.5 |

The edge was ranking on the projection/line ratio, which is not a measure of
value. **A short line is the book saying the role is uncertain** — WR5, a
committee back, someone easing back from injury — and role is precisely what
the projection does not model. It carries no snap share and no depth chart; it
is a season-long rate for a nominal starter. So on a short line the two numbers
are answering different questions and the gap measures our ignorance.

The first version of this guard covered only the under, on the reasoning that
an under at a short line is a bet on a player being inactive. That was right as
far as it went and missed that **the failure is symmetric**: the over at a short
line is a bet against a role risk we cannot see, which is the same ignorance
pointed the other way.

Two supporting measurements. On held-out 2023-25 the model is *overconfident*
exactly here, and only here:

| line | receiving: predicted / actual | rushing: predicted / actual |
|---|---|---|
| ≤ 10 | 68.1% / **65.0%** | 64.8% / **61.7%** |
| 10-20 | 51.6% / 52.6% | 49.8% / 51.0% |
| 20-30 | 37.1% / 39.7% | 34.4% / 37.3% |
| 30-50 | 25.5% / 26.9% | 24.1% / 25.7% |
| 50-100 | 15.1% / 13.1% | 14.6% / 12.7% |

Every other bucket errs low. And on the live board the median suggestion sat at
1.39x projection/line with the 90th percentile at 2.44x, so the failures were
all in the tail.

**Shipped:** `card.MIN_LINE` (20 yards, 2.5 receptions, 175 passing yards)
refuses both sides below the bar, and `card.MAX_PROJECTION_RATIO` (2.0) catches
the same failure at higher lines. Both are priors, and both are exactly what
the track record is for.

---

## Tracking, from week 1

Nothing above can be settled by argument, so everything is now recorded.

**What gets recorded.** Every row clearing the thresholds is a *suggestion* and
is written to `nfl_picks` with its price, model probability, edge and
projection. The *card* is the best two, one per game, frozen in `nfl_cards`. The
split is deliberate: a two-leg card produces two settled outcomes a week, which
would take a season to say anything, while the suggestions produce a dozen.

**When.** The freeze happens on the board's read path, and both writes are
idempotent, so the week is captured by the first page view or the daily cron —
whichever comes first. That matters more than in baseball: an NFL line moves all
week and FanDuel pulls each market at kickoff, so a card re-derived on Monday is
made of whichever games had not started.

**Settlement** is a separate daily pass against nflverse actuals — the same
source the model is calibrated on, because settling on one source and fitting on
another is how a track record quietly stops meaning anything. A player with no
row is VOID, not LOSS; voids and pushes leave the denominator.

**Reported as hit rate *and* ROI, always together.** That pairing is the single
most useful thing the baseball side learned: the retired batter screen hit 64.8%
and lost money, because its median price was -260 against a 72% break-even.

---

## Open — what should replace the guesses

**`SHRINK_PRESEASON = 0.25` / `SHRINK_INSEASON = 0.50` are priors, not
measurements.** They exist because a preseason projection of 21.8 rushing yards
for a starting back is a statement about August role uncertainty, and the
market's 74.5 is a statement about Sunday — we are not entitled to a 49-point
edge over the book on that question. Fitting them properly needs settled weeks
paired with the line that was posted at the time. **Nothing like the MLB odds
archive exists for football yet, and building it is the first thing to do.**

**Every selection threshold is a prior.** `MIN_EDGE_PTS` (3.0), the higher
`UNDER_MIN_EDGE_PTS` (6.0), `MIN_LINE`, `MAX_PROJECTION_RATIO` and both shrink
factors were all chosen from reasoning plus one live board, not from settled
results. They are now recorded per pick, so each becomes testable after a month:
split the track record by side to test the under bar, by line size to test
`MIN_LINE`, by edge bucket to test the floor.

**Projections are only scored by us.** `/projections/accuracy` on NFL-API
returns `no_data` for every season, so the model has never been scored
prospectively. Scoring 2025 week 18 by hand against nflverse actuals (n=332)
gave projection MAE 12.03 vs trailing-4's 15.94 on receiving yards — about 25%
skill, with a small negative bias — which is genuinely good, but it is one week.
Running NFL-API's `score_projections` job would replace that with a real record.

## Finding 8 — the projections describe last season's roles, and it is half the board

Raised from the board rather than the code: the Jaguars.

Travis Etienne carried 260 times for 1,107 yards for Jacksonville in 2025 and is
now in New Orleans. Bhayshul Tuten is RB1 on the 2026 depth chart
(`depth_team: 1`) and FanDuel prices him at 50.5 rushing yards. We project him
**16.9**, and suggest the under — because 16.9 is what he did as a rookie behind
Etienne. His backup Chris Rodriguez Jr. projects **51.4**, off a season in
*Washington*, and we suggest the over.

| | 2025 | 2025 rate | 2026 projection | line |
|---|---|---|---|---|
| Tuten | JAX, 83 car / 307 yds, behind Etienne | 20.5/g | 16.9 | 50.5 |
| Rodriguez | **WAS**, 112 car / 500 yds | 41.7/g | 51.4 | 33.5 |

**Two mechanical causes, both in the engine.**

The depth-rank multiplier is applied to `fanduel_fantasy_points` and to nothing
else (`predict.py:626`) — never to the component stats that a prop settles on.
And it only ever scales *down* (`if role_mult < 1.0`), so RB1 is x1.00: a player
inheriting a vacated role keeps his backup-rate projection, while the man he
replaced keeps a starter's. Promotion is unmodelled by construction.

**It is not one row.** Comparing our projected ordering against the market's
line ordering within each team and market, the live week-1 board had 66 inverted
teammate pairs and **20 of 41 suggestions on one side of one**:

| team | we fade | we back |
|---|---|---|
| DEN | Waddle 53.5 | Pat Bryant 23.5 |
| HOU | Montgomery 54.5 | Woody Marks 29.5 |
| TB | Egbuka 52.5 | Cade Otton 28.5 |
| MIN | Addison 42.5 | Jauan Jennings 22.5 |
| JAX | Tuten 50.5 | Rodriguez 33.5 |

Always the same shape: fade the market's lead man, back his backup, in pairs.

**Flagged, not filtered, and that is the whole point.** These rows are wrong for
a reason we can articulate, but nobody has measured whether they *lose*. Two
things could be true — the market's role read is right and we are donating, or
the market overprices name recognition on a lead back and the backup really is
live. Dropping them would remove the only evidence that could settle it. So
`screen.flag_role_conflicts` marks both sides, the flag rides along in each
pick's `metrics`, and the track record splits on it. After a few weeks the
question is answered with results.

**The real fix is upstream and is not a filter.** The projection needs a view of
what a player is *going to* do, not only what he has done — offseason role
change, depth chart, and coaching scheme. Jacksonville is the example again:
Liam Coen is the new head coach, and his Tampa Bay backfield usage is a better
prior for how Tuten gets used than Tuten's own 2025 snaps behind Etienne.
NFL-API already stores coach analytics (`coach_season_analytics`,
`/coaches/{name}/tendencies`) that could feed this. That is the next iteration,
driven by week-1 actuals rather than by argument.

## Finding 9 — matchup, but only opponent-adjusted, and only three markets

The share model fixed bias but not correlation, so the next question was
whether the defence a receiver faces carries the information that would.

**Raw allowed-per-game does not, and the reason is the finding.** It measures
who a defence happened to draw as much as how it played: face three elite tight
ends and it looks terrible, face three backups and it looks elite. Shipped
first in that form — tight ends only, because it was the one position where raw
moved anything.

**Compare every player against himself instead.** For each player-game against
a defence, the baseline is that player's production over all his *other* games
that season, and the game scores as a ratio; average those weighted by baseline
volume and opponent quality cancels. Leave-one-out is load-bearing — a season
average that includes the scored game leaks the answer into its own baseline.

Four week-1 samples (2022-25), prior season only. The bar is MAE improving in
at least three of four seasons: correlation alone is not enough, since a metric
can shuffle ranks without getting closer.

| market | MAE wins | corr wins | shipped |
|---|---|---|---|
| TE receiving yards | **4/4** | 3/4 | yes |
| RB receiving yards | **4/4** | 3/4 | yes |
| RB receptions | **3/4** | 3/4 | yes |
| TE receptions | 2/4 | 3/4 | no |
| RB rushing yards | 2/4 | 2/4 | no |
| WR (any market) | 0-2/4 | 0-1/4 | no |

The headline is not the tight-end gain, it is that adjusting **unlocked
running-back receiving**, which raw did not justify (2/4 seasons) and adjusted
does (4/4). Unweighted leave-one-out is slightly *worse* than raw, which is the
argument for the volume weighting rather than a free parameter.

**TE receptions was shipped in the first pass and has been removed** — it wins
correlation 3/4 but MAE only 2/4, and the bar is both.

**Wide receiver fails everywhere.** Yards allowed to WR is spread across a
defence's whole secondary and a team's whole receiving corps, so it says almost
nothing about the matchup one receiver faces. What would is a shadow-coverage
assignment — which corner travels with him and whether he is any good — and
nflverse publishes no such field. That is a missing data source, not a formula
to fix. **Rushing fails for a plainer reason:** the factor is built from
receiving yards allowed, which is a statement about pass defence.

**Coverage scheme is a null result.** Man/zone is published (49% of snaps
classified) and a receiver's own man-vs-zone yards per target is computable, but
regressed for sample size the adjustment spans 0.977 to 1.015 and changed MAE by
0.03 yards across 763 player-weeks. Not wired in.

Shipped as `nfl/matchup.py`, clamped to 0.75-1.30, touching 112 rows on the live
week-1 board across 28 distinct factors. Only the prior-season form is
validated, which is the week-1 case; blending in current-season defence once
weeks accumulate is untested.

**Fixed alongside:** `adjusted_projection` had no floor, so a player projected
far below his line came out at minus two receiving yards — not a quantity that
exists, and it fed a correspondingly overstated under.

## Week 1, first two games — projections against actuals

Wednesday and Thursday, four teams, 44 players matched. Small, but the shape is
already legible and it is not uniform.

| market | pos | n | MAE | bias | corr |
|---|---|---|---|---|---|
| receiving yards | WR | 18 | 18.0 | +5.6 | 0.750 |
| receiving yards | **TE** | 10 | **24.6** | **+22.5** | **0.300** |
| receiving yards | RB | 10 | 6.5 | +0.5 | 0.809 |
| rushing yards | RB | 10 | 19.1 | +2.1 | 0.552 |
| rushing yards | QB | 6 | 11.0 | -6.1 | 0.863 |
| receptions | WR | 18 | 1.5 | +0.2 | 0.709 |
| receptions | RB | 8 | 0.9 | -0.6 | 0.848 |
| passing yards | QB | 6 | 90.2 | +52.5 | 0.380 |

Running backs and wide receivers are in decent shape. **Tight end receiving is
the worst position on the board** and it is not close: +22.5 yards of bias
against a 0.300 correlation.

**That is uncomfortable, because TE receiving is the one market the matchup
factor was added to.** Finding 9 measured that factor against a *trailing
average* baseline, not against this production model, and a multiplicative
factor above 1.0 applied to a projection already running +22.5 high compounds
the error rather than correcting it. George Kittle carried x1.114 and came in
at 12 yards against a 60.6 projection. Treat the TE factor as unproven in
production until several weeks have settled; if the bias holds it wants fixing
at the projection level, not covering with a multiplier.

Passing yards at +52.5 bias is a second vindication of keeping that market off
the card.

**Our own five picks went 2-3**, which says nothing at n=5. What does say
something is that **all five projections were high** — MAE and bias both
exactly 25.0, so not one under-shot.

---

## The settlement bug that nearly erased week 1

The daily job ran on the Friday, saw that nflverse had published week 1, and
voided **40 of 45 picks** — Tuten, Lamar Jackson, Derrick Henry, CeeDee Lamb,
every one of whom played on the Sunday.

nflverse publishes a week **incrementally**: the Wednesday and Thursday games
land days before the Sunday slate. Settlement assumed that if a week's file
exists then every player in that week is in it, so "no row for this player" was
read as "did not play" when it meant "has not kicked off". The void branch
itself is correct and stays — an inactive player really is a void — it was the
precondition that was missing.

`_load_actuals` now also returns the set of teams present in the file, which is
a different question from which players are, and a pick whose team is absent is
left pending. The card is only scored once no leg is still waiting, since a
parlay cannot be graded off whichever legs happen to have finished. Verified
live: `waiting_on_kickoff: 42`, zero voids.

The 40 rows were repaired back to pending. Nothing was lost — they keep their
line, price and projection — but it is worth recording how close the week came
to being unrecoverable, because a voided pick is indistinguishable from a
legitimate one after the fact.

**A second, smaller one found the same afternoon.** Settlement was aborting
outright with `normalize() argument 2 must be str, not float`: nflverse ships
NaN for a missing `player_display_name`, and a float NaN is *truthy*, so
`if not name` sailed past it. One malformed row took a whole week's settlement
with it. The guard is an explicit `isinstance` now.

## Finding 10 — the receptions market was dead and nobody could see it

Added a per-week snapshot of the whole board (`nfl_board_snapshots`, one row
per market) because the over/under tilt cannot be diagnosed from the picks
alone: you cannot tell from a pick list whether the residuals were already
skewed before the threshold or whether the threshold made them so.

It paid for itself on the first run. **Receptions had fired zero times on 138
rows.**

A threshold only means something relative to how far the residuals actually
spread. Measured on the live week-1 board:

| market | residual spread (p90-p10) | threshold | ratio | fires |
|---|---|---|---|---|
| receiving yards | 22.6 | 10.0 | 0.44 | 34/141 |
| rushing yards | 34.7 | 10.0 | 0.29 | 23/69 |
| receptions | **1.8** | **2.0** | **1.11** | **0/138** |

Receptions was being asked to clear more than its entire spread. Even on the
*raw* projection-minus-line gap, before any rescaling, 2.0 fires 6 times in 138
— so the stated number was always near-prohibitive and the market rescaling
finished it off.

This is the failure mode a pick list cannot show you: a board that produces no
receptions picks looks exactly like a board with no receptions edges.

**Set to 0.75**, between the two yardage ratios, firing at a rate comparable to
rushing yards. A deliberate departure from the stated rule rather than an
oversight, and it changes what gets bet, so it is first on the list to revisit
against settled results.

Effect: suggestions went from 34 to 61, with receptions supplying 27 of them —
and the board's over/under split moved from 24/14 to 28/33. Some of the tilt
was one market being missing, not a bias in the residuals.

**Also captured, for the same reason.** `matchup_factor` now rides in each
pick's `metrics`. Without it there would be no way to ask later whether the
matchup adjustment helped, or to back out what a projection would have been
without one — and a field added after the fact only covers weeks that have not
happened yet.

## Finding 11 — the most stable defensive trait is also nearly inert

Prompted by a fair objection: anchoring team scoring to the Vegas total, which
the upstream notes recommend, concedes the level to the market and leaves edge
only in how a team's production is divided. If the point is to beat the line, the
model wants to be independent of it.

Worth stating plainly that **the shipped pipeline already anchors to the market
twice** — `calibrate_to_market` rescales our projection onto the line's scale and
keeps only the residual, and `probability_offset` centres our probabilities on
the market's. The defence is that both correct a known bias in *our* model (it
shrinks toward the mean, a line does not) rather than importing the market's
opinion of a game. That is a real distinction but a thinner one than it sounds.

**The granular idea does not work.** Split-half stability of a defence's trait,
186,137 plays with participation data, 2022-25 — same method as the prior null
result so the numbers are comparable:

| trait | split-half r | plays per cell |
|---|---|---|
| team: **success rate allowed** | **0.618** | 721 |
| team: EPA allowed per play | 0.397 | — |
| team: EPA per rush / per pass | 0.234 / 0.233 | — |
| team x **personnel**: EPA | **-0.062** | 21 |
| team x personnel: success rate | 0.026 | 21 |
| team x formation: success rate | 0.033 | 95 |
| team x man/zone: EPA | 0.139 | — |

Slicing by personnel or formation does not refine the signal, it destroys it —
one cut is negative. The last column is the mechanism: a team-half-personnel
cell holds about **21 plays**, and a defence's true rate against 12 personnel
cannot be measured from 21 snaps. Formation-vs-formation matchup is not
reachable with the data available, and no amount of modelling fixes a sample
size.

**But team success rate allowed is a genuine find** — r 0.618, more than double
anything in the prior work, well above EPA's 0.397, and computed entirely from
play-by-play. The prior work concluded "defence is weak" from *fantasy points
allowed*, which is volume-contaminated and noisy; success rate is bounded per
play, so explosive plays cannot drag it, and it behaves completely differently.

**And then stability turned out not to be predictive power.** As a player-level
multiplier over four week-1 samples:

| market | MAE change | seasons won |
|---|---|---|
| TE receiving | **-0.24** | **4/4** |
| WR receiving | -0.03 | 3/4 |
| RB rushing | +0.03 | 2/4 |
| RB receiving | +0.02 | 1/4 |
| QB passing | -0.98 | 2/4 |

The whole factor spans 0.94 to 1.06. A six per cent nudge cannot move a
projection far however well it is measured. This is the prior work's warning
arriving from the other direction: there the matchup signal was real but
unstable, here it is real *and* stable and still tiny.

**Shipped for tight-end receiving only**, which is the one market clearing the
three-of-four bar. It earns that place by being independent rather than large:
stacked on the existing matchup factor it takes TE receiving from 16.71 to 16.52
MAE, 4/4 seasons, and the two factors correlate **0.110** — different
information, not the same signal twice. Clamped to 0.90-1.12, tighter than the
main factor because the measured spread is tighter. Both are recorded separately
per pick so neither can hide behind the other.

Live board: 34 rows, factors 0.900 to 1.087.

## Finding 12 — the gate, and the one thing that got through it

Every usage result had been scored against a trailing average. The shipped
model is not one — it has an ML layer, a form blend, a depth-role multiplier and
a team budget — so "beats trailing" was a proxy, and a weak baseline flatters
any method. `evaluate.backtest` was run for real: trained on seasons before
2025, weeks 1-8, component projections, identical player-weeks.

**Single seed first, all four markets:**

| market | n | prod | trail | usage | usage-prod | wks won |
|---|---|---|---|---|---|---|
| RB rushing | 524 | **22.21** | 25.17 | 24.01 | **+1.80** | 4/8 |
| WR receiving | 888 | **22.59** | 25.20 | 24.09 | **+1.50** | 2/8 |
| TE receiving | 457 | 15.93 | 16.15 | **15.34** | -0.59 | 5/8 |
| RB receiving | 545 | 11.33 | 12.12 | 11.50 | +0.16 | 5/8 |

The earlier 4/4 and 4/5 wins were beating a weak baseline. Production beats
trailing everywhere by 2.5-3 MAE and beats usage by 1.5-1.8 on the two big
markets.

**Five seeds plus a bootstrap on the difference**, for the two the single-seed
run left open. A 0.15 margin against a +/-0.05 seed lottery cannot be settled by
a point estimate:

| market | n | prod | usage | blend | usage-prod 95% CI | blend-prod 95% CI |
|---|---|---|---|---|---|---|
| TE receiving | 456 | 16.11 | 15.38 | 15.49 | [-1.48, **-0.00**] | [-1.00, **-0.24**] |
| RB receiving | 531 | 11.52 | 11.80 | 11.44 | [-0.33, +0.86] | [-0.39, +0.21] |

**The interval earned its place.** Raw usage scores better than the blend on TE
(15.38 against 15.49) and its interval runs to -0.00 — touching zero — while the
blend's stops at -0.24. A better point estimate with an interval containing zero
is not a better result, so the blend ships and the raw projection does not.
Running-back receiving is a null on both and stays out.

**Shipped** as `nfl/usage.py`: tight-end receiving only, an even 50/50 blend,
weight untuned because a weight fitted on the same eight weeks that measured the
effect would be fitting noise. Applied before the matchup factors, since the
blend was measured against the model's own component projection. Live board: 33
rows, usage drawn from 2025 until the current season has two weeks. About four
per cent error reduction on one market — small, measured, and the only member of
this family that survived contact with the real model.

**Three bugs on the way in, and the third is the lesson.** The blend did not
reach the projection-vs-line fit, which would have put the fit and the rows on
different scales; a test caught that. Then the board died twice at runtime with
`NameError` — once a missing import, once a missing local — while all 250 tests
stayed green, because every wiring test inspected source *text* rather than
running the function. There is now a smoke test that actually executes
`build_board` against stubs. Source inspection proves a string is present; only
execution proves the code runs.

---

## Operations

**Daily settlement** runs in-cluster at 13:00 UTC
(`helm/sharp-edge/templates/nfl-settle-cronjob.yaml`). It refreshes the board
first — which is what *records* the upcoming week, since the freeze happens on
the read path — then settles whatever is pending. Daily rather than weekly
because nflverse's publish time is not guaranteed, a Monday-night game lands a
day after the Sunday slate, and both calls are idempotent.

**Daily report** at 14:00 UTC, an hour after settlement: a scheduled cloud agent
reads `/nfl/track-record` and `/nfl/screen` and writes a short check-in. Its
most useful section is the third one — `held_prior_only` non-empty with an
established name in it means the projections table upstream has gone stale or
started mis-joining names again, which is exactly how the Cook/Etienne bug would
resurface.

**Recomputing projections after an upstream fix.** The projections cronjob
pip-installs `nfl_projections` from the tarball of `Gin-G/nfl-data-py@main`, so
a fix merged there needs no image rebuild — only a re-run:

```bash
kubectl create job -n nfl-api --from=cronjob/nfl-api-projections proj-manual
```

It takes ~35 minutes (a 5-seed ensemble). Afterwards the board must be rebuilt
with `?force=true`, because the screen caches the projections it fetched for ten
minutes and would otherwise serve the pre-fix numbers.
