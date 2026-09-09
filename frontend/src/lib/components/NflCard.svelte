<script lang="ts">
  /** The week's card — the two legs we'd actually bet — and the wider
   *  suggestion list underneath it.
   *
   *  The split matters and is worth showing rather than hiding: the card is
   *  two legs because a parlay dies on any miss, but two legs a week is far
   *  too little to learn from, so the suggestions are what gets recorded and
   *  scored. Someone reading this page should be able to see both.
   */
  import { fmtOdds, fmtPct, fmtEv, evClass, MARKET_LABEL, fmtKickoff } from '$lib/nflScreen';
  import type { NflScreen, NflProp } from '$lib/types';

  export let data: NflScreen;

  let showAll = false;
  $: card = data.card?.legs ?? [];
  $: summary = data.card?.summary;
  $: suggestions = data.suggestions ?? [];
  $: rest = suggestions.filter((s) => !card.some((c) => c.key === s.key && c.market === s.market));
  $: shown = showAll ? rest : rest.slice(0, 8);
  $: conflicted = suggestions.filter((s) => s.role_conflict).length;

  let copied = false;
  let copyTimer: ReturnType<typeof setTimeout> | null = null;
  async function copyBetslip() {
    const url = data.card?.betslip_url;
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      copied = true;
      if (copyTimer) clearTimeout(copyTimer);
      copyTimer = setTimeout(() => (copied = false), 2000);
    } catch {
      // Clipboard is permission-gated; the link is on the page to copy by hand.
    }
  }

  function sideClass(side: string | null): string {
    return side === 'UNDER'
      ? 'bg-amber-600/20 text-amber-300 border-amber-600/30'
      : 'bg-emerald-600/20 text-emerald-300 border-emerald-600/30';
  }

  function leg(r: NflProp): string {
    return `${r.player} ${r.side?.toLowerCase()} ${r.line} ${MARKET_LABEL[r.market]?.toLowerCase() ?? r.market}`;
  }
</script>

