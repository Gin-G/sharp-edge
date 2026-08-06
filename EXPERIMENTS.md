# Experiments — batter screen

Running log of hypotheses about the MLB batter screen, how to test them, and
what the tests said. The screen sits around a 65% hit rate; everything here is
an attempt to move that without gutting volume.

**Nothing in this file is settled until the backtest column is filled in.**
The code changes shipped alongside it are deliberately split into *on by
default* (a subtractive rule that encodes an observed failure) and *off by
default* (a new, volume-expanding rule that has to earn its place).

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

| Band | Rule (last 3 starts) |
|---|---|
| `SHARP` | `h9 ≤ 6.50` or `baa ≤ .210`, with ≥2 starts of evidence |
| `HITTABLE` | `h9 ≥ 9.50` or `baa ≥ .270` |
| `NEUTRAL` | in between |
| `UNKNOWN` | no contact line in the game log — nothing is vetoed |

League-average starters sit near 8.5 H/9 / .250 BAA, so both bands are roughly
a run-and-a-half of hits per nine off the middle rather than splitting the
field in half. **These numbers are guesses until the sweep runs.**

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
- **`hittable_sp_edge` (OFF by default, `include_hittable_edge=False`).** Hot
  bat vs. a `HITTABLE` starter, no BvP or career split required. This is the
  "someone batting .300 against a pitcher who's given up 24 hits in his last 3
  starts" idea. It reaches far more of the board than the other two edges, so
  it stays off until the backtest says it's better than the alternative use of
  that volume. The candidates render under "Hot Bats vs Hittable Starters",
  marked experimental.

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

### Run 1 — _(date, board range, git sha)_

| variant | picks | decided | hit% | 95% CI | /day | Δ vs baseline |
|---|---|---|---|---|---|---|
| baseline | | | | | | — |
| baseline+veto | | | | | | |
| bvp_only | | | | | | |
| bvp_only+veto | | | | | | |
| hand_slump_only | | | | | | |
| bvp_pa8+veto | | | | | | |
| bvp_pa12+veto | | | | | | |
| hot_vs_hittable | | | | | | |
| hot_vs_hittable+veto | | | | | | |
| hot_vs_24hits_l3 | | | | | | |
| hot_vs_30hits_l3 | | | | | | |
| hot330_vs_hittable | | | | | | |
| all_edges+veto | | | | | | |

Whole-board hit rate by SP form band:

| band | decided | hit% | 95% CI |
|---|---|---|---|
| SHARP | | | |
| NEUTRAL | | | |
| HITTABLE | | | |
| UNKNOWN | | | |

Vetoed subset (baseline picks facing a SHARP starter): _n_ picks, _hit%_.

**Conclusions:**

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
- **The `.400 vs hand over 50 PA` bar looks near-impossible.** Career splits
  that high are rare, so `hand_slump_edge` may fire almost never and the screen
  may effectively be BvP-only. H3 will confirm; if so, that bar wants lowering
  to something like `.290` and the edge re-tested.
- **BvP sample size generally.** 5 PA is 2-for-5. It is very likely noise, and
  it is currently the main driver of picks. H4 tests raising it; if the answer
  is "the whole edge is noise", that's worth knowing before adding more on top.
- **No odds attached.** Hit rate is not ROI. A 65% hit rate at -160 is roughly
  break-even. Attaching prices to picks is already on the roadmap in IDEA.md
  and would change which of these variants actually wins.
