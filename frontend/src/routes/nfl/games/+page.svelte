<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { peek } from '$lib/cache';
  import { getNflGameRecord } from '$lib/api';
  import {
    NFL_CACHE_KEY, load as loadScreen, initialState,
    fmtOdds, fmtPct, fmtSigned, fmtEv, fmtKickoff,
  } from '$lib/nflScreen';
  import type { NflScreen, NflGameSim, NflGameRecord, NflSimRecord } from '$lib/types';

  let s = initialState(peek<NflScreen>(NFL_CACHE_KEY));
  $: ({ data, loading, error, warming, warmingElapsed } = s);

  let record: NflGameRecord | null = null;

  let cancel: (() => void) | null = null;
  function go(force = false) {
    cancel?.();
    cancel = loadScreen((patch) => (s = { ...s, ...patch }), force);
  }
  onMount(() => {
    go();
    getNflGameRecord().then((r) => (record = r)).catch(() => (record = null));
  });
  onDestroy(() => cancel?.());

  $: sims = [...(data?.game_sims ?? [])].sort((a, b) =>
    (a.kickoff ?? '').localeCompare(b.kickoff ?? ''));
  $: picks = sims.flatMap((g) => g.picks.map((p) => ({ ...p, game: g })));
  $: rules = data?.game_rules;

  const hcp = (v: number | null | undefined) =>
    v == null ? '—' : v > 0 ? `+${v}` : `${v}`;

  function rec(r: NflSimRecord | undefined): string {
    if (!r || r.bets === 0) return 'no settled bets yet';
    const p = r.pushes ? `-${r.pushes}` : '';
    return `${r.wins}-${r.losses}${p} · ${r.roi != null ? fmtSigned(r.roi) + '% ROI' : '—'}`;
  }

  /** Probabilities for the away side are the complement of the home ones. */
  const away = (p: number | null | undefined) => (p == null ? null : 1 - p);

  function leanText(g: NflGameSim, l: NflGameSim['leans'][number]): string {
    if (l.market === 'spread') return `${l.team} ${hcp(l.line)}`;
    return `${l.side === 'over' ? 'Over' : 'Under'} ${l.line}`;
  }
</script>

<svelte:head><title>NFL Games — Sharp Edge</title></svelte:head>

