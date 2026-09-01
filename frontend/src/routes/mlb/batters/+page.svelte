<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { getBatterScreen, ApiError } from '$lib/api';
  import { cached, peek } from '$lib/cache';
  import type { BatterScreen, BatterRow, HotBatRow } from '$lib/types';
  import PickTrackRecord from '$lib/components/PickTrackRecord.svelte';
  import ParlayRecord from '$lib/components/ParlayRecord.svelte';

  const CACHE_KEY = 'batters:screen';

  // Seed from the module cache so switching back to this tab paints the board
  // immediately rather than re-running the whole fetch-and-spin cycle.
  let data: BatterScreen | null = peek<BatterScreen>(CACHE_KEY);
  let loading = data === null;
  let error = '';
  let warming = false;
  let warmingElapsed: number | null = null;
  let pollTimer: ReturnType<typeof setTimeout> | null = null;

  async function load(force = false) {
    loading = true;
    error = '';
    try {
      data = await cached(CACHE_KEY, getBatterScreen, { force });
      warming = false;
      warmingElapsed = null;
    } catch (e) {
      if (e instanceof ApiError && e.status === 503) {
        warming = true;
        // Body carries elapsed_seconds for the spinner.
        const body = e.body as { elapsed_seconds?: number } | undefined;
        warmingElapsed = body?.elapsed_seconds ?? null;
        // Respect Retry-After (defaults to 15s); cap aggressively low while warming.
        const delay = Math.min((e.retryAfter ?? 15) * 1000, 20_000);
        pollTimer = setTimeout(load, delay);
        return; // keep `loading` true while we wait
      }
      error = e instanceof Error ? e.message : String(e);
      warming = false;
    } finally {
      if (!warming) loading = false;
    }
  }

  onMount(load);
  onDestroy(() => {
    if (pollTimer) clearTimeout(pollTimer);
    if (copyTimer) clearTimeout(copyTimer);
  });

  function fmtAvg(v: number | null): string {
    if (v === null || v === undefined) return '—';
    return v.toFixed(3).replace(/^0/, '');
  }

  function fmtNum(v: number | null | undefined, digits = 2): string {
    if (v === null || v === undefined) return '—';
    return v.toFixed(digits);
  }

  function fmtOdds(v: number | null | undefined): string {
    if (v === null || v === undefined) return '—';
    return v > 0 ? `+${v}` : `${v}`;
  }

  // Whether tapping the bet-slip link will hand off to the FanDuel app rather
  // than open a browser tab. Detected from the pointer type rather than a
  // user-agent string: it's what actually distinguishes the case we care
  // about (a phone with the app installed) and it doesn't rot the way UA
  // sniffing does. Resolved on mount so SSR renders the desktop form.
  let opensInApp = false;
  onMount(() => {
    opensInApp =
      typeof window !== 'undefined' &&
      window.matchMedia?.('(pointer: coarse)').matches === true;
  });

  let copied = false;
  let copyTimer: ReturnType<typeof setTimeout> | null = null;

  async function copyBetslip() {
    const url = data?.bundle?.betslip_url;
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      copied = true;
      if (copyTimer) clearTimeout(copyTimer);
      copyTimer = setTimeout(() => (copied = false), 2000);
    } catch {
      // Clipboard is permission-gated and fails silently in some contexts;
      // the link itself is right there to copy by hand.
    }
  }

  function fmtPct(v: number | null | undefined): string {
    if (v === null || v === undefined) return '—';
    return `${(100 * v).toFixed(1)}%`;
  }

  // EV per $1 staked. Shown, not obeyed — the card is chosen on probability.
  // Worth showing because it is what caught the old screen out: those picks
  // hit 70.5% at a median price of -260, whose break-even is 72%, so the card
  // read well and lost money. Betting the board instead lands the median leg
  // at -185 against a 77% read.
  function fmtEv(v: number | null | undefined): string {
    if (v === null || v === undefined) return '—';
    return `${v >= 0 ? '+' : ''}${v.toFixed(3)}`;
  }

  function evClass(v: number | null | undefined): string {
    if (v === null || v === undefined) return 'text-slate-500';
    if (v >= 0.03) return 'text-emerald-400 font-semibold';
    if (v > 0) return 'text-emerald-500/80';
    if (v > -0.03) return 'text-slate-400';
    return 'text-red-400';
  }

  // Colour the starter's recent contact profile. SHARP is the warning: a
  // pitcher holding lineups to a low average is the wrong side of a
  // "records a hit" bet however good the batter's history looks.
  const BAND_CLASS: Record<string, string> = {
    SHARP: 'bg-red-600/20 text-red-300 border-red-600/30',
    HITTABLE: 'bg-emerald-600/20 text-emerald-300 border-emerald-600/30',
    NEUTRAL: 'bg-surface-600/40 text-slate-400 border-border',
    UNKNOWN: 'bg-surface-600/40 text-slate-500 border-border'
  };

  $: bvpEdges = (data?.today ?? [])
    .filter((r) => r.bvp_edge)
    .sort((a, b) => (b.bvp_avg ?? 0) - (a.bvp_avg ?? 0));

  // Hot bats facing a starter who has been suppressing hits. This used to be
  // a veto list; nothing is vetoed now — filtering on the starter measured
  // worse, dropping the two-leg sweep from 58.9% to 52.0% over 129 days. Kept
  // as a watch list: it is the matchup the model is most often talked out of,
  // and worth an eye if one of these does reach the card.
  $: vetoed = (data?.today ?? [])
    .filter(
      (r) =>
        r.p_sharp &&
        r.is_hot &&
        (r.bvp_edge || r.hittable_sp_edge)
    )
    .sort((a, b) => (a.p_l3_h9 ?? 99) - (b.p_l3_h9 ?? 99));

  // The hittable-starter edge on its own. It no longer produces picks — the
  // card is ranked off the whole board — but it is still the matchup people
  // reach for first, so it stays broken out alongside BvP and hand+slump as
  // board context.
  $: hotVsHittable = (data?.today ?? [])
    .filter((r) => r.hittable_sp_edge && !r.bvp_edge)
    .sort((a, b) => (b.p_l3_h9 ?? 0) - (a.p_l3_h9 ?? 0));
