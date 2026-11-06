<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { peek } from '$lib/cache';
  import {
    NFL_CACHE_KEY, load as loadScreen, initialState,
    fmtOdds, fmtPct, fmtSigned, fmtEv, fmtKickoff, shortEvent,
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

  // Click a column header to sort by it; click again to flip. Null sorts
  // last either way. Numbers start high-first, text starts A-Z.
  type Row = NflScreen['tds'][number];
  const SORT: Record<string, (r: Row) => number | string | null | undefined> = {
    player: (r) => r.player,
    position: (r) => r.position,
    team: (r) => r.team,
    projected_tds: (r) => r.projected_tds,
    p_cal: (r) => r.p_cal,
    // A longer price is a smaller implied chance, so price sorts on that.
    odds: (r) => (r.implied_p != null ? -r.implied_p : null),
    implied_p: (r) => r.implied_p,
    true_edge_pts: (r) => r.true_edge_pts,
    ev: (r) => r.ev,
    edge_pts: (r) => r.edge_pts,
    kickoff: (r) => r.kickoff,
  };
  let sortKey: string | null = null;
  let sortDesc = true;
  function sortBy(k: string) {
    if (sortKey === k) sortDesc = !sortDesc;
    else { sortKey = k; sortDesc = !['player', 'position', 'team', 'kickoff'].includes(k); }
  }
  const arrow = (k: string, key: string | null, desc: boolean) =>
    key === k ? (desc ? ' ▼' : ' ▲') : '';

  $: rows = (() => {
    const out = (data?.tds ?? []).filter((r) => {
      if (hideThin && r.thin) return false;
      if (positive && !((r.true_edge_pts ?? -1) > 0)) return false;
      return true;
    });
    if (!sortKey) return out;
    const get = SORT[sortKey];
    const dir = sortDesc ? -1 : 1;
    return [...out].sort((a, b) => {
      const x = get(a), y = get(b);
      if (x == null) return y == null ? 0 : 1;
      if (y == null) return -1;
      return (x < y ? -1 : x > y ? 1 : 0) * dir;
    });
  })();

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
  $: picks = data?.td_picks ?? [];
  $: rules = data?.td_rules;

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
        Only beats the price
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
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between flex-wrap gap-2">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">TD picks</h2>
        <span class="text-xs text-slate-500">
          scores at least {rules ? Math.round(rules.min_p * 100) : 20}% of the time, and beats the price
          by {rules?.min_edge_pts ?? 5}+ points with the vig left in
        </span>
      </div>
      {#if data.preseason}
        <div class="px-5 py-4 text-sm text-slate-500">
          This week's projection is still the preseason version, which the calibration was not
          measured on. Picks appear when the in-season projection publishes (Tuesday night).
        </div>
      {:else if picks.length === 0}
        <div class="px-5 py-4 text-sm text-slate-500">No scorer clears the bar this week.</div>
      {:else}
        <div class="divide-y divide-border/50">
          {#each picks as r (r.key + r.fd_event_id)}
            <div class="px-5 py-3 flex items-center gap-4 flex-wrap text-sm">
              <span class="px-2 py-0.5 rounded bg-emerald-900/40 text-emerald-300 text-xs font-semibold">BET</span>
              <span class="text-slate-100 font-medium">{r.player}
                <span class="text-slate-500 font-normal">{r.position} · {r.team}</span></span>
              <span class="text-slate-300 tabular-nums">{fmtOdds(r.odds)}</span>
              <span class="text-slate-400 tabular-nums">scores {fmtPct(r.p_cal)} vs {fmtPct(r.implied_p)} priced</span>
              <span class="text-emerald-400 tabular-nums">{fmtSigned(r.true_edge_pts)} pts · EV {fmtEv(r.ev)}</span>
              {#if r.avail && r.avail !== 'ACTIVE'}
                <span class="text-amber-400 text-xs">{r.avail_reason ?? r.avail}</span>
              {/if}
              <span class="text-slate-500 text-xs ml-auto">{shortEvent(r.event)} · {fmtKickoff(r.kickoff)}</span>
            </div>
          {/each}
        </div>
      {/if}
    </section>

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
                <th class="cursor-pointer select-none hover:text-slate-200 text-left px-4 py-3"
                  on:click={() => sortBy('player')}>Player{arrow('player', sortKey, sortDesc)}</th>
                <th class="cursor-pointer select-none hover:text-slate-200 text-left px-4 py-3"
                  on:click={() => sortBy('position')}>Pos{arrow('position', sortKey, sortDesc)}</th>
                <th class="cursor-pointer select-none hover:text-slate-200 text-left px-4 py-3"
                  on:click={() => sortBy('team')}>Team{arrow('team', sortKey, sortDesc)}</th>
                <th class="cursor-pointer select-none hover:text-slate-200 text-right px-4 py-3" title="Projected rushing + receiving touchdowns"
                  on:click={() => sortBy('projected_tds')}>Proj TD{arrow('projected_tds', sortKey, sortDesc)}</th>
                <th class="cursor-pointer select-none hover:text-slate-200 text-right px-4 py-3" title="Probability of scoring, calibrated on 2,318 player-games this season with each week held out in turn"
                  on:click={() => sortBy('p_cal')}>Scores{arrow('p_cal', sortKey, sortDesc)}</th>
                <th class="cursor-pointer select-none hover:text-slate-200 text-right px-4 py-3"
                  on:click={() => sortBy('odds')}>Price{arrow('odds', sortKey, sortDesc)}</th>
                <th class="cursor-pointer select-none hover:text-slate-200 text-right px-4 py-3" title="Implied by the price, vig included"
                  on:click={() => sortBy('implied_p')}>Implied{arrow('implied_p', sortKey, sortDesc)}</th>
                <th class="cursor-pointer select-none hover:text-slate-200 text-right px-4 py-3" title="Scores minus Implied. Positive means the price is long enough to beat the vig."
                  on:click={() => sortBy('true_edge_pts')}>Edge{arrow('true_edge_pts', sortKey, sortDesc)}</th>
                <th class="cursor-pointer select-none hover:text-slate-200 text-right px-4 py-3"
                  on:click={() => sortBy('ev')}>EV{arrow('ev', sortKey, sortDesc)}</th>
                <th
                  class="cursor-pointer select-none hover:text-slate-200 text-right px-4 py-3"
                  title="Relative read: the model shifted until the game's field totals what the book's does. Says which players the field under-prices, not whether any of them is a bet."
                  on:click={() => sortBy('edge_pts')}>Field{arrow('edge_pts', sortKey, sortDesc)}</th>
                <th class="cursor-pointer select-none hover:text-slate-200 text-left px-4 py-3"
                  on:click={() => sortBy('kickoff')}>Game{arrow('kickoff', sortKey, sortDesc)}</th>
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
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-200"
                    title="{fmtPct(r.model_p_unanchored)} before calibration">{fmtPct(r.p_cal)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">{fmtOdds(r.odds)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{fmtPct(r.implied_p)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums font-medium
                             {(r.true_edge_pts ?? 0) > 0 ? 'text-emerald-400' : 'text-slate-500'}">
                    {r.true_edge_pts != null ? fmtSigned(r.true_edge_pts) : '—'}
                  </td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{fmtEv(r.ev)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-500">
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
