/** Module-level response cache.
 *
 * SvelteKit destroys a page component on navigation, so an `onMount` fetch
 * re-runs every time you switch tabs — even when the data is identical to what
 * was on screen a second ago. Module scope outlives the component, so a repeat
 * visit renders from memory instead of spinning.
 *
 * Scoped to the browser session: a hard reload starts empty.
 */

type Entry = { at: number; value: unknown };

const entries = new Map<string, Entry>();
const inflight = new Map<string, Promise<unknown>>();

/** Screens change once a day and the track record only moves when picks
 * settle, so minutes of staleness is invisible — but a stale tab left open
 * overnight should still catch up on its own. */
export const DEFAULT_TTL = 5 * 60_000;

export function cached<T>(
  key: string,
  fetcher: () => Promise<T>,
  { force = false, ttl = DEFAULT_TTL }: { force?: boolean; ttl?: number } = {},
): Promise<T> {
  if (force) {
    entries.delete(key);
  } else {
    const hit = entries.get(key);
    if (hit && Date.now() - hit.at < ttl) return Promise.resolve(hit.value as T);
    // Two components mounting at once should share one request, not race.
    const pending = inflight.get(key);
    if (pending) return pending as Promise<T>;
  }

  const p = fetcher()
    .then((value) => {
      entries.set(key, { at: Date.now(), value });
      return value;
    })
    .finally(() => inflight.delete(key));

  inflight.set(key, p);
  return p;
}

/** Synchronous read, for seeding a component's initial state so a revisit
 * paints immediately instead of flashing a spinner for one microtask. */
export function peek<T>(key: string, ttl = DEFAULT_TTL): T | null {
  const hit = entries.get(key);
  return hit && Date.now() - hit.at < ttl ? (hit.value as T) : null;
}

/** Drop cached entries whose key starts with `prefix` — call after anything
 * that changes server state (a backfill run, a resolve sweep). */
export function invalidate(prefix: string): void {
  for (const key of entries.keys()) {
    if (key.startsWith(prefix)) entries.delete(key);
  }
}