<div class="space-y-6">
  <div class="flex items-center justify-between flex-wrap gap-3">
    <p class="text-sm text-slate-400">
      {#if data}Week {data.week}, {data.season} —{/if}
      game predictions, {rules ? rules.sims.toLocaleString() : '20,000'} simulations per game
    </p>
    <button
      class="px-3 py-1.5 rounded-lg text-sm font-medium bg-surface-700 text-slate-300 hover:bg-surface-600 disabled:opacity-50"
      on:click={() => go(true)}
      disabled={loading}
    >{loading ? 'Loading…' : 'Refresh'}</button>
  </div>

  {#if error}
    <div class="card border-red-800 bg-red-950/30 text-red-300 text-sm">{error}</div>
  {/if}

  {#if loading && !data}
    <div class="card text-slate-400 text-sm flex items-center gap-3">
      <div class="w-4 h-4 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin"></div>
      {#if warming}
        Building this week's NFL board{warmingElapsed != null
          ? ` (${Math.round(warmingElapsed)}s elapsed)` : ''}…
      {:else}
        Loading board…
      {/if}
    </div>
  {:else if data}
    <!-- Recommendations, and the record that has to back them. -->
    <section class="card p-0 overflow-hidden">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between flex-wrap gap-2">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Moneyline picks</h2>
        <span class="text-xs text-slate-500"
          title="Recorded before kickoff with the FanDuel price, graded once the score is final.">
          live record: {rec(record?.ml_picks)}
        </span>
      </div>
      <div class="px-5 py-3 text-xs text-slate-400 border-b border-border/50">
        Each game is simulated from FanDuel's own spread. When the moneyline is priced at least
        {rules?.ml_gap_pts ?? 2} points longer than that spread says it should be (vig included),
        the moneyline is the pick. Backtest 2019–25: +8.5% ROI on 330 bets, mostly underdogs, and noisy
        year to year, so keep stakes small.
      </div>
      {#if picks.length === 0}
        <div class="px-5 py-4 text-sm text-slate-500">
          No moneyline is out of line with its spread this week. Most weeks there will be one or two.
        </div>
      {:else}
        <div class="divide-y divide-border/50">
          {#each picks as p}
            <div class="px-5 py-3 flex items-center gap-4 flex-wrap text-sm">
              <span class="px-2 py-0.5 rounded bg-emerald-900/40 text-emerald-300 text-xs font-semibold">BET</span>
              <span class="text-slate-100 font-medium">{p.team} ML {fmtOdds(p.odds)}</span>
              <span class="text-slate-400 tabular-nums">wins {fmtPct(p.p)} · fair {fmtOdds(p.fair)}</span>
              <span class="text-emerald-400 tabular-nums">{fmtSigned(p.edge_pts)} pts · EV {fmtEv(p.ev)}</span>
              <span class="text-slate-500 text-xs ml-auto">{p.game.away_team} @ {p.game.home_team} · {fmtKickoff(p.game.kickoff)}</span>
            </div>
          {/each}
        </div>
      {/if}
    </section>

    <div class="card border-amber-900/60 bg-amber-950/20 text-xs text-amber-200/80 py-2">
      <strong class="text-amber-300">Leans are not picks.</strong>
      The spread and total leans come from our own score projection. Against closing lines from 2016–25 that projection
      went 48.8% ATS and did <em>worse</em> as its disagreement grew, so leans are graded here
      (spread {rec(record?.leans?.spread)}; total {rec(record?.leans?.total)}) and stay leans until the record says otherwise.
    </div>

    {#if sims.length === 0}
      <div class="card text-sm text-slate-500">No game markets posted.</div>
    {:else}
      <div class="grid gap-4 md:grid-cols-2">
        {#each sims as g (g.fd_event_id ?? g.event)}
          {@const m = g.model}
          {@const mk = g.market}
          <section class="card p-0 overflow-hidden">
            <div class="px-4 py-3 border-b border-border flex items-baseline justify-between gap-2">
              <h3 class="text-slate-100 font-semibold">{g.away_team} @ {g.home_team}</h3>
              <span class="text-xs text-slate-500">{fmtKickoff(g.kickoff)}</span>
            </div>

            <div class="px-4 py-3 flex items-baseline gap-3 border-b border-border/50">
              <span class="text-xs uppercase tracking-wider text-slate-500 w-24">Projected</span>
              {#if m && m.exp_home_points != null && m.exp_away_points != null}
                <span class="text-lg tabular-nums text-slate-100">
                  {g.away_team} {m.exp_away_points.toFixed(1)} – {m.exp_home_points.toFixed(1)} {g.home_team}
                </span>
                {#if m.thin}<span class="text-xs text-amber-400" title="Mostly last season's ratings this early.">early season</span>{/if}
              {:else}
                <span class="text-slate-500 text-sm">no rating for one side</span>
              {/if}
            </div>

            <table class="w-full text-xs">
              <thead>
                <tr class="text-slate-500 uppercase tracking-wider">
                  <th class="text-left px-4 py-2 font-medium"></th>
                  <th class="text-left px-2 py-2 font-medium">FanDuel</th>
                  <th class="text-right px-2 py-2 font-medium" title="Simulated from FanDuel's own spread and total">Market sim</th>
                  <th class="text-right px-4 py-2 font-medium" title="Simulated from our projected score">Our sim</th>
                </tr>
              </thead>
              <tbody class="tabular-nums">
                <tr class="border-t border-border/40">
                  <td class="px-4 py-1.5 text-slate-400">{g.away_team} win</td>
                  <td class="px-2 py-1.5 text-slate-300">{fmtOdds(g.fd.away_ml)}</td>
                  <td class="px-2 py-1.5 text-right text-slate-400">{fmtPct(away(mk?.p_home_win), 0)}</td>
                  <td class="px-4 py-1.5 text-right text-slate-200">{fmtPct(away(m?.p_home_win), 0)}
                    <span class="text-slate-600">{fmtOdds(m?.fair_away_ml)}</span></td>
                </tr>
                <tr>
                  <td class="px-4 py-1.5 text-slate-400">{g.home_team} win</td>
                  <td class="px-2 py-1.5 text-slate-300">{fmtOdds(g.fd.home_ml)}</td>
                  <td class="px-2 py-1.5 text-right text-slate-400">{fmtPct(mk?.p_home_win, 0)}</td>
                  <td class="px-4 py-1.5 text-right text-slate-200">{fmtPct(m?.p_home_win, 0)}
                    <span class="text-slate-600">{fmtOdds(m?.fair_home_ml)}</span></td>
                </tr>
                <tr class="border-t border-border/40">
                  <td class="px-4 py-1.5 text-slate-400">{g.home_team} covers</td>
                  <td class="px-2 py-1.5 text-slate-300">{hcp(g.fd.home_spread)} {fmtOdds(g.fd.home_spread_odds)}</td>
                  <td class="px-2 py-1.5 text-right text-slate-400">{fmtPct(mk?.p_home_cover, 0)}</td>
                  <td class="px-4 py-1.5 text-right text-slate-200">{fmtPct(m?.p_home_cover, 0)}
                    {#if m?.exp_margin != null}<span class="text-slate-600" title="Our fair spread for the home side">{hcp(-m.exp_margin)}</span>{/if}</td>
                </tr>
                <tr class="border-t border-border/40">
                  <td class="px-4 py-1.5 text-slate-400">Over</td>
                  <td class="px-2 py-1.5 text-slate-300">{g.fd.total ?? '—'} {fmtOdds(g.fd.over_odds)}</td>
                  <td class="px-2 py-1.5 text-right text-slate-400">{fmtPct(mk?.p_over, 0)}</td>
                  <td class="px-4 py-1.5 text-right text-slate-200">{fmtPct(m?.p_over, 0)}
                    {#if m?.exp_total != null}<span class="text-slate-600">{m.exp_total.toFixed(1)}</span>{/if}</td>
                </tr>
              </tbody>
            </table>

            {#if g.picks.length || g.leans.length}
              <div class="px-4 py-3 border-t border-border flex flex-wrap gap-2 text-xs">
                {#each g.picks as p}
                  <span class="px-2 py-1 rounded bg-emerald-900/40 text-emerald-300"
                    title="Moneyline priced {p.edge_pts} points longer than the spread implies">
                    BET {p.team} ML {fmtOdds(p.odds)}
                  </span>
                {/each}
                {#each g.leans as l}
                  <span class="px-2 py-1 rounded bg-amber-900/30 text-amber-300/90"
                    title="From our projection, which has not beaten closing lines. Recorded, not recommended.">
                    lean {leanText(g, l)} · {fmtPct(l.p, 0)}
                  </span>
                {/each}
              </div>
            {/if}
          </section>
        {/each}
      </div>
    {/if}
  {/if}
</div>
