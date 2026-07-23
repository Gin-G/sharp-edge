/** The visitor's Anthropic API key, held only in this browser session.
 *
 * sessionStorage, not localStorage: the key is gone when the tab closes, so
 * it never outlives the session on a shared machine. It is sent to our
 * backend only as the per-request `api_key` on /chat — never persisted there
 * (chat spends the user's own credits, and Sharp Edge stores no key of its
 * own). Treat it like the FanDuel password: in memory, never logged.
 */
import { writable } from 'svelte/store';
import { browser } from '$app/environment';

const STORAGE_KEY = 'sharp_edge_anthropic_key';

function initial(): string {
  if (!browser) return '';
  return sessionStorage.getItem(STORAGE_KEY) ?? '';
}

export const claudeKey = writable<string>(initial());

if (browser) {
  claudeKey.subscribe((value) => {
    if (value) sessionStorage.setItem(STORAGE_KEY, value);
    else sessionStorage.removeItem(STORAGE_KEY);
  });
}

/** Show only enough to recognize which key is connected — never the whole
 * secret. Anthropic keys are `sk-ant-...`; show the prefix and last 4. */
export function maskKey(key: string): string {
  if (key.length <= 12) return '••••';
  return `${key.slice(0, 7)}…${key.slice(-4)}`;
}
