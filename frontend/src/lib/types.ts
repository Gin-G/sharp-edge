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
    // The card is served from the frozen record once one exists, and those
    // rows carry only what the card displays — not the full board row.
    // ``market_open`` is false when FanDuel has pulled the leg's market,
    // which happens the moment its game starts.
    legs: (Partial<BatterRow> & {
      batter: string;
      opposing_pitcher: string | null;
      fd_odds: number | null;
      model_p: number | null;
      market_open?: boolean;
    })[];
    summary: {
      legs: number;
      decimal: number | null;
      american: number | null;
      model_p: number | null;
      implied_p: number | null;
      ev: number | null;
      // Already quartered. Full Kelly on these cards routinely lands above
      // 30% of bankroll, so the quarter is the one to show.
      kelly: number | null;
      kelly_quarter: number | null;
    };
    // When the card was first written, and how it settled. The card is frozen
    // before first pitch because the live board can't reproduce it later —
    // FanDuel pulls the market on every game that starts.
    frozen_at: string | null;
    result: 'WIN' | 'LOSS' | 'VOID' | null;
    betslip_url: string | null;
    near_misses: {
      batter: string;
      opposing_pitcher: string;
      fd_odds: number | null;
      ev: number | null;
      edge_pts: number | null;
      model_p: number | null;
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

export interface ParlayRecord {
  cards: number;
  decided: number;
  wins: number;
  losses: number;
  pending: number;
  void: number;
  sweep_rate: number | null;
  roi: number | null;
  avg_legs: number | null;
  parlays: {
    pick_date: string;
    leg_count: number;
    american: number | null;
    decimal_odds: number | null;
    model_p: number | null;
    result: 'WIN' | 'LOSS' | 'VOID' | null;
    legs_won: number | null;
    legs_settled: number | null;
    legs: { batter: string; team: string | null; fd_odds: number | null }[];
  }[];
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

// --- NFL ---

/** One posted player prop, priced.
 *
 * Both gaps are here on purpose. `raw_gap` is the projection minus the line,
 * which is the rule as originally stated; `residual` is the same thing after
 * the week's projections are rescaled onto the market's scale. They disagree
 * a lot — the raw one fires UNDER on nearly every star, because the projection
 * model shrinks toward the mean and the market doesn't — so only `residual`
 * drives `signal`, and `raw_signal` is kept alongside it to be compared on
 * live results rather than on an argument.
 */
export interface NflProp {
  market: 'receiving_yards' | 'receptions' | 'rushing_yards' | 'passing_yards';
  player: string;
  key: string;
  player_id: string | null;
  position: string | null;
  team: string | null;
  event: string | null;
  fd_event_id: string | null;
  kickoff: string | null;

  line: number;
  projection: number;
  adjusted: number;
  raw_gap: number;
  residual: number;
  threshold: number;
  signal: 'OVER' | 'UNDER' | '';
  raw_signal: 'OVER' | 'UNDER' | '';
  bettable: boolean;
  prediction_type: string | null;
  exp_games: number | null;

  /** Before anchoring to the market — kept visible because the two can differ
   *  wildly and the gap is the honest measure of how much is being assumed. */
  model_p_raw: number;
  model_p_over: number;
  over_odds: number | null;
  under_odds: number | null;
  fair_p_over: number | null;
  fair_p_under: number | null;
  overround: number | null;

  side: 'OVER' | 'UNDER' | null;
  model_p: number | null;
  odds: number | null;
  implied_p?: number | null;
  fair_p?: number | null;
  ev: number | null;
  edge_pts: number | null;
  kelly: number | null;

  fd_market_id: string | null;
  over_selection_id: number | null;
  under_selection_id: number | null;
  sgm: boolean;
}

export interface NflTd {
  player: string;
  key: string;
  player_id: string | null;
  position: string | null;
  team: string | null;
  event: string | null;
  fd_event_id: string | null;
  kickoff: string | null;
  projected_tds: number | null;
  /** Shifted so the game's field totals what the book's does, which is what
   *  makes it comparable with `implied_p` — both carry the same margin. */
  model_p: number;
  model_p_unanchored: number | null;
  odds: number | null;
  implied_p: number | null;
  edge_pts: number | null;
  /** Always null. This market's margin can't be stripped, so a dollar EV
   *  would read positive across most of the board and mean nothing. */
  ev: null;
  kelly: null;
  thin: boolean;
  fd_market_id: string | null;
  fd_selection_id: number | null;
  sgm: boolean;
}

export interface NflGameMarket {
  market: 'moneyline' | 'total' | 'spread';
  event: string | null;
  fd_event_id: string | null;
  kickoff: string | null;
  fd_market_id: string | null;
  runners: {
    name: string | null;
    odds: number | null;
    handicap: number | null;
    fd_selection_id: number | null;
  }[];
}

/** How far the week's projections had to be moved to sit on the market's
 *  scale. A slope well below 1 is the projection model shrinking toward the
 *  mean; it is the reason `residual` exists. */
export interface NflFit {
  slope: number | null;
  intercept: number | null;
  n: number;
}

/** The log-odds shift applied to centre our probabilities on the market's,
 *  and how much of the remaining disagreement is kept. `shrink` is a prior,
 *  not a fitted number — see `nfl/model.py`. */
export interface NflProbFit {
  offset: number | null;
  shrink: number;
  n: number;
}

export interface NflScreen {
  season: number;
  week: number;
  /** Projections computed before kickoff of week 1 — priors off last season
   *  and rookie models, with no in-season usage behind them. */
  preseason: boolean;
  props: NflProp[];
  signals: NflProp[];
  tds: NflTd[];
  games: NflGameMarket[];
  fits: Record<string, NflFit>;
  prob_fits: Record<string, NflProbFit>;
  thresholds: Record<string, number>;
  bettable: string[];
  passing_yards_caveat: string;
  odds: { age_seconds: number | null; error: string | null };
  unmatched: string[];
  built_at: number;
  stale?: boolean;
}
