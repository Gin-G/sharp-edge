<script lang="ts">
  import type { CalendarDay } from '$lib/types';

  export let data: CalendarDay[] = [];
  export let year: number = new Date().getFullYear();
  export let loading = false;

  const CELL = 13;
  const GAP = 3;
  const STEP = CELL + GAP;
  const MONTH_LABELS_H = 20;
  const DOW_LABELS_W = 24;

  const DOW_LABELS = ['', 'Mon', '', 'Wed', '', 'Fri', ''];
  const MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

  $: lookup = new Map(data.map((d) => [d.day, d]));

  // Generate every day in the year
  $: days = (() => {
    const result: { date: string; col: number; row: number; d: CalendarDay | null }[] = [];
    const jan1 = new Date(year, 0, 1);
    const startDow = jan1.getDay(); // 0=Sun
    const totalDays = (year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)) ? 366 : 365;
    for (let i = 0; i < totalDays; i++) {
      const dateObj = new Date(year, 0, i + 1);
      const dateStr = dateObj.toISOString().split('T')[0];
      const col = Math.floor((i + startDow) / 7);
      const row = (i + startDow) % 7;
      result.push({ date: dateStr, col, row, d: lookup.get(dateStr) ?? null });
    }
    return result;
  })();

  $: numCols = days.length ? days[days.length - 1].col + 1 : 53;

  // Month label positions
  $: monthTicks = (() => {
    const jan1 = new Date(year, 0, 1);
    const startDow = jan1.getDay();
    return MONTH_NAMES.map((name, m) => {
      const d = new Date(year, m, 1);
      const dayOfYear = Math.round((d.getTime() - jan1.getTime()) / 86400000);
      const col = Math.floor((dayOfYear + startDow) / 7);
      return { name, col };
    });
  })();

  const SVG_W = DOW_LABELS_W + numCols * STEP;
  const SVG_H = MONTH_LABELS_H + 7 * STEP;

  function profitColor(d: CalendarDay | null): string {
    if (!d) return '#1e2330';
    const p = d.net_profit;
    if (p === 0) return '#1e2330';
    if (p > 50)  return '#059669';
    if (p > 20)  return '#10b981';
    if (p > 5)   return '#34d399';
    if (p > 0)   return '#6ee7b7';
    if (p > -5)  return '#fca5a5';
    if (p > -20) return '#f87171';
    if (p > -50) return '#ef4444';
    return '#dc2626';
  }

  function fmtTooltip(d: CalendarDay): string {
    const sign = d.net_profit >= 0 ? '+' : '';
    return `${d.day} · ${d.total_bets} bets · ${sign}$${d.net_profit.toFixed(2)} P/L`;
  }
</script>

<div class="card overflow-x-auto">
  <div class="flex items-center justify-between mb-4">
    <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
      Daily P/L — {year}
    </h2>
    <div class="flex items-center gap-2 text-xs text-slate-500">
      <span>Less</span>
      {#each ['#1e2330','#fca5a5','#f87171','#ef4444','#6ee7b7','#34d399','#10b981','#059669'] as c}
        <span class="w-3 h-3 rounded-sm inline-block" style="background:{c}"></span>
      {/each}
      <span>More</span>
    </div>
  </div>

  {#if loading}
    <div class="h-[120px] bg-surface-800 rounded animate-pulse"></div>
  {:else}
    <svg
      viewBox="0 0 {SVG_W} {SVG_H}"
      width={SVG_W}
      height={SVG_H}
      class="block"
      aria-label="Daily P/L heatmap {year}"
    >
      <!-- Month labels -->
      {#each monthTicks as tick}
        <text
          x={DOW_LABELS_W + tick.col * STEP}
          y={MONTH_LABELS_H - 6}
          font-size="10"
          fill="#64748b"
        >{tick.name}</text>
      {/each}

      <!-- Day-of-week labels -->
      {#each DOW_LABELS as dow, i}
        {#if dow}
          <text
            x={DOW_LABELS_W - 4}
            y={MONTH_LABELS_H + i * STEP + CELL / 2 + 1}
            text-anchor="end"
            dominant-baseline="middle"
            font-size="9"
            fill="#475569"
          >{dow}</text>
        {/if}
      {/each}

      <!-- Cells -->
      {#each days as day}
        <rect
          x={DOW_LABELS_W + day.col * STEP}
          y={MONTH_LABELS_H + day.row * STEP}
          width={CELL}
          height={CELL}
          rx="2"
          fill={profitColor(day.d)}
          class="cursor-pointer hover:opacity-80 transition-opacity"
        >
          {#if day.d}
            <title>{fmtTooltip(day.d)}</title>
          {:else}
            <title>{day.date}</title>
          {/if}
        </rect>
      {/each}
    </svg>
  {/if}
</div>
