<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { peek } from '$lib/cache';
  import {
    NFL_CACHE_KEY, load as loadScreen, initialState,
    fmtOdds, fmtPct, fmtSigned, fmtKickoff, shortEvent,
  } from '$lib/nflScreen';
  import type { NflScreen } from '$lib/types';

  let s = initialState(peek<NflScreen>(NFL_CACHE_KEY));
  $: ({ data, loading, error, warming, warmingElapsed } = s);

  let cancel: (() => void) | null = null;
  function go(force = false) {
    cancel?.();
    cancel = loadScreen((patch) => (s = { ...s, ...patch }), force);
  }
  onMount(() => go());
  onDestroy(() => cancel?.());

  let hideThin = true;
  let positive = false;

  $: rows = (data?.tds ?? []).filter((r) => {
    if (hideThin && r.thin) return false;
    if (positive && !((r.edge_pts ?? -1) > 0)) return false;
    return true;
  });

  // The market's total overround, which is the thing to know before reading
  // any edge on this page. FanDuel books every scorer in a game, so the book's
  // margin is spread across the whole field rather than across two runners.
  $: overrounds = (() => {
    const byEvent = new Map<string, number>();
    for (const r of data?.tds ?? []) {
      if (r.implied_p == null || !r.fd_event_id) continue;
      byEvent.set(r.fd_event_id, (byEvent.get(r.fd_event_id) ?? 0) + r.implied_p);
    }
    return [...byEvent.values()];
  })();
  $: medianOverround = overrounds.length
    ? overrounds.sort((a, b) => a - b)[Math.floor(overrounds.length / 2)]
    : null;
</script>

<svelte:head><title>NFL Touchdowns — Sharp Edge</title></svelte:head>

<div class="space-y-6">
  <div class="flex items-center justify-between flex-wrap gap-3">
    <p class="text-sm text-slate-400">
      {#if data}Week {data.week}, {data.season} —{/if}
      anytime touchdown scorer: model probability against the quoted price
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
    <div class="card p-4 flex items-center gap-4 flex-wrap text-sm">
      <label class="flex items-center gap-2 text-slate-300">
        <input type="checkbox" bind:checked={hideThin} class="accent-indigo-500" />
        Hide long shots (model under 8%)
      </label>
      <label class="flex items-center gap-2 text-slate-300">
        <input type="checkbox" bind:checked={positive} class="accent-indigo-500" />
        Only positive edge
      </label>
      <span class="text-slate-500 ml-auto tabular-nums">
        {#if medianOverround != null}
          <span title="A game's quoted scorer probabilities sum to this in the median game. Most of it is real — about four players score per game — so it cannot be normalised away and the margin inside it cannot be measured.">field {(100 * medianOverround).toFixed(0)}%</span>
          ·
        {/if}
        {rows.length} of {data.tds.length}
      </span>
    </div>

    <section class="card overflow-hidden p-0">
      <div class="px-5 py-4 border-b border-border">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
          Anytime Touchdown Scorer
        </h2>
      </div>
      {#if rows.length === 0}
        <div class="px-5 py-6 text-sm text-slate-500">Nothing with these filters.</div>
      {:else}
        <div class="overflow-x-auto max-h-[70vh] overflow-y-auto">
          <table class="w-full text-sm">
            <thead class="sticky top-0 bg-surface-800">
              <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                <th class="text-left px-4 py-3">Player</th>
                <th class="text-left px-4 py-3">Pos</th>
                <th class="text-left px-4 py-3">Team</th>
                <th class="text-right px-4 py-3" title="Projected rushing + receiving touchdowns">Proj TD</th>
                <th class="text-right px-4 py-3" title="Shifted so the game's field totals what the book's does">Model</th>
                <th class="text-right px-4 py-3">Price</th>
                <th class="text-right px-4 py-3" title="Implied by the price, same margin as the model column">Implied</th>
                <th
                  class="text-right px-4 py-3"
                  title="Which, not whether. The model is shifted until its total matches the book's, so both carry the same unmeasurable margin and what is left is a disagreement about which players score. No EV column for the same reason — one computed from a margin-inflated probability would show a profit on most of the board."
                >Edge</th>
                <th class="text-left px-4 py-3">Game</th>
              </tr>
            </thead>
            <tbody>
              {#each rows as r (r.key + r.fd_event_id)}
                <tr class="border-b border-border/50 hover:bg-surface-600/30">
                  <td class="px-4 py-2.5 text-slate-200 font-medium">{r.player}</td>
                  <td class="px-4 py-2.5 text-slate-400">{r.position ?? '—'}</td>
                  <td class="px-4 py-2.5 text-slate-400">{r.team ?? '—'}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">
                    {r.projected_tds != null ? r.projected_tds.toFixed(2) : '—'}
                  </td>
                  <td
                    class="px-4 py-2.5 text-right tabular-nums text-slate-200"
                    title="{fmtPct(r.model_p_unanchored)} before shifting onto the field's total"
                  >{fmtPct(r.model_p)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">{fmtOdds(r.odds)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{fmtPct(r.implied_p)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums font-medium
                             {(r.edge_pts ?? 0) > 0 ? 'text-emerald-400' : 'text-slate-500'}">
                    {r.edge_pts != null ? fmtSigned(r.edge_pts) : '—'}
                  </td>
                  <td class="px-4 py-2.5 text-slate-500 text-xs whitespace-nowrap" title={r.event ?? ''}>
                    {shortEvent(r.event)}
                    <span class="text-slate-600"> · {fmtKickoff(r.kickoff)}</span>
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
