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

## Finding 13 — six weeks of settled picks: the availability layer is worth about six points, and the record has three holes in it

First read of the real track record, 2026 weeks 1-6, 513 recorded picks of which
417 have settled (216-195-6, **52.6%**, ROI **-2.9%**). Break-even at -110 is
52.38%, so as a whole the board is paying for itself and nothing more. The
interesting part is not the total, it is what the splits say.

**The availability layer went live in week 4, and the record splits almost
exactly on it.** Weeks 1-3 carry no `avail` and no `depth_rank` on a single
frozen pick; weeks 4-6 carry both on every one. That makes the `by_availability`
split in `/nfl/track-record` an accidental before/after of `nfl/availability.py`:

| | picks | W-L | hit | ROI |
|---|---|---|---|---|
| weeks 1-3, no injury/role view (`avail` null) | 268 | 125-124 | 50.2% | **-7.2%** |
| weeks 4-6, injury/role view live (`avail` ACTIVE) | 245 | 91-71 | **56.2%** | **+3.7%** |

Six points of hit rate and eleven of ROI, on 162 settled bets after the change —
and, per hole 1 below, earned on the roster-designation half of the module
alone, because the practice report has never reached it. Not a controlled
comparison — the weeks differ in every other way too, and week
3 (36-54, -25.0%) is doing a lot of the damage on the early side — but it is the
first evidence that refusing bets on men who are not playing, and on UNDERs
whose role just grew, is worth what it cost to build.

**The vacated-share direction holds up.** `VACATED_SHARE_BLOCKS_UNDER = 0.33`
was shipped as a judgement with nothing settled behind it. The rows that survive
the guard — a vacancy visible but not large enough to block, or an OVER on the
man inheriting it — are the best bucket on the board:

| vacated_share | picks | W-L | hit | ROI |
|---|---|---|---|---|
| major (>=0.33) | 5 | 3-0 | 100.0% | +74.5% |
| minor (0.08-0.33) | 50 | 20-12 | 62.5% | +18.0% |
| none | 458 | 193-183 | 51.3% | -5.3% |

Five picks is not a result and fifty barely is, but the sign is the one the
module predicted: where we can *see* the role change, we are better than the
market, and where we cannot, we are the market.

**The role-conflict verdict predicts, and the losing side of it is still being
bet.** `flag_role_conflicts` adjudicates a conflict against the depth chart and
records `role_conflict_verdict` — `"model"` when the chart agrees with our
projection, `"market"` when it says the market is describing the player's
current role and we are describing an old one. That was shipped recorded but not
acted on, with the explicit note that whether it predicts anything was a
question for the record. It is answered:

| verdict | picks | W-L | hit | ROI |
|---|---|---|---|---|
| model | 48 | 24-12 | 66.7% | **+22.1%** |
| market | 27 | 6-11 | 35.3% | **-35.9%** |
| no conflict | 130 | 65-56 | 53.7% | -1.5% |

Seventeen settled picks is a thin basis for a filter, so this is logged rather
than shipped. But it is the sign the module argued for, and it is the only
bucket on the board that loses more than a third of its stake.

**Rushing yards is the losing market, by a distance.**

| market | picks | W-L | hit | ROI |
|---|---|---|---|---|
| receiving yards | 169 | 79-60 | 56.8% | +6.9% |
| receptions | 207 | 89-76 | 53.9% | -2.9% |
| rushing yards | 137 | 48-59 | **44.9%** | **-15.6%** |

Which is consistent with everything in Finding 8: a backfield split is the role
the projection is worst at, and 137 picks is enough to take seriously. UNDER
(54.8%, +0.8%) also continues to beat OVER (50.2%, -6.6%).

Dropping the two losing buckets, on the 301 settled picks the pick endpoint
returns (week 1 falls outside its 400-row window):

| | W-L | hit | ROI |
|---|---|---|---|
| as it stands | 160-141 | 53.2% | -1.7% |
| drop verdict == market | 154-130 | 54.2% | +0.3% |
| drop rushing yards | 126-97 | 56.5% | +4.0% |
| drop both | 120-88 | 57.7% | +6.3% |
| drop both, weeks 4-6 only | 67-37 | **64.4%** | **+18.4%** |

