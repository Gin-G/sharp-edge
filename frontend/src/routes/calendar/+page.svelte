<script lang="ts">
  import { onMount } from 'svelte';
  import CalendarHeatmap from '$lib/components/CalendarHeatmap.svelte';
  import { getCalendar } from '$lib/api';
  import type { CalendarDay } from '$lib/types';

  /** The whole history arrives in one request — it's one row per day — so
   *  switching ranges is a local filter, not a round-trip. */
  let allDays: CalendarDay[] = [];
  let loading = true;
  let error = '';

  type Range = { key: string; label: string; start: string; end: string };

  function iso(d: Date): string {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }

  const today = new Date();
  const todayIso = iso(today);

  const trailing12: Range = (() => {
    const from = new Date(today);
    from.setFullYear(from.getFullYear() - 1);
    from.setDate(from.getDate() + 1);
    return { key: 'r12', label: 'Last 12 months', start: iso(from), end: todayIso };
  })();

  const trailing90: Range = (() => {
    const from = new Date(today);
    from.setDate(from.getDate() - 89);
    return { key: 'r90', label: 'Last 90 days', start: iso(from), end: todayIso };
  })();

  /** Year buttons follow the data, not a hardcoded window — a year only
   *  appears once there are settled bets in it. */
  $: ranges = (() => {
    const years = new Set<number>();
    for (const d of allDays) years.add(Number(d.day.slice(0, 4)));
    years.add(today.getFullYear());
    const yearRanges: Range[] = Array.from(years)
      .sort((a, b) => b - a)
      .map((y) => ({
        key: String(y),
        label: String(y),
        start: `${y}-01-01`,
        // The current year stops at today rather than trailing empty weeks.
        end: y === today.getFullYear() ? todayIso : `${y}-12-31`,
      }));
    return [trailing90, trailing12, ...yearRanges];
  })();

  let selectedKey = trailing12.key;
  $: range = ranges.find((r) => r.key === selectedKey) ?? trailing12;

  $: visible = allDays.filter((d) => d.day >= range.start && d.day <= range.end);

  onMount(async () => {
    try {
      allDays = await getCalendar();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  });

  /** Newest month first — the current month is the one you're checking. */
  $: monthSummary = (() => {
    const map = new Map<string, { wagered: number; net: number; bets: number; wins: number }>();
    for (const d of visible) {
      const month = d.day.slice(0, 7);
      const acc = map.get(month) ?? { wagered: 0, net: 0, bets: 0, wins: 0 };
      acc.wagered += d.wagered;
      acc.net += d.net_profit;
      acc.bets += d.total_bets;
      acc.wins += d.wins;
      map.set(month, acc);
    }
    const thisMonth = todayIso.slice(0, 7);
    return Array.from(map.entries())
      .sort(([a], [b]) => b.localeCompare(a))
      .map(([month, stats]) => ({
        month,
        label: new Date(month + '-01T00:00:00').toLocaleString('en-US', {
          month: 'short',
          year: 'numeric',
        }),
        current: month === thisMonth,
        ...stats,
        roi: stats.wagered ? (stats.net / stats.wagered) * 100 : 0,
      }));
  })();

  $: totals = monthSummary.reduce(
    (a, r) => ({
      net: a.net + r.net,
      wagered: a.wagered + r.wagered,
      bets: a.bets + r.bets,
      wins: a.wins + r.wins,
    }),
    { net: 0, wagered: 0, bets: 0, wins: 0 },
  );

  $: lastActive = allDays.length ? allDays[allDays.length - 1].day : null;
  // Built in JS, not markup — Svelte trims the leading space inside an {#if}.
  $: subtitle = lastActive
    ? `Daily P/L heatmap · last settled ${lastActive}`
    : 'Daily P/L heatmap';
</script>

<svelte:head><title>Calendar — Sharp Edge</title></svelte:head>

<div class="space-y-6">
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div>
      <h1 class="text-xl font-bold text-white">Calendar</h1>
      <p class="text-sm text-slate-400 mt-0.5">{subtitle}</p>
    </div>
    <div class="flex flex-wrap items-center gap-2">
      {#each ranges as r}
        <button
          class="px-3 py-1.5 rounded-lg text-sm font-medium transition-colors
                 {selectedKey === r.key
                   ? 'bg-indigo-600 text-white'
                   : 'bg-surface-700 text-slate-400 hover:text-white hover:bg-surface-600'}"
          on:click={() => (selectedKey = r.key)}
        >{r.label}</button>
      {/each}
    </div>
  </div>

  {#if error}
    <div class="card border-red-800 bg-red-950/30 text-red-300 text-sm">{error}</div>
  {/if}

  <CalendarHeatmap
    data={visible}
    start={range.start}
    end={range.end}
    title="Daily P/L — {range.label}"
    {loading}
  />

  {#if !loading && !error && !monthSummary.length}
    <div class="card text-sm text-slate-400">No settled bets in this range.</div>
  {/if}

  <!-- Monthly summary table -->
  {#if !loading && monthSummary.length}
    <div class="card overflow-hidden p-0">
      <div class="px-5 py-4 border-b border-border">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Monthly Summary</h2>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-border">
              <th class="text-left px-5 py-3 text-xs font-medium text-slate-400 uppercase tracking-wider">Month</th>
              <th class="text-right px-5 py-3 text-xs font-medium text-slate-400 uppercase tracking-wider">Bets</th>
              <th class="text-right px-5 py-3 text-xs font-medium text-slate-400 uppercase tracking-wider">Win%</th>
              <th class="text-right px-5 py-3 text-xs font-medium text-slate-400 uppercase tracking-wider">Wagered</th>
              <th class="text-right px-5 py-3 text-xs font-medium text-slate-400 uppercase tracking-wider">Net P/L</th>
              <th class="text-right px-5 py-3 text-xs font-medium text-slate-400 uppercase tracking-wider">ROI</th>
            </tr>
          </thead>
          <tbody>
            {#each monthSummary as row (row.month)}
              <tr class="border-b border-border/50 hover:bg-surface-600/30 transition-colors
                         {row.current ? 'bg-indigo-950/20' : ''}">
                <td class="px-5 py-3 text-slate-200 font-medium whitespace-nowrap">
                  {row.label}
                  {#if row.current}<span class="ml-2 text-[10px] uppercase tracking-wider text-indigo-400">MTD</span>{/if}
                </td>
                <td class="px-5 py-3 text-right tabular-nums text-slate-300">{row.bets}</td>
                <td class="px-5 py-3 text-right tabular-nums text-slate-300">
                  {row.bets ? ((row.wins / row.bets) * 100).toFixed(1) : '—'}%
                </td>
                <td class="px-5 py-3 text-right tabular-nums text-slate-300">${row.wagered.toFixed(2)}</td>
                <td class="px-5 py-3 text-right tabular-nums font-medium
                  {row.net > 0 ? 'text-emerald-400' : row.net < 0 ? 'text-red-400' : 'text-slate-400'}">
                  {row.net >= 0 ? '+' : ''}{row.net.toFixed(2)}
                </td>
                <td class="px-5 py-3 text-right tabular-nums
                  {row.roi > 0 ? 'text-emerald-400' : row.roi < 0 ? 'text-red-400' : 'text-slate-400'}">
                  {row.roi >= 0 ? '+' : ''}{row.roi.toFixed(1)}%
                </td>
              </tr>
            {/each}
          </tbody>
          <tfoot>
            <tr class="bg-surface-800/50">
              <td class="px-5 py-3 text-slate-400 font-medium">Total</td>
              <td class="px-5 py-3 text-right tabular-nums text-slate-300">{totals.bets}</td>
              <td class="px-5 py-3 text-right tabular-nums text-slate-300">
                {totals.bets ? ((totals.wins / totals.bets) * 100).toFixed(1) + '%' : '—'}
              </td>
              <td class="px-5 py-3 text-right tabular-nums text-slate-300">${totals.wagered.toFixed(2)}</td>
              <td class="px-5 py-3 text-right tabular-nums font-bold
                {totals.net > 0 ? 'text-emerald-400' : totals.net < 0 ? 'text-red-400' : 'text-slate-400'}">
                {totals.net >= 0 ? '+' : ''}{totals.net.toFixed(2)}
              </td>
              <td class="px-5 py-3 text-right tabular-nums font-bold
                {totals.wagered > 0 && totals.net / totals.wagered > 0 ? 'text-emerald-400' : 'text-red-400'}">
                {totals.wagered
                  ? ((totals.net / totals.wagered) * 100 >= 0 ? '+' : '') +
                    ((totals.net / totals.wagered) * 100).toFixed(1) + '%'
                  : '—'}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  {/if}
</div>
