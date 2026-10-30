<script lang="ts">
  /** Alt-line parlays: a double-up and a thousand-to-one lotto.
   *
   *  Built from FanDuel's alternate ladders. A leg has to have been hitting
   *  (three of the player's last four games) and be priced by the model above
   *  what the book charges; the chance shown is the record-calibrated one,
   *  not the model's own. See backend nfl/altparlay.py.
   */
  import { onMount, onDestroy } from 'svelte';
  import { peek } from '$lib/cache';
  import { getNflRecommendations, getNflPlaced, placeNflBet, setNflPlacedStake, undoNflPlaced } from '$lib/api';
  import {
    NFL_CACHE_KEY, load as loadScreen, initialState,
    fmtOdds, fmtPct, fmtSigned, fmtKickoff, MARKET_LABEL, shortEvent,
  } from '$lib/nflScreen';
  import type { NflScreen, NflAltKind, NflAltParlay, NflRecommendations, NflLedgerBucket, NflPlacedBet } from '$lib/types';

  let s = initialState(peek<NflScreen>(NFL_CACHE_KEY));
  $: ({ data, loading, error, warming, warmingElapsed } = s);

  let rec: NflRecommendations | null = null;
  let cancel: (() => void) | null = null;
  function go(force = false) {
    cancel?.();
    cancel = loadScreen((patch) => (s = { ...s, ...patch }), force);
  }
  onMount(() => {
    go();
    getNflRecommendations().then((r) => (rec = r)).catch(() => (rec = null));
    loadPlaced();
  });

  // What was actually bet. The tickets above rebuild with the board until a
  // leg locks; a placed bet is written once, as it was on screen, and kept.
  let placed: NflPlacedBet[] = [];
  let placeError = '';
  function loadPlaced() {
    getNflPlaced().then((r) => (placed = r.bets)).catch(() => (placed = []));
  }
  $: thisWeek = placed.filter((b) => data && b.season === data.season && b.week === data.week);
  const sig = (legs: { player: string; market: string; line: number }[]) =>
    legs.map((l) => `${l.player}|${l.market}|${l.line}`).sort().join('~');
  $: placedSigs = new Set(thisWeek.map((b) => sig(b.legs)));

  async function place(k: NflAltKind, t: NflAltParlay) {
    if (!data || placedSigs.has(sig(t.legs))) return;
    placeError = '';
    try {
      await placeNflBet({
        season: data.season, week: data.week,
        label: k === 'lotto' ? 'Alt lotto' : 'Alt double-up', source: `alt:${k}`,
        american: t.american, stake: t.stake, model_p: t.model_p,
        legs: t.legs.map((l) => ({ ...l })),
      });
      loadPlaced();
    } catch (e) {
      placeError = e instanceof Error ? e.message : String(e);
    }
  }
  async function changeStake(b: NflPlacedBet, v: string) {
    const stake = parseFloat(v);
    if (!(stake > 0) || stake === b.stake) return;
    await setNflPlacedStake(b.id, stake).catch(() => {});
    loadPlaced();
  }
  async function undo(b: NflPlacedBet) {
    await undoNflPlaced(b.id).catch(() => {});
    loadPlaced();
  }
  onDestroy(() => cancel?.());

  const KINDS: { key: NflAltKind; title: string; blurb: string }[] = [
    { key: 'double_up', title: 'Double-up',
      blurb: 'The likeliest ticket that pays at least even money.' },
    { key: 'lotto', title: 'Lotto',
      blurb: 'The likeliest ticket that pays at least 1000x. Needs a full slate to reach.' },
  ];

  $: rules = data?.alt_rules;
  $: cands = [...(data?.alt_candidates ?? [])].sort((a, b) => b.p_cal - a.p_cal);
  $: parlayRec = rec?.all_time.by_type.parlay;
  $: tickets = (rec?.ledger ?? []).filter((e) => e.type === 'parlay');

  function ticket(k: NflAltKind): NflAltParlay | null | undefined {
    return data?.alt_parlays?.[k];
  }
  function fmtChance(p: number): string {
    if (p >= 0.01) return fmtPct(p, 1);
    return `1 in ${Math.round(1 / p).toLocaleString()}`;
  }
  function recLine(b: NflLedgerBucket | undefined): string {
    if (!b || b.bets === 0) return 'no tickets recorded yet';
    const roi = b.roi != null ? `${fmtSigned(b.roi)}% ROI` : 'none settled';
    return `${b.wins}-${b.losses} · ${fmtSigned(b.units, 2)}u on ${b.staked ?? 0}u staked · ${roi}`;
  }
  const last4 = (v: number[]) => v.map((x) => (Number.isInteger(x) ? x : x.toFixed(1))).join(' · ');

  let copied: NflAltKind | null = null;
  async function copy(k: NflAltKind, url: string) {
    try {
      await navigator.clipboard.writeText(url);
      copied = k;
      setTimeout(() => (copied = null), 2000);
    } catch {
      // Clipboard is permission-gated; the link is on the page.
    }
  }
