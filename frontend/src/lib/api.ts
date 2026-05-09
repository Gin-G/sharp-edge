import type { Stats, BreakdownRow, CalendarDay, ChatMessage, AuthStatus, InsightsResponse, BatterScreen } from './types';

const BASE = (import.meta.env.VITE_API_BASE as string) || 'http://localhost:8000';

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const msg = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${msg}`);
  }
  return res.json() as Promise<T>;
}

// --- Stats & analysis ---

export function getStats(params: Record<string, string> = {}): Promise<Stats> {
  const qs = new URLSearchParams(params).toString();
  return req<Stats>(`/bets/stats${qs ? `?${qs}` : ''}`);
}

export function getBreakdown(
  groupBy: 'league' | 'sportsbook' | 'bet_type' | 'sport',
  since?: string,
): Promise<BreakdownRow[]> {
  const qs = since ? `?since=${since}` : '';
  return req<BreakdownRow[]>(`/bets/breakdown/${groupBy}${qs}`);
}

export function getCalendar(since?: string, until?: string): Promise<CalendarDay[]> {
  const p = new URLSearchParams();
  if (since) p.set('since', since);
  if (until) p.set('until', until);
  const qs = p.toString();
  return req<CalendarDay[]>(`/bets/calendar${qs ? `?${qs}` : ''}`);
}

export function getInsights(params: Record<string, string> = {}): Promise<InsightsResponse> {
  const qs = new URLSearchParams(params).toString();
  return req<InsightsResponse>(`/bets/insights${qs ? `?${qs}` : ''}`);
}

export function getBatterScreen(params: Record<string, string> = {}): Promise<BatterScreen> {
  const qs = new URLSearchParams(params).toString();
  return req<BatterScreen>(`/batters/screen${qs ? `?${qs}` : ''}`);
}

// --- Chat ---

export function sendChat(
  messages: ChatMessage[],
  model = 'claude-sonnet-4-20250514',
): Promise<{ response: string; messages: ChatMessage[] }> {
  return req('/chat', {
    method: 'POST',
    body: JSON.stringify({ messages, model }),
  });
}

// --- Auth ---

export function getAuthStatus(): Promise<AuthStatus> {
  return req<AuthStatus>('/auth/status');
}

export function login(email: string, password: string): Promise<{ status: string; expires_in: number }> {
  return req('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  });
}

export function setManualToken(token: string): Promise<{ status: string; expires_in: number }> {
  return req('/auth/token', {
    method: 'POST',
    body: JSON.stringify({ token }),
  });
}

export function logout(): Promise<{ status: string }> {
  return req('/auth/logout', { method: 'POST' });
}

// --- Sync ---

export function syncBets(): Promise<{ status: string; bets_synced: number }> {
  return req('/bets/sync', { method: 'POST' });
}

export function importCsv(csvPath: string): Promise<{ status: string; bets_imported: number }> {
  return req('/bets/import', {
    method: 'POST',
    body: JSON.stringify({ csv_path: csvPath }),
  });
}
