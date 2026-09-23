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
  let showOut = false;
  $: card = data.card?.legs ?? [];
  $: summary = data.card?.summary;
  $: suggestions = data.suggestions ?? [];
  $: rest = suggestions.filter((s) => !card.some((c) => c.key === s.key && c.market === s.market));
  $: shown = showAll ? rest : rest.slice(0, 8);
  $: conflicted = suggestions.filter((s) => s.role_conflict).length;
  $: avail = data.availability;
  $: withheld = [...(avail?.withheld_unavailable ?? []),
                 ...(avail?.withheld_vacated_under ?? [])];
  $: promoted = suggestions.filter((s) => s.vacated_share).length;

  /** The depth chart's own words for a rank, which is how the market talks
   *  about it. RB1 says more than "rank 1". */
  function depthLabel(r: NflProp): string | null {
    if (r.depth_rank == null || !r.position) return null;
    return `${r.position}${r.depth_rank}`;
  }

  function roleTitle(r: NflProp): string {
    const base = `We rank ${r.player} differently from the market against his own teammate ${r.role_conflict_with}. That is a disagreement about who plays, which the projection cannot see — it reads last season's usage. Tracked, not filtered.`;
    if (r.role_conflict_verdict === 'market')
      return `${base}\n\nThe depth chart sides with the market here, so our number is most likely the stale one.`;
    if (r.role_conflict_verdict === 'model')
      return `${base}\n\nThe depth chart sides with us here.`;
    return `${base}\n\nThe depth chart has no view either way.`;
  }

  function inheritedTitle(r: NflProp): string {
    const who = (r.vacated_by ?? []).join(', ');
    return `${who} ${(r.vacated_by?.length ?? 0) > 1 ? 'are' : 'is'} out, leaving ${Math.round(100 * (r.vacated_share ?? 0))}% of this position group's recent production to the men who remain. Our projection is a season-long rate for the committee that no longer exists, so it reads low — which is why the under is refused on rows like this.`;
  }

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
            {#if depthLabel(r)}
              <span
                class="ml-2 px-1.5 py-0.5 rounded text-[10px] bg-surface-700 text-slate-400 border border-border tabular-nums"
                title="Position rank on the depth chart published {avail?.depth_as_of ?? 'this week'}."
              >{depthLabel(r)}</span>
            {/if}
            {#if r.avail === 'QUESTIONABLE'}
              <span
                class="ml-1.5 px-1.5 py-0.5 rounded text-[10px] bg-yellow-600/20 text-yellow-300 border border-yellow-600/30"
                title="Questionable — {r.avail_reason}. Most questionable players play and the market has priced the chance he does not, so this is shown rather than refused."
              >Q</span>
            {/if}
            {#if r.vacated_share}
              <span
                class="ml-1.5 px-1.5 py-0.5 rounded text-[10px] bg-sky-600/20 text-sky-300 border border-sky-600/30"
                title={inheritedTitle(r)}
              >inherited {Math.round(100 * r.vacated_share)}%</span>
            {/if}
            {#if r.role_conflict}
              <span
                class="ml-1.5 px-1.5 py-0.5 rounded text-[10px] bg-amber-600/20 text-amber-300 border border-amber-600/30"
                title={roleTitle(r)}
              >role?{r.role_conflict_verdict === 'market' ? ' ✗' : r.role_conflict_verdict === 'model' ? ' ✓' : ''}</span>
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
        whether it costs anything. A <span class="font-semibold">✗</span> means the depth
        chart sides with the market and our number is the stale one; a
        <span class="font-semibold">✓</span> means it sides with us.
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
        {#if promoted}
          · <span class="text-sky-400/90" title="Rows where a teammate at the same position is out, so this player has inherited work the projection has never seen him take.">{promoted} inherited</span>
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
              {#if depthLabel(r)}
                <span
                  class="ml-1.5 px-1 py-0.5 rounded text-[10px] bg-surface-700 text-slate-400 border border-border tabular-nums"
                  title="Position rank on the depth chart published {avail?.depth_as_of ?? 'this week'}."
                >{depthLabel(r)}</span>
              {/if}
              {#if r.avail === 'QUESTIONABLE'}
                <span
                  class="ml-1.5 px-1 py-0.5 rounded text-[10px] bg-yellow-600/20 text-yellow-300 border border-yellow-600/30"
                  title="Questionable — {r.avail_reason}."
                >Q</span>
              {/if}
              {#if r.vacated_share}
                <span
                  class="ml-1.5 px-1 py-0.5 rounded text-[10px] bg-sky-600/20 text-sky-300 border border-sky-600/30"
                  title={inheritedTitle(r)}
                >inherited {Math.round(100 * r.vacated_share)}%</span>
              {/if}
              {#if r.role_conflict}
                <span
                  class="ml-1.5 px-1 py-0.5 rounded text-[10px] bg-amber-600/20 text-amber-300 border border-amber-600/30"
                  title={roleTitle(r)}
                >role?{r.role_conflict_verdict === 'market' ? ' ✗' : r.role_conflict_verdict === 'model' ? ' ✓' : ''}</span>
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

{#if avail}
  <!-- The injury read, on the page rather than in a log.
       Two different things live here and they are stacked in the order that
       matters. What was taken *off* the card comes first — a guard nobody can
       see is a guard nobody revisits, and these are picks that would have been
       made yesterday. The vacancies underneath are the reason: they are where
       the projection is about to be wrong, whether or not a line is posted on
       it yet. -->
  <section class="card p-0 overflow-hidden">
    <div class="px-5 py-4 border-b border-border flex items-baseline justify-between flex-wrap gap-2">
      <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">
        Injury report
        <span class="ml-2 normal-case font-normal text-xs text-slate-500">
          roster designations, the official report, and the depth chart — re-read on every build
        </span>
      </h2>
      <span class="text-xs text-slate-500 tabular-nums">
        {avail.counts?.OUT ?? 0} out
        {#if avail.counts?.QUESTIONABLE}· {avail.counts.QUESTIONABLE} questionable{/if}
        {#if avail.depth_as_of}
          · <span title="When the depth chart this board used was published. It is republished about twice a day.">chart {avail.depth_as_of.slice(5, 16).replace('T', ' ')}</span>
        {/if}
        {#if avail.errors?.length}
          · <span class="text-red-400" title={avail.errors.join('; ')}>feed error</span>
        {/if}
      </span>
    </div>

    {#if withheld.length}
      <div class="px-5 py-3 border-b border-border/60">
        <div class="text-xs text-slate-400 mb-2">
          Kept off the card this week:
        </div>
        <div class="space-y-1">
          {#each withheld as r}
            <div class="text-xs text-slate-300 flex items-baseline gap-2 flex-wrap">
              <span class="px-1 py-0.5 rounded text-[10px] border {r.avail
                ? 'bg-red-600/20 text-red-300 border-red-600/30'
                : 'bg-sky-600/20 text-sky-300 border-sky-600/30'}">
                {r.avail ?? 'inherited role'}
              </span>
              <span class="text-slate-200">{r.player}</span>
              <span class="text-slate-500">
                {r.side?.toLowerCase()} {r.line} {MARKET_LABEL[r.market ?? '']?.toLowerCase() ?? r.market}
                · {r.team}
              </span>
              <span class="text-slate-600">
                {#if r.avail_reason}
                  {r.avail_reason}
                {:else if r.vacated_by}
                  {r.vacated_by.join(', ')} out — {Math.round(100 * (r.vacated_share ?? 0))}% of the group's work is now his
                {/if}
              </span>
            </div>
          {/each}
        </div>
      </div>
    {/if}

    {#if avail.vacancies?.length}
      <div class="px-5 py-3">
        <div class="text-xs text-slate-400 mb-2">
          Position groups that have lost work. The men who remain are being priced in a
          role our projection has never seen them hold — an
          <span class="text-amber-300">under</span> on one of them is refused above
          {Math.round(100 * avail.vacated_share_blocks_under)}%.
        </div>
        <div class="space-y-1">
          {#each (showOut ? avail.vacancies : avail.vacancies.slice(0, 6)) as v}
            <div class="text-xs flex items-baseline gap-2 flex-wrap">
              <span class="tabular-nums w-10 shrink-0 {v.share >= avail.vacated_share_blocks_under ? 'text-sky-300 font-semibold' : 'text-slate-500'}">
                {Math.round(100 * v.share)}%
              </span>
              <span class="text-slate-200 w-16 shrink-0">{v.team} {v.position}</span>
              <span class="text-slate-500 w-32 shrink-0">
                {MARKET_LABEL[v.component]?.toLowerCase() ?? v.component.replace('_', ' ')}
              </span>
              <span class="text-slate-400">
                {v.players.map((p) => `${p.player} (${p.reason})`).join(', ')}
              </span>
            </div>
          {/each}
        </div>
        {#if avail.vacancies.length > 6}
          <button
            class="mt-2 text-xs text-slate-400 hover:text-slate-200"
            on:click={() => (showOut = !showOut)}
          >{showOut ? 'Show fewer' : `Show all ${avail.vacancies.length}`}</button>
        {/if}
      </div>
    {:else if !withheld.length}
      <div class="px-5 py-4 text-sm text-slate-500">
        Nobody's absence has moved a position group enough to matter this week.
      </div>
    {/if}
  </section>
{/if}
