---
status: active
progress: 65
---

# Sharp Edge

<!--
IdeaBRD parses this file. It is the source of truth for this idea's tile:
the app re-reads it on every open and commits its own edits back here, so
the shape below matters more than it looks. Anything the parser
(backend/app/ideafile.py) can't read is dropped silently.

  frontmatter  status: one of idea, active, paused, done. progress: 0-100.
               Any other key is ignored.
  # heading    The idea title (first H1).
  prose        Everything outside the Todos section becomes the tile's
               notes, shown on the board — so keep it short. Documentation
               written here is published, not filed away.
  ## Todos     That heading exactly (or "## To-Dos"); "## ToDo", "## TODO"
               and "## Tasks" do not match and the whole list is lost.
               Inside it, only "- [ ] open" / "- [x] done" lines survive:
               sub-headings and blank-line grouping are discarded, and a
               wrapped item is cut at the line break, so keep each to-do on
               one line. The next "## " heading ends the list.

To-dos are matched to the board by exact text, so rewording one replaces it
rather than editing it in place — expect a checked item to come back
unchecked if you reword it.

HTML comments are stripped on read, so this block never reaches the board.
-->

Sports betting analytics platform: syncs bet history from the books, runs daily
MLB screens (batter hits, home runs) and a weekly NFL prop board, tracks every
pick to a settled outcome, and exposes the whole thing to Claude via chat and
MCP. Svelte + FastAPI, SQLite for dev and CloudNativePG in prod, deployed to
K3s at sharp-edge.nickknows.net.

The NFL side reuses the nfl_projections model already running in NFL-API and
prices it against FanDuel's board; see EXPERIMENTS_NFL.md. Next big push is
breaking the FanDuel-only assumption: a sportsbook provider interface with
DraftKings first, then line shopping across books.

## Todos

- [x] FastAPI backend with SQLite (dev) and PostgreSQL/CNPG (prod) behind one BetDatabase interface
- [x] Per-visitor data isolation via signed session cookie (uid on every user-owned query)
- [x] Pikkit CSV import for historical bet backfill
- [x] FanDuel direct login: /sessions auth, new-device MFA, refresh-token renewal, DB-persisted session
- [x] FanDuel resilience: real-vs-fallback token expiry, unparseable device token no longer strands MFA
- [x] Bet history sync + stats, breakdown by league/book/type, daily calendar P/L, bet scoring, insights
- [x] Svelte frontend: dashboard, calendar heatmap, ROI trend chart, win-rate donut, mobile-responsive layout
- [x] Client-side caching so tab switches stop refetching screens and track record
- [x] MLB batter screen: recent form, BvP, handedness splits, opposing SP last-3 ERA, edge flags
- [x] Batter screen reads the starter's last-3 hit suppression (H/9, BAA), holds back picks against sharp starters, and picks hot bats against battered ones
- [x] Backtest harness replays past slates through the screen and scores rule variants against settled outcomes
- [x] MLB home run screen: ISO/barrel power metrics, park factors, pitcher HR/9 and barrel% allowed, BvP
- [x] Statcast pipeline: chunked scrape, per-year parquet persistence, parquet fallback, self-healing stale re-warm
- [x] Pick persistence + outcome tracking (WIN/LOSS/VOID) with retroactive backfill and startup catch-up
- [x] Settle picks from the box score instead of lagging Statcast, and record screened days so catch-up converges
- [x] Intra-day re-screen replaces today's pending picks when probable pitchers change
- [x] Track record UI with per-edge and per-day hit rates
- [x] Chat panel on each visitor's own Anthropic key, defaulting to Sonnet 5
- [x] MCP server (stdio) exposing stats, breakdown, score_bet, history, insights, sync, import to Claude Desktop/Code
- [x] Helm chart on K3s: CNPG, ExternalSecrets via OpenBao, Cilium ingress, cert-manager TLS, Recreate strategy
- [x] GitHub Actions: pytest, image build, automatic image bump, smoke probe against the origin
- [x] Batter backtest run 1 (125 days): hot-bat-vs-hittable-starter edge on by default, HITTABLE bands retuned to 11.00 H/9 / .310 BAA (EXPERIMENTS.md)
- [x] Attach odds to picks — FanDuel price, model probability, EV and edge on every row, archived daily
- [x] Retire `hand_slump_edge` — .400 vs hand over 50 PA fired 0 times in 30,783 rows; removed entirely
- [x] Decide whether the BvP edge survives — it doesn't: +2.4 on n=391 (CI ±4.8), non-monotone in sample size, now a label not a filter
- [x] Rank the whole board on model probability instead of screening — +8.5pts of two-leg sweep and a better price (EXPERIMENTS.md run 3)
- [x] Size the card on price: two legs minimum, no cap, every leg that pays for its own risk
- [x] Freeze the day's card and settle it as one bet — sweep rate and flat-stake ROI, not leg hit rate
- [x] Bet-slip link opens the FanDuel app directly (account host, no state subdomain)
- [ ] Bullpen quality and lineup slot as batter-screen features — lineup slot is now the largest thing the model can't see
- [ ] Extract a Sportsbook provider interface from the FanDuel client (auth, history sync, odds, balance)
- [ ] DraftKings integration: login/session handling, bet history sync, canonical schema mapping
- [ ] Normalize book-specific market and bet-type codes into the shared MARKET_TYPE_MAP
- [ ] Multi-book account management in Settings (connect, refresh, disconnect per book)
- [ ] BetMGM integration
- [ ] Caesars and ESPN Bet integrations
- [ ] Line shopping: pull the same market across connected books and surface the best price
- [ ] Attach live odds to each screen pick so the track record reports ROI, not just hit rate
- [ ] Arbitrage and middling detection across books
- [ ] Closing line value tracking
- [x] Kelly sizing on the card — quarter-Kelly stake shown with the parlay price
- [ ] Scheduled sync as a K8s CronJob instead of startup-only warm-up
- [ ] NBA model: nba_api PRA projections
- [x] Group the MLB screens behind one MLB tab with a screen dropdown, NFL alongside it
- [x] NFL model integration reusing the existing nfl_data_py projections
- [x] NFL prop board: yardage, receptions, anytime TD priced off NFL-API projections vs FanDuel lines
- [x] Rescale projections onto the market's scale — raw gaps read model shrinkage as signal and fade every star
- [x] Moneyline measured and left alone: closing line is calibrated, every naive strategy loses the vig
- [x] Fix the projection name join — suffixed veterans (Cook III, Etienne Jr., Pitts Sr.) were typed rookies and projected off a prior
- [x] Record NFL picks weekly and settle them against nflverse actuals, with hit rate and ROI per market and side
- [x] NFL weekly card — two legs, one per game, frozen before kickoff
- [x] Refuse short lines on both sides — a short line prices a role the projection cannot see, and the edge there ranks backups
- [x] Defence-vs-position matchup on TE receiving props — measured over four week 1s; WR got worse and coverage scheme did nothing, both documented
- [x] Test formation/personnel-level defensive matchup — destroys the signal rather than refining it (~21 plays per cell); team success rate allowed is the stable trait (r 0.618) but moves projections ~1%
- [x] Blend an opportunity projection into TE receiving — the only market that beat the production model with a bootstrap interval clear of zero
- [ ] Close the weekly stale window: the upcoming week's projections are the August season board until that Wednesday's run overwrites them
- [ ] Feed coaching scheme and offseason role change into the projections — they read last season's usage, so a player inheriting a vacated role keeps his backup rate (half the week-1 board)
- [ ] Fix the tight-end projection bias (+22.5 yds in week 1) — the matchup factor multiplies an already-inflated number rather than correcting it
- [ ] Archive NFL closing lines the way data/odds/ does for MLB, so the shrink factors can be fit instead of guessed
- [ ] Refit the passing-yards model — it runs 4-5pts overconfident and is off the card until it doesn't
- [ ] Deduplicate bets across books so the same wager placed twice does not double-count P/L

