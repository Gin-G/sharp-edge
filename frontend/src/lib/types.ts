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

export interface HomerRow {
  batter: string;
  team: string;
  opposing_pitcher: string;
  p_hand: 'R' | 'L' | null;
  game_time: string;
  venue: string | null;
  park_factor: number | null;
  iso_career: number | null;
  iso_season: number | null;
  iso_vs_hand: number | null;
  barrel_pct: number | null;
  hard_hit_pct: number | null;
  hr_last_15d: number;
  hr_last_30d: number;
  pa_last_15d: number;
  pull_air_pct: number | null;
  p_hr9_season: number | null;
  p_hr9_l3: number | null;
  p_barrel_pct: number | null;
  p_hard_hit_pct: number | null;
  p_fb_pct: number | null;
  p_hr9_vs_hand: number | null;
  bvp_hr: number;
  bvp_pa: number;
  bvp_barrel_pct: number | null;
  power_hand_edge: boolean;
  barrel_edge: boolean;
  park_boost_edge: boolean;
  bvp_hr_edge: boolean;
  hot_pop: boolean;
  tags: string;
  hr_score?: number;
}

export interface HomerHotPop {
  batter: string;
  team: string;
  hr_last_15d: number;
  hr_last_30d: number;
  iso_career: number | null;
  barrel_pct: number | null;
  game_time: string;
  opposing_pitcher: string;
}

export interface HomerScreen {
  picks: HomerRow[];
  hot_pop: HomerHotPop[];
  today: HomerRow[];
  as_of?: string;
}
