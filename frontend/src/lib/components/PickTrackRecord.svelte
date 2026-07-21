<script lang="ts">
  import { onMount } from 'svelte';
  import { getTrackRecord } from '$lib/api';
  import type { TrackRecord } from '$lib/types';

  export let screen: 'hr' | 'batter';
  export let winLabel = 'HR';

  let data: TrackRecord | null = null;
  let error = '';
  let loading = true;
  let showAll = false;

  const PREVIEW_ROWS = 15;

  onMount(async () => {
    try {
      data = await getTrackRecord(screen);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  });

  function fmtRate(v: number | null | undefined): string {
    if (v === null || v === undefined) return '—';
    return v.toFixed(1) + '%';
  }

  function resultClass(result: string | null): string {
    if (result === 'WIN') return 'bg-emerald-600/20 text-emerald-300 border-emerald-600/30';
    if (result === 'LOSS') return 'bg-red-600/20 text-red-300 border-red-600/30';
    if (result === 'VOID') return 'bg-surface-600/50 text-slate-400 border-border';
    return 'bg-amber-600/20 text-amber-300 border-amber-600/30';
  }

  $: visiblePicks = showAll ? (data?.picks ?? []) : (data?.picks ?? []).slice(0, PREVIEW_ROWS);
</script>

<section class="card overflow-hidden p-0">
  <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
    <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Track Record</h2>
    {#if data}
      <span class="text-xs text-slate-500">
        {data.overall.picks} picks · voids excluded from hit rate
      </span>
    {/if}
  </div>

  {#if loading}
    <div class="px-5 py-6 text-sm text-slate-500">Loading track record…</div>
  {:else if error}
    <div class="px-5 py-6 text-sm text-red-300">{error}</div>
  {:else if data && data.overall.picks === 0}
    <div class="px-5 py-6 text-sm text-slate-500">
      No picks recorded yet — history builds up as the daily screen runs (or after a backfill).
    </div>
  {:else if data}
    <!-- Summary row -->
    <div class="grid grid-cols-2 sm:grid-cols-4 gap-px bg-border/50">
      <div class="bg-surface-800 px-5 py-4">
        <p class="text-xs font-medium text-slate-400 uppercase tracking-wider">Hit Rate</p>
        <p class="text-2xl font-bold tabular-nums {(data.overall.hit_rate ?? 0) >= 50 ? 'text-emerald-400' : 'text-slate-100'}">
          {fmtRate(data.overall.hit_rate)}
        </p>
        <p class="text-xs text-slate-500">≥1 {winLabel} on the day</p>
      </div>
      <div class="bg-surface-800 px-5 py-4">
        <p class="text-xs font-medium text-slate-400 uppercase tracking-wider">Record</p>
        <p class="text-2xl font-bold tabular-nums text-slate-100">
          {data.overall.wins}<span class="text-slate-500">–</span>{data.overall.losses}
        </p>
        <p class="text-xs text-slate-500">wins–losses</p>
      </div>
      <div class="bg-surface-800 px-5 py-4">
        <p class="text-xs font-medium text-slate-400 uppercase tracking-wider">Voids</p>
        <p class="text-2xl font-bold tabular-nums text-slate-100">{data.overall.voids}</p>
        <p class="text-xs text-slate-500">didn't play</p>
      </div>
      <div class="bg-surface-800 px-5 py-4">
        <p class="text-xs font-medium text-slate-400 uppercase tracking-wider">Pending</p>
        <p class="text-2xl font-bold tabular-nums text-slate-100">{data.overall.pending}</p>
        <p class="text-xs text-slate-500">awaiting results</p>
      </div>
    </div>

    <!-- Per-edge breakdown -->
    {#if data.by_tag.length > 0}
      <div class="px-5 py-4 border-t border-border">
        <p class="text-xs font-medium text-slate-400 uppercase tracking-wider mb-2">By Edge</p>
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="text-xs font-medium text-slate-400 uppercase tracking-wider">
                <th class="text-left py-1.5 pr-3">Edge</th>
                <th class="text-right py-1.5 px-3">Picks</th>
                <th class="text-right py-1.5 px-3">W–L</th>
                <th class="text-right py-1.5 pl-3">Hit Rate</th>
              </tr>
            </thead>
            <tbody>
              {#each data.by_tag as t (t.tag)}
                <tr class="border-t border-border/50">
                  <td class="py-1.5 pr-3">
                    <span class="inline-block px-2 py-0.5 text-xs rounded bg-indigo-600/20 text-indigo-300 border border-indigo-600/30 whitespace-nowrap">{t.tag}</span>
                  </td>
                  <td class="py-1.5 px-3 text-right tabular-nums text-slate-300">{t.picks}</td>
                  <td class="py-1.5 px-3 text-right tabular-nums text-slate-300">{t.wins}–{t.losses}</td>
                  <td class="py-1.5 pl-3 text-right tabular-nums font-medium {(t.hit_rate ?? 0) >= 50 ? 'text-emerald-400' : 'text-slate-300'}">{fmtRate(t.hit_rate)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>
    {/if}

    <!-- Pick history -->
    <div class="border-t border-border">
      <div class="overflow-x-auto max-h-[400px] overflow-y-auto">
        <table class="w-full text-sm">
          <thead class="sticky top-0 bg-surface-800">
            <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
              <th class="text-left px-5 py-3">Date</th>
              <th class="text-left px-3 py-3">Batter</th>
              <th class="text-left px-3 py-3">Opp SP</th>
              {#if screen === 'hr'}
                <th class="text-right px-3 py-3">Score</th>
              {/if}
              <th class="text-left px-3 py-3">Tags</th>
              <th class="text-center px-3 py-3">Result</th>
              <th class="text-right px-5 py-3">{winLabel} / PA</th>
            </tr>
          </thead>
          <tbody>
            {#each visiblePicks as p (p.pick_date + p.batter)}
              <tr class="border-b border-border/50 hover:bg-surface-600/30">
                <td class="px-5 py-2 text-slate-400 text-xs whitespace-nowrap">{p.pick_date}</td>
                <td class="px-3 py-2 text-slate-200">{p.batter}</td>
                <td class="px-3 py-2 text-slate-400">{p.opposing_pitcher ?? '—'}</td>
                {#if screen === 'hr'}
                  <td class="px-3 py-2 text-right tabular-nums text-slate-300">{p.score?.toFixed(1) ?? '—'}</td>
                {/if}
                <td class="px-3 py-2">
                  {#each (p.tags ?? '').split(',').filter(Boolean) as tag}
                    <span class="inline-block px-1.5 py-0.5 mr-1 text-[10px] rounded bg-indigo-600/20 text-indigo-300 border border-indigo-600/30 whitespace-nowrap">{tag}</span>
                  {/each}
                </td>
                <td class="px-3 py-2 text-center">
                  <span class="inline-block px-2 py-0.5 text-xs rounded border {resultClass(p.result)}">{p.result ?? 'PENDING'}</span>
                </td>
                <td class="px-5 py-2 text-right tabular-nums text-slate-300">
                  {screen === 'hr' ? (p.hr_actual ?? '—') : (p.hits_actual ?? '—')}<span class="text-xs text-slate-500"> /{p.pa_actual ?? '—'}</span>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      {#if (data.picks.length > PREVIEW_ROWS)}
        <button
          class="w-full px-5 py-2.5 text-xs font-medium text-slate-400 hover:text-slate-200 hover:bg-surface-600/30 border-t border-border"
          on:click={() => (showAll = !showAll)}
        >{showAll ? 'Show fewer' : `Show all ${data.picks.length} picks`}</button>
      {/if}
    </div>
  {/if}
</section>