</script>

<svelte:head><title>Batters — Sharp Edge</title></svelte:head>

<div class="space-y-6">
  <div class="flex items-center justify-between">
    <p class="text-sm text-slate-400">
      Today's MLB board — ranked on the probability of a hit
    </p>
    <button
      class="px-3 py-1.5 rounded-lg text-sm font-medium bg-surface-700 text-slate-300 hover:bg-surface-600 disabled:opacity-50"
      on:click={() => load(true)}
      disabled={loading}
    >{loading ? 'Loading…' : 'Refresh'}</button>
  </div>

  {#if error}
    <div class="card border-red-800 bg-red-950/30 text-red-300 text-sm">{error}</div>
  {/if}

  {#if data?.bundle && data.bundle.legs.length > 0}
    <!-- The day's card, first on the page: it is the only thing here that is
         actually a bet. The record panels below say how these have been doing
         and the board below that is where the legs came from — both are
         context for this, so both sit under it. -->
    <section class="card p-0 overflow-hidden border-emerald-800/50">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between flex-wrap gap-2">
        <h2 class="text-sm font-semibold text-emerald-300 uppercase tracking-wider">
          Today's Parlay
          <span class="ml-2 normal-case font-normal text-xs text-slate-500">
            the 2 most likely, plus every leg that pays for its own risk
          </span>
        </h2>
        {#if data.bundle.summary.american != null}
          <span class="text-xs text-slate-400 tabular-nums">
            {data.bundle.summary.legs}-leg parlay
            <span class="text-slate-200 font-semibold">{fmtOdds(data.bundle.summary.american)}</span>
            · model {fmtPct(data.bundle.summary.model_p)} vs implied {fmtPct(data.bundle.summary.implied_p)}
            · <span class={evClass(data.bundle.summary.ev)}>EV {fmtEv(data.bundle.summary.ev)}</span>
            {#if data.bundle.summary.kelly_quarter}
              · <span class="text-slate-300" title="Quarter-Kelly. Sizing is only as good as the model's probability, and a parlay compounds each leg's error — treat it as a ceiling.">
                stake {(100 * data.bundle.summary.kelly_quarter).toFixed(1)}%
              </span>
            {/if}
            {#if data.bundle.result}
              · <span class="px-1.5 py-0.5 rounded border text-[10px] {data.bundle.result === 'WIN'
                  ? 'bg-emerald-600/20 text-emerald-300 border-emerald-600/30'
                  : 'bg-rose-600/20 text-rose-300 border-rose-600/30'}">{data.bundle.result}</span>
            {/if}
          </span>
        {/if}
      </div>

      <div class="divide-y divide-border/50">
        {#each data.bundle.legs as r (r.batter)}
          <div class="px-5 py-2.5 flex items-center justify-between gap-4 text-sm">
            <div class="min-w-0">
              <span class="text-slate-200 font-medium">{r.batter}</span>
              {#if r.team}<span class="text-slate-500 text-xs"> ({r.team})</span>{/if}
              <span class="text-slate-500 text-xs"> vs {r.opposing_pitcher}</span>
              {#if r.market_open === false}
                <!-- The card is frozen at the morning's board, so a leg whose
                     game has started is still shown — it is what we said —
                     but FanDuel has pulled the market and it can't be bet. -->
                <span class="ml-2 px-1.5 py-0.5 rounded text-[10px] bg-surface-600 text-slate-400 whitespace-nowrap">started</span>
              {/if}
            </div>
            <div class="flex items-center gap-4 tabular-nums text-xs shrink-0">
              <span class="text-emerald-300 font-semibold">{fmtPct(r.model_p)}</span>
              <span class="text-slate-500">SP {fmtNum(r.p_l3_h9)} H/9</span>
              <span class="text-slate-400">{fmtOdds(r.fd_odds)}</span>
              <span class={evClass(r.ev)}>{fmtEv(r.ev)}</span>
            </div>
          </div>
        {/each}
      </div>

      <div class="px-5 py-4 border-t border-border flex items-center gap-3 flex-wrap">
        {#if data.bundle.betslip_url}
          <!-- No target="_blank" on touch devices. The link has to be a normal
               top-level navigation for the OS to hand it to the app; opened in
               a new tab it falls back to the mobile website. Desktop keeps the
               new tab, where there's no app to hand off to and losing the
               board would just be annoying. The host does the rest — see
               bundle.BETSLIP_BASE for why it is the account one. -->
          <a
            href={data.bundle.betslip_url}
            target={opensInApp ? undefined : '_blank'}
            rel={opensInApp ? undefined : 'noopener noreferrer'}
            class="px-3 py-1.5 rounded-lg text-sm font-medium bg-emerald-600 text-white hover:bg-emerald-500"
          >Open in FanDuel{opensInApp ? ' app' : ' bet slip'}</a>
          <button
            class="px-3 py-1.5 rounded-lg text-sm font-medium bg-surface-700 text-slate-300 hover:bg-surface-600"
            on:click={copyBetslip}
          >{copied ? 'Copied' : 'Copy link'}</button>
        {/if}
        <span class="text-xs text-slate-500">
          Prices move — re-check the slip before placing.
        </span>
      </div>

      {#if data.bundle.near_misses?.length}
        <!-- Names that didn't clear the bar to make the card. Worth showing
             for the read you have that the price happened to argue against. -->
        <div class="px-5 py-3 border-t border-border bg-surface-800/40">
          <div class="text-xs text-slate-500 mb-2">
            Next best on the board — one per game, not on the card:
          </div>
          {#each data.bundle.near_misses as m}
            <div class="flex items-center justify-between text-xs py-0.5 tabular-nums">
              <span class="text-slate-400">
                {m.batter}<span class="text-slate-600"> vs {m.opposing_pitcher}</span>
              </span>
              <span class="flex items-center gap-3 shrink-0">
                <span class="text-slate-300 w-12 text-right">{fmtPct(m.model_p)}</span>
                <span class="text-slate-500 w-14 text-right">{fmtOdds(m.fd_odds)}</span>
                <span class={evClass(m.ev)}>{fmtEv(m.ev)}</span>
              </span>
            </div>
          {/each}
        </div>
      {/if}
    </section>
  {/if}

  <ParlayRecord />

  <PickTrackRecord screen="batter" winLabel="Hit" />

  {#if loading && !data}
    <div class="card text-slate-400 text-sm flex items-center gap-3">
      <div class="w-4 h-4 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin"></div>
      {#if warming}
        Scraping today's MLB data{warmingElapsed != null ? ` (${Math.round(warmingElapsed)}s elapsed)` : ''}… retrying shortly.
      {:else}
        Loading screen…
      {/if}
    </div>
  {:else if data}
    <!-- Picks -->
    <section class="card overflow-hidden p-0">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
          Today's Board, Ranked
        </h2>
        <span class="text-xs text-slate-500">
          top {data.picks.length} by probability of a hit — the parlay takes the best 2
          {#if data.odds}
            {#if data.odds.error}
              · <span class="text-amber-500/80">odds unavailable</span>
            {:else}
              · FanDuel odds {data.odds.age_seconds != null && data.odds.age_seconds > 60
                ? `${Math.round(data.odds.age_seconds / 60)}m old`
                : 'live'}
            {/if}
          {/if}
        </span>
      </div>
      {#if data.picks.length === 0}
        <div class="px-5 py-6 text-sm text-slate-500">No picks today.</div>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                <th class="text-left px-4 py-3">Batter</th>
                <th class="text-left px-4 py-3">Team</th>
                <th class="text-left px-4 py-3">Opp SP</th>
                <th class="text-center px-4 py-3">Hand</th>
                <th class="text-right px-4 py-3">r7 AVG</th>
                <th class="text-right px-4 py-3">vs Hand</th>
                <th class="text-right px-4 py-3">BvP AVG</th>
                <th class="text-right px-4 py-3">BvP PA</th>
                <th class="text-right px-4 py-3">SP L3 ERA</th>
                <th class="text-right px-4 py-3">SP L3 H/9</th>
                <th class="text-right px-4 py-3">SP L3 BAA</th>
                <th class="text-center px-4 py-3">SP Form</th>
                <th class="text-right px-4 py-3">FD</th>
                <th class="text-right px-4 py-3">Model</th>
                <th class="text-right px-4 py-3">Edge</th>
                <th class="text-right px-4 py-3">EV/$1</th>
                <th class="text-left px-4 py-3">Tags</th>
                <th class="text-right px-4 py-3">Time</th>
              </tr>
            </thead>
            <tbody>
              {#each data.picks as r (r.batter + r.opposing_pitcher)}
                <tr class="border-b border-border/50 hover:bg-surface-600/30">
                  <td class="px-4 py-2.5 text-slate-200 font-medium">{r.batter}</td>
                  <td class="px-4 py-2.5 text-slate-400">{r.team}</td>
                  <td class="px-4 py-2.5 text-slate-300">{r.opposing_pitcher}</td>
                  <td class="px-4 py-2.5 text-center text-slate-400">{r.p_hand ?? '—'}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-200">{fmtAvg(r.recent_avg)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">
                    {fmtAvg(r.vs_hand_avg)}<span class="text-xs text-slate-500"> ({r.vs_hand_pa})</span>
                  </td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-emerald-400 font-medium">{fmtAvg(r.bvp_avg)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{r.bvp_pa}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">{fmtNum(r.p_l3_era)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">
                    {fmtNum(r.p_l3_h9)}<span class="text-xs text-slate-500"> ({r.p_l3_hits ?? '—'}H)</span>
                  </td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">{fmtAvg(r.p_l3_baa)}</td>
                  <td class="px-4 py-2.5 text-center">
                    <span class="inline-block px-2 py-0.5 text-xs rounded border {BAND_CLASS[r.p_form] ?? BAND_CLASS.UNKNOWN}">{r.p_form}</span>
                  </td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-200">
                    {#if r.fd_odds !== null && r.fd_odds !== undefined}
                      {fmtOdds(r.fd_odds)}<span class="block text-xs text-slate-500">{fmtPct(r.implied_p)}</span>
                    {:else}
                      <span class="text-slate-600">no line</span>
                      <span class="block text-xs text-slate-600">need {fmtOdds(r.breakeven_odds)}</span>
                    {/if}
                  </td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">{fmtPct(r.model_p)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums {evClass(r.ev)}">
                    {r.edge_pts !== null && r.edge_pts !== undefined
                      ? `${r.edge_pts >= 0 ? '+' : ''}${r.edge_pts.toFixed(1)}`
                      : '—'}
                  </td>
                  <td class="px-4 py-2.5 text-right tabular-nums {evClass(r.ev)}">{fmtEv(r.ev)}</td>
                  <td class="px-4 py-2.5">
                    {#each r.tags.split(',').filter(Boolean) as tag}
                      <span class="inline-block px-2 py-0.5 mr-1 text-xs rounded bg-indigo-600/20 text-indigo-300 border border-indigo-600/30">{tag}</span>
                    {/each}
                  </td>
                  <td class="px-4 py-2.5 text-right text-slate-400 text-xs">{r.game_time}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <!-- Vetoed by pitcher form -->
    <section class="card overflow-hidden p-0">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
          Hot Bats vs Sharp Starters
        </h2>
        <span class="text-xs text-slate-500">
          {vetoed.length} hot bat{vetoed.length === 1 ? '' : 's'} facing an SP suppressing hits — flagged, not dropped
        </span>
      </div>
      {#if vetoed.length === 0}
        <div class="px-5 py-6 text-sm text-slate-500">None — no hot bat is facing a sharp starter today.</div>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                <th class="text-left px-4 py-3">Batter</th>
                <th class="text-left px-4 py-3">Team</th>
                <th class="text-left px-4 py-3">Opp SP</th>
                <th class="text-right px-4 py-3">r7 AVG</th>
                <th class="text-right px-4 py-3">BvP AVG</th>
                <th class="text-right px-4 py-3">SP L3 H/9</th>
                <th class="text-right px-4 py-3">SP L3 BAA</th>
                <th class="text-right px-4 py-3">SP L3 ERA</th>
                <th class="text-right px-4 py-3">Season BAA</th>
                <th class="text-right px-4 py-3">Time</th>
              </tr>
            </thead>
            <tbody>
              {#each vetoed as r (r.batter + r.opposing_pitcher)}
                <tr class="border-b border-border/50 hover:bg-surface-600/30 opacity-70">
                  <td class="px-4 py-2.5 text-slate-300 font-medium">{r.batter}</td>
                  <td class="px-4 py-2.5 text-slate-400">{r.team}</td>
                  <td class="px-4 py-2.5 text-slate-300">{r.opposing_pitcher}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">{fmtAvg(r.recent_avg)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{fmtAvg(r.bvp_avg)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-red-300 font-medium">{fmtNum(r.p_l3_h9)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-red-300">{fmtAvg(r.p_l3_baa)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{fmtNum(r.p_l3_era)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{fmtAvg(r.p_season_baa)}</td>
                  <td class="px-4 py-2.5 text-right text-slate-400 text-xs">{r.game_time}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <!-- Hot bat vs. a starter who's been getting hit -->
    <section class="card overflow-hidden p-0">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
          Hot Bats vs Hittable Starters
        </h2>
        <span class="text-xs text-slate-500">
          {hotVsHittable.length} on this edge — context, not a pick list
        </span>
      </div>
      {#if hotVsHittable.length === 0}
        <div class="px-5 py-6 text-sm text-slate-500">None.</div>
      {:else}
        <div class="overflow-x-auto max-h-[420px] overflow-y-auto">
          <table class="w-full text-sm">
            <thead class="sticky top-0 bg-surface-800">
              <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                <th class="text-left px-4 py-3">Batter</th>
                <th class="text-left px-4 py-3">Team</th>
                <th class="text-left px-4 py-3">Opp SP</th>
                <th class="text-right px-4 py-3">r7 AVG</th>
                <th class="text-right px-4 py-3">SP L3 H</th>
                <th class="text-right px-4 py-3">SP L3 H/9</th>
                <th class="text-right px-4 py-3">SP L3 BAA</th>
                <th class="text-right px-4 py-3">SP L3 ERA</th>
                <th class="text-right px-4 py-3">Time</th>
              </tr>
            </thead>
            <tbody>
              {#each hotVsHittable as r (r.batter + r.opposing_pitcher)}
                <tr class="border-b border-border/50 hover:bg-surface-600/30">
                  <td class="px-4 py-2 text-slate-200 font-medium">{r.batter}</td>
                  <td class="px-4 py-2 text-slate-400">{r.team}</td>
                  <td class="px-4 py-2 text-slate-300">{r.opposing_pitcher}</td>
                  <td class="px-4 py-2 text-right tabular-nums text-slate-200">{fmtAvg(r.recent_avg)}</td>
                  <td class="px-4 py-2 text-right tabular-nums text-slate-400">{r.p_l3_hits ?? '—'}</td>
                  <td class="px-4 py-2 text-right tabular-nums text-emerald-400 font-medium">{fmtNum(r.p_l3_h9)}</td>
                  <td class="px-4 py-2 text-right tabular-nums text-emerald-400">{fmtAvg(r.p_l3_baa)}</td>
                  <td class="px-4 py-2 text-right tabular-nums text-slate-400">{fmtNum(r.p_l3_era)}</td>
                  <td class="px-4 py-2 text-right text-slate-400 text-xs">{r.game_time}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <!-- BvP edges -->
    <section class="card overflow-hidden p-0">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
          BvP Edges
        </h2>
        <span class="text-xs text-slate-500">
          {bvpEdges.length} batter{bvpEdges.length === 1 ? '' : 's'} ≥.400 vs today's SP — usually small samples
        </span>
      </div>
      {#if bvpEdges.length === 0}
        <div class="px-5 py-6 text-sm text-slate-500">None.</div>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
                <th class="text-left px-4 py-3">Batter</th>
                <th class="text-left px-4 py-3">Team</th>
                <th class="text-left px-4 py-3">Opp SP</th>
                <th class="text-right px-4 py-3">BvP AVG</th>
                <th class="text-right px-4 py-3">PA</th>
                <th class="text-right px-4 py-3">H</th>
                <th class="text-right px-4 py-3">r7 AVG</th>
                <th class="text-right px-4 py-3">Time</th>
              </tr>
            </thead>
            <tbody>
              {#each bvpEdges as r (r.batter + r.opposing_pitcher)}
                <tr class="border-b border-border/50 hover:bg-surface-600/30">
                  <td class="px-4 py-2.5 text-slate-200 font-medium">{r.batter}</td>
                  <td class="px-4 py-2.5 text-slate-400">{r.team}</td>
                  <td class="px-4 py-2.5 text-slate-300">{r.opposing_pitcher}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-emerald-400 font-medium">{fmtAvg(r.bvp_avg)}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{r.bvp_pa}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{r.bvp_hits}</td>
                  <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">{fmtAvg(r.recent_avg)}</td>
                  <td class="px-4 py-2.5 text-right text-slate-400 text-xs">{r.game_time}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <!-- Hot Bats -->
    <section class="card overflow-hidden p-0">
      <div class="px-5 py-4 border-b border-border flex items-baseline justify-between">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Hot Bats</h2>
        <span class="text-xs text-slate-500">{data.hot_bats.length} players hitting >.300 over last 7d</span>
      </div>
      <div class="overflow-x-auto max-h-[600px] overflow-y-auto">
        <table class="w-full text-sm">
          <thead class="sticky top-0 bg-surface-800">
            <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
              <th class="text-left px-4 py-3">Batter</th>
              <th class="text-left px-4 py-3">Team</th>
              <th class="text-right px-4 py-3">r7 AVG</th>
              <th class="text-right px-4 py-3">AB</th>
              <th class="text-right px-4 py-3">H</th>
              <th class="text-right px-4 py-3">HR</th>
              <th class="text-right px-4 py-3">OBP</th>
              <th class="text-right px-4 py-3">OPS</th>
            </tr>
          </thead>
          <tbody>
            {#each data.hot_bats as r (r.batter)}
              <tr class="border-b border-border/50 hover:bg-surface-600/30">
                <td class="px-4 py-2 text-slate-200 font-medium">{r.batter}</td>
                <td class="px-4 py-2 text-slate-400">{r.team}</td>
                <td class="px-4 py-2 text-right tabular-nums text-emerald-400 font-medium">{fmtAvg(r.recent_avg)}</td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-400">{r.recent_ab}</td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-400">{r.H}</td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-400">{r.HR}</td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-300">{fmtAvg(r.OBP)}</td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-300">{fmtNum(r.OPS, 3)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  {/if}
</div>
