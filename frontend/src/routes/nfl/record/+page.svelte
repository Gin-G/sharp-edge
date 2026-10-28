<script lang="ts">
  /** Every recommendation the NFL screens have made, graded.
   *
   *  One unit flat on each, at the FanDuel price recorded before kickoff.
   *  Split at the week the Finding 14 rules shipped, because the record before
   *  it is what those rules were written to fix, and blending the two would
   *  hide whether they did.
   */
  import { onMount } from 'svelte';
  import { getNflRecommendations } from '$lib/api';
  import { fmtOdds, fmtPct, fmtSigned, fmtKickoff } from '$lib/nflScreen';
  import type { NflRecommendations, NflLedgerBucket, NflRecType } from '$lib/types';

  let data: NflRecommendations | null = null;
  let loading = true;
  let error = '';

  onMount(async () => {
    try {
      data = await getNflRecommendations();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  });

  const TYPES: { key: NflRecType; label: string }[] = [
    { key: 'prop', label: 'Props' },
    { key: 'touchdown', label: 'Touchdowns' },
    { key: 'moneyline', label: 'Moneylines' },
  ];
  const TYPE_LABEL: Record<NflRecType, string> = { prop: 'Prop', touchdown: 'TD', moneyline: 'ML' };

  let era: 'current' | 'before' | 'all' = 'current';
  let type: NflRecType | 'all' = 'all';
  let status: 'all' | 'settled' | 'pending' = 'all';

  $: summary = data ? (era === 'current' ? data.current : era === 'before' ? data.before : data.all_time) : null;
  $: rows = (data?.ledger ?? []).filter((e) =>
    (era === 'all' || (era === 'current') === e.current_rules) &&
    (type === 'all' || e.type === type) &&
    (status === 'all' || (status === 'pending') === !e.result));
  $: weeks = (data?.by_week ?? []).filter((w) =>
    era === 'all' || (era === 'current') === w.current_rules);
  $: since = data ? `week ${data.rules_since.week}, ${data.rules_since.season}` : '';

  function rec(b: NflLedgerBucket | undefined): string {
    if (!b) return '—';
    return `${b.wins}-${b.losses}${b.pushes ? `-${b.pushes}` : ''}`;
  }
  const units = (v: number | null | undefined) => (v == null ? '—' : `${fmtSigned(v, 2)}u`);
  const roi = (v: number | null | undefined) => (v == null ? '—' : `${fmtSigned(v)}%`);
  function tone(v: number | null | undefined): string {
    if (v == null || v === 0) return 'text-slate-400';
    return v > 0 ? 'text-emerald-400' : 'text-red-400';
  }
  function resultClass(r: string | null): string {
    if (r === 'WIN') return 'bg-emerald-600/20 text-emerald-300 border-emerald-600/30';
    if (r === 'LOSS') return 'bg-rose-600/20 text-rose-300 border-rose-600/30';
    if (r === 'PUSH' || r === 'VOID') return 'bg-surface-600 text-slate-400 border-border';
    return 'bg-surface-600/40 text-slate-500 border-border';
  }
  const actual = (v: number | string | null) =>
    v == null ? '' : typeof v === 'number' ? `${Number.isInteger(v) ? v : v.toFixed(1)}` : v;

  const btn = (on: boolean) =>
    `px-2.5 py-1 rounded text-xs font-medium ${on ? 'bg-indigo-600 text-white' : 'bg-surface-700 text-slate-400 hover:bg-surface-600'}`;
</script>

<svelte:head><title>NFL Record — Sharp Edge</title></svelte:head>

<div class="space-y-6">
  <div class="flex items-center justify-between flex-wrap gap-3">
    <p class="text-sm text-slate-400">
      Every recommendation, one unit each at the FanDuel price recorded before kickoff.
    </p>
    <div class="flex gap-1">
      <button class={btn(era === 'current')} on:click={() => (era = 'current')}>Current rules</button>
      <button class={btn(era === 'before')} on:click={() => (era = 'before')}>Before</button>
      <button class={btn(era === 'all')} on:click={() => (era = 'all')}>All time</button>
    </div>
  </div>

  {#if error}
    <div class="card border-red-800 bg-red-950/30 text-red-300 text-sm">{error}</div>
  {/if}

  {#if loading}
    <div class="card text-slate-400 text-sm">Loading record…</div>
  {:else if data && summary}
    <p class="text-xs text-slate-500 -mt-3">
      {#if era === 'current'}Since {since}: edge cap, no rushing yards, touchdown and moneyline picks.
      {:else if era === 'before'}Before {since} — the record the current rules were written to fix.
      {:else}Everything recorded, both rule sets together.{/if}
    </p>

    <!-- Headline: record, units, ROI. -->
    <div class="grid gap-3 grid-cols-2 md:grid-cols-4">
      <div class="card py-3">
        <div class="text-xs uppercase tracking-wider text-slate-500">Record</div>
        <div class="text-2xl tabular-nums text-slate-100">{rec(summary.overall)}</div>
        <div class="text-xs text-slate-500">{summary.overall.pending} pending</div>
      </div>
      <div class="card py-3">
        <div class="text-xs uppercase tracking-wider text-slate-500">Hit rate</div>
        <div class="text-2xl tabular-nums text-slate-100">{summary.overall.hit_rate != null ? `${summary.overall.hit_rate}%` : '—'}</div>
        <div class="text-xs text-slate-500">{summary.overall.bets} bets</div>
      </div>
      <div class="card py-3">
        <div class="text-xs uppercase tracking-wider text-slate-500">Units</div>
        <div class="text-2xl tabular-nums {tone(summary.overall.units)}">{units(summary.overall.units)}</div>
        <div class="text-xs text-slate-500">1u flat stake</div>
      </div>
      <div class="card py-3">
        <div class="text-xs uppercase tracking-wider text-slate-500">ROI</div>
        <div class="text-2xl tabular-nums {tone(summary.overall.roi)}">{roi(summary.overall.roi)}</div>
        <div class="text-xs text-slate-500">on settled bets</div>
      </div>
    </div>

    <!-- By type and by week. -->
    <div class="grid gap-4 md:grid-cols-2">
      <section class="card p-0 overflow-hidden">
        <h2 class="px-5 py-3 border-b border-border text-sm font-semibold text-slate-300 uppercase tracking-wider">By type</h2>
        <table class="w-full text-sm tabular-nums">
          <thead><tr class="text-xs text-slate-500 uppercase tracking-wider">
            <th class="text-left px-5 py-2 font-medium"></th>
            <th class="text-right px-2 py-2 font-medium">Record</th>
            <th class="text-right px-2 py-2 font-medium">Hit</th>
            <th class="text-right px-2 py-2 font-medium">Units</th>
            <th class="text-right px-5 py-2 font-medium">ROI</th>
          </tr></thead>
          <tbody>
            {#each TYPES as t}
              {@const b = summary.by_type[t.key]}
              <tr class="border-t border-border/40">
                <td class="px-5 py-2 text-slate-300">{t.label}
                  {#if b.pending}<span class="text-xs text-slate-500"> · {b.pending} pending</span>{/if}</td>
                <td class="px-2 py-2 text-right text-slate-200">{rec(b)}</td>
                <td class="px-2 py-2 text-right text-slate-400">{b.hit_rate != null ? `${b.hit_rate}%` : '—'}</td>
                <td class="px-2 py-2 text-right {tone(b.units)}">{units(b.units)}</td>
                <td class="px-5 py-2 text-right {tone(b.roi)}">{roi(b.roi)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </section>

      <section class="card p-0 overflow-hidden">
        <h2 class="px-5 py-3 border-b border-border text-sm font-semibold text-slate-300 uppercase tracking-wider">By week</h2>
        {#if weeks.length === 0}
          <div class="px-5 py-4 text-sm text-slate-500">Nothing recorded yet.</div>
        {:else}
          <table class="w-full text-sm tabular-nums">
            <thead><tr class="text-xs text-slate-500 uppercase tracking-wider">
              <th class="text-left px-5 py-2 font-medium"></th>
              <th class="text-right px-2 py-2 font-medium">Record</th>
              <th class="text-right px-2 py-2 font-medium">Units</th>
              <th class="text-right px-5 py-2 font-medium">ROI</th>
            </tr></thead>
            <tbody>
              {#each weeks as w}
                <tr class="border-t border-border/40">
                  <td class="px-5 py-2 text-slate-300">Week {w.week}
                    {#if w.pending}<span class="text-xs text-slate-500"> · {w.pending} pending</span>{/if}</td>
                  <td class="px-2 py-2 text-right text-slate-200">{rec(w)}</td>
                  <td class="px-2 py-2 text-right {tone(w.units)}">{units(w.units)}</td>
                  <td class="px-5 py-2 text-right {tone(w.roi)}">{roi(w.roi)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}
      </section>
    </div>

    <!-- The ledger itself. -->
    <section class="card p-0 overflow-hidden">
      <div class="px-5 py-3 border-b border-border flex items-center justify-between flex-wrap gap-2">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Every pick ({rows.length})</h2>
        <div class="flex gap-1 flex-wrap">
          <button class={btn(type === 'all')} on:click={() => (type = 'all')}>All</button>
          {#each TYPES as t}
            <button class={btn(type === t.key)} on:click={() => (type = t.key)}>{t.label}</button>
          {/each}
          <span class="w-2"></span>
          <button class={btn(status === 'all')} on:click={() => (status = 'all')}>Any</button>
          <button class={btn(status === 'settled')} on:click={() => (status = 'settled')}>Settled</button>
          <button class={btn(status === 'pending')} on:click={() => (status = 'pending')}>Pending</button>
        </div>
      </div>
      {#if rows.length === 0}
        <div class="px-5 py-4 text-sm text-slate-500">No picks match.</div>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full text-sm tabular-nums">
            <thead><tr class="text-xs text-slate-500 uppercase tracking-wider">
              <th class="text-left px-5 py-2 font-medium">Wk</th>
              <th class="text-left px-2 py-2 font-medium">Pick</th>
              <th class="text-right px-2 py-2 font-medium">Odds</th>
              <th class="text-right px-2 py-2 font-medium" title="Our probability when recorded">Ours</th>
              <th class="text-right px-2 py-2 font-medium">Edge</th>
              <th class="text-left px-2 py-2 font-medium">Result</th>
              <th class="text-right px-5 py-2 font-medium">Units</th>
            </tr></thead>
            <tbody>
              {#each rows as e}
                <tr class="border-t border-border/40">
                  <td class="px-5 py-2 text-slate-500">{e.week}</td>
                  <td class="px-2 py-2">
                    <span class="text-[10px] uppercase px-1.5 py-0.5 rounded bg-surface-700 text-slate-400 mr-1.5">{TYPE_LABEL[e.type]}</span>
                    <span class="text-slate-200">{e.pick}</span>
                    {#if !e.result && e.kickoff}<span class="text-xs text-slate-500"> · {fmtKickoff(e.kickoff)}</span>{/if}
                  </td>
                  <td class="px-2 py-2 text-right text-slate-300">{fmtOdds(e.odds)}</td>
                  <td class="px-2 py-2 text-right text-slate-400">{fmtPct(e.model_p, 0)}</td>
                  <td class="px-2 py-2 text-right text-slate-400">{e.edge_pts != null ? fmtSigned(e.edge_pts) : '—'}</td>
                  <td class="px-2 py-2">
                    <span class="text-xs px-1.5 py-0.5 rounded border {resultClass(e.result)}">{e.result ?? 'PENDING'}</span>
                    {#if actual(e.actual)}<span class="text-xs text-slate-500 ml-1">{actual(e.actual)}</span>{/if}
                  </td>
                  <td class="px-5 py-2 text-right {tone(e.units)}">{units(e.units)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <p class="text-xs text-slate-500">
      A pick can change until it locks: props and touchdowns two hours before kickoff, moneylines at kickoff.
      What's recorded is what stood at that moment. Model leans and the parlay card are left out. Leans were never
      recommended, and the card is built from picks already listed here.
    </p>
  {/if}
</div>
