<script lang="ts">
  import type { CalendarDay } from '$lib/types';

  export let data: CalendarDay[] = [];
  export let loading = false;

  const W = 760;
  const H = 180;
  const PAD = { top: 16, right: 16, bottom: 36, left: 56 };
  const CW = W - PAD.left - PAD.right;
  const CH = H - PAD.top - PAD.bottom;

  // Build cumulative P/L series from sorted calendar data
  $: series = (() => {
    if (!data.length) return [];
    const sorted = [...data].sort((a, b) => a.day.localeCompare(b.day));
    let cumulative = 0;
    return sorted.map((d) => {
      cumulative += d.net_profit;
      return { day: d.day, cumulative };
    });
  })();

  $: minVal = series.length ? Math.min(0, ...series.map((d) => d.cumulative)) : 0;
  $: maxVal = series.length ? Math.max(0, ...series.map((d) => d.cumulative)) : 1;
  $: range = maxVal - minVal || 1;

  function xScale(i: number): number {
    return PAD.left + (i / Math.max(series.length - 1, 1)) * CW;
  }
  function yScale(v: number): number {
    return PAD.top + CH - ((v - minVal) / range) * CH;
  }

  $: zeroY = yScale(0);

  $: linePath = series.length
    ? series.map((d, i) => `${i === 0 ? 'M' : 'L'} ${xScale(i)} ${yScale(d.cumulative)}`).join(' ')
    : '';

  // Filled area split at zero: positive fill (above zero line) and negative fill (below)
  $: areaAbove = series.length
    ? `${linePath} L ${xScale(series.length - 1)} ${zeroY} L ${xScale(0)} ${zeroY} Z`
    : '';
  $: areaBelow = areaAbove;

  // Month label ticks — one label per month
  $: monthTicks = (() => {
    const seen = new Set<string>();
    return series
      .map((d, i) => {
        const ym = d.day.slice(0, 7);
        if (seen.has(ym)) return null;
        seen.add(ym);
        const [, m] = ym.split('-');
        const label = new Date(d.day + 'T00:00:00').toLocaleString('en-US', { month: 'short' });
        return { x: xScale(i), label };
      })
      .filter(Boolean) as { x: number; label: string }[];
  })();

  // Y-axis ticks
  $: yTicks = [minVal, 0, maxVal].filter((v, i, arr) => arr.indexOf(v) === i);

  function fmt(v: number): string {
    return (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(0);
  }
</script>

<div class="card">
  <div class="flex items-center justify-between mb-4">
    <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Cumulative P/L</h2>
    {#if series.length}
      <span class="text-xs text-slate-500">{series.length} days</span>
    {/if}
  </div>

  {#if loading}
    <div class="h-[180px] bg-surface-800 rounded-lg animate-pulse"></div>
  {:else if !series.length}
    <div class="h-[180px] flex items-center justify-center text-slate-500 text-sm">No data</div>
  {:else}
    <svg
      viewBox="0 0 {W} {H}"
      class="w-full"
      aria-label="Cumulative P/L over time"
    >
      <defs>
        <linearGradient id="grad-pos" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#34d399" stop-opacity="0.35"/>
          <stop offset="100%" stop-color="#34d399" stop-opacity="0.02"/>
        </linearGradient>
        <linearGradient id="grad-neg" x1="0" y1="1" x2="0" y2="0">
          <stop offset="0%" stop-color="#f87171" stop-opacity="0.35"/>
          <stop offset="100%" stop-color="#f87171" stop-opacity="0.02"/>
        </linearGradient>
        <clipPath id="clip-above">
          <rect x={PAD.left} y={PAD.top} width={CW} height={zeroY - PAD.top}/>
        </clipPath>
        <clipPath id="clip-below">
          <rect x={PAD.left} y={zeroY} width={CW} height={PAD.top + CH - zeroY}/>
        </clipPath>
      </defs>

      <!-- Grid lines -->
      {#each yTicks as v}
        <line
          x1={PAD.left} x2={PAD.left + CW}
          y1={yScale(v)} y2={yScale(v)}
          stroke={v === 0 ? '#475569' : '#1e2a3a'}
          stroke-width={v === 0 ? 1 : 0.5}
          stroke-dasharray={v === 0 ? '' : '4 4'}
        />
        <text
          x={PAD.left - 6} y={yScale(v)}
          text-anchor="end" dominant-baseline="middle"
          class="fill-slate-500 text-[10px]"
          font-size="10"
        >{fmt(v)}</text>
      {/each}

      <!-- Area fills -->
      <path d={areaAbove} fill="url(#grad-pos)" clip-path="url(#clip-above)"/>
      <path d={areaBelow} fill="url(#grad-neg)" clip-path="url(#clip-below)"/>

      <!-- Line -->
      <path
        d={linePath}
        fill="none"
        stroke="#6366f1"
        stroke-width="2"
        stroke-linejoin="round"
        stroke-linecap="round"
      />

      <!-- Month labels -->
      {#each monthTicks as tick}
        <text
          x={tick.x} y={H - 6}
          text-anchor="middle"
          class="fill-slate-500"
          font-size="10"
        >{tick.label}</text>
      {/each}
    </svg>
  {/if}
</div>
