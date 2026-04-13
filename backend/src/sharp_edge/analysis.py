"""Bet analysis and scoring logic.

This module contains the 'brain' — functions that analyze historical
betting data to score new potential bets and surface insights.
"""

import json
import re
from typing import Optional


# Maps FanDuel marketType codes to canonical market categories.
# Checked against leg.market_type values captured from the API.
MARKET_TYPE_MAP = {
    # Moneyline
    "MONEY_LINE": "moneyline",
    "MATCH_LINES": "moneyline",
    "TO_WIN_MATCH": "moneyline",
    # Spread / run line
    "HANDICAP": "spread",
    "SPREAD": "spread",
    "ASIAN_HANDICAP": "spread",
    "RUN_LINE": "spread",
    # Totals
    "TOTAL_POINTS": "totals",
    "TOTAL_RUNS": "totals",
    "TOTAL_GOALS": "totals",
    "OVER_UNDER": "totals",
    "ALTERNATIVE_TOTAL_RUNS": "totals",
    # Hits
    "PLAYER_TO_RECORD_A_HIT": "hits",
    "PLAYER_HITS": "hits",
    # Total bases
    "TO_RECORD_2+_TOTAL_BASES": "total_bases",
    "TO_RECORD_3+_TOTAL_BASES": "total_bases",
    "TO_RECORD_4+_TOTAL_BASES": "total_bases",
    "TOTAL_BASES": "total_bases",
    "PLAYER_TOTAL_BASES": "total_bases",
    # Home runs
    "HOME_RUN": "home_run",
    "BATTER_HOME_RUN": "home_run",
    "PLAYER_HOME_RUN": "home_run",
    # Touchdowns
    "ANYTIME_TOUCHDOWN_SCORER": "anytime_td",
    "FIRST_TOUCHDOWN_SCORER": "anytime_td",
    # Strikeouts
    "PITCHER_STRIKEOUTS": "strikeouts",
    "STRIKEOUTS": "strikeouts",
    "PLAYER_STRIKEOUTS": "strikeouts",
    # Stolen bases
    "STOLEN_BASE": "stolen_bases",
    "PLAYER_STOLEN_BASES": "stolen_bases",
    # Basketball props
    "THREE_POINTERS_MADE": "three_pointers",
    "PLAYER_THREES": "three_pointers",
    "PLAYER_POINTS_REBOUNDS_ASSISTS": "pra_combo",
    "PLAYER_PRA": "pra_combo",
    # Yardage props
    "RECEIVING_YARDS": "yardage_props",
    "RUSHING_YARDS": "yardage_props",
    "PASSING_YARDS": "yardage_props",
    "PLAYER_RECEPTIONS": "yardage_props",
}


def categorize_market_type(market_type_code: str) -> str:
    """Map a FanDuel marketType code to a canonical market category."""
    return MARKET_TYPE_MAP.get(market_type_code.upper(), "other")


def get_leg_markets(bet: dict) -> set[str]:
    """Parse a bet's legs JSON and return the set of canonical market categories."""
    legs_raw = bet.get("legs", "")
    if not legs_raw:
        return set()
    try:
        legs = json.loads(legs_raw) if isinstance(legs_raw, str) else legs_raw
    except (json.JSONDecodeError, TypeError):
        return set()
    markets = set()
    for leg in legs:
        mt = leg.get("market_type", "")
        if mt:
            markets.add(categorize_market_type(mt))
    return markets


def categorize_market(bet_info: str) -> str:
    """Classify a bet into a market category from the bet description.

    Used as a fallback when leg-level market_type data is unavailable
    (e.g., bets imported from CSV).
    """
    if not bet_info:
        return "unknown"
    info = bet_info.lower()

    if "moneyline" in info or "money line" in info:
        return "moneyline"
    if "spread" in info:
        return "spread"
    if any(kw in info for kw in ("total runs", "total points", "over ", "under ")):
        return "totals"
    if "home run" in info:
        return "home_run"
    if any(kw in info for kw in ("touchdown", "score anytime", "anytime td")):
        return "anytime_td"
    if "record a hit" in info:
        return "hits"
    if "total bases" in info:
        return "total_bases"
    if "pts + reb + ast" in info or "points + rebounds" in info:
        return "pra_combo"
    if "strikeout" in info:
        return "strikeouts"
    if any(kw in info for kw in ("receiving", "rushing", "passing", "receptions")):
        return "yardage_props"
    if "stolen base" in info:
        return "stolen_bases"
    if "three pointer" in info:
        return "three_pointers"
    return "other"