These are filters chosen after seeing the outcomes, so the last row is an
upper bound on what they are worth, not an estimate of it. Recorded here to be
re-measured on weeks 7+ before anything is cut.

### Four holes the record exposed, and the first one is the big one

**1. The official injury report has never once reached the board.** `_verdict`
does `(report_status or "").strip().lower()`, and nflverse writes a *null*
`report_status` for every player who appears on the report with practice
information but no game designation — 195 of week 6's 376 rows, and a majority
of every week's. `float('nan')` is truthy, so `or ""` does not catch it and
`nan.strip()` raises `AttributeError`. That propagates out of `_fetch`, and
`week_board` answers an exception by returning **the last board it successfully
built**:

```python
except Exception as e:
    logger.warning(...)
    return cached if _cache["key"] == key and cached else Board(...)
```

A board only builds successfully while `inj[inj.week == week]` is *empty* — that
is, before the week's first club files. From the first filing onward every
rebuild raises and silently re-serves the pre-report snapshot. The live week-6
board, rebuilt on demand on 2026-10-19, still reports
`injury_report_rows: 0` and `depth_as_of: 2026-10-13T14:58:07Z`, while the feeds
themselves are current (376 injury rows for week 6; newest depth snapshot
`2026-10-19T14:35:37Z`). Null `report_status` rows exist in all six weeks
(121/182 in week 1), so this has been true since the module shipped.

What survived is the roster-designation leg — `RES`/`IR`/`PUP`/`SUS`/`NON` and
the `I01` inactive flag — because those come from the roster feed, which is read
before the crash. So the layer has been running on *half* its evidence: it knows
who is on injured reserve and who was ruled inactive, and it has never known
who was listed Out or Doubtful on the practice report. **The +6 points above
were earned without the injury report.** The fix is one line — coerce with
`pandas.isna` or `str(x or "")` rather than relying on `or`, in `_verdict` for
both `report_status` and `practice_status` — and the honest consequence is that
the availability measurement above has to be re-taken afterwards, because the
feed it is named for was not in it.

Worth separating from the upstream projection: NFL-API's own
`parse_nflverse_injuries` coerces with `str(... or "").strip().upper()` and
handles the same nulls correctly, so the *projection* has been reading the
report all along. Only the board's gate has not.

**2. Every Monday-night game is stranded, permanently.**

36 picks across weeks
2-5 are still pending, and all 36 are from exactly four games: NYG@LAR (wk2, 7),
PHI@CHI (wk3, 8), ATL@NO (wk4, 14), BUF@LAR (wk5, 7). Every one is the week's
Monday nighter. The cause is in `/nfl/settle` with no arguments:
`target = max((p["season"], p["week"]) for p in pending)` settles only the
single highest week that has anything pending. The settle cronjob refreshes the
board before settling, which *creates* the next week's picks — so by the time
Tuesday's settle runs, the max pending week has already advanced past the week
whose Monday game just finished, and it never goes back. `settle_week` itself is
correct; it waits on each pick's own team appearing in the published file. The
no-arg week picker is the bug. It should iterate every week with pending picks,
not take the max. Until it does, the record is missing one full slate a week,
9% of recorded picks, and it is a biased slice rather than a random one.

**3. About 15% of picks each week are frozen off the August projection.**
NFL-API publishes only the upcoming week to `player_projections`; later weeks
keep their preseason vintage until their own turn comes. `current_week` rolls to
week N+1 as soon as week N's last game is in the past — Monday night — while the
in-season projection for week N+1 is not published until the Tuesday 21:00
Denver run. The board therefore spends roughly a day each week pricing against
an August number computed with `use_injuries=False` and no current-season form.
`projections._is_preseason` catches it and `calibrate_to_market` rescales it, so
it is not blind, but it is a different model:

