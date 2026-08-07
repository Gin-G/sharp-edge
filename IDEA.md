---
status: active
progress: 60
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
MLB screens (batter hits, home runs), tracks every pick to a settled outcome, and
exposes the whole thing to Claude via chat and MCP. Svelte + FastAPI, SQLite for
dev and CloudNativePG in prod, deployed to K3s at sharp-edge.nickknows.net.

Next big push is breaking the FanDuel-only assumption: a sportsbook provider
interface with DraftKings first, then line shopping across books.

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
- [ ] Attach odds to picks — hit rate isn't ROI, and the screen sits near break-even at -160
- [ ] Retire or re-bar `hand_slump_edge`: it fired 0 times in 125 days
- [ ] Decide whether the BvP edge survives — it's +2.2 over "any hot bat" with 8.7pts of split-half drift
- [ ] Bullpen quality and lineup slot as batter-screen features
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
- [ ] Bankroll management and Kelly criterion sizing
- [ ] Scheduled sync as a K8s CronJob instead of startup-only warm-up
- [ ] NBA model: nba_api PRA projections
- [ ] NFL model integration reusing the existing nfl_data_py projections
- [ ] Deduplicate bets across books so the same wager placed twice does not double-count P/L
