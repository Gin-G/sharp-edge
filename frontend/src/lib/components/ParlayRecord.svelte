<script lang="ts">
  import { onMount } from 'svelte';
  import { getParlayRecord } from '$lib/api';
  import { cached, peek } from '$lib/cache';
  import type { ParlayRecord } from '$lib/types';

  // The card settles as one bet, so this counts tickets rather than legs.
  // Two legs winning out of three is a loss, not 67% — which is exactly why
  // this can't be read off the pick list's hit rate.
  const cacheKey = 'parlay-record';
  let data: ParlayRecord | null = peek<ParlayRecord>(cacheKey);
  let error = '';
  let loading = data === null;
  let showAll = false;
  const PREVIEW_ROWS = 10;

  onMount(async () => {
    try {
      data = await cached(cacheKey, () => getParlayRecord());
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  });

  $: rows = data?.parlays ?? [];
  $: visible = showAll ? rows : rows.slice(0, PREVIEW_ROWS);

  function fmtOdds(v: number | null): string {
    if (v === null || v === undefined) return '—';
    return v >= 0 ? `+${v}` : `${v}`;
  }
  function resultClass(r: string | null): string {
    if (r === 'WIN') return 'bg-emerald-600/20 text-emerald-300 border-emerald-600/30';
    if (r === 'LOSS') return 'bg-rose-600/20 text-rose-300 border-rose-600/30';
    if (r === 'VOID') return 'bg-slate-600/20 text-slate-400 border-slate-600/30';
    return 'bg-amber-600/20 text-amber-300 border-amber-600/30';
  }
</script>

<section class="card p-0 overflow-hidden">
  <div class="px-5 py-4 border-b border-border flex items-baseline justify-between flex-wrap gap-2">
    <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
      Parlay Record
      <span class="ml-2 normal-case font-normal text-xs text-slate-500">
        the card as one bet — it swept or it didn't
      </span>
    </h2>
    {#if data && data.decided > 0}
      <span class="text-xs text-slate-400 tabular-nums">
        {data.wins}–{data.losses}
        · <span class="text-slate-200 font-semibold">{data.sweep_rate?.toFixed(1)}% swept</span>
        {#if data.roi !== null}
          · <span class={data.roi >= 0 ? 'text-emerald-400' : 'text-rose-400'}>
            ROI {data.roi >= 0 ? '+' : ''}{data.roi.toFixed(1)}%
          </span>
        {/if}
        {#if data.avg_legs !== null}· {data.avg_legs} legs avg{/if}
        {#if data.pending}· {data.pending} pending{/if}
      </span>
    {/if}
  </div>

  {#if loading}
    <div class="px-5 py-6 text-sm text-slate-400">Loading…</div>
  {:else if error}
    <div class="px-5 py-6 text-sm text-amber-500/80">{error}</div>
  {:else if rows.length === 0}
    <div class="px-5 py-6 text-sm text-slate-500">
      No cards recorded yet — the first one is frozen the next time a slate is screened before first pitch.
    </div>
  {:else}
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
            <th class="text-left px-5 py-3">Date</th>
            <th class="text-right px-3 py-3">Legs</th>
            <th class="text-right px-3 py-3">Price</th>
            <th class="text-left px-3 py-3">Card</th>
            <th class="text-center px-5 py-3">Result</th>
          </tr>
        </thead>
        <tbody>
          {#each visible as p (p.pick_date)}
            <tr class="border-b border-border/50 hover:bg-surface-600/30">
              <td class="px-5 py-2 text-slate-400 text-xs whitespace-nowrap">{p.pick_date}</td>
              <td class="px-3 py-2 text-right tabular-nums text-slate-300">
                {p.leg_count}{#if p.result === 'LOSS' && p.legs_won !== null}<span class="text-xs text-slate-500"> ({p.legs_won} hit)</span>{/if}
              </td>
              <td class="px-3 py-2 text-right tabular-nums text-slate-200">{fmtOdds(p.american)}</td>
              <td class="px-3 py-2 text-slate-400 text-xs">
                {(p.legs ?? []).map((l) => l.batter).join(', ')}
              </td>
              <td class="px-5 py-2 text-center">
                <span class="inline-block px-2 py-0.5 text-xs rounded border {resultClass(p.result)}">
                  {p.result ?? 'PENDING'}
                </span>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if rows.length > PREVIEW_ROWS}
      <button
        class="w-full px-5 py-2 text-xs text-slate-400 hover:text-slate-200 border-t border-border"
        on:click={() => (showAll = !showAll)}
      >{showAll ? 'Show less' : `Show all ${rows.length}`}</button>
    {/if}
  {/if}
</section>
