<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { getHomerScreen, ApiError } from '$lib/api';
  import type { HomerScreen } from '$lib/types';
  import PickTrackRecord from '$lib/components/PickTrackRecord.svelte';

  let data: HomerScreen | null = null;
  let loading = true;
  let error = '';
  let warming = false;
  let warmingElapsed: number | null = null;
  let pollTimer: ReturnType<typeof setTimeout> | null = null;

  async function load() {
    loading = true;
    error = '';
    try {
      data = await getHomerScreen();
      warming = false;
      warmingElapsed = null;
    } catch (e) {
      if (e instanceof ApiError && e.status === 503) {
        warming = true;
        const body = e.body as { elapsed_seconds?: number } | undefined;
        warmingElapsed = body?.elapsed_seconds ?? null;
        const delay = Math.min((e.retryAfter ?? 15) * 1000, 20_000);
        pollTimer = setTimeout(load, delay);
        return;
      }
      error = e instanceof Error ? e.message : String(e);
      warming = false;
    } finally {
      if (!warming) loading = false;
    }
  }

  onMount(load);
  onDestroy(() => {
    if (pollTimer) clearTimeout(pollTimer);
  });

  function fmtAvg(v: number | null): string {
    if (v === null || v === undefined) return '—';
    return v.toFixed(3).replace(/^0/, '');
  }

  function fmtNum(v: number | null | undefined, digits = 2): string {
    if (v === null || v === undefined) return '—';
    return v.toFixed(digits);
  }

  function fmtPct(v: number | null): string {
    if (v === null || v === undefined) return '—';
    return v.toFixed(1) + '%';
  }

  $: powerHand = (data?.today ?? [])
    .filter((r) => r.power_hand_edge)
    .sort((a, b) => (b.iso_vs_hand ?? 0) - (a.iso_vs_hand ?? 0));

  $: barrelEdges = (data?.today ?? [])
    .filter((r) => r.barrel_edge)
    .sort((a, b) => (b.barrel_pct ?? 0) - (a.barrel_pct ?? 0));

  $: parkBoost = (data?.today ?? [])
    .filter((r) => r.park_boost_edge)
    .sort((a, b) => (b.park_factor ?? 0) - (a.park_factor ?? 0));

  $: bvpHr = (data?.today ?? [])
    .filter((r) => r.bvp_hr_edge)
    .sort((a, b) => (b.bvp_hr ?? 0) - (a.bvp_hr ?? 0));
</script>

<svelte:head><title>Home Runs — Sharp Edge</title></svelte:head>

