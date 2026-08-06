# Claude prompt — batter screen backtest (2026-08-06)

Context: the batter screen changes live on branch
`claude/batters-endpoint-improvements-7edeql`. They were written in a sandbox
where the MLB endpoints (`statsapi.mlb.com`, `baseballsavant.mlb.com`,
`baseball-reference.com`) are blocked by egress policy, so **the backtest was
never run and every threshold in the code is an untested starting point**.
This file is the handoff for running it somewhere with real network access.

---

## Run this first, before opening Claude

Five-second check that gates everything else. The screen reads the starter's
recent hit suppression out of the StatsAPI pitching game log; if those field
names are wrong, the code degrades to the `UNKNOWN` band, the veto silently
never fires, and every backtest number will look like "no change."

```bash
curl -s 'https://statsapi.mlb.com/api/v1/people?personIds=656302&hydrate=stats(group=[pitching],type=[gameLog],season=2026,sportId=1)' \
  | python -m json.tool | grep -E '"(hits|atBats|battersFaced|inningsPitched)"' | head
```

656302 is Dylan Cease. Empty output means the parsing needs fixing before any
of the rest is worth doing.

---

## The prompt

```
I'm on my home machine now — full network access (MLB endpoints reachable) and
kubectl access to the sharp-edge cluster.

Pick up the batter screen work on branch claude/batters-endpoint-improvements-7edeql.
Read EXPERIMENTS.md first: it has the hypotheses (H1-H7), the variant list, and
what result would make each change ship.

In order:

1. Verify the StatsAPI pitching game log actually returns hits / atBats /
   battersFaced per start. Everything downstream degrades silently to the
   UNKNOWN band if those key names are wrong, which would mean the veto never
   fires and nothing improved. Confirm Dylan Cease comes back as SHARP.
2. Build the season's backtest boards:
   python backend/scripts/backtest_batters.py build --start 2026-04-01
3. Run eval, then sweep sharp_h9, sharp_baa, hittable_h9, hits_l3, and bvp_pa.
4. Fill in the Results section of EXPERIMENTS.md with the real numbers and
   write the conclusions.
5. Recommend final threshold defaults and whether include_hittable_edge should
   default on. Tell me what the evidence supports before you change any default.

Judge variants on the volume/hit-rate curve and the Wilson CIs, not raw hit
rate — I care about picks per day too. If H3 shows hand_slump_edge basically
never fires, say so and propose a lower vs-hand bar to re-test.
```

### Variants on that prompt

- **Just run it, don't discuss:** replace steps 4–5 with
  `Fill in EXPERIMENTS.md, commit, and push. Don't change any defaults yet.`
- **Prefer CI over the laptop:** `Run this through the Batter screen backtest
  workflow in Actions instead of locally, then pull the results from the job
  summary.` First run there is slow but cached, and it keeps the three-season
  Statcast scrape off the local machine.

---

## Background — why this work exists

2026-08-06. The screen picked two Cubs, Nico Hoerner and Pete Crow-Armstrong,
against Toronto and Dylan Cease. PCA got a hit in the 9th, Hoerner didn't;
Cease gave up 2 hits. He'd recently taken a perfect game deep into a start
against a Red Sox team that had lost about 5 of its last 40.

The screen had no way to know any of that. It judged the opposing starter on
one number — ERA over his last 3 starts — and only for `hand_slump_edge`. The
`bvp_edge`, which is what most picks actually fire on, looked at the pitcher
**not at all**. Cease could have been throwing a no-hitter every start and a
.400 BvP line over 5 career PA would still have produced the pick.

ERA is also the wrong stat even where it was used: three solo homers push a
start past 5.00 while the lineup manages four hits. That pitcher reads as a
slumping target and is in fact the worst matchup on the board.

### What shipped on the branch

Each starter now carries last-3 **H/9, BAA, WHIP, K/9, raw hits** (plus a
season line for context) and is banded `SHARP` / `HITTABLE` / `NEUTRAL` /
`UNKNOWN`.

- **On by default:** picks against a SHARP starter (≤6.5 H/9 or ≤.210 BAA, with
  ≥2 starts of evidence) are held back — all edge types, BvP included. They
  stay on the board tagged `SHARP-SP` in a "Held Back" section, so the veto is
  visible rather than silent.
- **Off by default:** the hot-bat-vs-shelled-pitcher edge (`hittable_sp_edge`).
  Implemented and rendered as an experimental section, but it reaches far more
  of the board than the existing edges, so it waits on evidence rather than
  quietly tripling pick volume.
- `GET /batters/pitcher-form?name=...` exposes the same numbers the screen
  used, for checking a surprising pick or a surprising absence.

### Two things the backtest will probably show

Neither is what prompted the work:

1. **`hand_slump_edge` may be near-unreachable.** It needs a `.400` career
   average vs. a handedness over 50+ PA. Almost nobody clears that. If so, the
   current ~65% is essentially a pure BvP number and the "hand+slump" half of
   the screen is decoration. (H3)
2. **The 5-PA BvP floor is 2-for-5.** That's noise, and it's currently driving
   the picks. The `bvp_pa8` / `bvp_pa12` variants and `sweep --param bvp_pa`
   test it. (H4)

### Afterward

If the veto turns out to help, re-run `/picks/backfill` for the batter screen
so the track record isn't a mix of old-rule and new-rule picks. Command is at
the bottom of EXPERIMENTS.md.

Also worth keeping in view: hit rate is not ROI. 65% at -160 is roughly
break-even, so none of these variants is truly ranked until odds are attached
to picks.
