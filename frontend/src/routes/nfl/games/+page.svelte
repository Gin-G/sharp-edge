<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { peek } from '$lib/cache';
  import {
    NFL_CACHE_KEY, load as loadScreen, initialState,
    fmtOdds, fmtPct, fmtKickoff,
  } from '$lib/nflScreen';
  import type { NflScreen, NflGameMarket } from '$lib/types';

  let s = initialState(peek<NflScreen>(NFL_CACHE_KEY));
  $: ({ data, loading, error, warming, warmingElapsed } = s);

  let cancel: (() => void) | null = null;
  function go(force = false) {
    cancel?.();
    cancel = loadScreen((patch) => (s = { ...s, ...patch }), force);
  }
  onMount(() => go());
  onDestroy(() => cancel?.());

  function implied(american: number | null): number | null {
    if (american === null) return null;
    return american < 0 ? -american / (-american + 100) : 100 / (american + 100);
  }

  /** One row per game, with its moneyline, spread and total side by side. */
  $: games = (() => {
    const byEvent = new Map<string, { event: string; kickoff: string | null;
                                      markets: Record<string, NflGameMarket> }>();
    for (const m of data?.games ?? []) {
      if (!m.fd_event_id) continue;
      const g = byEvent.get(m.fd_event_id) ?? {
        event: m.event ?? '—', kickoff: m.kickoff, markets: {},
      };
      g.markets[m.market] = m;
      byEvent.set(m.fd_event_id, g);
    }
    return [...byEvent.values()].sort((a, b) =>
      (a.kickoff ?? '').localeCompare(b.kickoff ?? ''));
  })();

  function ml(g: { markets: Record<string, NflGameMarket> }) {
    const runners = g.markets.moneyline?.runners ?? [];
    const total = runners.reduce((acc, r) => acc + (implied(r.odds) ?? 0), 0);
    return { runners, overround: total || null };
  }
</script>

<svelte:head><title>NFL Games — Sharp Edge</title></svelte:head>

<div class="space-y-6">
  <div class="flex items-center justify-between flex-wrap gap-3">
    <p class="text-sm text-slate-400">
      {#if data}Week {data.week}, {data.season} — {/if}moneyline, spread and total
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

  <!-- No model here, and that is a finding rather than an omission. -->
  <div class="card border-slate-700 bg-surface-800/60 text-sm text-slate-300 space-y-2">
    <p>
      <span class="font-semibold text-slate-200">No moneyline picks — the closing line is already right.</span>
      Measured on 2,884 regular-season games from 2015 to 2025, using nflverse's
      real closing prices, the devigged line is calibrated at every level: it
      says 64.8% and the home team wins 61.7%, it says 74.9% and they win 75.8%,
      it says 84.7% and they win 86.9%. Those gaps are noise, not a pattern.
    </p>
    <p class="text-slate-400">
      Every naive strategy loses roughly the vig — always home
      <span class="tabular-nums">−5.01%</span>, always away
      <span class="tabular-nums">−2.41%</span>, always favourite
      <span class="tabular-nums">−2.90%</span>, always underdog
      <span class="tabular-nums">−4.52%</span> — at a median overround of 2.7%.
      Beating that needs a model genuinely better than the market's, and nothing
      here is. The prices are listed as context for the prop board; they are not
      a bet list.
    </p>
  </div>

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
    <section class="card overflow-hidden p-0">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
          This Week's Games
        </h2>
        <span class="text-xs text-slate-500">{games.length} games</span>
      </div>
      {#if games.length === 0}
        <div class="px-5 py-6 text-sm text-slate-500">No game markets posted.</div>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                <th class="text-left px-4 py-3">Game</th>
                <th class="text-left px-4 py-3">Kickoff</th>
                <th class="text-left px-4 py-3">Moneyline</th>
                <th class="text-right px-4 py-3" title="Quoted probabilities summed; 100% would be a fair book">Vig</th>
                <th class="text-left px-4 py-3">Spread</th>
                <th class="text-left px-4 py-3">Total</th>
              </tr>
            </thead>
            <tbody>
              {#each games as g (g.event + g.kickoff)}
                {@const m = ml(g)}
                <tr class="border-b border-border/50 hover:bg-surface-600/30">
                  <td class="px-4 py-2.5 text-slate-200">{g.event}</td>
                  <td class="px-4 py-2.5 text-slate-500 text-xs">{fmtKickoff(g.kickoff)}</td>
                  <td class="px-4 py-2.5 text-slate-300 text-xs">
                    {#each m.runners as r}
                      <div class="flex justify-between gap-3 tabular-nums">
                        <span class="text-slate-400">{r.name}</span>
                        <span>{fmtOdds(r.odds)}
                          <span class="text-slate-600">{fmtPct(implied(r.odds), 0)}</span>
                        </span>
                      </div>
                    {/each}
                  </td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-500 text-xs">
                    {m.overround ? `${(100 * m.overround).toFixed(1)}%` : '—'}
                  </td>
                  <td class="px-4 py-2.5 text-slate-300 text-xs">
                    {#each g.markets.spread?.runners ?? [] as r}
                      <div class="flex justify-between gap-3 tabular-nums">
                        <span class="text-slate-400">{r.name}</span>
                        <span>{r.handicap != null ? (r.handicap > 0 ? `+${r.handicap}` : r.handicap) : ''} {fmtOdds(r.odds)}</span>
                      </div>
                    {/each}
                  </td>
                  <td class="px-4 py-2.5 text-slate-300 text-xs">
                    {#each g.markets.total?.runners ?? [] as r}
                      <div class="flex justify-between gap-3 tabular-nums">
                        <span class="text-slate-400">{r.name}</span>
                        <span>{fmtOdds(r.odds)}</span>
                      </div>
                    {/each}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>
  {/if}
</div>
