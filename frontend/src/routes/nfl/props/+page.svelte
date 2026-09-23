<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { peek } from '$lib/cache';
  import {
    NFL_CACHE_KEY, load as loadScreen, initialState,
    fmtOdds, fmtPct, fmtSigned, fmtEv, evClass, MARKET_LABEL, fmtKickoff,
    shortEvent, MARKET_GROUP, GROUP_ORDER, GROUP_BLURB,
  } from '$lib/nflScreen';
  import type { NflScreen, NflProp } from '$lib/types';
  import NflCard from '$lib/components/NflCard.svelte';
  import NflTrackRecord from '$lib/components/NflTrackRecord.svelte';

  let s = initialState(peek<NflScreen>(NFL_CACHE_KEY));
  $: ({ data, loading, error, warming, warmingElapsed } = s);

  let cancel: (() => void) | null = null;
  function go(force = false) {
    cancel?.();
    cancel = loadScreen((patch) => (s = { ...s, ...patch }), force);
  }
  onMount(() => go());
  onDestroy(() => cancel?.());

  // Filters. `onlySignals` defaults on — the whole point of the board is the
  // rows where the projection and the line disagree, and 250 rows of agreement
  // is not something anyone reads.
  let onlySignals = true;
  let market: string = 'all';
  let includeUnbettable = false;

  $: markets = data ? [...new Set(data.props.map((p) => p.market))] : [];

  $: rows = (data?.props ?? []).filter((p: NflProp) => {
    if (onlySignals && !p.signal) return false;
    if (market !== 'all' && p.market !== market) return false;
    if (!includeUnbettable && !p.bettable) return false;
    return true;
  });

  $: overs = rows.filter((r) => r.signal === 'OVER').length;
  $: unders = rows.filter((r) => r.signal === 'UNDER').length;

  /** The board split by market family, empty groups dropped.
   *
   *  Grouped rather than filtered because the families answer different
   *  questions and are not comparable: a receiving edge is measured in yards
   *  off a target projection, a passing edge in yards off a model that is
   *  known to run overconfident. Stacking them in one list invites reading the
   *  biggest number as the best bet.
   */
  $: groups = GROUP_ORDER
    .map((name) => ({
      name,
      blurb: GROUP_BLURB[name],
      rows: rows.filter((r) => MARKET_GROUP[r.market] === name),
    }))
    .filter((g) => g.rows.length > 0);

  /** Everything the board carries, whether or not it is modelled. The counts
   *  are the honest part: game lines are priced but carry no projection, so
   *  they are listed as prices rather than as edges. */
  $: coverage = data
    ? [
        ...GROUP_ORDER.map((name) => ({
          label: name,
          n: (data.props ?? []).filter((r) => MARKET_GROUP[r.market] === name).length,
          modelled: true,
          href: null as string | null,
        })),
        { label: 'Touchdowns', n: data.tds?.length ?? 0, modelled: true,
          href: '/nfl/touchdowns' },
        { label: 'Game lines', n: data.games?.length ?? 0, modelled: false,
          href: '/nfl/games' },
      ]
    : [];

  // How much the raw rule and the rescaled one disagree. Worth a line on the
  // page rather than only in the code: it is the single biggest judgement call
  // in this screen.
  $: rawOnly = (data?.props ?? []).filter(
    (p) => p.bettable && p.raw_signal && !p.signal,
  ).length;
  $: bothAgree = (data?.props ?? []).filter(
    (p) => p.bettable && p.raw_signal && p.signal === p.raw_signal,
  ).length;

  function sideClass(side: string): string {
    if (side === 'OVER') return 'bg-emerald-600/20 text-emerald-300 border-emerald-600/30';
    if (side === 'UNDER') return 'bg-amber-600/20 text-amber-300 border-amber-600/30';
    return 'bg-surface-600/40 text-slate-500 border-border';
  }
</script>

<svelte:head><title>NFL Props — Sharp Edge</title></svelte:head>

