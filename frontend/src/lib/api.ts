import type { Stats, BreakdownRow, CalendarDay, ChatMessage, AuthStatus, InsightsResponse, BatterScreen, HomerScreen, TrackRecord } from './types';

const BASE = (import.meta.env.VITE_API_BASE as string) || 'http://localhost:8000';

export class ApiError extends Error {
  status: number;
  retryAfter?: number;
  body?: unknown;
  constructor(status: number, message: string, retryAfter?: number, body?: unknown) {
    super(message);
    this.status = status;
    this.retryAfter = retryAfter;
    this.body = body;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    let body: unknown;
    try { body = JSON.parse(text); } catch { body = text; }
    const retryHeader = res.headers.get('Retry-After');
    const retryAfter = retryHeader ? parseInt(retryHeader, 10) : undefined;
    throw new ApiError(res.status, `${res.status}: ${text}`, retryAfter, body);
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

export interface BatterScreenStatus {
  available: boolean;
  warming?: boolean;
  has_cache?: boolean;
  cached_date?: string | null;
  elapsed_seconds?: number | null;
  last_error?: string | null;
}

export function getBatterScreenStatus(): Promise<BatterScreenStatus> {
  return req<BatterScreenStatus>('/batters/screen/status');
}

export function getHomerScreen(params: Record<string, string> = {}): Promise<HomerScreen> {
  const qs = new URLSearchParams(params).toString();
  return req<HomerScreen>(`/homers/screen${qs ? `?${qs}` : ''}`);
}

export function getHomerScreenStatus(): Promise<BatterScreenStatus> {
  return req<BatterScreenStatus>('/homers/screen/status');
}

export function getTrackRecord(screen: 'hr' | 'batter', since?: string): Promise<TrackRecord> {
  const p = new URLSearchParams({ screen });
  if (since) p.set('since', since);
  return req<TrackRecord>(`/picks/track-record?${p.toString()}`);
}

export interface BackfillStatus {
  running: boolean;
  start: string | null;
  end: string | null;
  current_date: string | null;
  days_done: number;
  days_total: number;
  picks_written: number;
  errors: string[];
  last_error: string | null;
  elapsed_seconds?: number;
}

export function startBackfill(start: string, end?: string): Promise<{ status: string }> {
  return req('/picks/backfill', {
    method: 'POST',
    body: JSON.stringify({ start, end, screens: ['hr', 'batter'] }),
  });
}

export function getBackfillStatus(): Promise<BackfillStatus> {
  return req<BackfillStatus>('/picks/backfill/status');
}

// --- Chat ---

export function sendChat(
  messages: ChatMessage[],
  apiKey: string,
  model = 'claude-sonnet-5',
): Promise<{ response: string; messages: ChatMessage[] }> {
  return req('/chat', {
    method: 'POST',
    body: JSON.stringify({ messages, api_key: apiKey, model }),
  });
}

/** Validate a pasted Anthropic key with a cheap round-trip before marking
 *  it connected. The key rides in the request body only — never stored
 *  server-side. */
export function verifyChatKey(apiKey: string): Promise<{ status: string }> {
  return req('/chat/verify', {
    method: 'POST',
    body: JSON.stringify({ api_key: apiKey }),
  });
}

// --- Auth ---

export function getAuthStatus(): Promise<AuthStatus> {
  return req<AuthStatus>('/auth/status');
}

/** expiry_assumed: the countdown is a 1h default because FanDuel's token
 *  carried no exp claim. can_refresh: renewal survives a pod restart, since
 *  the password itself is never persisted. */
export interface SessionInfo {
  status: string;
  expires_in?: number;
  expiry_assumed?: boolean;
  can_refresh?: boolean;
}

export function login(
  email: string,
  password: string,
): Promise<SessionInfo & { status: 'ok' | 'mfa_required'; message?: string }> {
  return req('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  });
}

export function submitMfaCode(code: string): Promise<SessionInfo> {
  return req('/auth/mfa', {
    method: 'POST',
    body: JSON.stringify({ code }),
  });
}

export function setManualToken(token: string): Promise<SessionInfo> {
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
