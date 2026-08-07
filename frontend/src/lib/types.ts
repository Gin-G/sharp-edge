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
  // Opposing starter's last-3 contact line. Null when his game log carried
  // no hits/at-bats — the band is then UNKNOWN and nothing is vetoed.
  p_l3_hits: number | null;
  p_l3_h9: number | null;
  p_l3_baa: number | null;
  p_l3_whip: number | null;
  p_l3_k9: number | null;
  p_season_h9: number | null;
  p_season_baa: number | null;
  p_season_starts: number;
  p_form: 'SHARP' | 'HITTABLE' | 'NEUTRAL' | 'UNKNOWN';
  p_sharp: boolean;
  p_hittable: boolean;
  is_hot: boolean;
  bvp_edge: boolean;
  hand_slump_edge: boolean;
  hittable_sp_edge: boolean;
  tags: string;
  game_time: string;
  // FanDuel to-record-a-hit price and what it implies. Null when no market is
  // posted for this batter (not in a confirmed lineup, market pulled, etc.) —
  // model_p and breakeven_odds are still filled in, so it's clear what price
  // the pick would need.
  fd_odds: number | null;
  implied_p: number | null;
  model_p: number;
  ev: number | null;
  edge_pts: number | null;
  kelly: number | null;
  breakeven_odds: number;
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
  as_of?: string | null;
  stale?: boolean;
  odds?: {
    age_seconds: number | null;
    error: string | null;
    count: number;
  };
  bundle?: {
    legs: BatterRow[];
    summary: {
      legs: number;
      decimal: number | null;
      american: number | null;
      model_p: number | null;
      implied_p: number | null;
      ev: number | null;
    };
    betslip_url: string | null;
    near_misses: {
      batter: string;
      opposing_pitcher: string;
      fd_odds: number | null;
      ev: number | null;
      edge_pts: number | null;
      needs: number | null;
    }[];
  };
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

export interface TrackRecordBucket {
  picks: number;
  wins: number;
  losses: number;
  voids: number;
  pending: number;
  decided: number;
  hit_rate: number | null;
}

export interface TrackRecordPick {
  pick_date: string;
  batter: string;
  team: string | null;
  opposing_pitcher: string | null;
  venue: string | null;
  score: number | null;
  rank: number | null;
  tags: string | null;
  source: string;
  result: 'WIN' | 'LOSS' | 'VOID' | null;
  hr_actual: number | null;
  hits_actual: number | null;
  pa_actual: number | null;
}

export interface TrackRecord {
  screen: string;
  overall: TrackRecordBucket;
  by_tag: ({ tag: string } & TrackRecordBucket)[];
  by_source: ({ source: string } & TrackRecordBucket)[];
  daily: ({ date: string } & TrackRecordBucket)[];
  picks: TrackRecordPick[];
}
