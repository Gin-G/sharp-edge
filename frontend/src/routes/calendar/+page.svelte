<script lang="ts">
  import { onMount } from 'svelte';
  import CalendarHeatmap from '$lib/components/CalendarHeatmap.svelte';
  import { getCalendar } from '$lib/api';
  import type { CalendarDay } from '$lib/types';

  const currentYear = new Date().getFullYear();
  let selectedYear = currentYear;
  let calendarData: CalendarDay[] = [];
  let loading = true;
  let error = '';

  async function load(yr: number) {
    loading = true;
    error = '';
    try {
      calendarData = await getCalendar(`${yr}-01-01`, `${yr}-12-31`);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  }

  onMount(() => load(selectedYear));

  $: footerTotalNet = monthSummary.reduce((s, r) => s + r.net, 0);
  $: footerTotalWag = monthSummary.reduce((s, r) => s + r.wagered, 0);
  $: footerTotalBets = monthSummary.reduce((s, r) => s + r.bets, 0);
  $: footerTotalWins = monthSummary.reduce((s, r) => s + r.wins, 0);

  $: monthSummary = (() => {
    const map = new Map<string, { wagered: number; net: number; bets: number; wins: number }>();
    for (const d of calendarData) {
      const month = d.day.slice(0, 7);
      const existing = map.get(month) ?? { wagered: 0, net: 0, bets: 0, wins: 0 };
      existing.wagered += d.wagered;
      existing.net += d.net_profit;
      existing.bets += d.total_bets;
      existing.wins += d.wins;
      map.set(month, existing);
    }
    return Array.from(map.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([month, stats]) => ({
        month,
        label: new Date(month + '-01').toLocaleString('en-US', { month: 'long' }),
        ...stats,
        roi: stats.wagered ? (stats.net / stats.wagered) * 100 : 0,
      }));
  })();

  const years = Array.from({ length: 4 }, (_, i) => currentYear - i);
</script>

<svelte:head><title>Calendar — Sharp Edge</title></svelte:head>

<div class="space-y-6">
  <div class="flex items-center justify-between">
    <div>
      <h1 class="text-xl font-bold text-white">Calendar</h1>
      <p class="text-sm text-slate-400 mt-0.5">Daily P/L heatmap</p>
    </div>
    <div class="flex items-center gap-2">
      {#each years as yr}
        <button
          class="px-3 py-1.5 rounded-lg text-sm font-medium transition-colors
                 {selectedYear === yr
                   ? 'bg-indigo-600 text-white'
                   : 'bg-surface-700 text-slate-400 hover:text-white hover:bg-surface-600'}"
          on:click={() => { selectedYear = yr; load(yr); }}
        >{yr}</button>
      {/each}
    </div>
  </div>

  {#if error}
    <div class="card border-red-800 bg-red-950/30 text-red-300 text-sm">{error}</div>
  {/if}

  <!-- Heatmap -->
  <CalendarHeatmap data={calendarData} year={selectedYear} {loading} />

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
            {#each monthSummary as row}
              <tr class="border-b border-border/50 hover:bg-surface-600/30 transition-colors">
                <td class="px-5 py-3 text-slate-200 font-medium">{row.label}</td>
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
              <td class="px-5 py-3 text-right tabular-nums text-slate-300">{footerTotalBets}</td>
              <td class="px-5 py-3 text-right tabular-nums text-slate-300">
                {footerTotalBets ? ((footerTotalWins / footerTotalBets) * 100).toFixed(1) + '%' : '—'}
              </td>
              <td class="px-5 py-3 text-right tabular-nums text-slate-300">${footerTotalWag.toFixed(2)}</td>
              <td class="px-5 py-3 text-right tabular-nums font-bold
                {footerTotalNet > 0 ? 'text-emerald-400' : footerTotalNet < 0 ? 'text-red-400' : 'text-slate-400'}">
                {footerTotalNet >= 0 ? '+' : ''}{footerTotalNet.toFixed(2)}
              </td>
              <td class="px-5 py-3 text-right tabular-nums font-bold
                {footerTotalWag > 0 && footerTotalNet / footerTotalWag > 0 ? 'text-emerald-400' : 'text-red-400'}">
                {footerTotalWag ? ((footerTotalNet / footerTotalWag) * 100 >= 0 ? '+' : '') + ((footerTotalNet / footerTotalWag) * 100).toFixed(1) + '%' : '—'}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  {/if}
</div>
