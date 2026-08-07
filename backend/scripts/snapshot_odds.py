#!/usr/bin/env python
"""Save today's FanDuel to-record-a-hit prices next to today's board.

The reason ``bundle_backtest.py`` can only report ROI as a function of an
*assumed* price is that no odds history exists — FanDuel serves the current
board and nothing else, and a price that has moved is gone. Every day this
doesn't run is a day that can never be backtested properly.

One row per batter with a posted market, joined to the screen's own view of
that matchup, so a later backtest can ask "what did the market think, versus
what did the screen think, versus what happened".

Run it once a day before first pitch (cron, or the Batter screen backtest
workflow). Re-running the same day overwrites — later in the day is closer to
the price you'd actually get.

Usage:
    python scripts/snapshot_odds.py
    python scripts/snapshot_odds.py --date 2026-08-07 --dir ~/odds
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from sharp_edge import batters, pricing
from sharp_edge._data import _norm
from sharp_edge.fanduel.odds import FanDuelOdds, american_to_implied

logger = logging.getLogger("snapshot-odds")

DEFAULT_DIR = Path.home() / ".sharp-edge" / "odds"


def snapshot(target: date, outdir: Path, state: str = "CO") -> Path | None:
    outdir.mkdir(parents=True, exist_ok=True)

    odds = asyncio.run(FanDuelOdds(state=state).hit_odds_for_slate(target))
    if not odds:
        logger.warning("%s: no prices returned, nothing written", target)
        return None

    # The board gives each priced batter his matchup context. Without it the
    # snapshot is just numbers; with it, it's a row a backtest can score.
    try:
        res = batters.screen_for_date(target, verbose=False)
        board = res.today
    except Exception:
        logger.exception("%s: screen failed, writing prices without context", target)
        board = pd.DataFrame()

    rows = []
    seen = set()
    if not board.empty:
        for rec in board.to_dict(orient="records"):
            key = _norm(rec.get("batter") or "")
            american = odds.get(key)
            if american is None:
                continue
            seen.add(key)
            rows.append({
                "pick_date": target.isoformat(),
                "batter": rec.get("batter"),
                "batter_id": rec.get("batter_id"),
                "team": rec.get("team"),
                "opposing_pitcher": rec.get("opposing_pitcher"),
                "pitcher_id": rec.get("pitcher_id"),
                "fd_odds": int(american),
                "implied_p": round(american_to_implied(int(american)), 5),
                "model_p": pricing.model_probability(rec.get("p_l3_h9")),
                "recent_avg": rec.get("recent_avg"),
                "recent_ab": rec.get("recent_ab"),
                "vs_hand_avg": rec.get("vs_hand_avg"),
                "bvp_avg": rec.get("bvp_avg"),
                "bvp_pa": rec.get("bvp_pa"),
                "p_l3_h9": rec.get("p_l3_h9"),
                "p_l3_baa": rec.get("p_l3_baa"),
                "p_form": rec.get("p_form"),
                "tags": rec.get("tags"),
                "is_pick": bool(rec.get("is_hot")) and bool(rec.get("tags")),
            })

    # Prices with no board row still go in — a batter the screen never
    # considered is exactly the control group a price model needs.
    for key, american in odds.items():
        if key in seen:
            continue
        rows.append({
            "pick_date": target.isoformat(), "batter": key, "batter_id": None,
            "fd_odds": int(american),
            "implied_p": round(american_to_implied(int(american)), 5),
            "model_p": None, "is_pick": False,
        })

    df = pd.DataFrame(rows)
    path = outdir / f"odds_{target.isoformat()}.parquet"
    df.to_parquet(path, index=False)
    logger.info(
        "%s: %d prices (%d joined to the board) -> %s",
        target, len(df), len(seen), path,
    )
    return path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--state", default="CO")
    args = ap.parse_args()
    if snapshot(date.fromisoformat(args.date), args.dir, args.state) is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
