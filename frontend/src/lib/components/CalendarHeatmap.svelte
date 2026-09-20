<script lang="ts">
  import type { CalendarDay } from '$lib/types';

  /** Inclusive ISO date bounds (YYYY-MM-DD). The grid pads out to whole
   *  weeks around them, but only days inside the range get a cell. */
  export let start: string;
  export let end: string;
  export let data: CalendarDay[] = [];
  export let title = 'Daily P/L';
  export let loading = false;

  const CELL = 13;
  const GAP = 3;
  const STEP = CELL + GAP;
  const MONTH_LABELS_H = 20;
  const DOW_LABELS_W = 26;

  const DOW_LABELS = ['', 'Mon', '', 'Wed', '', 'Fri', ''];
  const MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

  const EMPTY = '#1e2330';   // no bets
  const BREAKEVEN = '#3b4258';
  const LOSS = ['#7f1d1d', '#b91c1c', '#ef4444', '#f87171'];
  const PROFIT = ['#065f46', '#047857', '#10b981', '#34d399'];

  /** Local-calendar date <-> ISO. Deliberately not toISOString(), which
   *  shifts to UTC and slides every cell a day for positive-offset zones. */
  function iso(d: Date): string {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }
  function parse(s: string): Date {
    const [y, m, d] = s.split('-').map(Number);
    return new Date(y, m - 1, d);
  }
  /** Whole-day delta, immune to DST hour shifts. */
  function dayDelta(a: Date, b: Date): number {
    const ua = Date.UTC(a.getFullYear(), a.getMonth(), a.getDate());
    const ub = Date.UTC(b.getFullYear(), b.getMonth(), b.getDate());
    return Math.round((ub - ua) / 86400000);
  }

  const todayIso = iso(new Date());

  $: lookup = new Map(data.map((d) => [d.day, d]));

  $: cells = (() => {
    const from = parse(start);
    const to = parse(end);
    // Back up to the Sunday of the opening week so rows line up with the DOW axis.
    const gridStart = new Date(from);
    gridStart.setDate(from.getDate() - from.getDay());

    const total = dayDelta(gridStart, to) + 1;
    const out: {
      date: string;
      dateObj: Date;
      col: number;
      row: number;
      d: CalendarDay | null;
      inRange: boolean;
      future: boolean;
    }[] = [];
    for (let i = 0; i < total; i++) {
      const dt = new Date(gridStart);
      dt.setDate(gridStart.getDate() + i);
      const s = iso(dt);
      out.push({
        date: s,
        dateObj: dt,
        col: Math.floor(i / 7),
        row: i % 7,
        d: lookup.get(s) ?? null,
        inRange: s >= start,
        future: s > todayIso,
      });
    }
    return out;
  })();

  $: numCols = cells.length ? cells[cells.length - 1].col + 1 : 1;
  $: svgW = DOW_LABELS_W + numCols * STEP;
  $: svgH = MONTH_LABELS_H + 7 * STEP;

  /** One tick per month that opens inside the range, plus the partial
   *  opening month, minus any that would collide with its neighbour. */
  $: monthTicks = (() => {
    const label = (d: Date) =>
      d.getMonth() === 0
        ? `${MONTH_NAMES[0]} ${String(d.getFullYear()).slice(2)}`
        : MONTH_NAMES[d.getMonth()];

    const starts: { name: string; col: number }[] = [];
    for (const c of cells) {
      if (c.inRange && c.dateObj.getDate() === 1) starts.push({ name: label(c.dateObj), col: c.col });
    }
    const kept: { name: string; col: number }[] = [];
    for (const t of starts) {
      if (kept.length && t.col - kept[kept.length - 1].col < 3) continue;
      kept.push(t);
    }
    // Name the opening partial month too, but never at the cost of a real
    // month start — it loses the tie when the two would collide.
    const first = cells.find((c) => c.inRange);
    if (first && first.dateObj.getDate() !== 1 && (!kept.length || kept[0].col - first.col >= 3)) {
      kept.unshift({ name: label(first.dateObj), col: first.col });
    }
    return kept;
  })();

  /** Colour bins sized to the window actually on screen, so a quiet month
   *  isn't a flat wash and a loud one isn't all top-of-scale. */
  $: cuts = (() => {
    const mags = cells
      .filter((c) => c.d && c.d.net_profit !== 0)
      .map((c) => Math.abs(c.d!.net_profit))
      .sort((a, b) => a - b);
    if (!mags.length) return [5, 20, 50];
    const q = (p: number) => mags[Math.min(mags.length - 1, Math.floor(p * mags.length))];
    const c = [q(0.4), q(0.7), q(0.9)];
    // Keep strictly increasing; uniform data would otherwise collapse to one bin.
    for (let i = 1; i < c.length; i++) c[i] = Math.max(c[i], c[i - 1] + 0.01);
    return c;
  })();

  function bin(mag: number): number {
    if (mag < cuts[0]) return 0;
    if (mag < cuts[1]) return 1;
    if (mag < cuts[2]) return 2;
    return 3;
  }

  function cellFill(d: CalendarDay | null): string {
    if (!d || !d.total_bets) return EMPTY;
    const p = d.net_profit;
    if (p === 0) return BREAKEVEN;
    return p > 0 ? PROFIT[bin(p)] : LOSS[bin(-p)];
  }

  /** Exact, for tooltips. */
  function money(v: number): string {
    return `$${Math.abs(v).toFixed(2)}`;
  }

  /** Compact, for the legend's threshold labels. */
  function compact(v: number): string {
    const a = Math.abs(v);
    if (a >= 1000) return `$${(a / 1000).toFixed(a >= 10000 ? 0 : 1)}k`;
    return `$${a.toFixed(a < 10 ? 2 : 0)}`;
  }

  function fmtTooltip(c: { date: string; d: CalendarDay | null }): string {
    const pretty = parse(c.date).toLocaleDateString('en-US', {
      weekday: 'short', month: 'short', day: 'numeric', year: 'numeric',
    });
    if (!c.d || !c.d.total_bets) return `${pretty} — no bets`;
    const { total_bets, wins, wagered, net_profit } = c.d;
    const sign = net_profit >= 0 ? '+' : '−';
    return `${pretty}\n${wins}-${total_bets - wins} · ${money(wagered)} wagered · ${sign}${money(net_profit)}`;
  }

  /** Keep the most recent weeks in view when the grid overflows its card.
   *  An action rather than a reactive statement: calling tick() from inside
   *  one re-enters the flush on every update and wedges the main thread. */
  function pinRight(node: HTMLElement, _key: unknown) {
    const scroll = () => { node.scrollLeft = node.scrollWidth; };
    scroll();
    return { update: scroll };
  }
