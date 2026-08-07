# Experiments — batter screen

Running log of hypotheses about the MLB batter screen, how to test them, and
what the tests said. The screen sits around a 65% hit rate; everything here is
an attempt to move that without gutting volume.

The code changes shipped alongside it are deliberately split into *on by
default* (a subtractive rule that encodes an observed failure) and *off by
default* (a new, volume-expanding rule that has to earn its place).

**Backtest run 1 is in** (2026-08-06, 125 days) — see [Results](#results). Short
version: the screen is 100% BvP, `hand_slump_edge` has never fired, the veto is
real but small, and the `hittable` bar shipped at 9.5 was roughly 1.5 H/9 too
loose to be worth anything.

**Defaults were updated on 2026-08-07** on the strength of that run — HITTABLE
moved to 11.00 H/9 / .310 BAA and `include_hittable_edge` turned on, taking the
screen from 2.7 to 12.5 picks/day at a higher hit rate (66.2% → 67.4%). See
[the applied table](#defaults-after-run-1--applied-2026-08-07) and
[run 2](#run-2--the-shipped-configuration-against-the-history).

---

## The observation that started this

2026-08-06. The screen picked two Cubs — Nico Hoerner and Pete Crow-Armstrong —
against Toronto and **Dylan Cease**. PCA got a hit in the 9th, Hoerner didn't;
Cease gave up 2 hits on the day. He'd recently taken a perfect game deep into a
start against a Red Sox team that had lost about 5 of its last 40.

The screen had no way to know any of that. It read the opposing starter through
exactly one number: **ERA over his last 3 starts**, and only for the
`hand_slump_edge`. The `bvp_edge` — which is what most picks actually fire on —
looked at the pitcher not at all.

ERA is the wrong lens for this bet. The prop settles on *did this batter record
a hit*, and ERA can be inflated by three solo homers while the pitcher holds the
lineup to four hits. That pitcher looks like a target under the old rule and is
in fact the worst possible matchup. Hit-suppression is the thing to measure.

---

## What changed in the code

### 1. The starter's contact line, over his last 3 starts

`_pitcher_gamelog_starts` now carries `hits` / `atBats` / `battersFaced` /
`baseOnBalls` / `strikeOuts` per start, and `_pitcher_form(pid, season, starts,
before)` turns a window into `era / ip / hits / h9 / baa / whip / k9`.
`starts=None` gives the season line for context.

Every row on the board gained: `p_l3_hits`, `p_l3_h9`, `p_l3_baa`,
`p_l3_whip`, `p_l3_k9`, `p_season_h9`, `p_season_baa`, `p_season_starts`.

Contact stats are `None`, never `0`, unless *every* start in the window
reported them — a game log missing the field must not read as a no-hitter.

### 2. Banding the starter — `_sp_band`

| Band | Rule (last 3 starts) | as shipped | after run 1 |
|---|---|---|---|
| `SHARP` | `h9 ≤ X` or `baa ≤ Y`, ≥2 starts | 6.50 / .210 | **unchanged** |
| `HITTABLE` | `h9 ≥ X` or `baa ≥ Y`, ≥2 starts | 9.50 / .270 | **11.00 / .310** |
| `NEUTRAL` | in between, or too few starts to judge | | |
| `UNKNOWN` | no contact line in the game log — nothing is vetoed | | |

League-average starters sit near 8.5 H/9 / .250 BAA. The original guess put both
bands a run-and-a-half of hits per nine off the middle, symmetrically. Run 1
kept the SHARP side and pushed the HITTABLE side much further out: hit
suppression turned out to be a tail effect, not a gradient, so a symmetric band
spent most of its volume on a flat stretch of the curve.

The `≥2 starts` requirement originally applied only to SHARP; run 1 extended it
to HITTABLE, which had been branding starters off a single outing.

### 3. Rule changes

- **`veto_sharp_sp=True` (ON by default).** A batter facing a `SHARP` starter is
  dropped from `picks` regardless of edge type — including BvP, which
  previously ignored the pitcher entirely. He stays on the board tagged
  `SHARP-SP`, and the UI shows the dropped set under "Held Back — Sharp
  Starter", so the veto is visible rather than silent.
- **`hand_slump_edge` widened, then narrowed.** "Slumping" now means giving up
  runs (ERA ≥ 5.00) *or* giving up hits (`HITTABLE`) — but never while
  suppressing hits. The ERA path survives; a hit-suppressing pitcher no longer
  qualifies through it. That is the Cease case, precisely.
- **`hittable_sp_edge` (ON by default since run 1, `include_hittable_edge=True`).**
  Hot bat vs. a `HITTABLE` starter, no BvP or career split required. This is the
  "someone batting .300 against a pitcher who's given up 24 hits in his last 3
  starts" idea. It shipped off, because it reaches far more of the board than
  the other two edges and hadn't earned that volume yet; run 1 says it has, at
  the retuned 11.00/.310 bars. It is now the screen's primary source of picks —
  1,200 of 1,493 over the backtest window — and renders under "Hot Bats vs
  Hittable Starters". Keeps its own `starts >= 3` floor, stricter than the
  band's.

### 4. Diagnostics

`GET /batters/pitcher-form?name=Dylan+Cease&starts=3` returns the last-N line,
the season line, the band, and the raw game log — the same numbers the screen
used, so a surprising pick (or a surprising absence) can be checked directly.

---

## Hypotheses

| # | Hypothesis | Test | Ships if |
|---|---|---|---|
| H1 | A starter's recent hit suppression predicts hit-prop outcomes at all | `eval` → "hit rate by SP form band" over the whole board | SHARP band's whole-board hit rate is clearly below HITTABLE's, CIs not overlapping |
| H2 | Vetoing SHARP starters raises the hit rate | `baseline` vs `baseline+veto` | veto is up on baseline, and the vetoed subset's own hit rate is materially below baseline |
| H3 | The BvP edge carries the current 65% | `bvp_only` vs `hand_slump_only` | diagnostic only — tells us where to spend effort |
| H4 | The 5-PA BvP floor is mostly noise | `bvp_pa8+veto`, `bvp_pa12+veto`, `sweep --param bvp_pa` | a higher floor beats `bvp_only+veto` without cutting volume below ~3 picks/day |
| H5 | Hot bat vs. a hittable starter is a real edge | `hot_vs_hittable+veto` vs `baseline` | hit rate at or above baseline **at much higher volume**, or clearly above at similar volume |
| H6 | Raw hits allowed is as good as a rate | `hot_vs_24hits_l3` / `hot_vs_30hits_l3` vs `hot_vs_hittable` | if the raw count wins, prefer it — it's easier to explain |
| H7 | The band thresholds are near-optimal | `sweep --param sharp_h9 / sharp_baa / hittable_h9 / hittable_baa` | pick the knee of the volume/hit-rate curve, not the peak |

### Reading the results

- **Hit rate alone is not the goal.** Any filter raises hit rate if it's allowed
  to cut volume to nothing. Judge on the volume/hit-rate curve, and on the
  Wilson CI the report prints — a 70% over 40 picks and a 70% over 400 picks
  are not the same claim.
- **The baseline in the report is the rules as they were**, recomputed from raw
  columns, so it's an apples-to-apples comparison on identical boards.
- **Don't tune to the third decimal.** A season is ~120 slates; the whole board
  is large but any single variant's decided-pick count is what sets the error
  bar.

---

## How to run it

The MLB endpoints (`statsapi.mlb.com`, `baseballsavant.mlb.com`,
`baseball-reference.com`) are blocked from the Claude Code sandbox by egress
policy, so this has to run somewhere with open network — a GitHub Actions
runner, a terminal, or the k8s pod.

Run 1 took ~25 min end to end on a laptop: ~2.5 min for the 2026 Statcast
scrape (2024/2025 came from the parquet cache) and ~10 s per slate after that.
Two dates are skipped with "no games" — the All-Star break, 2026-07-13 and
2026-07-15.

### GitHub Actions (easiest)

Actions → **Batter screen backtest** → Run workflow.

- First run: `mode=build+eval`, `start=2026-04-01`. Slow — it scrapes three
  Statcast seasons plus a slate a day — but the boards are cached, so
  every later run is fast.
- After that: `mode=eval` (seconds) or `mode=sweep` to scan a threshold.
- Results land in the run's job summary; boards and `report.txt` are uploaded
  as an artifact.

### Local terminal

```bash
cd backend
pip install -r requirements.txt && pip install --no-deps -e .

python scripts/backtest_batters.py build --start 2026-04-01
python scripts/backtest_batters.py eval
python scripts/backtest_batters.py sweep --param sharp_h9 --lo 5.0 --hi 9.0 --step 0.25
python scripts/backtest_batters.py sweep --param hits_l3 --lo 14 --hi 34 --step 2
```

Boards default to `~/.sharp-edge/backtest/batters` (`--dir` to move them).
`build` is resumable — a date already written is skipped unless `--force`.

### How the harness works

`build` replays each date through `screen_for_date` with **every gate off**, so
it captures the whole board rather than just the picks the current rules would
have made, then joins each row to the box-score outcome (started / PA / hits)
under the exact settlement rules `tracking.resolve_pending` uses — non-starters
and zero-PA batters are VOID and leave the denominator.

`eval` recomputes every variant from **raw metric columns**, not from the stored
boolean edge flags. That's what makes thresholds free to vary at eval time
without rebuilding boards, and what keeps the `baseline` variant honest after
the production rules changed underneath it.

---

## Results

Fill in from the workflow's job summary. One row per run; keep the old rows.

### Run 1 — 2026-08-06, boards 2026-04-01 → 2026-08-05, sha `2dc3569`

125 days, 42,569 batter-games. Slate-wide hit rate (every batter on every
board, no filter at all): **60.6%**. Run locally, not in CI.

| variant | picks | decided | hit% | 95% CI | /day | Δ vs baseline |
|---|---|---|---|---|---|---|
| baseline | 398 | 376 | 64.9 | 59.9–69.5 | 3.5 | — |
| baseline+veto | 293 | 278 | 66.2 | 60.4–71.5 | 2.7 | +1.3 |
| bvp_only | 398 | 376 | 64.9 | 59.9–69.5 | 3.5 | +0.0 |
| bvp_only+veto | 293 | 278 | 66.2 | 60.4–71.5 | 2.7 | +1.3 |
| hand_slump_only | **0** | 0 | — | — | 0.0 | — |
| bvp_pa8+veto | 96 | 93 | 65.6 | 55.5–74.5 | 1.5 | +0.7 |
| bvp_pa12+veto | 20 | 20 | 70.0 | 48.1–85.5 | 1.1 | +5.1 |
| hot_vs_hittable | 2534 | 2105 | 64.7 | 62.6–66.7 | 22.4 | −0.2 |
| hot_vs_hittable+veto | 2534 | 2105 | 64.7 | 62.6–66.7 | 22.4 | −0.2 |
| hot_vs_24hits_l3 | 168 | 139 | 68.3 | 60.2–75.5 | 3.4 | +3.4 |
| hot_vs_30hits_l3 | 1 | 1 | 0.0 | 0.0–79.3 | 1.0 | −64.9 |
| hot330_vs_hittable | 1821 | 1516 | 63.9 | 61.5–66.3 | 16.1 | −1.0 |
| all_edges+veto | 2674 | 2233 | 64.8 | 62.8–66.7 | 22.3 | −0.1 |

Whole-board hit rate by SP form band:

| band | decided | hit% | 95% CI |
|---|---|---|---|
| SHARP | 8006 | 58.8 | 57.7–59.9 |
| NEUTRAL | 10082 | 61.3 | 60.3–62.2 |
| HITTABLE | 10033 | 61.3 | 60.3–62.2 |
| UNKNOWN | 1656 | 60.5 | 58.1–62.8 |

Vetoed subset (baseline picks facing a SHARP starter): **105 picks, 98 decided,
61.2%** (CI 51.3–70.3).

#### The control the variant list was missing

Every variant above compares against `baseline`, but `baseline` is itself a
filter. The number that says whether *any* of this is an edge is **the hot bat
with no pitcher filter at all**:

| variant | picks | decided | hit% | 95% CI | /day | Δ vs hot_only |
|---|---|---|---|---|---|---|
| everyone (whole board) | 42569 | 29777 | 60.6 | 60.0–61.1 | 340.6 | −2.1 |
| **hot_only** | 8728 | 7262 | **62.7** | 61.5–63.8 | 70.4 | — |
| hot_only+veto | 6339 | 5288 | 63.5 | 62.2–64.8 | 51.5 | +0.8 |
| baseline (= BvP) | 398 | 376 | 64.9 | 59.9–69.5 | 3.5 | +2.2 |
| hot+hittable h9≥9.5 (shipped) | 2534 | 2105 | 64.7 | 62.6–66.7 | 22.4 | +2.0 |
| hot+hittable h9≥11 | 1269 | 1038 | 67.8 | 64.9–70.6 | 11.8 | +5.1 |
| hot+hittable h9≥12 | 849 | 691 | 69.0 | 65.5–72.4 | 7.9 | +6.3 |
| hot+hits_l3≥20 | 965 | 794 | 68.4 | 65.1–71.5 | 9.3 | +5.7 |

Being a hot bat is worth +2.1 on its own. **The entire BvP edge is worth another
+2.2, with overlapping CIs, on 376 decided picks.**

#### Dose-response: hot bats only, by the starter's last-3 H/9

| H/9 bin | decided | hit% | 95% CI |
|---|---|---|---|
| ≤ 6.5 | 1455 | 61.4 | 58.8–63.8 |
| 6.5–8.0 | 1218 | 59.8 | 57.0–62.5 |
| 8.0–9.5 | 1205 | 63.7 | 61.0–66.4 |
| 9.5–11.0 | 961 | 61.7 | 58.6–64.7 |
| **11.0–12.0** | 378 | **66.4** | 61.5–71.0 |
| **> 12.0** | 649 | **68.7** | 65.1–72.2 |

This is a **tail effect, not a gradient**. The middle four bins are flat and
non-monotone; only genuinely battered starters separate. That is why the shipped
9.5 bar buys almost nothing — it spends most of its volume on the flat region.

#### Split-half, to check for post-hoc threshold fitting

`h9≥11` and `h9≥12` were picked off a sweep, so they need an out-of-sample
check. Split at 2026-06-02 (62d / 63d):

| variant | 1st-half dec | 1st half | 2nd-half dec | 2nd half | drift |
|---|---|---|---|---|---|
| hot_only (control) | 3430 | 62.2 | 3832 | 63.1 | +0.9 |
| baseline (BvP) | 182 | 60.4 | 194 | 69.1 | **+8.7** |
| baseline+veto | 138 | 60.1 | 140 | 72.1 | **+12.0** |
| hot+hittable h9≥9.5 | 810 | 64.3 | 1295 | 64.9 | +0.6 |
| hot+hittable h9≥11 | 439 | 67.4 | 599 | 68.1 | +0.7 |
| hot+hittable h9≥12 | 296 | 68.6 | 395 | 69.4 | +0.8 |
| hot+hits_l3≥20 | 335 | 71.0 | 459 | 66.4 | −4.6 |

The h9 rules reproduce almost exactly across halves. **The BvP edge does not** —
its 64.9% season number is the average of a 60.4% half and a 69.1% half.

#### Is the h9≥11 effect just extra plate appearances?

A bad starter means a longer inning, and a "records a hit" prop is mechanically
easier with a 5th plate appearance. If that were the whole story the rule would
be a game-length bet wearing a matchup costume. It isn't:

| H/9 bin | decided | hit% | mean PA |
|---|---|---|---|
| ≤ 6.5 | 1455 | 61.4 | 4.10 |
| 6.5–8.0 | 1218 | 59.8 | 4.09 |
| 8.0–9.5 | 1205 | 63.7 | 4.17 |
| 9.5–11.0 | 961 | 61.7 | 4.11 |
| 11.0–12.0 | 378 | 66.4 | 4.22 |
| > 12.0 | 649 | 68.7 | 4.22 |

Mean PA moves 4.10 → 4.22 across the whole range — nowhere near enough to carry
6 points of hit rate. Holding PA fixed, the gap survives:

| PA | h9<11 decided | hit% | h9≥11 decided | hit% | diff |
|---|---|---|---|---|---|
| 3 | 608 | 42.3 | 95 | 43.2 | +0.9 |
| 4 | 2718 | 60.2 | 559 | 65.1 | **+4.9** |
| 5 | 1275 | 76.4 | 320 | 80.3 | **+3.9** |

So roughly +4 points of the ~+5 to +6 is a genuine per-plate-appearance quality
effect, not a game-length artifact. (The 3-PA row is flat, but n=95.)

**Conclusions:**

- **H1 — partially supported.** Restricted to `starts ≥ 3`, the band means are
  SHARP 58.9% (57.7–60.0) / NEUTRAL 61.1% (60.0–62.2) / HITTABLE 61.6%
  (60.6–62.7). SHARP separates from both with disjoint CIs, so recent hit
  suppression genuinely predicts hit props. **HITTABLE does not separate from
  NEUTRAL at all** at the shipped 9.5 bar. The premise holds at the sharp end
  and fails at the hittable end.
- **H2 — not supported as specified.** The bar was "veto up on baseline *and*
  the vetoed subset materially below baseline." It's up (+1.3), but the vetoed
  picks hit 61.2% with a CI of 51.3–70.3 that covers the 64.9% baseline. On the
  whole board the veto is worth +0.8 (62.7 → 63.5). Real, directionally right,
  much smaller than the branch assumed.
- **H3 — confirmed, decisively.** `hand_slump_edge` fired **0 times in 125
  days**. `baseline` and `bvp_only` are identical row for row (398 picks, 244W /
  132L). The screen is 100% BvP and the hand+slump half is decoration, exactly
  as suspected. The `.400 vs hand over 50 PA` bar is unreachable in practice.
- **H4 — the floor isn't the problem; the edge is.** The `bvp_pa` sweep has no
  trend (63.8 at 4, 66.2 at 5, 66.7 at 6, 63.5 at 9, 72.3 at 10, 55.6 at 14) —
  noise once n collapses past ~7 PA. The ship criterion (beat `bvp_only+veto`
  without falling under ~3 picks/day) is met by nothing: every floor above 5
  drops below 2.1/day. The real finding is upstream — BvP is +2.2 over "any hot
  bat" and swings 8.7 points between halves.
- **H5 — supported, but only far above the shipped threshold.** At h9≥9.5 the
  edge is +2.0 over the hot-only control at 22.4/day, which is volume at the
  control's own hit rate — not an edge. At h9≥11 it's 67.8% at 11.8/day (+5.1,
  CIs disjoint from control) and split-half stable. **This is the most robust
  result in the experiment.**
- **H6 — raw hits works, but doesn't win.** `hits_l3≥20` is 68.4% at 9.3/day,
  comparable to h9≥11 and overlapping it heavily (587 of 849/965 rows shared).
  But it drifts −4.6 across halves where the rate drifts +0.8. H6 said prefer
  the raw count *if it wins*; it doesn't, so keep the rate.
- **H7 — rejected for SHARP, and the HITTABLE bar is badly misplaced.** The
  `sharp_h9` sweep is flat from 5.0 to 9.0 (64.3–66.4, no knee) and `sharp_baa`
  likewise (64.0–66.4) — those thresholds barely matter because they only gate
  398 baseline picks. The `hittable_h9` sweep *does* have a knee, at roughly
  10.75–11.0, and the shipped 9.5 sits well below it.

**Bug found while reading the boards:** `_sp_band` gates SHARP on
`min_sharp_starts` but leaves HITTABLE ungated, so a pitcher bands HITTABLE off
a single bad start — 2,147 of 14,359 HITTABLE rows (15%) have fewer than 3
starts. It doesn't change the conclusion (restricting to `starts ≥ 3` moves
HITTABLE from 61.3% to 61.6%) because `hittable_sp_edge` applies its own
`starts >= 3` check, but the band label is wrong on the board and in
`GET /batters/pitcher-form`.

**Caveat that outranks all of the above:** these are hit rates, not ROI. At −160
break-even is 61.5%, which the 60.6% slate-wide rate already sits below and the
64.9% baseline barely clears. The h9≥11 rule's 67.8% is the first number here
with real margin in it — but none of this is ranked until prices are attached.

---

## Defaults after run 1 — APPLIED 2026-08-07

| # | Change | Status | Evidence |
|---|---|---|---|
| 1 | `MIN_HITTABLE_H9` **9.50 → 11.00** | **applied** | Knee of the `hittable_h9` sweep; +5.1 over the hot-only control with disjoint CIs; split-half drift +0.7 |
| 1b | `MIN_HITTABLE_BAA` **.270 → .310** | **applied** | Forced by #1 — see below. Knee of the `hittable_baa` sweep (68.2% at 9.3/day standalone) |
| 2 | `include_hittable_edge` **False → True** | **applied** | 67.4% at 12.5/day vs 66.2% at 2.7 for BvP alone |
| 3 | Gate HITTABLE on `min_sharp_starts` in `_sp_band` | **applied** | 15% of HITTABLE rows came off 1–2 starts; the label was wrong on the board and in the API |
| 4 | `veto_sharp_sp` — keep `True` | unchanged | SHARP separates on the whole board (58.9% vs 61.1/61.6, disjoint CIs). Small (+0.8) but free |
| 5 | `MAX_SHARP_H9` / `MAX_SHARP_BAA` — keep 6.50 / .210 | unchanged | Both sweeps flat, no knee. No evidence to move them either way |
| 6 | `hand_slump_edge` — remove, or drop `min_hand_avg` to ~.290 | **not done** | Fired 0 times in 125 days. Dead as configured, but the replacement bar is untested — left for a run 2 |
| 7 | BvP thresholds | **not done** | +2.2 over "any hot bat" with overlapping CIs and 8.7 points of split-half drift. Needs more seasons, not a new threshold |

### Why both HITTABLE bars had to move

The recommendation as first written moved only `MIN_HITTABLE_H9`, and that
would have been close to a no-op. The band is an **OR**, and over these boards
`h9 ≥ 11.0` is a strict *subset* of `baa ≥ .270` — every starter clearing the
H/9 bar also clears the BAA bar, so the BAA arm stays binding and keeps
selecting the population the H/9 change was meant to exclude:

| rule | picks | /day | hit% | 95% CI |
|---|---|---|---|---|
| h9 ≥ 11 only (what was measured) | 1269 | 11.8 | 67.8 | 64.9–70.6 |
| **h9 ≥ 11 OR baa ≥ .270 (naive apply)** | 2421 | 21.6 | **65.0** | 62.9–67.0 |
| h9 ≥ 11 OR baa ≥ .300 | 1390 | 12.8 | 66.8 | 64.0–69.5 |
| **h9 ≥ 11 OR baa ≥ .310 (shipped)** | 1288 | 11.8 | **67.7** | 64.9–70.5 |

Applying #1 alone would have landed on the second row — 65.0% at 21.6/day,
statistically indistinguishable from the 9.5 bar it replaced. .310 is the knee
of the `hittable_baa` sweep and the value at which the two arms select the same
population again.

Why 11.0 and not 12.0: 12.0 scores marginally better (69.0% vs 67.8%) at 7.9
picks/day against 11.8. The rates sit inside each other's CIs while the volume
differs by 50%, and 11.0 is at the sweep's knee rather than out on the thin end
where a post-hoc pick is most likely to be fitting noise.

---

## Run 2 — the shipped configuration against the history

Same 125 days of boards, no rebuild. Every variant is recomputed from raw
columns, so this is an apples-to-apples comparison on identical slates.

| configuration | picks | decided | /day | hit% | 95% CI |
|---|---|---|---|---|---|
| slate-wide (no filter at all) | 42569 | 29777 | 340.6 | 60.6 | 60.0–61.1 |
| hot bat only (no pitcher filter) | 8728 | 7262 | 70.4 | 62.7 | 61.5–63.8 |
| **pre-branch** — hot + BvP | 398 | 376 | 3.5 | 64.9 | 59.9–69.5 |
| **branch as shipped** — + SHARP veto | 293 | 278 | 2.7 | 66.2 | 60.4–71.5 |
| branch with hittable ON at 9.5/.270 | 2674 | 2233 | 22.3 | 64.8 | 62.8–66.7 |
| **applied config** — 11.0/.310, edge ON, veto ON | 1493 | 1241 | 12.5 | **67.4** | 64.8–70.0 |

Split-half (cut at 2026-06-02), to show the new config isn't a fitted artifact:

| configuration | 1st-half dec | 1st half | 2nd-half dec | 2nd half | drift |
|---|---|---|---|---|---|
| pre-branch (hot + BvP) | 182 | 60.4 | 194 | 69.1 | **+8.7** |
| branch as shipped (+veto) | 138 | 60.1 | 140 | 72.1 | **+12.0** |
| hittable ON at 9.5/.270 | 881 | 64.4 | 1352 | 65.0 | +0.6 |
| **applied config** | 540 | 66.7 | 701 | 68.0 | **+1.3** |

The applied config is 2.5 points above the pre-branch screen and 1.2 above the
branch as it shipped, at **4.6× the pick volume** of the latter — and it is far
more stable across halves than either, because the volume is no longer coming
from a 20-pick BvP sample.

### The change is purely additive

It drops nothing the current rules pick:

| | picks | decided | hit% | 95% CI |
|---|---|---|---|---|
| kept (picked by both) | 293 | 278 | 66.2 | 60.4–71.5 |
| dropped by the new rules | **0** | 0 | — | — |
| added by the new rules | 1200 | 963 | **67.8** | 64.8–70.7 |

The added picks hit *better* than the retained ones, which is the cleanest
statement of the run-1 result: the hittable-starter edge is a better rule than
BvP, and BvP was only ever contributing ~3 picks a day.

### What the change costs: correlated picks

The old screen picked individual batters on their personal BvP history. The new
one picks *whole lineups against one bad starter* — the first live slate run
under it returned 8 Red Sox against Luis Castillo out of 17 picks. That's a
portfolio property worth stating explicitly:

| config | picks | /day | distinct date×SP groups | max on one SP | mean | % of picks in a group of 5+ |
|---|---|---|---|---|---|---|
| old (BvP + veto) | 293 | 2.7 | 258 | 2 | 1.14 | 0% |
| **applied config** | 1493 | 12.5 | 631 | **8** | 2.37 | **19%** |

Those stacks resolve *better* than the config average, not worse — 23 groups of
5 or more, mean within-group hit rate 70.8%, four that went a perfect 5-for-5,
and **none that lost every pick**. So this isn't a hidden loss, but it is a real
change in variance: within-group hit rates have a 20.9-point standard deviation,
and one starter having a good night now moves the whole day. Expect the daily
track record to swing much harder than the old 2.7-picks-a-day screen did, at
the same or better long-run rate.

> **Caveat on the band table in `eval`.** `p_form` is a *stored* column, written
> at build time, so the "hit rate by SP form band" breakdown still reflects the
> 9.5/.270 bands and the ungated HITTABLE. Every other number recomputes from
> raw columns and does pick up the new thresholds. Rebuild with `--force` before
> reading that table again.

---

## After a rule change ships

Persisted picks carry the rules that were live when they were written, so the
track record mixes generations after any change. To regenerate history under
the current rules:

```bash
curl -X POST https://sharp-edge.nickknows.net/api/picks/backfill \
  -H 'content-type: application/json' \
  -d '{"start":"2026-04-01","screens":["batter"]}'

curl https://sharp-edge.nickknows.net/api/picks/backfill/status
```

`start_backfill` regenerates the range unconditionally (unlike the startup
catch-up, which only fills gaps). Note it re-screens with whatever defaults are
compiled in, so it reflects the shipped configuration, not a variant.

---

## Open questions / not yet tried

- **Bullpen quality.** The starter goes 5–6 innings; two of a batter's four
  plate appearances are often against relievers. A team-level bullpen BAA over
  the last 7 days is the obvious next feature, and nothing in the screen looks
  at it.
- **Lineup slot.** Leadoff gets ~4.6 PA, ninth gets ~3.7. That is a large
  difference in the probability of *any* hit, and the screen ignores it
  entirely. Probable lineups aren't posted at warm-up time for every game, but
  a batter's recent average slot is a decent stand-in.
- **Park and weather.** Coors vs. a cold night in Cleveland is not the same bet.
- ~~**The `.400 vs hand over 50 PA` bar looks near-impossible.**~~ **Answered by
  run 1: it fired 0 times in 125 days.** The screen is BvP-only in practice.
  Either drop `hand_slump_edge` or drop the bar to ~`.290` and re-test — but
  note that the "slump" half of it is now also suspect, since the H/9 dose-
  response says there's no signal until a starter is genuinely battered.
- **BvP sample size generally.** ~~5 PA is 2-for-5.~~ **Answered, and it's worse
  than "the floor is too low":** the whole BvP edge is +2.2 over taking any hot
  bat (CIs overlapping, 376 decided) and swings 60.4% → 69.1% across the two
  halves of the season. Raising the floor doesn't rescue it — no floor beats
  `bvp_only+veto` while staying above ~2 picks/day. The open question is now
  whether BvP should survive at all, which wants a third season of boards
  rather than a threshold tweak.
- **Why is the middle of the H/9 range flat?** Hot bats face starters at
  6.5–11.0 H/9 with essentially no variation in outcome (59.8–63.7 across three
  bins, non-monotone), then jump at 11+. Still unexplained. The obvious
  confound — blowouts handing the lineup extra plate appearances — has been
  checked and **ruled out** (see below), so something else is going on. Worth
  knowing before leaning harder on the rule.
- **No odds attached.** Hit rate is not ROI. A 65% hit rate at -160 is roughly
  break-even. Attaching prices to picks is already on the roadmap in IDEA.md
  and would change which of these variants actually wins. Run 1 sharpens this:
  the baseline's 64.9% is within noise of break-even, so the screen may not
  currently be profitable at all.