def classify_odds_bucket(odds: Optional[float]) -> str:
    """Classify decimal odds into a named bucket."""
    if odds is None:
        return "unknown"
    if odds < 2:
        return "short (< 2x)"
    if odds < 3:
        return "low_plus (2-3x)"
    if odds < 5:
        return "mid_plus (3-5x)"
    if odds < 10:
        return "long (5-10x)"
    if odds < 50:
        return "longshot (10-50x)"
    return "moonshot (50x+)"


def score_bet(
    proposed_bet: dict,
    history: list[dict],
) -> dict:
    """Score a proposed bet against historical performance.

    Args:
        proposed_bet: Dict with keys like:
            - league (e.g., "MLB", "NFL", "NBA")
            - bet_type ("straight" or "parlay")
            - market (e.g., "total_bases", "moneyline", "anytime_td")
            - odds (decimal odds)
            - stake (proposed wager amount)
            - leg_count (number of legs if parlay)
            - description (free text description of the bet)
        history: List of settled bet dicts from the database.

    Returns:
        Dict with scoring breakdown and recommendation.
    """
    league = proposed_bet.get("league", "").upper()
    bet_type = proposed_bet.get("bet_type", "straight")
    market = proposed_bet.get("market", "")
    odds = proposed_bet.get("odds", 0)
    leg_count = proposed_bet.get("leg_count", 1)

    # If market not provided, try to categorize from description
    if not market and proposed_bet.get("description"):
        market = categorize_market(proposed_bet["description"])

    odds_bucket = classify_odds_bucket(odds)

    # Filter history to matching patterns
    filters = {
        "league": lambda b: b.get("league", "").upper() == league if league else True,
        "bet_type": lambda b: b.get("bet_type") == bet_type,
        "market": lambda b: (
            market in get_leg_markets(b)
            if get_leg_markets(b)
            else categorize_market(b.get("bet_info", "")) == market
        ) if market else True,
        "odds_bucket": lambda b: classify_odds_bucket(b.get("odds")) == odds_bucket,
    }

    # Score each dimension
    scores = {}
    for dim, filter_fn in filters.items():
        matching = [b for b in history if filter_fn(b)]
        settled = [b for b in matching if b["status"] in ("SETTLED_WIN", "SETTLED_LOSS")]
        if not settled:
            scores[dim] = {
                "sample_size": 0,
                "win_rate": None,
                "roi_pct": None,
                "net_profit": None,
                "verdict": "no_data",
            }
            continue

        wins = sum(1 for b in settled if b["status"] == "SETTLED_WIN")
        total_wagered = sum(b.get("stake", 0) for b in settled)
        net_profit = sum(b.get("profit", 0) for b in settled)
        win_rate = wins / len(settled) if settled else 0
        roi = (net_profit / total_wagered * 100) if total_wagered > 0 else 0

        # Verdict based on sample size and ROI
        if len(settled) < 10:
            verdict = "insufficient_data"
        elif roi > 5:
            verdict = "edge"
        elif roi > -3:
            verdict = "break_even"
        elif roi > -10:
            verdict = "slight_leak"
        else:
            verdict = "bleeding"

        scores[dim] = {
            "sample_size": len(settled),
            "win_rate": round(win_rate * 100, 1),
            "roi_pct": round(roi, 2),
            "net_profit": round(net_profit, 2),
            "verdict": verdict,
        }

    # Parlay leg count check
    if bet_type == "parlay" and leg_count:
        leg_matching = [
            b for b in history
            if b.get("bet_type") == "parlay"
            and b.get("leg_count") == leg_count
            and b["status"] in ("SETTLED_WIN", "SETTLED_LOSS")
        ]
        if leg_matching:
            wins = sum(1 for b in leg_matching if b["status"] == "SETTLED_WIN")
            wagered = sum(b.get("stake", 0) for b in leg_matching)
            profit = sum(b.get("profit", 0) for b in leg_matching)
            scores["leg_count"] = {
                "legs": leg_count,
                "sample_size": len(leg_matching),
                "win_rate": round(wins / len(leg_matching) * 100, 1),
                "roi_pct": round(profit / wagered * 100, 2) if wagered else 0,
                "net_profit": round(profit, 2),
                "verdict": "edge" if profit > 0 and len(leg_matching) >= 10 else (
                    "bleeding" if profit < 0 and len(leg_matching) >= 10 else "insufficient_data"
                ),
            }

    # Breakeven odds calculation
    if odds and odds > 0:
        implied_prob = 1 / odds
        breakeven_pct = round(implied_prob * 100, 1)
    else:
        breakeven_pct = None

    # Overall recommendation
    verdicts = [s["verdict"] for s in scores.values()]
    bleeding_count = verdicts.count("bleeding") + verdicts.count("slight_leak")
    edge_count = verdicts.count("edge")

    if bleeding_count >= 2:
        overall = "AVOID — multiple dimensions show negative ROI in your history"
    elif "bleeding" in verdicts:
        bleed_dim = [k for k, v in scores.items() if v["verdict"] == "bleeding"][0]
        overall = f"CAUTION — you historically bleed in this {bleed_dim} pattern"
    elif edge_count >= 2:
        overall = "LOOKS GOOD — multiple dimensions show positive ROI"
    elif edge_count == 1:
        edge_dim = [k for k, v in scores.items() if v["verdict"] == "edge"][0]
        overall = f"LEAN YES — you have edge in this {edge_dim} pattern"
    elif all(v["verdict"] == "insufficient_data" or v["verdict"] == "no_data" for v in scores.values()):
        overall = "NO HISTORY — can't score this, no matching bets found"
    else:
        overall = "NEUTRAL — no clear edge or leak detected"

    return {
        "proposed": proposed_bet,
        "breakeven_win_pct": breakeven_pct,
        "dimension_scores": scores,
        "overall": overall,
    }