<div class="space-y-6">
  <div class="flex items-center justify-between flex-wrap gap-3">
    <p class="text-sm text-slate-400">
      {#if data}
        Week {data.week}, {data.season} — projection against the posted line
        {#if data.odds?.age_seconds != null}
          <span class="text-slate-500">
            · FanDuel {data.odds.age_seconds > 60
              ? `${Math.round(data.odds.age_seconds / 60)}m old`
              : 'live'}
          </span>
        {/if}
      {:else}
        Projection against the posted line
      {/if}
    </p>
    <button
      class="px-3 py-1.5 rounded-lg text-sm font-medium bg-surface-700 text-slate-300 hover:bg-surface-600 disabled:opacity-50"
      on:click={() => go(true)}
      disabled={loading}
    >{loading ? 'Loading…' : 'Refresh'}</button>
  </div>

  {#if error}
    <div class="card border-red-800 bg-red-950/30 text-red-300 text-sm">{error}</div>
  {/if}

  {#if data?.preseason}
    <div
      class="card border-amber-800/50 bg-amber-950/20 text-xs text-amber-200/90 py-2"
      title="Week 1 projections are priors computed in August with no in-season usage behind them. The disagreement with the market is shrunk hard as a result, and the model probabilities should be read as a ranking rather than as a price."
    >
      Preseason projections · shrink {data.prob_fits?.receiving_yards?.shrink ?? '—'} ·
      read the model column as a ranking, not a price
    </div>
  {/if}

  {#if loading && !data}
    <div class="card text-slate-400 text-sm flex items-center gap-3">
      <div class="w-4 h-4 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin"></div>
      {#if warming}
        Building this week's NFL board{warmingElapsed != null
          ? ` (${Math.round(warmingElapsed)}s elapsed)` : ''}…
      {:else}
        Loading board…
      {/if}
    </div>
  {:else if data}
    <!-- The card first: it is the only thing on this page that is a bet. The
         track record says how these have been doing, and the board below is
         where they came from — both are context for it. -->
    <NflCard {data} />

    <NflTrackRecord />

    <!-- Controls -->
    <div class="card p-4 flex items-center gap-4 flex-wrap text-sm">
      <label class="flex items-center gap-2 text-slate-300">
        <input type="checkbox" bind:checked={onlySignals} class="accent-indigo-500" />
        Only rows past the threshold
      </label>
      <label class="flex items-center gap-2 text-slate-300" title={data.passing_yards_caveat}>
        <input type="checkbox" bind:checked={includeUnbettable} class="accent-indigo-500" />
        Include passing yards
      </label>
      <div class="flex items-center gap-2">
        <span class="text-slate-500">Market</span>
        <select bind:value={market} class="bg-surface-800 border border-border rounded-lg px-2 py-1 text-slate-200">
          <option value="all">All</option>
          {#each markets as m}
            <option value={m}>{MARKET_LABEL[m] ?? m}</option>
          {/each}
        </select>
      </div>
      <span class="text-slate-500 ml-auto tabular-nums">
        {rows.length} rows · {overs} over / {unders} under
      </span>
    </div>

    {#if data.held_prior_only?.length}
      <div
        class="card border-amber-800/50 bg-amber-950/20 text-xs text-amber-200/90 py-2"
        title="These cleared the threshold on a positional prior — draft capital through a curve — with no read on the player in it, so the gap measures the prior's distance from the market. Established players appearing here mean the projections table upstream is stale or failed to match their history."
      >
        {data.held_prior_only.length} held back (prior only):
        <span class="text-amber-200">
          {data.held_prior_only.slice(0, 8).map((r) => r.player).join(', ')}{data
            .held_prior_only.length > 8 ? '…' : ''}
        </span>
      </div>
    {/if}

    <!-- What the board covers -->
    <section class="card p-0 overflow-hidden">
      <div class="px-5 py-3 border-b border-border">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
          Markets on the board
        </h2>
      </div>
      <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 divide-x divide-y sm:divide-y-0 divide-border">
        {#each coverage as c}
          <div class="px-5 py-3">
            <div class="text-xs text-slate-500 uppercase tracking-wider">{c.label}</div>
            <div class="text-xl font-bold text-white tabular-nums mt-0.5">{c.n}</div>
            {#if c.modelled}
              <div class="text-[11px] text-slate-500 mt-0.5">projected</div>
            {:else}
              <div class="text-[11px] text-amber-400/80 mt-0.5" title="Priced from FanDuel but with no model behind them yet — nothing to compare the price to.">
                priced only
              </div>
            {/if}
            {#if c.href}
              <a href={c.href} class="text-[11px] text-sky-400 hover:text-sky-300">view →</a>
            {/if}
          </div>
        {/each}
      </div>
    </section>

    <!-- Board, one section per market family -->
    {#each groups as g (g.name)}
    <section class="card overflow-hidden p-0">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between flex-wrap gap-2">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider" title={g.blurb}>
          {g.name} <span class="text-slate-600 normal-case font-normal">· {g.rows.length}</span>
        </h2>
      </div>

      {#if g.rows.length === 0}
        <div class="px-5 py-6 text-sm text-slate-500">
          Nothing past the threshold with these filters.
        </div>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                <th class="text-left px-3 py-3">Bet</th>
                <th class="text-left px-3 py-3">Player</th>
                <th class="text-left px-3 py-3">Market</th>
                <th class="text-right px-3 py-3">Line</th>
                <th class="text-right px-3 py-3" title="The projection as published, before rescaling.">Proj</th>
                <th class="text-right px-3 py-3" title="The projection restated on the market's scale.">Adj</th>
                <th class="text-right px-3 py-3 whitespace-nowrap" title="Projection minus line, as published. Biased toward UNDER on good players.">Raw</th>
                <th class="text-right px-3 py-3" title="The gap after rescaling — what the signal is measured on.">Gap</th>
                <th class="text-right px-3 py-3">Price</th>
                <th class="text-right px-3 py-3">Model</th>
                <th class="text-right px-3 py-3">Edge</th>
                <th class="text-right px-3 py-3">EV/$1</th>
                <th class="text-left px-3 py-3">Game</th>
              </tr>
            </thead>
            <tbody>
              {#each g.rows as r (r.market + r.key + r.line)}
                <tr class="border-b border-border/50 hover:bg-surface-600/30 {r.bettable ? '' : 'opacity-60'}">
                  <td class="px-3 py-2.5">
                    <span class="inline-block px-2 py-0.5 text-xs rounded border {sideClass(r.signal || r.side || '')}">
                      {r.signal || r.side || '—'}
                    </span>
                  </td>
                  <td class="px-3 py-2.5 text-slate-200 font-medium whitespace-nowrap">
                    {r.player}
                    <span class="text-xs text-slate-500">{r.position ?? ''} {r.team ?? ''}</span>
                  </td>
                  <td class="px-3 py-2.5 text-slate-400 whitespace-nowrap">{MARKET_LABEL[r.market] ?? r.market}</td>
                  <td class="px-3 py-2.5 text-right tabular-nums text-slate-200">{r.line}</td>
                  <td class="px-3 py-2.5 text-right tabular-nums text-slate-500">{r.projection}</td>
                  <td class="px-3 py-2.5 text-right tabular-nums text-slate-300">{r.adjusted}</td>
                  <td class="px-3 py-2.5 text-right tabular-nums text-slate-600">{fmtSigned(r.raw_gap)}</td>
                  <td class="px-3 py-2.5 text-right tabular-nums font-medium
                             {r.residual > 0 ? 'text-emerald-400' : 'text-amber-400'}">
                    {fmtSigned(r.residual)}
                  </td>
                  <td
                    class="px-3 py-2.5 text-right tabular-nums text-slate-300"
                    title="Over {fmtOdds(r.over_odds)} / under {fmtOdds(r.under_odds)}"
                  >{fmtOdds(r.odds)}</td>
                  <td
                    class="px-3 py-2.5 text-right tabular-nums text-slate-200"
                    title="{fmtPct(r.model_p_raw)} before anchoring to the market"
                  >{fmtPct(r.model_p)}</td>
                  <td class="px-3 py-2.5 text-right tabular-nums {evClass(r.ev)}">
                    {r.edge_pts != null ? fmtSigned(r.edge_pts) : '—'}
                  </td>
                  <td class="px-3 py-2.5 text-right tabular-nums {evClass(r.ev)}">{fmtEv(r.ev)}</td>
                  <td class="px-3 py-2.5 text-slate-500 text-xs whitespace-nowrap" title={r.event ?? ''}>
                    {shortEvent(r.event)}
                    <span class="text-slate-600"> · {fmtKickoff(r.kickoff)}</span>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>
    {/each}

    {#if groups.length === 0}
      <section class="card px-5 py-6 text-sm text-slate-500">
        Nothing past the threshold with these filters.
      </section>
    {/if}

    <!-- How much work the rescaling is doing -->
    <section class="card p-0 overflow-hidden">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between flex-wrap gap-2">
        <h2
          class="text-sm font-semibold text-slate-300 uppercase tracking-wider"
          title="The projections regress toward the mean and the lines don't, so the raw gap reads that shrinkage as signal — it wants the under on nearly every star. Each market is refit weekly against its own board; a slope of 1.0 would mean no correction was needed."
        >Rescaling</h2>
        <span class="text-xs text-slate-500 tabular-nums">
          <span title="Rows where the raw rule and the rescaled one pick the same side.">{bothAgree} agree</span>
          · <span title="Rows the raw rule fires on and the rescaled one rejects — the shrinkage artefacts.">{rawOnly} raw only</span>
          {#if data.unmatched.length}
            · <span title="Priced players with no projection to join: {data.unmatched.slice(0, 8).join(', ')}">{data.unmatched.length} unmatched</span>
          {/if}
        </span>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
              <th class="text-left px-3 py-3">Market</th>
              <th class="text-right px-3 py-3">Slope</th>
              <th class="text-right px-3 py-3">Intercept</th>
              <th class="text-right px-3 py-3">Players fit</th>
              <th class="text-right px-3 py-3" title="Log-odds shift applied to centre the model on the market">Anchor</th>
              <th class="text-right px-3 py-3" title="Fraction of the disagreement kept after anchoring">Shrink</th>
            </tr>
          </thead>
          <tbody>
            {#each Object.entries(data.fits) as [m, f]}
              <tr class="border-b border-border/50">
                <td class="px-3 py-2.5 text-slate-300">{MARKET_LABEL[m] ?? m}</td>
                <td class="px-3 py-2.5 text-right tabular-nums {f.slope != null && Math.abs(f.slope - 1) > 0.15 ? 'text-amber-400' : 'text-slate-300'}">
                  {f.slope ?? '—'}
                </td>
                <td class="px-3 py-2.5 text-right tabular-nums text-slate-400">{f.intercept ?? '—'}</td>
                <td class="px-3 py-2.5 text-right tabular-nums text-slate-500">{f.n}</td>
                <td class="px-3 py-2.5 text-right tabular-nums text-slate-400">
                  {data.prob_fits?.[m]?.offset ?? '—'}
                </td>
                <td class="px-3 py-2.5 text-right tabular-nums text-slate-400">
                  {data.prob_fits?.[m]?.shrink ?? '—'}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  {/if}
</div>