## The batter model is four features, and the missing one is at-bats

`pricing._FEATURES` is `vs_hand_avg`, `recent_ab`, `p_l3_h9`, `p_l3_k9`. That is
the whole model. `recent_ab` is the batter's at-bats over the prior week and is
documented as a "does he play?" guard, not a projection of today. There is no
lineup slot, no walk expectation, and no bullpen — every `p_*` feature
describes the opposing **starter**.

It shows: over 243 graded picks `model_p` has an **AUC of 0.507** and
correlates **-0.026** with getting a hit. It cannot rank its own board. Picking
the top two by probability (65.0%) does not beat two drawn at random off the
same board (66.2%).

What the model is missing is the variable that dominates the outcome. Measured
over 1,674 starter-games across 12 slates (Sept 2026):

**Official at-bats, not plate appearances, is the thing.** A hit prop needs a
swing, and a walk burns a trip to the plate without giving one.

    AB        1      2      3      4      5+
    P(hit)  21.1%  36.1%  50.7%  68.9%  83.9%

**Lineup slot sets the plate appearances.**

    slot      1      2      3      4      5      6      7      8      9
    PA      4.49   4.37   4.27   4.22   4.06   3.87   3.64   3.52   3.42
    AB      4.07   3.77   3.77   3.75   3.66   3.44   3.25   3.16   3.06
    P(hit)  72.6%  64.5%  62.9%  66.1%  55.9%  57.0%  57.0%  55.4%  54.3%

Leadoff against ninth is +1.07 PA and **+18.3 points of hit probability**
(72.6% vs 54.3%); slots 1-2 against 8-9 is 68.5% vs 54.8%, Fisher p = 1.6e-04.
That single spread is larger than anything the current four features capture.

**Walks are the second half, and they are predictable.** At equal plate
appearances each walk costs about twelve points:

    PA=4   0 walks 65.9% (3.95 AB) | 1 walk 53.4% (2.98 AB) | 2 walks 36.6% (1.98 AB)
    PA=5   0 walks 82.6% (4.94 AB) | 1 walk 76.0% (3.95 AB) | 2 walks 54.1% (3.00 AB)

Good hitters get pitched around, so the batters the model likes most are the
ones most likely to lose at-bats to a walk — which is a mechanism for the
overconfident top end, where 0.775-0.800 predicts 81.1% and delivers 56.2%.

- [ ] **Project at-bats and feed it in.** `AB ≈ PA(lineup slot, team) − walks`,
      where walks come from the batter's own BB% against the starter's and the
      **bullpen's** walk rates. Starter BB/9 is already fetched alongside
      `p_l3_h9`; bullpen rates are not fetched at all. A starter goes five or
      six innings, so roughly a third of a batter's trips are against relievers
      the model has never looked at.
- [ ] **Get the lineup pre-game.** `_boxscore_summary` already parses
      `battingOrder` (slot `X00`) but only from finished games, for settlement.
      A `Scheduled` game's boxscore returns no batting order at all — checked
      on 2026-09-14, ten games, zero slots — so the live path needs whatever
      MLB posts a few hours out, with the prior-week modal slot as the fallback
      when it is not up yet. Without a live lineup this feature cannot ship, so
      this is the gating piece rather than the modelling.
- [ ] Re-check the top end after the above. If the overconfidence above 0.72 is
      the walk effect, projecting at-bats should flatten it — and that is the
      test, not in-sample fit.
