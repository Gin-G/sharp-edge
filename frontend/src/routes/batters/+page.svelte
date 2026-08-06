<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { getBatterScreen, ApiError } from '$lib/api';
  import { cached, peek } from '$lib/cache';
  import type { BatterScreen, BatterRow, HotBatRow } from '$lib/types';
  import PickTrackRecord from '$lib/components/PickTrackRecord.svelte';

  const CACHE_KEY = 'batters:screen';

  // Seed from the module cache so switching back to this tab paints the board
  // immediately rather than re-running the whole fetch-and-spin cycle.
  let data: BatterScreen | null = peek<BatterScreen>(CACHE_KEY);
  let loading = data === null;
  let error = '';
  let warming = false;
  let warmingElapsed: number | null = null;
  let pollTimer: ReturnType<typeof setTimeout> | null = null;

  async function load(force = false) {
    loading = true;
    error = '';
    try {
      data = await cached(CACHE_KEY, getBatterScreen, { force });
      warming = false;
      warmingElapsed = null;
    } catch (e) {
      if (e instanceof ApiError && e.status === 503) {
        warming = true;
        // Body carries elapsed_seconds for the spinner.
        const body = e.body as { elapsed_seconds?: number } | undefined;
        warmingElapsed = body?.elapsed_seconds ?? null;
        // Respect Retry-After (defaults to 15s); cap aggressively low while warming.
        const delay = Math.min((e.retryAfter ?? 15) * 1000, 20_000);
        pollTimer = setTimeout(load, delay);
        return; // keep `loading` true while we wait
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

  function fmtNum(v: number | null, digits = 2): string {
    if (v === null || v === undefined) return '—';
    return v.toFixed(digits);
  }

  // Colour the starter's recent contact profile. SHARP is the warning: a
  // pitcher holding lineups to a low average is the wrong side of a
  // "records a hit" bet however good the batter's history looks.
  const BAND_CLASS: Record<string, string> = {
    SHARP: 'bg-red-600/20 text-red-300 border-red-600/30',
    HITTABLE: 'bg-emerald-600/20 text-emerald-300 border-emerald-600/30',
    NEUTRAL: 'bg-surface-600/40 text-slate-400 border-border',
    UNKNOWN: 'bg-surface-600/40 text-slate-500 border-border'
  };

  $: bvpEdges = (data?.today ?? [])
    .filter((r) => r.bvp_edge)
    .sort((a, b) => (b.bvp_avg ?? 0) - (a.bvp_avg ?? 0));

  $: handSlumpEdges = (data?.today ?? [])
    .filter((r) => r.hand_slump_edge)
    .sort((a, b) => (b.vs_hand_avg ?? 0) - (a.vs_hand_avg ?? 0));

  // Qualified on the batter side but dropped because the opposing starter has
  // been suppressing hits. Shown so the veto is visible rather than silent.
  $: vetoed = (data?.today ?? [])
    .filter((r) => r.p_sharp && r.is_hot && (r.bvp_edge || r.hand_slump_edge))
    .sort((a, b) => (a.p_l3_h9 ?? 99) - (b.p_l3_h9 ?? 99));

  // Candidate pool for the experimental edge (hot bat vs. a starter who's
  // been getting hit). Not picks yet — see EXPERIMENTS.md.
  $: hotVsHittable = (data?.today ?? [])
    .filter((r) => r.hittable_sp_edge && !r.bvp_edge && !r.hand_slump_edge)
    .sort((a, b) => (b.p_l3_h9 ?? 0) - (a.p_l3_h9 ?? 0));
</script>

<svelte:head><title>Batters — Sharp Edge</title></svelte:head>

<div class="space-y-6">
  <div class="flex items-center justify-between">
    <div>
      <h1 class="text-xl font-bold text-white">Batters</h1>
      <p class="text-sm text-slate-400 mt-0.5">
        Today's MLB screen — hot bats, BvP edges, and pitcher form
      </p>
    </div>
    <button
      class="px-3 py-1.5 rounded-lg text-sm font-medium bg-surface-700 text-slate-300 hover:bg-surface-600 disabled:opacity-50"
      on:click={() => load(true)}
      disabled={loading}
    >{loading ? 'Loading…' : 'Refresh'}</button>
  </div>

  {#if error}
    <div class="card border-red-800 bg-red-950/30 text-red-300 text-sm">{error}</div>
  {/if}

  <PickTrackRecord screen="batter" winLabel="Hit" />

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
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
          Picks
        </h2>
        <span class="text-xs text-slate-500">
          {data.picks.length} hot bat{data.picks.length === 1 ? '' : 's'} with ≥1 edge
        </span>
      </div>
      {#if data.picks.length === 0}
        <div class="px-5 py-6 text-sm text-slate-500">No picks today.</div>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                <th class="text-left px-4 py-3">Batter</th>
                <th class="text-left px-4 py-3">Team</th>
                <th class="text-left px-4 py-3">Opp SP</th>
                <th class="text-center px-4 py-3">Hand</th>
                <th class="text-right px-4 py-3">r7 AVG</th>
                <th class="text-right px-4 py-3">vs Hand</th>
                <th class="text-right px-4 py-3">BvP AVG</th>
                <th class="text-right px-4 py-3">BvP PA</th>
                <th class="text-right px-4 py-3">SP L3 ERA</th>
                <th class="text-right px-4 py-3">SP L3 H/9</th>
                <th class="text-right px-4 py-3">SP L3 BAA</th>
                <th class="text-center px-4 py-3">SP Form</th>
                <th class="text-left px-4 py-3">Tags</th>
                <th class="text-right px-4 py-3">Time</th>
              </tr>
            </thead>
            <tbody>
              {#each data.picks as r (r.batter + r.opposing_pitcher)}
                <tr class="border-b border-border/50 hover:bg-surface-600/30">
                  <td class="px-4 py-2.5 text-slate-200 font-medium">{r.batter}</td>
                  <td class="px-4 py-2.5 text-slate-400">{r.team}</td>
                  <td class="px-4 py-2.5 text-slate-300">{r.opposing_pitcher}</td>
                  <td class="px-4 py-2.5 text-center text-slate-400">{r.p_hand ?? '—'}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-200">{fmtAvg(r.recent_avg)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">
                    {fmtAvg(r.vs_hand_avg)}<span class="text-xs text-slate-500"> ({r.vs_hand_pa})</span>
                  </td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-emerald-400 font-medium">{fmtAvg(r.bvp_avg)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{r.bvp_pa}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">{fmtNum(r.p_l3_era)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">
                    {fmtNum(r.p_l3_h9)}<span class="text-xs text-slate-500"> ({r.p_l3_hits ?? '—'}H)</span>
                  </td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">{fmtAvg(r.p_l3_baa)}</td>
                  <td class="px-4 py-2.5 text-center">
                    <span class="inline-block px-2 py-0.5 text-xs rounded border {BAND_CLASS[r.p_form] ?? BAND_CLASS.UNKNOWN}">{r.p_form}</span>
                  </td>
                  <td class="px-4 py-2.5">
                    {#each r.tags.split(',').filter(Boolean) as tag}
                      <span class="inline-block px-2 py-0.5 mr-1 text-xs rounded bg-indigo-600/20 text-indigo-300 border border-indigo-600/30">{tag}</span>
                    {/each}
                  </td>
                  <td class="px-4 py-2.5 text-right text-slate-400 text-xs">{r.game_time}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <!-- Vetoed by pitcher form -->
    <section class="card overflow-hidden p-0">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
          Held Back — Sharp Starter
        </h2>
        <span class="text-xs text-slate-500">
          {vetoed.length} qualified bat{vetoed.length === 1 ? '' : 's'} dropped: SP suppressing hits
        </span>
      </div>
      {#if vetoed.length === 0}
        <div class="px-5 py-6 text-sm text-slate-500">None — no qualified bat is facing a sharp starter today.</div>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                <th class="text-left px-4 py-3">Batter</th>
                <th class="text-left px-4 py-3">Team</th>
                <th class="text-left px-4 py-3">Opp SP</th>
                <th class="text-right px-4 py-3">r7 AVG</th>
                <th class="text-right px-4 py-3">BvP AVG</th>
                <th class="text-right px-4 py-3">SP L3 H/9</th>
                <th class="text-right px-4 py-3">SP L3 BAA</th>
                <th class="text-right px-4 py-3">SP L3 ERA</th>
                <th class="text-right px-4 py-3">Season BAA</th>
                <th class="text-right px-4 py-3">Time</th>
              </tr>
            </thead>
            <tbody>
              {#each vetoed as r (r.batter + r.opposing_pitcher)}
                <tr class="border-b border-border/50 hover:bg-surface-600/30 opacity-70">
                  <td class="px-4 py-2.5 text-slate-300 font-medium">{r.batter}</td>
                  <td class="px-4 py-2.5 text-slate-400">{r.team}</td>
                  <td class="px-4 py-2.5 text-slate-300">{r.opposing_pitcher}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">{fmtAvg(r.recent_avg)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{fmtAvg(r.bvp_avg)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-red-300 font-medium">{fmtNum(r.p_l3_h9)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-red-300">{fmtAvg(r.p_l3_baa)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{fmtNum(r.p_l3_era)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{fmtAvg(r.p_season_baa)}</td>
                  <td class="px-4 py-2.5 text-right text-slate-400 text-xs">{r.game_time}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <!-- Experimental: hot bat vs. a starter who's been getting hit -->
    <section class="card overflow-hidden p-0">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
          Hot Bats vs Hittable Starters
          <span class="ml-2 normal-case font-normal text-xs text-amber-400/80">experimental — not picks</span>
        </h2>
        <span class="text-xs text-slate-500">
          {hotVsHittable.length} bat{hotVsHittable.length === 1 ? '' : 's'} with no BvP/hand edge
        </span>
      </div>
      {#if hotVsHittable.length === 0}
        <div class="px-5 py-6 text-sm text-slate-500">None.</div>
      {:else}
        <div class="overflow-x-auto max-h-[420px] overflow-y-auto">
          <table class="w-full text-sm">
            <thead class="sticky top-0 bg-surface-800">
              <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                <th class="text-left px-4 py-3">Batter</th>
                <th class="text-left px-4 py-3">Team</th>
                <th class="text-left px-4 py-3">Opp SP</th>
                <th class="text-right px-4 py-3">r7 AVG</th>
                <th class="text-right px-4 py-3">SP L3 H</th>
                <th class="text-right px-4 py-3">SP L3 H/9</th>
                <th class="text-right px-4 py-3">SP L3 BAA</th>
                <th class="text-right px-4 py-3">SP L3 ERA</th>
                <th class="text-right px-4 py-3">Time</th>
              </tr>
            </thead>
            <tbody>
              {#each hotVsHittable as r (r.batter + r.opposing_pitcher)}
                <tr class="border-b border-border/50 hover:bg-surface-600/30">
                  <td class="px-4 py-2 text-slate-200 font-medium">{r.batter}</td>
                  <td class="px-4 py-2 text-slate-400">{r.team}</td>
                  <td class="px-4 py-2 text-slate-300">{r.opposing_pitcher}</td>
                  <td class="px-4 py-2 text-right tabular-nums text-slate-200">{fmtAvg(r.recent_avg)}</td>
                  <td class="px-4 py-2 text-right tabular-nums text-slate-400">{r.p_l3_hits ?? '—'}</td>
                  <td class="px-4 py-2 text-right tabular-nums text-emerald-400 font-medium">{fmtNum(r.p_l3_h9)}</td>
                  <td class="px-4 py-2 text-right tabular-nums text-emerald-400">{fmtAvg(r.p_l3_baa)}</td>
                  <td class="px-4 py-2 text-right tabular-nums text-slate-400">{fmtNum(r.p_l3_era)}</td>
                  <td class="px-4 py-2 text-right text-slate-400 text-xs">{r.game_time}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <!-- BvP edges -->
    <section class="card overflow-hidden p-0">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
          BvP Edges
        </h2>
        <span class="text-xs text-slate-500">{bvpEdges.length} batter{bvpEdges.length === 1 ? '' : 's'} ≥.400 vs today's SP</span>
      </div>
      {#if bvpEdges.length === 0}
        <div class="px-5 py-6 text-sm text-slate-500">None.</div>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                <th class="text-left px-4 py-3">Batter</th>
                <th class="text-left px-4 py-3">Team</th>
                <th class="text-left px-4 py-3">Opp SP</th>
                <th class="text-right px-4 py-3">BvP AVG</th>
                <th class="text-right px-4 py-3">PA</th>
                <th class="text-right px-4 py-3">H</th>
                <th class="text-right px-4 py-3">r7 AVG</th>
                <th class="text-right px-4 py-3">Time</th>
              </tr>
            </thead>
            <tbody>
              {#each bvpEdges as r (r.batter + r.opposing_pitcher)}
                <tr class="border-b border-border/50 hover:bg-surface-600/30">
                  <td class="px-4 py-2.5 text-slate-200 font-medium">{r.batter}</td>
                  <td class="px-4 py-2.5 text-slate-400">{r.team}</td>
                  <td class="px-4 py-2.5 text-slate-300">{r.opposing_pitcher}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-emerald-400 font-medium">{fmtAvg(r.bvp_avg)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{r.bvp_pa}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{r.bvp_hits}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">{fmtAvg(r.recent_avg)}</td>
                  <td class="px-4 py-2.5 text-right text-slate-400 text-xs">{r.game_time}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <!-- Hand+Slump edges -->
    <section class="card overflow-hidden p-0">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
          Hand + Slump Edges
        </h2>
        <span class="text-xs text-slate-500">{handSlumpEdges.length} batter{handSlumpEdges.length === 1 ? '' : 's'} ≥.400 vs hand & SP giving up runs or hits (not sharp)</span>
      </div>
      {#if handSlumpEdges.length === 0}
        <div class="px-5 py-6 text-sm text-slate-500">None.</div>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                <th class="text-left px-4 py-3">Batter</th>
                <th class="text-left px-4 py-3">Team</th>
                <th class="text-left px-4 py-3">Opp SP</th>
                <th class="text-center px-4 py-3">Hand</th>
                <th class="text-right px-4 py-3">vs Hand AVG</th>
                <th class="text-right px-4 py-3">PA</th>
                <th class="text-right px-4 py-3">SP L3 ERA</th>
                <th class="text-right px-4 py-3">SP L3 H/9</th>
                <th class="text-right px-4 py-3">SP L3 IP</th>
                <th class="text-right px-4 py-3">Time</th>
              </tr>
            </thead>
            <tbody>
              {#each handSlumpEdges as r (r.batter + r.opposing_pitcher)}
                <tr class="border-b border-border/50 hover:bg-surface-600/30">
                  <td class="px-4 py-2.5 text-slate-200 font-medium">{r.batter}</td>
                  <td class="px-4 py-2.5 text-slate-400">{r.team}</td>
                  <td class="px-4 py-2.5 text-slate-300">{r.opposing_pitcher}</td>
                  <td class="px-4 py-2.5 text-center text-slate-400">{r.p_hand ?? '—'}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-emerald-400 font-medium">{fmtAvg(r.vs_hand_avg)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{r.vs_hand_pa}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">{fmtNum(r.p_l3_era)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">{fmtNum(r.p_l3_h9)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{fmtNum(r.p_l3_ip, 1)}</td>
                  <td class="px-4 py-2.5 text-right text-slate-400 text-xs">{r.game_time}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <!-- Hot Bats -->
    <section class="card overflow-hidden p-0">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Hot Bats</h2>
        <span class="text-xs text-slate-500">{data.hot_bats.length} players hitting >.300 over last 7d</span>
      </div>
      <div class="overflow-x-auto max-h-[600px] overflow-y-auto">
        <table class="w-full text-sm">
          <thead class="sticky top-0 bg-surface-800">
            <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
              <th class="text-left px-4 py-3">Batter</th>
              <th class="text-left px-4 py-3">Team</th>
              <th class="text-right px-4 py-3">r7 AVG</th>
              <th class="text-right px-4 py-3">AB</th>
              <th class="text-right px-4 py-3">H</th>
              <th class="text-right px-4 py-3">HR</th>
              <th class="text-right px-4 py-3">OBP</th>
              <th class="text-right px-4 py-3">OPS</th>
            </tr>
          </thead>
          <tbody>
            {#each data.hot_bats as r (r.batter)}
              <tr class="border-b border-border/50 hover:bg-surface-600/30">
                <td class="px-4 py-2 text-slate-200 font-medium">{r.batter}</td>
                <td class="px-4 py-2 text-slate-400">{r.team}</td>
                <td class="px-4 py-2 text-right tabular-nums text-emerald-400 font-medium">{fmtAvg(r.recent_avg)}</td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-400">{r.recent_ab}</td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-400">{r.H}</td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-400">{r.HR}</td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-300">{fmtAvg(r.OBP)}</td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-300">{fmtNum(r.OPS, 3)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  {/if}
</div>
