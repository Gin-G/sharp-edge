export interface Stats {
  total_bets: number;
  wins: number;
  losses: number;
  total_wagered: number;
  net_profit: number;
  roi_pct: number;
  avg_odds: number;
  avg_stake: number;
  first_bet: string | null;
  last_bet: string | null;
}

export interface BreakdownRow {
  league?: string;
  sportsbook?: string;
  bet_type?: string;
  sport?: string;
  total_bets: number;
  wins: number;
  losses: number;
  total_wagered: number;
  net_profit: number;
  win_pct: number;
  roi_pct: number;
}

export interface CalendarDay {
  day: string;        // "YYYY-MM-DD"
  total_bets: number;
  wins: number;
  wagered: number;
  net_profit: number;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string | unknown[];
}

export interface AuthStatus {
  authenticated: boolean;
  expired?: boolean;
}

export interface InsightsResponse {
  insights: string[];
}

export interface BatterRow {
  batter: string;
  team: string;
  opposing_pitcher: string;
  p_hand: 'R' | 'L' | null;
  recent_avg: number | null;
  recent_ab: number;
  vs_hand_avg: number | null;
  vs_hand_pa: number;
  bvp_avg: number | null;
  bvp_pa: number;
  bvp_hits: number;
  p_l3_era: number | null;
  p_l3_ip: number;
  p_l3_starts: number;
  is_hot: boolean;
  bvp_edge: boolean;
  hand_slump_edge: boolean;
  tags: string;
  game_time: string;
}

export interface HotBatRow {
  batter: string;
  team: string;
  recent_avg: number;
  recent_ab: number;
  H: number;
  HR: number;
  OBP: number;
  OPS: number;
}

export interface BatterScreen {
  picks: BatterRow[];
  hot_bats: HotBatRow[];
  today: BatterRow[];
}
