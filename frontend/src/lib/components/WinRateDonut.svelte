<script lang="ts">
  import type { BreakdownRow } from '$lib/types';

  export let rows: BreakdownRow[] = [];
  export let groupKey: 'league' | 'bet_type' | 'sport' | 'sportsbook' = 'league';
  export let title: string = '';
  export let loading = false;

  const CX = 90;
  const CY = 90;
  const R_OUTER = 70;
  const R_INNER = 46;

  const COLORS = [
    '#6366f1', '#22d3ee', '#f59e0b', '#34d399',
    '#f472b6', '#a78bfa', '#fb923c', '#4ade80',
  ];

  function polarToCart(cx: number, cy: number, r: number, deg: number) {
    const rad = ((deg - 90) * Math.PI) / 180;
    return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
  }

  function arcPath(cx: number, cy: number, ro: number, ri: number, startDeg: number, endDeg: number): string {
    // Clamp to avoid full-circle artifacts
    const sweep = Math.min(endDeg - startDeg, 359.99);
    const s1 = polarToCart(cx, cy, ro, startDeg);
    const e1 = polarToCart(cx, cy, ro, startDeg + sweep);
    const s2 = polarToCart(cx, cy, ri, startDeg + sweep);
    const e2 = polarToCart(cx, cy, ri, startDeg);
    const large = sweep > 180 ? 1 : 0;
    return [
      `M ${s1.x} ${s1.y}`,
      `A ${ro} ${ro} 0 ${large} 1 ${e1.x} ${e1.y}`,
      `L ${s2.x} ${s2.y}`,
      `A ${ri} ${ri} 0 ${large} 0 ${e2.x} ${e2.y}`,
      'Z',
    ].join(' ');
  }

  // Top N rows by total bets, rest collapsed to "Other"
  $: topRows = (() => {
    if (!rows.length) return [];
    const sorted = [...rows].sort((a, b) => b.total_bets - a.total_bets);
    const top = sorted.slice(0, 7);
    const rest = sorted.slice(7);
    if (rest.length) {
      top.push({
        [groupKey]: 'Other',
        total_bets: rest.reduce((s, r) => s + r.total_bets, 0),
        wins: rest.reduce((s, r) => s + r.wins, 0),
        losses: rest.reduce((s, r) => s + r.losses, 0),
        total_wagered: rest.reduce((s, r) => s + r.total_wagered, 0),
        net_profit: rest.reduce((s, r) => s + r.net_profit, 0),
        win_pct: 0,
        roi_pct: 0,
      } as BreakdownRow);
    }
    return top;
  })();

  $: totalBets = topRows.reduce((s, r) => s + r.total_bets, 0);

  $: segments = (() => {
    let startDeg = 0;
    return topRows.map((row, i) => {
      const frac = totalBets ? row.total_bets / totalBets : 0;
      const sweep = frac * 360;
      const seg = {
        path: arcPath(CX, CY, R_OUTER, R_INNER, startDeg, startDeg + sweep),
        color: COLORS[i % COLORS.length],
        label: (row[groupKey] as string) || '—',
        bets: row.total_bets,
        win_pct: row.win_pct,
        roi_pct: row.roi_pct,
        net_profit: row.net_profit,
      };
      startDeg += sweep;
      return seg;
    });
  })();

  $: overallWinPct = (() => {
    const totalWins = rows.reduce((s, r) => s + r.wins, 0);
    const total = rows.reduce((s, r) => s + r.total_bets, 0);
    return total ? ((totalWins / total) * 100).toFixed(1) : '—';
  })();

  let hovered: (typeof segments)[0] | null = null;
</script>

<div class="card">
  <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-4">{title}</h2>

  {#if loading}
    <div class="flex gap-4 items-center">
      <div class="w-[180px] h-[180px] bg-surface-800 rounded-full animate-pulse flex-shrink-0"></div>
      <div class="flex-1 space-y-2">
        {#each Array(4) as _}
          <div class="h-4 bg-surface-800 rounded animate-pulse"></div>
        {/each}
      </div>
    </div>
  {:else if !rows.length}
    <div class="h-[180px] flex items-center justify-center text-slate-500 text-sm">No data</div>
  {:else}
    <div class="flex gap-5 items-center">
      <!-- Donut -->
      <div class="flex-shrink-0">
        <svg width="180" height="180" viewBox="0 0 180 180">
          {#each segments as seg}
            <path
              d={seg.path}
              fill={seg.color}
              opacity={hovered && hovered !== seg ? 0.35 : 1}
              class="cursor-pointer transition-opacity"
              on:mouseenter={() => (hovered = seg)}
              on:mouseleave={() => (hovered = null)}
            >
              <title>{seg.label}: {seg.bets} bets · {seg.win_pct.toFixed(1)}% win · {seg.roi_pct >= 0 ? '+' : ''}{seg.roi_pct.toFixed(1)}% ROI</title>
            </path>
          {/each}
          <!-- Center text -->
          <text x={CX} y={CY - 6} text-anchor="middle" font-size="20" font-weight="bold" fill="#f1f5f9">
            {hovered ? hovered.win_pct.toFixed(0) : overallWinPct}%
          </text>
          <text x={CX} y={CY + 12} text-anchor="middle" font-size="10" fill="#94a3b8">
            {hovered ? hovered.label : 'win rate'}
          </text>
        </svg>
      </div>

      <!-- Legend -->
      <ul class="flex-1 space-y-1.5 min-w-0">
        {#each segments as seg}
          <li
            class="flex items-center gap-2 text-xs cursor-pointer transition-opacity
                   {hovered && hovered !== seg ? 'opacity-40' : 'opacity-100'}"
            on:mouseenter={() => (hovered = seg)}
            on:mouseleave={() => (hovered = null)}
          >
            <span class="w-2.5 h-2.5 rounded-sm flex-shrink-0" style="background:{seg.color}"></span>
            <span class="text-slate-300 truncate flex-1">{seg.label}</span>
            <span class="tabular-nums text-slate-400 flex-shrink-0">{seg.bets}</span>
            <span class="tabular-nums flex-shrink-0
              {seg.roi_pct > 0 ? 'text-emerald-400' : seg.roi_pct < 0 ? 'text-red-400' : 'text-slate-400'}">
              {seg.roi_pct >= 0 ? '+' : ''}{seg.roi_pct.toFixed(1)}%
            </span>
          </li>
        {/each}
      </ul>
    </div>
  {/if}
</div>