<div class="space-y-6">
  <div class="flex items-center justify-between">
    <div>
      <h1 class="text-xl font-bold text-white">Home Runs</h1>
      <p class="text-sm text-slate-400 mt-0.5">
        Today's MLB HR probability screen — power matchups, barrels, park boosts
      </p>
    </div>
    <button
      class="px-3 py-1.5 rounded-lg text-sm font-medium bg-surface-700 text-slate-300 hover:bg-surface-600 disabled:opacity-50"
      on:click={load}
      disabled={loading}
    >{loading ? 'Loading…' : 'Refresh'}</button>
  </div>

  {#if error}
    <div class="card border-red-800 bg-red-950/30 text-red-300 text-sm">{error}</div>
  {/if}

  <PickTrackRecord screen="hr" winLabel="HR" />

  {#if loading && !data}
    <div class="card text-slate-400 text-sm flex items-center gap-3">
      <div class="w-4 h-4 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin"></div>
      {#if warming}
        Scraping today's MLB data{warmingElapsed != null ? ` (${Math.round(warmingElapsed)}s elapsed)` : ''}… retrying shortly.
      {:else}
        Loading screen…
      {/if}
    </div>
  {:else if data}
    <!-- Picks -->
    <section class="card overflow-hidden p-0">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Picks</h2>
        <span class="text-xs text-slate-500">
          {data.picks.length} top HR play{data.picks.length === 1 ? '' : 's'} (hot pop + ≥1 edge)
        </span>
      </div>
      {#if data.picks.length === 0}
        <div class="px-5 py-6 text-sm text-slate-500">No picks today.</div>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                <th class="text-right px-3 py-3">Score</th>
                <th class="text-left px-3 py-3">Batter</th>
                <th class="text-left px-3 py-3">Team</th>
                <th class="text-left px-3 py-3">Opp SP</th>
                <th class="text-center px-3 py-3">Hand</th>
                <th class="text-right px-3 py-3">Park</th>
                <th class="text-right px-3 py-3">ISO vs Hand</th>
                <th class="text-right px-3 py-3">Barrel%</th>
                <th class="text-right px-3 py-3">HR 15d</th>
                <th class="text-right px-3 py-3">SP HR/9 L3</th>
                <th class="text-right px-3 py-3">BvP HR</th>
                <th class="text-left px-3 py-3">Tags</th>
                <th class="text-right px-3 py-3">Time</th>
              </tr>
            </thead>
            <tbody>
              {#each data.picks as r (r.batter + r.opposing_pitcher)}
                <tr class="border-b border-border/50 hover:bg-surface-600/30">
                  <td class="px-3 py-2.5 text-right tabular-nums font-bold text-emerald-400">{fmtNum(r.hr_score, 1)}</td>
                  <td class="px-3 py-2.5 text-slate-200 font-medium">{r.batter}</td>
                  <td class="px-3 py-2.5 text-slate-400">{r.team}</td>
                  <td class="px-3 py-2.5 text-slate-300">{r.opposing_pitcher}</td>
                  <td class="px-3 py-2.5 text-center text-slate-400">{r.p_hand ?? '—'}</td>
                  <td class="px-3 py-2.5 text-right tabular-nums text-slate-300">{fmtNum(r.park_factor, 0)}</td>
                  <td class="px-3 py-2.5 text-right tabular-nums text-slate-300">{fmtAvg(r.iso_vs_hand)}</td>
                  <td class="px-3 py-2.5 text-right tabular-nums text-slate-300">{fmtPct(r.barrel_pct)}</td>
                  <td class="px-3 py-2.5 text-right tabular-nums text-slate-300">{r.hr_last_15d}</td>
                  <td class="px-3 py-2.5 text-right tabular-nums text-slate-300">{fmtNum(r.p_hr9_l3, 2)}</td>
                  <td class="px-3 py-2.5 text-right tabular-nums text-slate-300">
                    {r.bvp_hr}<span class="text-xs text-slate-500"> /{r.bvp_pa}</span>
                  </td>
                  <td class="px-3 py-2.5">
                    {#each r.tags.split(',').filter(Boolean) as tag}
                      <span class="inline-block px-2 py-0.5 mr-1 text-xs rounded bg-indigo-600/20 text-indigo-300 border border-indigo-600/30 whitespace-nowrap">{tag}</span>
                    {/each}
                  </td>
                  <td class="px-3 py-2.5 text-right text-slate-400 text-xs">{r.game_time}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <!-- Edge subsets, two-up on wider screens -->
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <section class="card overflow-hidden p-0">
        <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
          <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Power + Hand</h2>
          <span class="text-xs text-slate-500">{powerHand.length} · ISO vs hand ≥ .250 & SP HR/9 L3 ≥ 1.5</span>
        </div>
        {#if powerHand.length === 0}
          <div class="px-5 py-6 text-sm text-slate-500">None.</div>
        {:else}
          <div class="overflow-x-auto">
            <table class="w-full text-sm">
              <thead>
                <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                  <th class="text-left px-3 py-3">Batter</th>
                  <th class="text-left px-3 py-3">Opp SP</th>
                  <th class="text-right px-3 py-3">ISO vs H</th>
                  <th class="text-right px-3 py-3">SP HR/9 L3</th>
                  <th class="text-right px-3 py-3">Time</th>
                </tr>
              </thead>
              <tbody>
                {#each powerHand as r (r.batter + r.opposing_pitcher)}
                  <tr class="border-b border-border/50 hover:bg-surface-600/30">
                    <td class="px-3 py-2 text-slate-200">{r.batter}</td>
                    <td class="px-3 py-2 text-slate-400">{r.opposing_pitcher}</td>
                    <td class="px-3 py-2 text-right tabular-nums text-emerald-400">{fmtAvg(r.iso_vs_hand)}</td>
                    <td class="px-3 py-2 text-right tabular-nums text-slate-300">{fmtNum(r.p_hr9_l3, 2)}</td>
                    <td class="px-3 py-2 text-right text-slate-400 text-xs">{r.game_time}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </section>

      <section class="card overflow-hidden p-0">
        <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
          <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Barrel Edge</h2>
          <span class="text-xs text-slate-500">{barrelEdges.length} · batter barrel% ≥ 12 & SP barrel% allowed ≥ 10</span>
        </div>
        {#if barrelEdges.length === 0}
          <div class="px-5 py-6 text-sm text-slate-500">None.</div>
        {:else}
          <div class="overflow-x-auto">
            <table class="w-full text-sm">
              <thead>
                <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                  <th class="text-left px-3 py-3">Batter</th>
                  <th class="text-left px-3 py-3">Opp SP</th>
                  <th class="text-right px-3 py-3">Barrel%</th>
                  <th class="text-right px-3 py-3">SP Barrel%</th>
                  <th class="text-right px-3 py-3">Time</th>
                </tr>
              </thead>
              <tbody>
                {#each barrelEdges as r (r.batter + r.opposing_pitcher)}
                  <tr class="border-b border-border/50 hover:bg-surface-600/30">
                    <td class="px-3 py-2 text-slate-200">{r.batter}</td>
                    <td class="px-3 py-2 text-slate-400">{r.opposing_pitcher}</td>
                    <td class="px-3 py-2 text-right tabular-nums text-emerald-400">{fmtPct(r.barrel_pct)}</td>
                    <td class="px-3 py-2 text-right tabular-nums text-slate-300">{fmtPct(r.p_barrel_pct)}</td>
                    <td class="px-3 py-2 text-right text-slate-400 text-xs">{r.game_time}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </section>

      <section class="card overflow-hidden p-0">
        <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
          <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Park Boost</h2>
          <span class="text-xs text-slate-500">{parkBoost.length} · park factor ≥ 110 & ISO career ≥ .200</span>
        </div>
        {#if parkBoost.length === 0}
          <div class="px-5 py-6 text-sm text-slate-500">None.</div>
        {:else}
          <div class="overflow-x-auto">
            <table class="w-full text-sm">
              <thead>
                <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                  <th class="text-left px-3 py-3">Batter</th>
                  <th class="text-left px-3 py-3">Venue</th>
                  <th class="text-right px-3 py-3">PF</th>
                  <th class="text-right px-3 py-3">ISO</th>
                  <th class="text-right px-3 py-3">Time</th>
                </tr>
              </thead>
              <tbody>
                {#each parkBoost as r (r.batter + r.opposing_pitcher)}
                  <tr class="border-b border-border/50 hover:bg-surface-600/30">
                    <td class="px-3 py-2 text-slate-200">{r.batter}</td>
                    <td class="px-3 py-2 text-slate-400">{r.venue ?? '—'}</td>
                    <td class="px-3 py-2 text-right tabular-nums text-emerald-400">{fmtNum(r.park_factor, 0)}</td>
                    <td class="px-3 py-2 text-right tabular-nums text-slate-300">{fmtAvg(r.iso_career)}</td>
                    <td class="px-3 py-2 text-right text-slate-400 text-xs">{r.game_time}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </section>

      <section class="card overflow-hidden p-0">
        <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
          <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">BvP HR</h2>
          <span class="text-xs text-slate-500">{bvpHr.length} · ≥1 HR vs this SP with ≥5 PA</span>
        </div>
        {#if bvpHr.length === 0}
          <div class="px-5 py-6 text-sm text-slate-500">None.</div>
        {:else}
          <div class="overflow-x-auto">
            <table class="w-full text-sm">
              <thead>
                <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                  <th class="text-left px-3 py-3">Batter</th>
                  <th class="text-left px-3 py-3">Opp SP</th>
                  <th class="text-right px-3 py-3">BvP HR</th>
                  <th class="text-right px-3 py-3">PA</th>
                  <th class="text-right px-3 py-3">Time</th>
                </tr>
              </thead>
              <tbody>
                {#each bvpHr as r (r.batter + r.opposing_pitcher)}
                  <tr class="border-b border-border/50 hover:bg-surface-600/30">
                    <td class="px-3 py-2 text-slate-200">{r.batter}</td>
                    <td class="px-3 py-2 text-slate-400">{r.opposing_pitcher}</td>
                    <td class="px-3 py-2 text-right tabular-nums text-emerald-400">{r.bvp_hr}</td>
                    <td class="px-3 py-2 text-right tabular-nums text-slate-400">{r.bvp_pa}</td>
                    <td class="px-3 py-2 text-right text-slate-400 text-xs">{r.game_time}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </section>
    </div>

    <!-- Hot Pop -->
    <section class="card overflow-hidden p-0">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Hot Pop</h2>
        <span class="text-xs text-slate-500">{data.hot_pop.length} players with ≥2 HR in last 15d</span>
      </div>
      <div class="overflow-x-auto max-h-[500px] overflow-y-auto">
        <table class="w-full text-sm">
          <thead class="sticky top-0 bg-surface-800">
            <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
              <th class="text-left px-4 py-3">Batter</th>
              <th class="text-left px-4 py-3">Team</th>
              <th class="text-right px-4 py-3">HR 15d</th>
              <th class="text-right px-4 py-3">HR 30d</th>
              <th class="text-right px-4 py-3">ISO Career</th>
              <th class="text-right px-4 py-3">Barrel%</th>
              <th class="text-left px-4 py-3">Opp SP</th>
              <th class="text-right px-4 py-3">Time</th>
            </tr>
          </thead>
          <tbody>
            {#each data.hot_pop as r (r.batter)}
              <tr class="border-b border-border/50 hover:bg-surface-600/30">
                <td class="px-4 py-2 text-slate-200">{r.batter}</td>
                <td class="px-4 py-2 text-slate-400">{r.team}</td>
                <td class="px-4 py-2 text-right tabular-nums text-emerald-400 font-medium">{r.hr_last_15d}</td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-300">{r.hr_last_30d}</td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-300">{fmtAvg(r.iso_career)}</td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-300">{fmtPct(r.barrel_pct)}</td>
                <td class="px-4 py-2 text-slate-400">{r.opposing_pitcher}</td>
                <td class="px-4 py-2 text-right text-slate-400 text-xs">{r.game_time}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  {/if}
</div>
