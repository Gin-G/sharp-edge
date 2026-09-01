<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { peek } from '$lib/cache';
  import {
    NFL_CACHE_KEY, load as loadScreen, initialState,
    fmtOdds, fmtPct, fmtSigned, fmtEv, evClass, MARKET_LABEL, fmtKickoff,
  } from '$lib/nflScreen';
  import type { NflScreen, NflProp } from '$lib/types';

  let s = initialState(peek<NflScreen>(NFL_CACHE_KEY));
  $: ({ data, loading, error, warming, warmingElapsed } = s);

  let cancel: (() => void) | null = null;
  function go(force = false) {
    cancel?.();
    cancel = loadScreen((patch) => (s = { ...s, ...patch }), force);
  }
  onMount(() => go());
  onDestroy(() => cancel?.());

  // Filters. `onlySignals` defaults on — the whole point of the board is the
  // rows where the projection and the line disagree, and 250 rows of agreement
  // is not something anyone reads.
  let onlySignals = true;
  let market: string = 'all';
  let includeUnbettable = false;

  $: markets = data ? [...new Set(data.props.map((p) => p.market))] : [];

  $: rows = (data?.props ?? []).filter((p: NflProp) => {
    if (onlySignals && !p.signal) return false;
    if (market !== 'all' && p.market !== market) return false;
    if (!includeUnbettable && !p.bettable) return false;
    return true;
  });

  $: overs = rows.filter((r) => r.signal === 'OVER').length;
  $: unders = rows.filter((r) => r.signal === 'UNDER').length;

  // How much the raw rule and the rescaled one disagree. Worth a line on the
  // page rather than only in the code: it is the single biggest judgement call
  // in this screen.
  $: rawOnly = (data?.props ?? []).filter(
    (p) => p.bettable && p.raw_signal && !p.signal,
  ).length;
  $: bothAgree = (data?.props ?? []).filter(
    (p) => p.bettable && p.raw_signal && p.signal === p.raw_signal,
  ).length;

  function sideClass(side: string): string {
    if (side === 'OVER') return 'bg-emerald-600/20 text-emerald-300 border-emerald-600/30';
    if (side === 'UNDER') return 'bg-amber-600/20 text-amber-300 border-amber-600/30';
    return 'bg-surface-600/40 text-slate-500 border-border';
  }
</script>

<svelte:head><title>NFL Props — Sharp Edge</title></svelte:head>