| frozen | W-L | hit |
|---|---|---|
| before that week's in-season projection | 22-21 | 51.2% |
| after it | 138-120 | 53.5% |

43 settled picks against 258 — directionally worse, nowhere near significant,
and worth re-reading at the end of the season. The cheap mitigation is to not
freeze picks against a projection `_is_preseason` has already flagged, once the
season is underway.

**4. The upstream projection has not been recomputed since week 6.** The
`nfl-api-projections` cronjob has failed every run since 2026-10-15 with
`pyarrow requires NumPy 2.0 or newer, found 1.26.4` — the same pyarrow/numpy
break that took out the roster sync. NFL-API pinned `pyarrow>=7.0.0,<18` in its
own image, but this job pip-installs `nfl_projections` from the `main` tarball
on top of that image and `nfl-data-py`'s `pyproject.toml` leaves `pyarrow`
uncapped, so the cap is only as good as whatever pip decides to leave alone. The
injury and fill-in machinery the board depends on — zeroing OUT and DOUBTFUL
from the nflverse report, promoting whoever is behind them via
`roles.effective_rank`, capping the group with `roles.apply_team_budget` — all
of it runs inside that job, so no run means no fresh view of either. Week 6's
projection carried 105 `injured_out` rows; week 7's, as it stands, is the August
one and carries none.

### What shipped

Holes 1, 2 and 4 are fixed. Hole 3 is left as it is, deliberately.

**1 — `availability._text`.** One coercion helper, applied where the injury
feed is read rather than at each use, so a parquet null never reaches
`_verdict`, a `Player`, or the JSON the screen serialises. Verified against the
live feeds on 2026-10-19: `injury_report_rows` 0 → **376**, `depth_as_of`
`2026-10-13` → `2026-10-19T14:35:37Z`, and the week-6 board went from 455 OUT
and *no* QUESTIONABLE to 503 OUT and 82 QUESTIONABLE — about fifty absences
that had never reached the gate, plus a whole status that had never appeared on
the board at all.

`week_board` also stopped re-serving a stale board silently. The fallback is
still right — the alternative is dropping every availability guard over one bad
fetch — but the copy it returns now carries `stale_seconds` and the reason, and
`summary()` reports both, so the next time this happens the board says so
instead of looking built.

Tests: `_verdict` over every null parquet hands us, and — the ones that would
actually have caught it — `_fetch` and `week_board` executed against stubbed
feeds. Every availability test before this called `_verdict` directly, which is
precisely why a bug in how `_fetch` assembles its arguments survived six weeks.

**2 — the settle walk.** `/nfl/settle` with no arguments now iterates every
week holding an unresolved pick, oldest first, each in its own try so one bad
week cannot strand the others, and reports per-week results plus totals. A
named `(season, week)` behaves exactly as before. The cronjob's curl timeout
went to 600s for the one-time catch-up of weeks 2-6.

**4 — the pyarrow cap**, in `nfl-data-py`'s `pyproject.toml` *and*
`requirements.txt`: `pyarrow>=7.0.0,<18`. The cronjob installs the package from
the `main` tarball over the API image, so the image's own pin could not survive
a resolve it did not control — pip saw 26.0.0 as newest and took it. Under the
cap it resolves 17.0.0, which is the version already in the image and the one
the last successful run used. Guarded by two tests: a parquet round-trip that
exercises the pyarrow/numpy bridge rather than reading a version number, and a
check that the cap is present in both files.

**3 — left alone.** Not freezing picks against a projection `_is_preseason`
has already flagged would be acting on 43 settled bets at a 2.3-point
difference, which is noise, and it would cut 15% of the sample that is meant to
measure it. The two real options are owner calls rather than bug fixes: publish
the upcoming *two* weeks from NFL-API, or roll the board to week N+1 on the
Tuesday publish instead of the moment week N's last game ends. Re-read at the
end of the season.

---

## The lock — open wide, tighten, commit

Shipped on the owner's design: look at the opening line, refine the set as the
week fills in, and commit each bet before its own game. It replaces a freeze
model where a pick was upserted with the current line for as long as it stayed
unresolved, which meant the recorded "bet" was whatever the board last saw
rather than anything anyone could have placed.

