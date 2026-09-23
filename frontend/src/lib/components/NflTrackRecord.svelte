<script lang="ts">
  /** How the NFL suggestions have actually done.
   *
   *  Hit rate and ROI side by side everywhere, never hit rate alone — that
   *  pairing is the single most useful thing the baseball side learned. The
   *  retired batter screen hit 64.8% and lost money, because its median price
   *  was -260 and the break-even was 72%.
   *
   *  Split by market and by side because those are where the model is most
   *  likely to be wrong in a way an overall number hides. The UNDER bar and
   *  the short-line guard in nfl/card.py are both priors, and this panel is
   *  what will confirm or kill them.
   */
  import { onMount } from 'svelte';
  import { getNflTrackRecord } from '$lib/api';
  import { cached } from '$lib/cache';
  import type { NflTrackRecord } from '$lib/types';

  const CACHE_KEY = 'nfl:track-record';

  let data: NflTrackRecord | null = null;
  let loading = true;
  let error = '';

  onMount(async () => {
    try {
      data = await cached(CACHE_KEY, () => getNflTrackRecord());
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  });

  function pct(v: number | null): string {
    return v === null || v === undefined ? '—' : `${v.toFixed(1)}%`;
  }

  function roiClass(v: number | null): string {
    if (v === null || v === undefined) return 'text-slate-500';
    if (v > 0) return 'text-emerald-400';
    if (v < 0) return 'text-red-400';
    return 'text-slate-400';
  }

  function resultClass(r: string | null): string {
    if (r === 'WIN') return 'bg-emerald-600/20 text-emerald-300 border-emerald-600/30';
    if (r === 'LOSS') return 'bg-rose-600/20 text-rose-300 border-rose-600/30';
    if (r === 'PUSH') return 'bg-surface-600 text-slate-300 border-border';
    if (r === 'VOID') return 'bg-surface-600 text-slate-500 border-border';
    return 'bg-surface-600/40 text-slate-500 border-border';
  }

  function odds(v: number | null): string {
    if (v === null || v === undefined) return '—';
    return v > 0 ? `+${v}` : `${v}`;
  }

  /** A card is decided as soon as one leg loses — a parlay cannot recover —
   *  so "pending" here means still live, not merely ungraded. */
  function cardState(c: { result: string | null; legs: { result: string | null }[] }) {
    if (c.result === 'WIN') return { label: 'HIT', cls: 'bg-emerald-600/20 text-emerald-300 border-emerald-600/30' };
    if (c.result === 'LOSS') return { label: 'MISSED', cls: 'bg-rose-600/20 text-rose-300 border-rose-600/30' };
    if (c.result === 'VOID') return { label: 'VOID', cls: 'bg-surface-600 text-slate-500 border-border' };
    const live = c.legs.filter((l) => !l.result).length;
    return {
      label: live ? `${live} LEG${live > 1 ? 'S' : ''} LIVE` : 'PENDING',
      cls: 'bg-surface-600/40 text-slate-400 border-border',
    };
  }

  $: o = data?.overall;
  $: cardRows = (data?.cards.rows ?? []).slice().sort(
    (a, b) => b.season - a.season || b.week - a.week
  );

  /** Flattened so the template doesn't index a union type by a dynamic key —
   *  each group carries its own label alongside the bucket. */
  $: groups = data
    ? [
        { title: 'By market', rows: data.by_market.map((r) => ({ label: r.market, ...r })) },
        { title: 'By side', rows: data.by_side.map((r) => ({ label: r.side, ...r })) },
        { title: 'By week', rows: data.by_week.map((r) => ({ label: `Week ${r.week}`, ...r })) },
        // The season's open question — see screen.flag_role_conflicts.
        { title: 'By role conflict', rows: (data.by_role_conflict ?? []).map((r) => ({
            label: r.role_conflict ? 'We disagree on role' : 'Agrees with the market', ...r })) },
        // ...and whether the depth chart, a third source, settles it. This is
        // the split that says if reading the chart was worth anything.
        { title: 'By depth chart', rows: (data.by_role_verdict ?? []).map((r) => ({
            label: r.verdict === 'market' ? 'Chart backs the market'
              : r.verdict === 'model' ? 'Chart backs us'
              : 'Chart has no view', ...r })) },
        // Picks on a player whose group lost work to an injury. card.py refuses
        // the unders in the 'major' band, so the overs are what this learns
        // from — see VACATED_SHARE_BLOCKS_UNDER.
        { title: 'By inherited role', rows: (data.by_vacated ?? []).map((r) => ({
            label: r.vacated === 'major' ? 'Inherited a third or more'
              : r.vacated === 'minor' ? 'Inherited something'
              : 'Group intact', ...r })) },
        // Only questionable survives the card guard, so this asks one thing:
        // does backing through a questionable tag cost more than the market
        // has already priced?
        { title: 'By injury status', rows: (data.by_availability ?? []).map((r) => ({
            label: r.avail === 'ACTIVE' ? 'No designation'
              : r.avail === 'unknown' ? 'Not on the feeds'
              : r.avail.toLowerCase().replace(/^./, (c) => c.toUpperCase()), ...r })) },
      ]
    : [];
</script>

<section class="card p-0 overflow-hidden">
  <div class="px-5 py-4 border-b border-border flex items-baseline justify-between flex-wrap gap-2">
    <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Track Record</h2>
    <span class="text-xs text-slate-500">
      voids and pushes leave the denominator
    </span>
  </div>

  {#if loading}
    <div class="px-5 py-6 text-sm text-slate-500">Loading…</div>
  {:else if error}
    <div class="px-5 py-6 text-sm text-red-300">{error}</div>
  {:else if !data || !o || o.picks === 0}
    <div class="px-5 py-6 text-sm text-slate-500">
      Nothing recorded yet — the first suggestions are written the next time the
      board is built before kickoff.
    </div>
  {:else}
    <div class="grid grid-cols-2 sm:grid-cols-4 divide-x divide-y sm:divide-y-0 divide-border">
      <div class="px-5 py-4">
        <div class="text-xs text-slate-500 uppercase tracking-wider">Hit rate</div>
        <div class="text-2xl font-bold text-white tabular-nums mt-1">{pct(o.hit_rate)}</div>
        <div class="text-xs text-slate-500 mt-0.5">{o.wins}–{o.losses} graded</div>
      </div>
      <div class="px-5 py-4">
        <div class="text-xs text-slate-500 uppercase tracking-wider">ROI</div>
        <div class="text-2xl font-bold tabular-nums mt-1 {roiClass(o.roi)}">
          {o.roi === null ? '—' : `${o.roi > 0 ? '+' : ''}${o.roi.toFixed(1)}%`}
        </div>
        <div class="text-xs text-slate-500 mt-0.5">flat stake, recorded price</div>
      </div>
      <div class="px-5 py-4">
        <div class="text-xs text-slate-500 uppercase tracking-wider">Pending</div>
        <div class="text-2xl font-bold text-slate-300 tabular-nums mt-1">{o.pending}</div>
        <div class="text-xs text-slate-500 mt-0.5">awaiting results</div>
      </div>
      <div class="px-5 py-4">
        <div class="text-xs text-slate-500 uppercase tracking-wider">Cards</div>
        <div class="text-2xl font-bold text-slate-300 tabular-nums mt-1">
          {data.cards.won}<span class="text-slate-600">/{data.cards.played}</span>
        </div>
        <div class="text-xs text-slate-500 mt-0.5">weeks swept</div>
      </div>
    </div>

    {#each groups as group}
      {#if group.rows.length}
        <div class="border-t border-border">
          <div class="px-5 pt-4 pb-2 text-xs font-medium text-slate-400 uppercase tracking-wider">
            {group.title}
          </div>
          <div class="overflow-x-auto">
            <table class="w-full text-sm">
              <thead>
                <tr class="text-xs text-slate-500 border-b border-border">
                  <th class="text-left px-5 py-2 font-medium"></th>
                  <th class="text-right px-4 py-2 font-medium">Picks</th>
                  <th class="text-right px-4 py-2 font-medium">W–L</th>
                  <th class="text-right px-4 py-2 font-medium">Hit</th>
                  <th class="text-right px-5 py-2 font-medium">ROI</th>
                </tr>
              </thead>
              <tbody>
                {#each group.rows as r}
                  <tr class="border-b border-border/50">
                    <td class="px-5 py-2 text-slate-300">{r.label}</td>
                    <td class="px-4 py-2 text-right tabular-nums text-slate-500">{r.picks}</td>
                    <td class="px-4 py-2 text-right tabular-nums text-slate-400">{r.wins}–{r.losses}</td>
                    <td class="px-4 py-2 text-right tabular-nums text-slate-200">{pct(r.hit_rate)}</td>
                    <td class="px-5 py-2 text-right tabular-nums {roiClass(r.roi)}">
                      {r.roi === null ? '—' : `${r.roi > 0 ? '+' : ''}${r.roi.toFixed(1)}%`}
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        </div>
      {/if}
    {/each}

    {#if cardRows.length}
      <div class="border-t border-border">
        <div class="px-5 pt-4 pb-2 flex items-baseline justify-between gap-2">
          <span class="text-xs font-medium text-slate-400 uppercase tracking-wider">
            The suggested parlay, week by week
          </span>
          <span class="text-[11px] text-slate-500">every leg must hit</span>
        </div>
        <div class="px-5 pb-4 space-y-3">
          {#each cardRows as c}
            {@const st = cardState(c)}
            <div class="rounded-lg border border-border overflow-hidden">
              <div class="flex items-baseline justify-between gap-3 px-4 py-2.5 bg-surface-600/30">
                <div class="flex items-baseline gap-2.5 flex-wrap">
                  <span class="text-sm font-semibold text-slate-200">Week {c.week}</span>
                  <span class="text-xs text-slate-500">
                    {c.leg_count} legs · {odds(c.american)}
                  </span>
                  {#if c.model_p !== null}
                    <span class="text-xs text-slate-500">
                      model {(100 * c.model_p).toFixed(1)}%
                    </span>
                  {/if}
                </div>
                <span class="inline-block px-2 py-0.5 text-[10px] font-medium rounded border {st.cls}">
                  {st.label}
                </span>
              </div>
              <div class="divide-y divide-border/50">
                {#each c.legs as l}
                  <div class="flex items-baseline justify-between gap-3 px-4 py-2">
                    <div class="min-w-0">
                      <div class="text-sm text-slate-200 truncate">{l.player}</div>
                      <div class="text-xs text-slate-500">
                        {l.side?.toLowerCase()} {l.line} {l.market.replace(/_/g, ' ')}
                        {#if l.fd_odds !== null}<span class="text-slate-600"> · {odds(l.fd_odds)}</span>{/if}
                      </div>
                    </div>
                    <div class="flex items-baseline gap-3 shrink-0">
                      <span class="text-sm tabular-nums text-slate-300">
                        {l.actual === null || l.actual === undefined ? '—' : l.actual}
                      </span>
                      <span class="inline-block px-1.5 py-0.5 text-[10px] rounded border {resultClass(l.result)}">
                        {l.result ?? 'LIVE'}
                      </span>
                    </div>
                  </div>
                {/each}
              </div>
            </div>
          {/each}
        </div>
      </div>
    {/if}

    {#if data.picks.length}
      <div class="border-t border-border">
        <div class="px-5 pt-4 pb-2 text-xs font-medium text-slate-400 uppercase tracking-wider">
          Every pick
        </div>
        <div class="overflow-x-auto max-h-[480px] overflow-y-auto">
          <table class="w-full text-sm">
            <thead class="sticky top-0 bg-surface-800">
              <tr class="text-xs text-slate-500 border-b border-border">
                <th class="text-left px-5 py-2 font-medium">Wk</th>
                <th class="text-left px-3 py-2 font-medium">Player</th>
                <th class="text-left px-3 py-2 font-medium">Bet</th>
                <th class="text-right px-3 py-2 font-medium">Price</th>
                <th class="text-right px-3 py-2 font-medium">Model</th>
                <th class="text-right px-3 py-2 font-medium">Actual</th>
                <th class="text-right px-5 py-2 font-medium">Result</th>
              </tr>
            </thead>
            <tbody>
              {#each data.picks as p}
                <tr class="border-b border-border/50 hover:bg-surface-600/30">
                  <td class="px-5 py-2 text-slate-500 tabular-nums">{p.week}</td>
                  <td class="px-3 py-2 text-slate-200">{p.player}</td>
                  <td class="px-3 py-2 text-slate-400 text-xs">
                    {p.side?.toLowerCase()} {p.line} {p.market.replace(/_/g, ' ')}
                  </td>
                  <td class="px-3 py-2 text-right tabular-nums text-slate-400">
                    {p.fd_odds === null ? '—' : p.fd_odds > 0 ? `+${p.fd_odds}` : p.fd_odds}
                  </td>
                  <td class="px-3 py-2 text-right tabular-nums text-slate-400">
                    {p.model_p === null ? '—' : `${(100 * p.model_p).toFixed(1)}%`}
                  </td>
                  <td class="px-3 py-2 text-right tabular-nums text-slate-300">
                    {p.actual === null ? '—' : p.actual}
                  </td>
                  <td class="px-5 py-2 text-right">
                    <span class="inline-block px-1.5 py-0.5 text-[10px] rounded border {resultClass(p.result)}">
                      {p.result ?? 'PENDING'}
                    </span>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>
    {/if}
  {/if}
</section>
