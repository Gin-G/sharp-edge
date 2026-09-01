/** Shared loader for the three NFL boards.
 *
 * All three read the same `/nfl/screen` response, so they share one cache
 * entry: switching between Props, Touchdowns and Games is a re-render, not a
 * refetch. The warm-up polling is the same shape as the batter screen's — a
 * cold pod answers 503 with Retry-After while it builds the week.
 */
import { getNflScreen, ApiError } from '$lib/api';
import { cached, invalidate } from '$lib/cache';
import type { NflScreen } from '$lib/types';

export const NFL_CACHE_KEY = 'nfl:screen';

export interface LoadState {
  data: NflScreen | null;
  loading: boolean;
  error: string;
  warming: boolean;
  warmingElapsed: number | null;
}

export function initialState(data: NflScreen | null): LoadState {
  return {
    data,
    loading: data === null,
    error: '',
    warming: false,
    warmingElapsed: null,
  };
}

/** Fetch, retrying on the warm-up 503. `onState` is called on every change so
 *  a component can just assign the result into its own reactive locals.
 *  Returns a cancel function — call it on destroy so a pending retry doesn't
 *  fire into an unmounted component. */
export function load(
  onState: (s: Partial<LoadState>) => void,
  force = false,
): () => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let cancelled = false;

  async function run(isForce: boolean) {
    if (cancelled) return;
    onState({ loading: true, error: '' });
    try {
      if (isForce) invalidate(NFL_CACHE_KEY);
      const data = await cached(NFL_CACHE_KEY, () => getNflScreen(isForce), {
        force: isForce,
      });
      if (cancelled) return;
      onState({ data, loading: false, warming: false, warmingElapsed: null });
    } catch (e) {
      if (cancelled) return;
      if (e instanceof ApiError && e.status === 503) {
        const body = e.body as { elapsed_seconds?: number } | undefined;
        onState({ warming: true, warmingElapsed: body?.elapsed_seconds ?? null });
        const delay = Math.min((e.retryAfter ?? 10) * 1000, 15_000);
        // Retries are never forced: the first request already kicked the
        // build off, and forcing again would restart it each time round.
        timer = setTimeout(() => run(false), delay);
        return;
      }
      onState({
        error: e instanceof Error ? e.message : String(e),
        loading: false,
        warming: false,
      });
    }
  }

  run(force);
  return () => {
    cancelled = true;
    if (timer) clearTimeout(timer);
  };
}

// --- formatting shared by the NFL boards ---

export function fmtOdds(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  return v > 0 ? `+${v}` : `${v}`;
}

export function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined) return '—';
  return `${(100 * v).toFixed(digits)}%`;
}

export function fmtSigned(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined) return '—';
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}`;
}

export function fmtEv(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  return `${v >= 0 ? '+' : ''}${v.toFixed(3)}`;
}

export function evClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return 'text-slate-500';
  if (v >= 0.05) return 'text-emerald-400 font-semibold';
  if (v > 0) return 'text-emerald-500/80';
  if (v > -0.03) return 'text-slate-400';
  return 'text-red-400';
}

export const MARKET_LABEL: Record<string, string> = {
  receiving_yards: 'Receiving yds',
  rushing_yards: 'Rushing yds',
  passing_yards: 'Passing yds',
  receptions: 'Receptions',
};

/** Kickoff as a short local weekday + time. */
export function fmtKickoff(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString(undefined, {
    weekday: 'short',
    hour: 'numeric',
    minute: '2-digit',
  });
}