</script>

<svelte:head><title>NFL Alt Parlays — Sharp Edge</title></svelte:head>

<div class="space-y-6">
  <div class="flex items-center justify-between flex-wrap gap-3">
    <p class="text-sm text-slate-400">
      {#if data}Week {data.week}, {data.season} —{/if}
      parlays from alt lines the player has been clearing
    </p>
    <button
      class="px-3 py-1.5 rounded-lg text-sm font-medium bg-surface-700 text-slate-300 hover:bg-surface-600 disabled:opacity-50"
      on:click={() => go(true)} disabled={loading}
    >{loading ? 'Loading…' : 'Refresh'}</button>
  </div>

  {#if error}
    <div class="card border-red-800 bg-red-950/30 text-red-300 text-sm">{error}</div>
  {/if}

  {#if loading && !data}
    <div class="card text-slate-400 text-sm flex items-center gap-3">
      <div class="w-4 h-4 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin"></div>
      {warming ? `Building this week's NFL board${warmingElapsed != null ? ` (${Math.round(warmingElapsed)}s elapsed)` : ''}…` : 'Loading board…'}
    </div>
  {:else if data}
    {#if data.preseason}
      <div class="card text-sm text-amber-300/90">
        The projection is still the preseason one, so no alt legs are priced yet. The in-season projection lands Tuesday night.
      </div>
    {/if}

    <div class="card border-amber-900/60 bg-amber-950/20 text-xs text-amber-200/80 py-2">
      <strong class="text-amber-300">Expect these to lose money on average.</strong>
      Every leg pays FanDuel's margin and a parlay multiplies it. The chance shown is what the record says
      this model's legs actually land at, not what the model claims, so expected value below is usually negative.
      Record: {recLine(parlayRec)}.
    </div>

    <section class="card p-0 overflow-hidden">
      <div class="px-5 py-3 border-b border-border flex items-baseline justify-between flex-wrap gap-2">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Your bets this week</h2>
        <span class="text-xs text-slate-500">recorded when you open a ticket on FanDuel; kept as placed</span>
      </div>
      {#if placeError}<div class="px-5 py-2 text-xs text-red-300">{placeError}</div>{/if}
      {#if thisWeek.length === 0}
        <div class="px-5 py-4 text-sm text-slate-500">Nothing placed yet this week.</div>
      {:else}
        <div class="divide-y divide-border/50">
          {#each thisWeek as b (b.id)}
            <div class="px-5 py-3 text-sm">
              <div class="flex items-center gap-3 flex-wrap">
                <span class="text-slate-100 font-medium">{b.label ?? 'Parlay'} {fmtOdds(b.odds)}</span>
                <span class="text-xs px-1.5 py-0.5 rounded border border-border {b.result === 'WIN' ? 'text-emerald-300' : b.result === 'LOSS' ? 'text-rose-300' : 'text-slate-400'}">{b.result ?? 'PENDING'}{#if b.actual} · {b.actual}{/if}</span>
                <span class="ml-auto flex items-center gap-2 text-xs text-slate-400">
                  {#if b.result}
                    {b.stake}u · <span class={b.units != null && b.units >= 0 ? 'text-emerald-400' : 'text-red-400'}>{b.units != null ? `${fmtSigned(b.units, 2)}u` : ''}</span>
                  {:else}
                    stake
                    <input type="number" min="0.01" step="0.1" value={b.stake}
                      class="w-16 px-1.5 py-0.5 rounded bg-surface-700 text-slate-200 text-right"
                      on:change={(e) => changeStake(b, e.currentTarget.value)} />u
                    <button class="px-2 py-0.5 rounded bg-surface-700 hover:bg-surface-600" on:click={() => undo(b)}>undo</button>
                  {/if}
                </span>
              </div>
              <div class="text-xs text-slate-500 mt-1">
                {b.legs.map((l) => `${l.player} ${l.threshold ?? l.line}${l.threshold ? '+' : ''} ${MARKET_LABEL[l.market] ?? l.market} (${fmtOdds(l.odds)})`).join(' · ')}
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </section>

    <div class="grid gap-4 lg:grid-cols-2">
      {#each KINDS as k}
        {@const t = ticket(k.key)}
        <section class="card p-0 overflow-hidden">
          <div class="px-5 py-4 border-b border-border flex items-baseline justify-between gap-2 flex-wrap">
            <div>
              <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">{k.title}</h2>
              <p class="text-xs text-slate-500 mt-0.5">{k.blurb}</p>
            </div>
            {#if t}
              <span class="text-lg tabular-nums text-slate-100">{fmtOdds(t.american)}</span>
            {/if}
          </div>
          {#if !t}
            <div class="px-5 py-4 text-sm text-slate-500">
              {#if k.key === 'lotto'}
                Not enough qualifying legs across separate games to reach 1000x yet. Most weeks this fills in once Sunday's ladders are posted.
              {:else}
                No two legs from separate games qualify right now.
              {/if}
            </div>
          {:else}
            <div class="px-5 py-3 grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm border-b border-border/50">
              <div><div class="text-xs text-slate-500">Legs</div><div class="tabular-nums text-slate-200">{t.leg_count}</div></div>
              <div><div class="text-xs text-slate-500">Chance</div><div class="tabular-nums text-slate-200">{fmtChance(t.model_p)}</div></div>
              <div><div class="text-xs text-slate-500">Stake → win</div><div class="tabular-nums text-slate-200">{t.stake}u → {t.to_win.toLocaleString()}u</div></div>
              <div><div class="text-xs text-slate-500">EV</div><div class="tabular-nums {t.ev >= 0 ? 'text-emerald-400' : 'text-red-400'}">{fmtSigned(100 * t.ev)}%</div></div>
            </div>
            <div class="overflow-x-auto">
              <table class="w-full text-sm tabular-nums">
                <tbody>
                  {#each t.legs as l}
                    <tr class="border-t border-border/40">
                      <td class="px-5 py-2">
                        <div class="text-slate-200">{l.player} {l.threshold}+ {MARKET_LABEL[l.market] ?? l.market}</div>
                        <div class="text-xs text-slate-500">{shortEvent(l.event)} · {fmtKickoff(l.kickoff)} · last 4: {last4(l.last4)}</div>
                      </td>
                      <td class="px-2 py-2 text-right text-slate-300">{fmtOdds(l.odds)}</td>
                      <td class="px-5 py-2 text-right text-slate-400" title="Record-calibrated chance">{fmtPct(l.p_cal, 0)}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
            {#if t.betslip_url}
              {@const done = placedSigs.has(sig(t.legs))}
              <div class="px-5 py-3 border-t border-border flex gap-2 flex-wrap items-center">
                <a href={t.betslip_url} target="_blank" rel="noopener"
                   on:click={() => t && place(k.key, t)}
                   title="Opens the slip on FanDuel and records this ticket as placed"
                   class="px-3 py-1.5 rounded-lg text-xs font-medium bg-indigo-600 text-white hover:bg-indigo-500">Open on FanDuel</a>
                <button class="px-3 py-1.5 rounded-lg text-xs font-medium bg-surface-700 text-slate-300 hover:bg-surface-600"
                        on:click={() => t?.betslip_url && copy(k.key, t.betslip_url)}>{copied === k.key ? 'Copied' : 'Copy link'}</button>
                {#if done}
                  <span class="text-xs text-emerald-400">✓ placed — in Your bets</span>
                {:else}
                  <button class="px-3 py-1.5 rounded-lg text-xs font-medium bg-surface-700 text-slate-300 hover:bg-surface-600"
                          title="Record this ticket as placed without opening FanDuel"
                          on:click={() => t && place(k.key, t)}>I placed this</button>
                {/if}
              </div>
            {/if}
          {/if}
        </section>
      {/each}
    </div>

    <section class="card p-0 overflow-hidden">
      <div class="px-5 py-3 border-b border-border flex items-baseline justify-between flex-wrap gap-2">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Qualifying legs ({cands.length})</h2>
        {#if rules}
          <span class="text-xs text-slate-500">
            {rules.rungs_priced.toLocaleString()} rungs priced · cleared in {rules.min_hits}+ of last {rules.record_games} ·
            edge {rules.min_edge_pts}–{rules.max_edge_pts} pts · one leg per game
          </span>
        {/if}
      </div>
      {#if cands.length === 0}
        <div class="px-5 py-4 text-sm text-slate-500">No alt rung clears every rule right now.</div>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full text-sm tabular-nums">
            <thead><tr class="text-xs text-slate-500 uppercase tracking-wider">
              <th class="text-left px-5 py-2 font-medium">Leg</th>
              <th class="text-left px-2 py-2 font-medium">Last 4</th>
              <th class="text-right px-2 py-2 font-medium">Price</th>
              <th class="text-right px-2 py-2 font-medium" title="Quoted, vig included">Implied</th>
              <th class="text-right px-2 py-2 font-medium" title="Record-calibrated">Chance</th>
              <th class="text-right px-5 py-2 font-medium" title="Model vs fair price">Edge</th>
            </tr></thead>
            <tbody>
              {#each cands as c}
                <tr class="border-t border-border/40">
                  <td class="px-5 py-2">
                    <span class="text-slate-200">{c.player} {c.threshold}+ {MARKET_LABEL[c.market] ?? c.market}</span>
                    <span class="text-xs text-slate-500"> · {shortEvent(c.event)}</span>
                  </td>
                  <td class="px-2 py-2 text-slate-400">{last4(c.last4)}</td>
                  <td class="px-2 py-2 text-right text-slate-300">{fmtOdds(c.odds)}</td>
                  <td class="px-2 py-2 text-right text-slate-500">{fmtPct(c.implied, 0)}</td>
                  <td class="px-2 py-2 text-right text-slate-200">{fmtPct(c.p_cal, 0)}</td>
                  <td class="px-5 py-2 text-right text-emerald-400">{fmtSigned(c.edge_pts)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    {#if tickets.length}
      <section class="card p-0 overflow-hidden">
        <h2 class="px-5 py-3 border-b border-border text-sm font-semibold text-slate-300 uppercase tracking-wider">Tickets on record</h2>
        <table class="w-full text-sm tabular-nums">
          <tbody>
            {#each tickets as e}
              <tr class="border-t border-border/40">
                <td class="px-5 py-2 text-slate-500">Wk {e.week}</td>
                <td class="px-2 py-2 text-slate-200">{e.pick}{#if !e.frozen && !e.result}<span class="text-xs text-slate-500"> · still rebuilding</span>{/if}</td>
                <td class="px-2 py-2 text-right text-slate-300">{fmtOdds(e.odds)}</td>
                <td class="px-2 py-2 text-slate-400">{e.result ?? 'PENDING'}{#if e.actual} · {e.actual}{/if}</td>
                <td class="px-5 py-2 text-right {e.units == null ? 'text-slate-500' : e.units >= 0 ? 'text-emerald-400' : 'text-red-400'}">
                  {e.units == null ? `${e.stake}u` : `${fmtSigned(e.units, 2)}u`}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </section>
    {/if}
  {/if}
</div>