{#if card.length}
  <section class="card p-0 overflow-hidden border-emerald-800/50">
    <div class="px-5 py-4 border-b border-border flex items-baseline justify-between flex-wrap gap-2">
      <h2 class="text-sm font-semibold text-emerald-300 uppercase tracking-wider">
        Week {data.week} Card
        <span class="ml-2 normal-case font-normal text-xs text-slate-500">
          the {card.length} best disagreements, one per game
        </span>
      </h2>
      {#if summary?.american != null}
        <span class="text-xs text-slate-400 tabular-nums">
          {summary.legs}-leg parlay
          <span class="text-slate-200 font-semibold">{fmtOdds(summary.american)}</span>
          · model {fmtPct(summary.model_p)} vs market {fmtPct(summary.implied_p)}
          · <span class={evClass(summary.ev)}>EV {fmtEv(summary.ev)}</span>
          {#if summary.kelly_quarter}
            · <span
                class="text-slate-300"
                title="Quarter-Kelly, and it deserves less trust here than on the baseball card — these probabilities have never been scored on a settled NFL week."
              >stake {(100 * summary.kelly_quarter).toFixed(1)}%</span>
          {/if}
        </span>
      {/if}
    </div>

    <div class="divide-y divide-border/50">
      {#each card as r (r.market + r.key)}
        <div class="px-5 py-2.5 flex items-center justify-between gap-4 text-sm flex-wrap">
          <div class="min-w-0">
            <span class="inline-block px-2 py-0.5 mr-2 text-xs rounded border {sideClass(r.side)}">
              {r.side}
            </span>
            <span class="text-slate-200 font-medium">{r.player}</span>
            <span class="text-slate-500 text-xs"> {r.position ?? ''} {r.team ?? ''}</span>
            {#if r.role_conflict}
              <span
                class="ml-2 px-1.5 py-0.5 rounded text-[10px] bg-amber-600/20 text-amber-300 border border-amber-600/30"
                title="We rank {r.player} differently from the market against his own teammate {r.role_conflict_with}. That is a disagreement about who plays, which the projection cannot see — it reads last season's usage. Tracked, not filtered."
              >role?</span>
            {/if}
            <span class="text-slate-400 text-xs">
              · {r.line} {MARKET_LABEL[r.market]?.toLowerCase() ?? r.market}
            </span>
            <span class="block text-xs text-slate-600">{r.event} · {fmtKickoff(r.kickoff)}</span>
          </div>
          <div class="flex items-center gap-4 tabular-nums text-xs shrink-0">
            <span class="text-slate-500">proj {r.adjusted}</span>
            <span class="text-emerald-300 font-semibold">{fmtPct(r.model_p)}</span>
            <span class="text-slate-400">{fmtOdds(r.odds)}</span>
            <span class={evClass(r.ev)}>{r.edge_pts != null ? `${r.edge_pts >= 0 ? '+' : ''}${r.edge_pts.toFixed(1)}` : '—'}</span>
          </div>
        </div>
      {/each}
    </div>

    {#if card.some((r) => r.role_conflict)}
      <!-- The known systematic error, named on the card itself rather than
           buried in a doc. It is on roughly half the board this week. -->
      <div class="px-5 py-3 border-t border-border bg-amber-950/20 text-xs text-amber-200/90">
        A leg here is marked <span class="font-semibold">role?</span> — we rank that player
        the opposite way to the market against his own teammate. The projection reads last
        season's usage and has no view of snap share, so when a role changed in the offseason
        it describes the wrong player. Kept on the card and tracked so the record can settle
        whether it costs anything.
      </div>
    {/if}

    <div class="px-5 py-4 border-t border-border flex items-center gap-3 flex-wrap">
      {#if data.card?.betslip_url}
        <a
          href={data.card.betslip_url}
          target="_blank"
          rel="noopener noreferrer"
          class="px-3 py-1.5 rounded-lg text-sm font-medium bg-emerald-600 text-white hover:bg-emerald-500"
        >Open in FanDuel</a>
        <button
          class="px-3 py-1.5 rounded-lg text-sm font-medium bg-surface-700 text-slate-300 hover:bg-surface-600"
          on:click={copyBetslip}
        >{copied ? 'Copied' : 'Copy link'}</button>
      {/if}
      <span class="text-xs text-slate-500">
        Lines move all week and FanDuel pulls each market at kickoff — re-check before placing.
      </span>
    </div>
  </section>
{/if}

{#if suggestions.length}
  <section class="card p-0 overflow-hidden">
    <div class="px-5 py-4 border-b border-border flex items-baseline justify-between flex-wrap gap-2">
      <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
        Suggestions
        <span class="ml-2 normal-case font-normal text-xs text-slate-500">
          recorded and scored — this is what the track record is built from
        </span>
      </h2>
      <span class="text-xs text-slate-500 tabular-nums">
        {suggestions.length} this week
        {#if conflicted}
          · <span class="text-amber-400/90" title="Rows where we rank a player the opposite way to the market against his own teammate.">{conflicted} role?</span>
        {/if}
        {#if data.frozen}
          · <span class={data.frozen.error ? 'text-red-400' : 'text-emerald-500/80'}>
            {data.frozen.error ? 'not recorded' : `${data.frozen.written} recorded`}
          </span>
        {/if}
      </span>
    </div>

    {#if rest.length === 0}
      <div class="px-5 py-4 text-sm text-slate-500">
        Everything suggested this week is on the card.
      </div>
    {:else}
      <div class="divide-y divide-border/50">
        {#each shown as r (r.market + r.key)}
          <div class="px-5 py-2 flex items-center justify-between gap-4 text-sm flex-wrap">
            <div class="min-w-0">
              <span class="inline-block px-1.5 py-0.5 mr-2 text-[10px] rounded border {sideClass(r.side)}">
                {r.side}
              </span>
              <span class="text-slate-300">{leg(r)}</span>
              <span class="text-slate-600 text-xs"> · {r.team ?? ''}</span>
              {#if r.role_conflict}
                <span
                  class="ml-1.5 px-1 py-0.5 rounded text-[10px] bg-amber-600/20 text-amber-300 border border-amber-600/30"
                  title="Ranked against teammate {r.role_conflict_with} the opposite way to the market — a disagreement about role, not production."
                >role?</span>
              {/if}
            </div>
            <div class="flex items-center gap-4 tabular-nums text-xs shrink-0">
              <span class="text-slate-500">proj {r.adjusted}</span>
              <span class="text-slate-300">{fmtPct(r.model_p)}</span>
              <span class="text-slate-500">{fmtOdds(r.odds)}</span>
              <span class={evClass(r.ev)}>{r.edge_pts != null ? `${r.edge_pts >= 0 ? '+' : ''}${r.edge_pts.toFixed(1)}` : '—'}</span>
            </div>
          </div>
        {/each}
      </div>
      {#if rest.length > 8}
        <button
          class="w-full px-5 py-2 text-xs text-slate-400 hover:text-slate-200 hover:bg-surface-600/40 border-t border-border"
          on:click={() => (showAll = !showAll)}
        >{showAll ? 'Show fewer' : `Show all ${rest.length}`}</button>
      {/if}
    {/if}
  </section>
{/if}