</script>

<div class="card">
  <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
    <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">{title}</h2>
    <div class="flex items-center gap-1.5 text-xs text-slate-500">
      <span class="tabular-nums">−{compact(cuts[2])}</span>
      {#each [...LOSS].reverse() as c}
        <span class="w-3 h-3 rounded-sm inline-block" style="background:{c}"></span>
      {/each}
      <span class="w-3 h-3 rounded-sm inline-block" style="background:{EMPTY}"></span>
      {#each PROFIT as c}
        <span class="w-3 h-3 rounded-sm inline-block" style="background:{c}"></span>
      {/each}
      <span class="tabular-nums">+{compact(cuts[2])}</span>
    </div>
  </div>

  {#if loading}
    <div class="h-[132px] bg-surface-800 rounded animate-pulse"></div>
  {:else}
    <div class="overflow-x-auto -mx-1 px-1" use:pinRight={start + end + cells.length}>
      <svg
        viewBox="0 0 {svgW} {svgH}"
        width={svgW}
        height={svgH}
        class="block"
        role="img"
        aria-label="{title} heatmap, {start} to {end}"
      >
        {#each monthTicks as mt}
          <text x={DOW_LABELS_W + mt.col * STEP} y={MONTH_LABELS_H - 6} font-size="10" fill="#64748b">
            {mt.name}
          </text>
        {/each}

        {#each DOW_LABELS as dow, i}
          {#if dow}
            <text
              x={DOW_LABELS_W - 5}
              y={MONTH_LABELS_H + i * STEP + CELL / 2 + 1}
              text-anchor="end"
              dominant-baseline="middle"
              font-size="9"
              fill="#475569"
            >{dow}</text>
          {/if}
        {/each}

        {#each cells as c (c.date)}
          {#if c.inRange && !c.future}
            <rect
              x={DOW_LABELS_W + c.col * STEP}
              y={MONTH_LABELS_H + c.row * STEP}
              width={CELL}
              height={CELL}
              rx="2"
              fill={cellFill(c.d)}
              stroke={c.date === todayIso ? '#818cf8' : 'none'}
              stroke-width={c.date === todayIso ? 1.5 : 0}
              class="hover:opacity-75 transition-opacity"
            ><title>{fmtTooltip(c)}</title></rect>
          {/if}
        {/each}
      </svg>
    </div>
  {/if}
</div>