**The deadline is each game's own kickoff less two hours, not a time of day.**
The brief was "Thursday night locking at noon Thursday, Sunday bets locking at
11 on Sunday". Anchored per game that comes out at 11:00 Eastern for a 1pm
kickoff — the same answer — and stays right everywhere a flat rule breaks:

| slate | kickoff | lock |
|---|---|---|
| Thursday night | Thu 20:15 ET | Thu 18:15 ET |
| London | **Sun 09:30 ET** | **Sun 07:30 ET** |
| Sunday early | Sun 13:00 ET | Sun 11:00 ET |
| Sunday late | Sun 16:25 ET | Sun 14:25 ET |
| Sunday night | Sun 20:20 ET | Sun 18:20 ET |
| Monday night | Mon 20:15 ET | Mon 18:15 ET |

A flat "Sunday 11:00" locks the late slate five hours early, both night games
nearly a day early, and a London game **ninety minutes after it kicked off**.
There is a London game most weeks in September and October, so that is not a
corner case — it is a silent recording of bets on games already in progress,
which is the exact class of bug `purge_late_picks` exists to clean up after.
Two hours because the official inactives land at ninety minutes; locking after
them would record a price the market had already moved off.

**Three states, and the database enforces the boundaries.** `lock_at` is
stored on the row, so a bet is committed by the clock passing a value already
written rather than by a job running — there is nothing to miss and nothing to
race. The upsert carries `AND (lock_at IS NULL OR lock_at > NOW())`, and the
`open_*` columns are absent from every `DO UPDATE` clause, so neither the
opening number nor a committed bet can be rewritten by a later board. That is
a guarantee from the schema rather than from discipline upstream.

An unreadable kickoff leaves the row open rather than locking it, the same
direction `kickoff_passed` already defaulted: freezing a row we cannot date
would pick a moment at random and call it a commitment.

**Tightening had to be built, not just named.** `freeze_week` was add-only, so
a row recorded on Tuesday that developed a role conflict or an injury flag by
Friday stayed on the record and settled anyway. `_tighten` now drops a
candidate the board has turned down — but only on positive evidence: the market
is on the board *and* the gate declined it. A locked pick, a graded pick, and a
pick whose market is simply not posted are all left alone, because FanDuel
pulls markets routinely and a thin fetch must never read as "the gate declined
these". One bad board refresh would otherwise delete the week.

**What it unlocks.** Both numbers are graded, so the record can now answer a
question it previously could not ask, because the opening line was destroyed by
the next refresh: *would we have been better off firing at open?* The
comparison also yields `moved_to_us` — of the picks whose line moved between
open and lock, the share that moved toward the side taken at open. That is
closing line value measured against our own commitment point, and it is the
best read available on whether a bet was good that does not depend on whether
it won. Nothing to report yet: no pick carries both lines, and the first
numbers arrive when the first locked week settles.

**Read that comparison with hole 3 above in mind, because the lock half-fixes
it and the open half-inherits it.** The board rolls to week N+1 the moment week
N's last game ends, which is Monday night, while the in-season projection for
week N+1 is not published until Tuesday 21:00 Denver. A lock two hours before
kickoff is therefore always on the current projection — the committed bet is
clean, which is the structural fix hole 3 was asking for. The *opening* line,
though, is captured on whatever board first carried the market, and for a
Monday-or-Tuesday open that can still be the August vintage with
`use_injuries=False` behind it. So if the open grades worse than the lock, part
of the gap may be the projection rather than the price, and the two cannot be
separated from the record alone. Splitting the comparison on whether the open
was taken against a `_is_preseason` board is the way to tell, and is worth
adding once there are enough locked weeks for it to say anything.

One consequence worth stating plainly. Every settled pick is necessarily a
locked pick, since the lock precedes kickoff which precedes settlement — so the
track record remains a record of committed bets, and dropping an unlocked
candidate removes something that was never one.

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
