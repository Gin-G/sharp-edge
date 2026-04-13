<script lang="ts">
  import { onMount } from 'svelte';
  import StatCard from '$lib/components/StatCard.svelte';
  import RoiTrendChart from '$lib/components/RoiTrendChart.svelte';
  import WinRateDonut from '$lib/components/WinRateDonut.svelte';
  import { getStats, getBreakdown, getCalendar, getInsights } from '$lib/api';
  import type { Stats, BreakdownRow, CalendarDay } from '$lib/types';

  let stats: Stats | null = null;
  let leagueBreakdown: BreakdownRow[] = [];
  let betTypeBreakdown: BreakdownRow[] = [];
  let calendarData: CalendarDay[] = [];
  let insights: string[] = [];
  let loading = true;
  let error = '';

  // Quick date helper — ISO string N days ago
  function daysAgo(n: number): string {
    const d = new Date();
    d.setDate(d.getDate() - n);
    return d.toISOString().split('T')[0];
  }

  onMount(async () => {
    try {
      [stats, leagueBreakdown, betTypeBreakdown, calendarData] = await Promise.all([
        getStats(),
        getBreakdown('league'),
        getBreakdown('bet_type'),
        getCalendar(daysAgo(365)),
      ]);
      // Insights non-blocking
      getInsights().then((r) => (insights = r.insights)).catch(() => {});
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  });

  function fmtMoney(v: number | undefined): string {
    if (v == null) return '—';
    return (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);
  }
  function fmtPct(v: number | undefined): string {
    if (v == null) return '—';
    return (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
  }
  function fmtNum(v: number | undefined): string {
    return v?.toLocaleString() ?? '—';
  }
</script>

<svelte:head><title>Dashboard — Sharp Edge</title></svelte:head>

<div class="space-y-6">
  <!-- Page header -->
  <div>
    <h1 class="text-xl font-bold text-white">Dashboard</h1>
    <p class="text-sm text-slate-400 mt-0.5">Your complete betting performance</p>
  </div>

  {#if error}
    <div class="card border-red-800 bg-red-950/30 text-red-300 text-sm">
      Failed to load data: {error}
      <span class="ml-2 text-red-400">· Is the backend running?</span>
    </div>
  {/if}

  <!-- Stat cards -->
  <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
    <StatCard
      label="Total Bets"
      value={fmtNum(stats?.total_bets)}
      sub="{stats?.wins ?? 0}W / {stats?.losses ?? 0}L"
      {loading}
    />
    <StatCard
      label="Win Rate"
      value={stats ? ((stats.wins / stats.total_bets) * 100).toFixed(1) + '%' : '—'}
      sub="settled bets"
      trend={stats && stats.wins / stats.total_bets > 0.5 ? 'pos' : 'neg'}
      {loading}
    />
    <StatCard
      label="Net P/L"
      value={fmtMoney(stats?.net_profit)}
      sub={stats ? `on $${stats.total_wagered.toFixed(0)} wagered` : ''}
      trend={stats ? (stats.net_profit > 0 ? 'pos' : stats.net_profit < 0 ? 'neg' : 'neutral') : 'neutral'}
      {loading}
    />
    <StatCard
      label="ROI"
      value={fmtPct(stats?.roi_pct)}
      sub="return on investment"
      trend={stats ? (stats.roi_pct > 0 ? 'pos' : stats.roi_pct < 0 ? 'neg' : 'neutral') : 'neutral'}
      {loading}
    />
    <StatCard
      label="Total Wagered"
      value={stats ? '$' + stats.total_wagered.toFixed(0) : '—'}
      sub={stats ? `avg $${stats.avg_stake?.toFixed(2) ?? '—'}/bet` : ''}
      {loading}
    />
    <StatCard
      label="Avg Odds"
      value={stats?.avg_odds?.toFixed(2) ?? '—'}
      sub="decimal"
      {loading}
    />
  </div>

  <!-- ROI trend chart -->
  <RoiTrendChart data={calendarData} {loading} />

  <!-- Breakdown donuts -->
  <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
    <WinRateDonut
      rows={leagueBreakdown}
      groupKey="league"
      title="P/L by League"
      {loading}
    />
    <WinRateDonut
      rows={betTypeBreakdown}
      groupKey="bet_type"
      title="P/L by Bet Type"
      {loading}
    />
  </div>

  <!-- Insights -->
  {#if insights.length}
    <div class="card">
      <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Insights</h2>
      <ul class="space-y-2">
        {#each insights as insight}
          <li class="flex items-start gap-2 text-sm text-slate-300">
            <span class="text-indigo-400 mt-0.5 flex-shrink-0">›</span>
            {insight}
          </li>
        {/each}
      </ul>
    </div>
  {/if}
</div>