def generate_insights(history: list[dict]) -> list[str]:
    """Generate actionable insights from bet history.

    Returns a list of plain-text observations.
    """
    settled = [b for b in history if b["status"] in ("SETTLED_WIN", "SETTLED_LOSS")]
    if not settled:
        return ["No settled bets to analyze."]

    insights = []
    total_wagered = sum(b.get("stake", 0) for b in settled)
    net_profit = sum(b.get("profit", 0) for b in settled)
    overall_roi = (net_profit / total_wagered * 100) if total_wagered > 0 else 0

    # Top-level
    insights.append(
        f"Overall: {len(settled)} bets, ${total_wagered:.0f} wagered, "
        f"${net_profit:+.2f} P/L ({overall_roi:+.1f}% ROI)"
    )

    # Best and worst leagues
    league_stats = {}
    for b in settled:
        lg = b.get("league", "unknown")
        if lg not in league_stats:
            league_stats[lg] = {"wagered": 0, "profit": 0, "count": 0}
        league_stats[lg]["wagered"] += b.get("stake", 0)
        league_stats[lg]["profit"] += b.get("profit", 0)
        league_stats[lg]["count"] += 1

    for lg, stats in sorted(league_stats.items(), key=lambda x: x[1]["profit"], reverse=True):
        if stats["count"] >= 10:
            roi = (stats["profit"] / stats["wagered"] * 100) if stats["wagered"] else 0
            direction = "PROFITABLE" if roi > 3 else ("BLEEDING" if roi < -5 else "BREAK-EVEN")
            insights.append(f"{lg}: {direction} — {stats['count']} bets, ${stats['profit']:+.2f} ({roi:+.1f}% ROI)")

    # Parlay leg sweet spot
    parlay_legs = {}
    for b in settled:
        if b.get("bet_type") == "parlay":
            lc = b.get("leg_count", 0)
            if lc not in parlay_legs:
                parlay_legs[lc] = {"wagered": 0, "profit": 0, "count": 0}
            parlay_legs[lc]["wagered"] += b.get("stake", 0)
            parlay_legs[lc]["profit"] += b.get("profit", 0)
            parlay_legs[lc]["count"] += 1

    profitable_legs = [
        lc for lc, s in parlay_legs.items()
        if s["profit"] > 0 and s["count"] >= 10
    ]
    bleeding_legs = [
        lc for lc, s in parlay_legs.items()
        if s["profit"] < -10 and s["count"] >= 5
    ]
    if profitable_legs:
        insights.append(f"Parlay sweet spot: {sorted(profitable_legs)} legs show positive ROI")
    if bleeding_legs:
        insights.append(f"Parlay avoid: {sorted(bleeding_legs)}+ legs are consistently negative")

    # Promo / boost performance
    promo_bets = [b for b in settled if any(
        kw in (b.get("tags") or "") for kw in ("boost", "no_sweat", "free_bet", "profit_boost")
    )]
    clean_bets = [b for b in settled if b not in promo_bets]
    if len(promo_bets) >= 5 and len(clean_bets) >= 5:
        def _roi(bets):
            w = sum(b.get("stake", 0) for b in bets)
            p = sum(b.get("profit", 0) for b in bets)
            return (p / w * 100) if w else 0

        promo_roi = _roi(promo_bets)
        clean_roi = _roi(clean_bets)
        promo_wins = sum(1 for b in promo_bets if b["status"] == "SETTLED_WIN")
        clean_wins = sum(1 for b in clean_bets if b["status"] == "SETTLED_WIN")
        promo_wr = promo_wins / len(promo_bets) * 100
        clean_wr = clean_wins / len(clean_bets) * 100

        verdict = "better" if promo_roi > clean_roi else "worse"
        insights.append(
            f"Promo bets ({len(promo_bets)} bets): {promo_wr:.0f}% win rate, {promo_roi:+.1f}% ROI — "
            f"{verdict} than your clean bets ({clean_wr:.0f}% win rate, {clean_roi:+.1f}% ROI)"
        )

    # Near-miss parlay analysis — parlays lost by exactly 1 leg
    losing_parlays = [
        b for b in settled
        if b["status"] == "SETTLED_LOSS" and b.get("bet_type") == "parlay"
    ]
    near_misses = []
    breaker_markets = {}
    for b in losing_parlays:
        try:
            legs = json.loads(b["legs"]) if isinstance(b.get("legs"), str) else (b.get("legs") or [])
        except (json.JSONDecodeError, TypeError):
            continue
        if not legs:
            continue
        lost_legs = [l for l in legs if l.get("result") == "LOST"]
        won_legs = [l for l in legs if l.get("result") == "WON"]
        if len(lost_legs) >= 1 and len(won_legs) > len(lost_legs):
            near_misses.append(b)
            mt = lost_legs[0].get("market_type", "")
            category = categorize_market_type(mt) if mt else categorize_market(lost_legs[0].get("market", ""))
            breaker_markets[category] = breaker_markets.get(category, 0) + 1

    if near_misses:
        top_breakers = sorted(breaker_markets.items(), key=lambda x: x[1], reverse=True)
        breaker_str = ", ".join(f"{cat} ({n}x)" for cat, n in top_breakers[:3])
        insights.append(
            f"Near-miss parlays: {len(near_misses)} of your {len(losing_parlays)} losing parlays "
            f"lost by exactly 1 leg. Most common breaker: {breaker_str}"
        )

    # Big win dependency
    wins = [b for b in settled if b["status"] == "SETTLED_WIN"]
    if wins:
        wins_sorted = sorted(wins, key=lambda b: b.get("profit", 0), reverse=True)
        top3_profit = sum(b.get("profit", 0) for b in wins_sorted[:3])
        total_win_profit = sum(b.get("profit", 0) for b in wins)
        if total_win_profit > 0:
            pct = top3_profit / total_win_profit * 100
            if pct > 40:
                insights.append(
                    f"Big win dependency: Top 3 wins account for {pct:.0f}% of all win profit. "
                    f"Remove them and you'd be ${net_profit - top3_profit:+.2f}."
                )

    return insights