<div class="space-y-6">
  <div class="flex items-center justify-between flex-wrap gap-3">
    <p class="text-sm text-slate-400">
      {#if data}
        Week {data.week}, {data.season} — projection against the posted line
        {#if data.odds?.age_seconds != null}
          <span class="text-slate-500">
            · FanDuel {data.odds.age_seconds > 60
              ? `${Math.round(data.odds.age_seconds / 60)}m old`
              : 'live'}
          </span>
        {/if}
      {:else}
        Projection against the posted line
      {/if}
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

  {#if data?.preseason}
    <!-- The honest health warning on week 1. These projections were computed
         in August off last season and rookie priors; they have never seen a
         snap of this season, and they disagree with the market by a lot more
         than they will in October. -->
    <div class="card border-amber-800/50 bg-amber-950/20 text-sm text-amber-200/90">
      <span class="font-semibold">Preseason projections.</span>
      Week {data.week} is built from priors computed before the season — no
      in-season usage behind them. The disagreement with the market is shrunk
      hard as a result ({data.prob_fits?.receiving_yards?.shrink ?? '—'} of it is
      kept), and the model probabilities below should be read as a ranking, not
      as a price. They earn their credibility once settled weeks exist to
      measure them against.
    </div>
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
    <!-- Controls -->
    <div class="card p-4 flex items-center gap-4 flex-wrap text-sm">
      <label class="flex items-center gap-2 text-slate-300">
        <input type="checkbox" bind:checked={onlySignals} class="accent-indigo-500" />
        Only rows past the threshold
      </label>
      <label class="flex items-center gap-2 text-slate-300">
        <input type="checkbox" bind:checked={includeUnbettable} class="accent-indigo-500" />
        Include passing yards
      </label>
      <div class="flex items-center gap-2">
        <span class="text-slate-500">Market</span>
        <select bind:value={market} class="bg-surface-800 border border-border rounded-lg px-2 py-1 text-slate-200">
          <option value="all">All</option>
          {#each markets as m}
            <option value={m}>{MARKET_LABEL[m] ?? m}</option>
          {/each}
        </select>
      </div>
      <span class="text-slate-500 ml-auto tabular-nums">
        {rows.length} rows · {overs} over / {unders} under
      </span>
    </div>

    {#if includeUnbettable}
      <div class="card border-amber-800/40 bg-amber-950/10 text-xs text-amber-200/80">
        {data.passing_yards_caveat}
      </div>
    {/if}

    <!-- Board -->
    <section class="card overflow-hidden p-0">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between flex-wrap gap-2">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
          Projection vs Line
        </h2>
        <span class="text-xs text-slate-500">
          fires at ±{data.thresholds.receiving_yards} yds / ±{data.thresholds.receptions} rec,
          measured on the rescaled gap
        </span>
      </div>

      {#if rows.length === 0}
        <div class="px-5 py-6 text-sm text-slate-500">
          Nothing past the threshold with these filters.
        </div>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                <th class="text-left px-4 py-3">Bet</th>
                <th class="text-left px-4 py-3">Player</th>
                <th class="text-left px-4 py-3">Market</th>
                <th class="text-right px-4 py-3">Line</th>
                <th class="text-right px-4 py-3" title="The projection as published, before rescaling.">Proj</th>
                <th class="text-right px-4 py-3" title="The projection restated on the market's scale.">Adj</th>
                <th class="text-right px-4 py-3" title="Projection minus line, as published. Biased toward UNDER on good players.">Raw gap</th>
                <th class="text-right px-4 py-3" title="The gap after rescaling — what the signal is measured on.">Gap</th>
                <th class="text-right px-4 py-3">Price</th>
                <th class="text-right px-4 py-3">Model</th>
                <th class="text-right px-4 py-3">Edge</th>
                <th class="text-right px-4 py-3">EV/$1</th>
                <th class="text-left px-4 py-3">Game</th>
              </tr>
            </thead>
            <tbody>
              {#each rows as r (r.market + r.key + r.line)}
                <tr class="border-b border-border/50 hover:bg-surface-600/30 {r.bettable ? '' : 'opacity-60'}">
                  <td class="px-4 py-2.5">
                    <span class="inline-block px-2 py-0.5 text-xs rounded border {sideClass(r.signal || r.side || '')}">
                      {r.signal || r.side || '—'}
                    </span>
                  </td>
                  <td class="px-4 py-2.5 text-slate-200 font-medium">
                    {r.player}
                    <span class="text-xs text-slate-500">{r.position ?? ''} {r.team ?? ''}</span>
                  </td>
                  <td class="px-4 py-2.5 text-slate-400">{MARKET_LABEL[r.market] ?? r.market}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-200">{r.line}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-500">{r.projection}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">{r.adjusted}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-600">{fmtSigned(r.raw_gap)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums font-medium
                             {r.residual > 0 ? 'text-emerald-400' : 'text-amber-400'}">
                    {fmtSigned(r.residual)}
                  </td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">
                    {fmtOdds(r.odds)}
                    <span class="block text-xs text-slate-600">
                      {fmtOdds(r.over_odds)}/{fmtOdds(r.under_odds)}
                    </span>
                  </td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-200">
                    {fmtPct(r.model_p)}
                    <span class="block text-xs text-slate-600" title="Before anchoring to the market">
                      raw {fmtPct(r.model_p_raw)}
                    </span>
                  </td>
                  <td class="px-4 py-2.5 text-right tabular-nums {evClass(r.ev)}">
                    {r.edge_pts != null ? fmtSigned(r.edge_pts) : '—'}
                  </td>
                  <td class="px-4 py-2.5 text-right tabular-nums {evClass(r.ev)}">{fmtEv(r.ev)}</td>
                  <td class="px-4 py-2.5 text-slate-500 text-xs">
                    {r.event ?? '—'}
                    <span class="block text-slate-600">{fmtKickoff(r.kickoff)}</span>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <!-- How much work the rescaling is doing -->
    <section class="card p-0 overflow-hidden">
      <div class="px-5 py-4 border-b border-border">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
          Rescaling
        </h2>
        <p class="text-xs text-slate-500 mt-1">
          The projections regress toward the mean and the lines don't, so the raw
          gap reads that shrinkage as signal — it wants the under on nearly every
          star. Each market is refit weekly against its own board; a slope of 1.0
          would mean no correction was needed.
        </p>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
              <th class="text-left px-4 py-3">Market</th>
              <th class="text-right px-4 py-3">Slope</th>
              <th class="text-right px-4 py-3">Intercept</th>
              <th class="text-right px-4 py-3">Players fit</th>
              <th class="text-right px-4 py-3" title="Log-odds shift applied to centre the model on the market">Anchor</th>
              <th class="text-right px-4 py-3" title="Fraction of the disagreement kept after anchoring">Shrink</th>
            </tr>
          </thead>
          <tbody>
            {#each Object.entries(data.fits) as [m, f]}
              <tr class="border-b border-border/50">
                <td class="px-4 py-2.5 text-slate-300">{MARKET_LABEL[m] ?? m}</td>
                <td class="px-4 py-2.5 text-right tabular-nums {f.slope != null && Math.abs(f.slope - 1) > 0.15 ? 'text-amber-400' : 'text-slate-300'}">
                  {f.slope ?? '—'}
                </td>
                <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{f.intercept ?? '—'}</td>
                <td class="px-4 py-2.5 text-right tabular-nums text-slate-500">{f.n}</td>
                <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">
                  {data.prob_fits?.[m]?.offset ?? '—'}
                </td>
                <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">
                  {data.prob_fits?.[m]?.shrink ?? '—'}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <div class="px-5 py-3 border-t border-border text-xs text-slate-500">
        On this board the raw rule and the rescaled one agree on {bothAgree} rows
        and the raw rule fires on {rawOnly} more that the rescaled one rejects.
        Those {rawOnly} are the shrinkage artefacts.
        {#if data.unmatched.length}
          · {data.unmatched.length} priced player{data.unmatched.length === 1 ? '' : 's'}
          had no projection to join: {data.unmatched.slice(0, 5).join(', ')}
        {/if}
      </div>
    </section>
  {/if}
</div>
