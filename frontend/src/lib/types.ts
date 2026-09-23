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
  // Every other book's price for the same leg, via the aggregator. Absent
  // when no Odds API key is configured — the FanDuel columns above are
  // unaffected either way.
  books?: Record<string, BookQuote>;
  best_book?: string | null;
  best_odds?: number | null;
  best_ev?: number | null;
}

/** One book's quote for a leg.
 *
 *  `devig_p` is the book's own fair probability with its margin removed —
 *  measurable here, unlike a one-sided FanDuel runner, because the aggregator
 *  returns both sides. `overround` of 1.06 means it is holding six points. */
export interface BookQuote {
  odds: number;
  line: number | null;
  devig_p: number | null;
  overround: number | null;
  ev?: number;
  edge_pts?: number;
}

/** A sportsbook and which of the four capabilities it actually has.
 *
 *  DraftKings has odds but not login/sync: its own API is unreachable behind
 *  Akamai, so prices arrive through the aggregator while the account side
 *  waits on a browser capture. */
export interface BookInfo {
  key: string;
  name: string;
  supports_login: boolean;
  supports_sync: boolean;
  supports_odds: boolean;
  unsupported_reason: string;
  authenticated: boolean;
  expired: boolean;
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
  // The multi-book board. `quota` carries the aggregator's remaining credit
  // balance, which is the binding constraint on how often prices refresh.
  books?: {
    error: string | null;
    age_seconds: number | null;
    books: string[];
    quota: {
      quota_remaining?: number | null;
      quota_used?: number | null;
      events_fetched?: number;
      events_total?: number;
      partial?: boolean;
      quota_exhausted?: boolean;
    };
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
  /** The projection is a positional prior (draft capital through a curve),
   *  not a read on this player — so it is priced and shown but never fires.
   *  See PRIOR_ONLY_TYPES in nfl/screen.py. */
  prior_only: boolean;
  /** We rank this player differently from the market within his own team and
   *  market — i.e. we disagree about who plays, not how well. Flagged rather
   *  than filtered so the record can test whether it actually loses. */
  role_conflict?: boolean;
  role_conflict_with?: string | null;
  /** Which side of that disagreement the depth chart takes. 'market' means
   *  our number is most likely the stale one; null means the chart has no
   *  view. Recorded, not acted on. */
  role_conflict_verdict?: 'market' | 'model' | null;

  /** Whether this man is playing, from the roster designation and the injury
   *  report. OUT and DOUBTFUL never reach the card; QUESTIONABLE does.
   *  See nfl/availability.py. */
  avail?: 'ACTIVE' | 'QUESTIONABLE' | 'DOUBTFUL' | 'OUT' | null;
  avail_reason?: string | null;
  /** Position rank on the latest published depth chart. */
  depth_rank?: number | null;
  /** The share of this player's position group's recent production that
   *  belongs to men who are out — i.e. how much work he just inherited, and
   *  how stale the projection is as a result. */
  vacated_share?: number | null;
  vacated_by?: string[] | null;

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

export interface NflCardSummary {
  legs: number;
  decimal: number | null;
  american: number | null;
  model_p: number | null;
  implied_p: number | null;
  ev: number | null;
  kelly_quarter: number | null;
}

export interface NflTrackBucket {
  picks: number;
  wins: number;
  losses: number;
  voids: number;
  pushes: number;
  pending: number;
  hit_rate: number | null;
  /** Flat-stake ROI at the recorded price. Hit rate alone is what misled the
   *  baseball screen for months — 64.8% at a median -260 still loses. */
  roi: number | null;
}

export interface NflTrackRecord {
  season: number | null;
  overall: NflTrackBucket;
  by_market: ({ market: string } & NflTrackBucket)[];
  by_side: ({ side: string } & NflTrackBucket)[];
  by_week: ({ week: number } & NflTrackBucket)[];
  by_role_conflict: ({ role_conflict: boolean } & NflTrackBucket)[];
  /** Of the role conflicts, which side the depth chart took — and whether
   *  knowing that was worth anything. */
  by_role_verdict?: ({ verdict: string } & NflTrackBucket)[];
  /** Picks on a player whose position group had lost work to an injury.
   *  'major' is the band where card.py refuses the under, so the overs are
   *  what this split has to learn from. */
  by_vacated?: ({ vacated: string } & NflTrackBucket)[];
  /** Picks carrying an injury designation. Only QUESTIONABLE survives the
   *  card guard, so this is really asking whether backing through a
   *  questionable tag costs us anything the market has not already priced. */
  by_availability?: ({ avail: string } & NflTrackBucket)[];
  cards: {
    played: number;
    won: number;
    rows: {
      season: number; week: number; leg_count: number; american: number | null;
      decimal_odds: number | null; model_p: number | null;
      result: string | null; legs_won: number | null; legs_settled: number | null;
      /** An addToBetslip link for the parlay as recorded. Null once the week
       *  has graded — FanDuel pulls every market at kickoff — and null for
       *  cards frozen before the leg rows carried FanDuel ids. */
      betslip_url?: string | null;
      /** The frozen legs, with each leg's settled result joined on by the
       *  backend — the snapshot itself is written before kickoff and has no
       *  result of its own. */
      legs: {
        player: string; market: string; side: string; line: number;
        team: string | null; event: string | null; fd_odds: number | null;
        result: string | null; actual: number | null;
      }[];
    }[];
  };
  picks: {
    season: number; week: number; player: string; market: string; line: number;
    side: string; fd_odds: number | null; model_p: number | null;
    edge_pts: number | null; result: string | null; actual: number | null;
    team: string | null; event: string | null;
  }[];
}

/** One position group's loss: who is out, and how much of the group's recent
 *  production went with them. */
export interface NflVacancy {
  team: string;
  position: string;
  component: string;
  share: number;
  players: Array<{ player: string; status: string; reason: string | null;
                   baseline: number }>;
}

/** A row that would have been suggested but for an availability guard. The
 *  two lists have different reasons and so fill in different halves of this —
 *  one type rather than two so they can be rendered together. */
export type NflWithheld = Partial<Pick<NflProp, 'player' | 'team' | 'market' |
  'line' | 'side' | 'edge_pts' | 'avail' | 'avail_reason' | 'vacated_share' |
  'vacated_by'>>;

export interface NflScreen {
  season: number;
  week: number;
  /** Projections computed before kickoff of week 1 — priors off last season
   *  and rookie models, with no in-season usage behind them. */
  preseason: boolean;
  props: NflProp[];
  signals: NflProp[];
  /** Rows that cleared the threshold but are backed only by a prior. A long
   *  list here means the upstream projections table is stale or mis-joining
   *  names, and is worth surfacing rather than silently dropping. */
  held_prior_only: NflProp[];
  /** Every row worth recording as a prediction — wider than the card, and the
   *  set the track record is built from. */
  suggestions: NflProp[];
  role_conflicts: number;
  /** Who is out this week, what their absence vacated, and what that took off
   *  the board. Reported rather than silently applied. */
  availability?: {
    season: number;
    week: number;
    players: number;
    counts: Record<string, number>;
    depth_as_of: string | null;
    injury_report_rows: number;
    errors: string[];
    rows_out: number;
    vacancies: NflVacancy[];
    withheld_unavailable: NflWithheld[];
    withheld_vacated_under: NflWithheld[];
    vacated_share_blocks_under: number;
  };
  card: {
    legs: NflProp[];
    summary: NflCardSummary;
    betslip_url: string | null;
    min_edge_pts: number;
    under_min_edge_pts: number;
  };
  frozen?: { suggestions: number; written: number; card_legs: number;
             card_frozen: boolean; error?: string };
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
