<script lang="ts">
  /** The week's card, the wider suggestion list, and the injury read.
   *
   *  Tables, not prose. Every "why" that used to sit in a paragraph is now a
   *  title attribute on the column or chip it explains — the reasoning has not
   *  been deleted, it has been moved off the page and into hover.
   */
  import { fmtOdds, fmtPct, fmtEv, evClass, MARKET_LABEL, fmtKickoff, shortEvent } from '$lib/nflScreen';
  import type { NflScreen, NflProp } from '$lib/types';

  export let data: NflScreen;

  let showAll = false;
  let showAllVacancies = false;
  $: card = data.card?.legs ?? [];
  $: summary = data.card?.summary;
  $: suggestions = data.suggestions ?? [];
  $: rest = suggestions.filter((s) => !card.some((c) => c.key === s.key && c.market === s.market));
  $: shown = showAll ? rest : rest.slice(0, 10);
  $: conflicted = suggestions.filter((s) => s.role_conflict).length;
  $: promoted = suggestions.filter((s) => s.vacated_share).length;
  $: avail = data.availability;
  $: withheld = [...(avail?.withheld_unavailable ?? []),
                 ...(avail?.withheld_vacated_under ?? [])];
  $: vacancies = avail?.vacancies ?? [];
  $: shownVacancies = showAllVacancies ? vacancies : vacancies.slice(0, 8);

  /** The depth chart's own words for a rank — RB1 says more than "rank 1". */
  function depthLabel(r: NflProp): string | null {
    if (r.depth_rank == null || !r.position) return null;
    return `${r.position}${r.depth_rank}`;
  }

  function roleTitle(r: NflProp): string {
    const verdict = r.role_conflict_verdict === 'market'
      ? 'Depth chart sides with the market — our number is likely the stale one.'
      : r.role_conflict_verdict === 'model'
        ? 'Depth chart sides with us.'
        : 'Depth chart has no view.';
    return `Ranked against teammate ${r.role_conflict_with} the opposite way to the market — a disagreement about role, not production. ${verdict}`;
  }

  function inheritedTitle(r: NflProp): string {
    const who = (r.vacated_by ?? []).join(', ');
    const pct = Math.round(100 * (r.vacated_share ?? 0));
    return `${who} out — ${pct}% of this position group's recent production is now shared among the men who remain. The projection still describes the old committee, so an UNDER here is refused.`;
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

  function sideClass(side: string | null | undefined): string {
    return side === 'UNDER'
      ? 'bg-amber-600/20 text-amber-300 border-amber-600/30'
      : 'bg-emerald-600/20 text-emerald-300 border-emerald-600/30';
  }
</script>

{#if card.length}
  <section class="card p-0 overflow-hidden border-emerald-800/50">
    <div class="px-5 py-4 border-b border-border flex items-baseline justify-between flex-wrap gap-2">
      <h2 class="text-sm font-semibold text-emerald-300 uppercase tracking-wider">
        Week {data.week} Card
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
                title="Quarter-Kelly, already divided. These probabilities have never been scored on a settled NFL week."
              >stake {(100 * summary.kelly_quarter).toFixed(1)}%</span>
          {/if}
        </span>
      {/if}
    </div>

    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
            <th class="text-left px-4 py-2.5">Bet</th>
            <th class="text-left px-4 py-2.5">Player</th>
            <th class="text-left px-4 py-2.5">Market</th>
            <th class="text-right px-4 py-2.5">Line</th>
            <th class="text-right px-4 py-2.5" title="The projection restated on the market's scale.">Proj</th>
            <th class="text-right px-4 py-2.5">Model</th>
            <th class="text-right px-4 py-2.5">Price</th>
            <th class="text-right px-4 py-2.5" title="Model probability minus the devigged market probability, in points.">Edge</th>
            <th class="text-left px-4 py-2.5">Game</th>
          </tr>
        </thead>
        <tbody>
          {#each card as r (r.market + r.key)}
            <tr class="border-b border-border/50">
              <td class="px-4 py-2.5">
                <span class="inline-block px-2 py-0.5 text-xs rounded border {sideClass(r.side)}">{r.side}</span>
              </td>
              <td class="px-4 py-2.5 whitespace-nowrap">
                <span class="text-slate-200 font-medium">{r.player}</span>
                <span class="text-xs text-slate-500"> {r.team ?? ''}</span>
                {#if depthLabel(r)}
                  <span
                    class="ml-1.5 px-1 py-0.5 rounded text-[10px] bg-surface-700 text-slate-400 border border-border tabular-nums"
                    title="Depth chart rank, published {avail?.depth_as_of ?? 'this week'}."
                  >{depthLabel(r)}</span>
                {/if}
                {#if r.avail === 'QUESTIONABLE'}
                  <span
                    class="ml-1 px-1 py-0.5 rounded text-[10px] bg-yellow-600/20 text-yellow-300 border border-yellow-600/30"
                    title="Questionable — {r.avail_reason}."
                  >Q</span>
                {/if}
                {#if r.vacated_share}
                  <span
                    class="ml-1 px-1 py-0.5 rounded text-[10px] bg-sky-600/20 text-sky-300 border border-sky-600/30 tabular-nums"
                    title={inheritedTitle(r)}
                  >+{Math.round(100 * r.vacated_share)}%</span>
                {/if}
                {#if r.role_conflict}
                  <span
                    class="ml-1 px-1 py-0.5 rounded text-[10px] bg-amber-600/20 text-amber-300 border border-amber-600/30"
                    title={roleTitle(r)}
                  >role?{r.role_conflict_verdict === 'market' ? ' ✗' : r.role_conflict_verdict === 'model' ? ' ✓' : ''}</span>
                {/if}
              </td>
              <td class="px-4 py-2.5 text-slate-400">{MARKET_LABEL[r.market] ?? r.market}</td>
              <td class="px-4 py-2.5 text-right tabular-nums text-slate-200">{r.line}</td>
              <td class="px-4 py-2.5 text-right tabular-nums text-slate-400">{r.adjusted}</td>
              <td class="px-4 py-2.5 text-right tabular-nums text-emerald-300 font-semibold">{fmtPct(r.model_p)}</td>
              <td class="px-4 py-2.5 text-right tabular-nums text-slate-300">{fmtOdds(r.odds)}</td>
              <td class="px-4 py-2.5 text-right tabular-nums {evClass(r.ev)}">
                {r.edge_pts != null ? `${r.edge_pts >= 0 ? '+' : ''}${r.edge_pts.toFixed(1)}` : '—'}
              </td>
              <td class="px-4 py-2.5 text-slate-500 text-xs whitespace-nowrap" title={r.event ?? ''}>
                {shortEvent(r.event)}
                <span class="block text-slate-600">{fmtKickoff(r.kickoff)}</span>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <div class="px-5 py-3 border-t border-border flex items-center gap-3 flex-wrap">
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
      <span class="text-xs text-slate-500">Re-check before placing — lines move and FanDuel pulls each market at kickoff.</span>
    </div>
  </section>
{/if}

{#if suggestions.length}
  <section class="card p-0 overflow-hidden">
    <div class="px-5 py-4 border-b border-border flex items-baseline justify-between flex-wrap gap-2">
      <h2
        class="text-sm font-semibold text-slate-300 uppercase tracking-wider"
        title="Every row past the threshold with enough edge to record. Wider than the card — this is what the track record is built from."
      >Suggestions</h2>
      <span class="text-xs text-slate-500 tabular-nums">
        {suggestions.length}
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
      <div class="px-5 py-4 text-sm text-slate-500">Everything suggested this week is on the card.</div>
    {:else}
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-border text-xs font-medium text-slate-400 uppercase tracking-wider">
              <th class="text-left px-4 py-2.5">Bet</th>
              <th class="text-left px-4 py-2.5">Player</th>
              <th class="text-left px-4 py-2.5">Market</th>
              <th class="text-right px-4 py-2.5">Line</th>
              <th class="text-right px-4 py-2.5" title="The projection restated on the market's scale.">Proj</th>
              <th class="text-right px-4 py-2.5">Model</th>
              <th class="text-right px-4 py-2.5">Price</th>
              <th class="text-right px-4 py-2.5" title="Model probability minus the devigged market probability, in points.">Edge</th>
              <th class="text-left px-4 py-2.5">Game</th>
            </tr>
          </thead>
          <tbody>
            {#each shown as r (r.market + r.key)}
              <tr class="border-b border-border/50 hover:bg-surface-600/30">
                <td class="px-4 py-2">
                  <span class="inline-block px-1.5 py-0.5 text-[10px] rounded border {sideClass(r.side)}">{r.side}</span>
                </td>
                <td class="px-4 py-2 whitespace-nowrap">
                  <span class="text-slate-200">{r.player}</span>
                  <span class="text-xs text-slate-600"> {r.team ?? ''}</span>
                  {#if depthLabel(r)}
                    <span
                      class="ml-1.5 px-1 py-0.5 rounded text-[10px] bg-surface-700 text-slate-400 border border-border tabular-nums"
                      title="Depth chart rank, published {avail?.depth_as_of ?? 'this week'}."
                    >{depthLabel(r)}</span>
                  {/if}
                  {#if r.avail === 'QUESTIONABLE'}
                    <span
                      class="ml-1 px-1 py-0.5 rounded text-[10px] bg-yellow-600/20 text-yellow-300 border border-yellow-600/30"
                      title="Questionable — {r.avail_reason}."
                    >Q</span>
                  {/if}
                  {#if r.vacated_share}
                    <span
                      class="ml-1 px-1 py-0.5 rounded text-[10px] bg-sky-600/20 text-sky-300 border border-sky-600/30 tabular-nums"
                      title={inheritedTitle(r)}
                    >+{Math.round(100 * r.vacated_share)}%</span>
                  {/if}
                  {#if r.role_conflict}
                    <span
                      class="ml-1 px-1 py-0.5 rounded text-[10px] bg-amber-600/20 text-amber-300 border border-amber-600/30"
                      title={roleTitle(r)}
                    >role?{r.role_conflict_verdict === 'market' ? ' ✗' : r.role_conflict_verdict === 'model' ? ' ✓' : ''}</span>
                  {/if}
                </td>
                <td class="px-4 py-2 text-slate-400">{MARKET_LABEL[r.market] ?? r.market}</td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-200">{r.line}</td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-400">{r.adjusted}</td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-300">{fmtPct(r.model_p)}</td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-400">{fmtOdds(r.odds)}</td>
                <td class="px-4 py-2 text-right tabular-nums {evClass(r.ev)}">
                  {r.edge_pts != null ? `${r.edge_pts >= 0 ? '+' : ''}${r.edge_pts.toFixed(1)}` : '—'}
                </td>
                <td class="px-4 py-2 text-slate-600 text-xs whitespace-nowrap" title={r.event ?? ''}>
                  {shortEvent(r.event)}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      {#if rest.length > 10}
        <button
          class="w-full px-5 py-2 text-xs text-slate-400 hover:text-slate-200 hover:bg-surface-600/40 border-t border-border"
          on:click={() => (showAll = !showAll)}
        >{showAll ? 'Show fewer' : `Show all ${rest.length}`}</button>
      {/if}
    {/if}
  </section>
{/if}

{#if avail}
  <section class="card p-0 overflow-hidden">
    <div class="px-5 py-4 border-b border-border flex items-baseline justify-between flex-wrap gap-2">
      <h2
        class="text-sm font-semibold text-slate-300 uppercase tracking-wider"
        title="Roster designations, the official injury report and the depth chart, re-read on every build. A player who is out never reaches the card; an UNDER on a player whose position group lost a third of its work is refused."
      >Injury report</h2>
      <span class="text-xs text-slate-500 tabular-nums">
        {avail.counts?.OUT ?? 0} out
        {#if avail.counts?.QUESTIONABLE}· {avail.counts.QUESTIONABLE} questionable{/if}
        {#if avail.depth_as_of}
          · <span title="When the depth chart this board used was published.">chart {avail.depth_as_of.slice(5, 16).replace('T', ' ')}</span>
        {/if}
        {#if avail.errors?.length}
          · <span class="text-red-400" title={avail.errors.join('; ')}>feed error</span>
        {/if}
      </span>
    </div>

    {#if withheld.length}
      <div class="overflow-x-auto border-b border-border">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-border/60 text-xs font-medium text-slate-400 uppercase tracking-wider">
              <th class="text-left px-4 py-2" title="Rows that cleared every other bar and were dropped by an availability guard.">Withheld</th>
              <th class="text-left px-4 py-2">Player</th>
              <th class="text-left px-4 py-2">Bet</th>
              <th class="text-right px-4 py-2">Edge</th>
              <th class="text-left px-4 py-2">Reason</th>
            </tr>
          </thead>
          <tbody>
            {#each withheld as r}
              <tr class="border-b border-border/40">
                <td class="px-4 py-2">
                  <span class="px-1.5 py-0.5 rounded text-[10px] border {r.avail
                    ? 'bg-red-600/20 text-red-300 border-red-600/30'
                    : 'bg-sky-600/20 text-sky-300 border-sky-600/30'}">
                    {r.avail ?? 'INHERITED'}
                  </span>
                </td>
                <td class="px-4 py-2 text-slate-200 whitespace-nowrap">
                  {r.player}<span class="text-xs text-slate-600"> {r.team ?? ''}</span>
                </td>
                <td class="px-4 py-2 text-slate-400 whitespace-nowrap">
                  {r.side?.toLowerCase()} {r.line} {MARKET_LABEL[r.market ?? '']?.toLowerCase() ?? r.market}
                </td>
                <td class="px-4 py-2 text-right tabular-nums text-slate-400">
                  {r.edge_pts != null ? `+${r.edge_pts.toFixed(1)}` : '—'}
                </td>
                <td class="px-4 py-2 text-slate-500 text-xs">
                  {#if r.avail_reason}{r.avail_reason}
                  {:else if r.vacated_by}{r.vacated_by.join(', ')} out · {Math.round(100 * (r.vacated_share ?? 0))}%{/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}

    {#if vacancies.length}
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-border/60 text-xs font-medium text-slate-400 uppercase tracking-wider">
              <th
                class="text-right px-4 py-2"
                title="Share of the group's recent per-game production belonging to players who are out. An UNDER is refused above {Math.round(100 * (avail.vacated_share_blocks_under ?? 0))}%."
              >Vacated</th>
              <th class="text-left px-4 py-2">Group</th>
              <th class="text-left px-4 py-2">Market</th>
              <th class="text-left px-4 py-2">Out</th>
            </tr>
          </thead>
          <tbody>
            {#each shownVacancies as v}
              <tr class="border-b border-border/40">
                <td class="px-4 py-2 text-right tabular-nums {v.share >= (avail.vacated_share_blocks_under ?? 1) ? 'text-sky-300 font-semibold' : 'text-slate-500'}">
                  {Math.round(100 * v.share)}%
                </td>
                <td class="px-4 py-2 text-slate-200 whitespace-nowrap">{v.team} {v.position}</td>
                <td class="px-4 py-2 text-slate-400 whitespace-nowrap">
                  {MARKET_LABEL[v.component] ?? v.component.replace(/_/g, ' ')}
                </td>
                <td class="px-4 py-2 text-slate-400 text-xs">
                  {v.players.map((p) => `${p.player} (${p.reason})`).join(', ')}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      {#if vacancies.length > 8}
        <button
          class="w-full px-5 py-2 text-xs text-slate-400 hover:text-slate-200 hover:bg-surface-600/40 border-t border-border"
          on:click={() => (showAllVacancies = !showAllVacancies)}
        >{showAllVacancies ? 'Show fewer' : `Show all ${vacancies.length}`}</button>
      {/if}
    {:else if !withheld.length}
      <div class="px-5 py-4 text-sm text-slate-500">No absence has moved a position group this week.</div>
    {/if}
  </section>
{/if}
