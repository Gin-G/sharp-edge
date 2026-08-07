#!/usr/bin/env python
"""Save today's FanDuel to-record-a-hit prices next to today's board.

The reason ``bundle_backtest.py`` can only report ROI as a function of an
*assumed* price is that no odds history exists — FanDuel serves the current
board and nothing else, and a price that has moved is gone. Every day this
doesn't run is a day that can never be backtested properly.

One row per batter with a posted market, joined to the screen's own view of
that matchup, so a later backtest can ask "what did the market think, versus
what did the screen think, versus what happened".

Re-running the same day **appends**, so the file accumulates the day's line
movement rather than just its last state. That matters for two reasons: the
closing price is the honest benchmark for whether the screen beats the market,
and the drift from open to close is itself a signal.

"Before first pitch" is per-game, not per-slate — the card runs from lunchtime
to late evening, so a single pass is near the bell for a few games and hours
early for the rest. Every row therefore carries ``game_start`` and
``mins_to_start``, and the closing quote for a batter is simply his last row
with ``mins_to_start`` still positive. ``closing_prices()`` does that pick.

Two modes, because the passes have very different costs:

    (default)       full pass — joins each price to the screen's view of the
                    matchup. Needs Statcast, so it's minutes. Run once.
    --prices-only   prices, game and timestamp only. Seconds, no Statcast.
                    The matchup context is already in the day's first row for
                    that batter, so later passes don't need to re-derive it.

Usage:
    python scripts/snapshot_odds.py
    python scripts/snapshot_odds.py --prices-only
    python scripts/snapshot_odds.py --date 2026-08-07 --dir ~/odds
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from sharp_edge import batters, pricing
from sharp_edge._data import _norm
from sharp_edge.fanduel.odds import FanDuelOdds, american_to_implied

logger = logging.getLogger("snapshot-odds")

DEFAULT_DIR = Path.home() / ".sharp-edge" / "odds"


def _mins_to_start(event_start: str | None, now: datetime) -> float | None:
    """Minutes from ``now`` until first pitch. Negative once it's underway."""
    if not event_start:
        return None
    try:
        dt = datetime.fromisoformat(event_start.replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((dt - now).total_seconds() / 60.0, 1)


def snapshot(
    target: date, outdir: Path, state: str = "CO", prices_only: bool = False
) -> Path | None:
    outdir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    odds = asyncio.run(FanDuelOdds(state=state).hit_prices_detailed(target))
    if not odds:
        logger.warning("%s: no prices returned, nothing written", target)
        return None

    # The board gives each priced batter his matchup context. Without it the
    # snapshot is just numbers; with it, it's a row a backtest can score.
    # Later passes skip it — the context is already on the day's first row for
    # that batter, and re-deriving it costs a Statcast load for nothing.
    board = pd.DataFrame()
    if not prices_only:
        try:
            board = batters.screen_for_date(target, verbose=False).today
        except Exception:
            logger.exception("%s: screen failed, writing prices without context", target)

    def _base(key: str, q: dict) -> dict:
        american = int(q["odds"])
        return {
            "pick_date": target.isoformat(),
            # The stable join key across passes. A full pass knows the batter's
            # display name from the board; a --prices-only pass only has
            # FanDuel's spelling. Deduping on the display name would treat
            # "Blaze Jordan" and "blaze jordan" as two players and every close
            # would be double-counted.
            "batter_key": key,
            "fd_odds": american,
            "implied_p": round(american_to_implied(american), 5),
            "captured_at": now.isoformat(timespec="seconds"),
            "game_start": q.get("event_start"),
            "mins_to_start": _mins_to_start(q.get("event_start"), now),
            "game": q.get("event_name"),
        }

    rows = []
    seen = set()
    if not board.empty:
        for rec in board.to_dict(orient="records"):
            key = _norm(rec.get("batter") or "")
            q = odds.get(key)
            if q is None:
                continue
            seen.add(key)
            rows.append({
                **_base(key, q),
                "batter": rec.get("batter"),
                "batter_id": rec.get("batter_id"),
                "team": rec.get("team"),
                "opposing_pitcher": rec.get("opposing_pitcher"),
                "pitcher_id": rec.get("pitcher_id"),
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
                # The actual pick rule, not "has a tag" — a row tagged only
                # SHARP-SP is one the veto *held back*, and counting it as a
                # pick would quietly poison any backtest built on this file.
                "is_pick": bool(
                    rec.get("is_hot")
                    and (
                        rec.get("bvp_edge")
                        or rec.get("hand_slump_edge")
                        or rec.get("hittable_sp_edge")
                    )
                    and not rec.get("p_sharp")
                ),
            })

    # Prices with no board row still go in — a batter the screen never
    # considered is exactly the control group a price model needs.
    for key, q in odds.items():
        if key in seen:
            continue
        row = {**_base(key, q), "batter": None, "batter_id": None}
        if not prices_only:
            # A full pass looked at the board and this batter genuinely wasn't
            # on it, so False is a fact. A prices-only pass never looked, so
            # it must leave these blank — writing False would be a claim it
            # can't make, and would block the merge from backfilling the
            # context that the full pass did record.
            row.update({"batter": key, "model_p": None, "is_pick": False})
        rows.append(row)

    df = pd.DataFrame(rows)

    path = outdir / f"odds_{target.isoformat()}.parquet"
    if path.exists():
        # Append rather than replace: the day's line movement is the point,
        # and the closing quote is whichever row was taken last before that
        # batter's own first pitch. A batter whose game has already started is
        # absent from this fetch and simply keeps his earlier rows.
        try:
            prior = pd.read_parquet(path)
            before = len(prior)
            df = (
                pd.concat([prior, df], ignore_index=True)
                .drop_duplicates(subset=["batter_key", "captured_at"], keep="last")
                .sort_values(["batter_key", "captured_at"])
                .reset_index(drop=True)
            )
            # A --prices-only pass carries no board context, so backfill each
            # batter's matchup columns from whichever pass did have it.
            ctx = [c for c in ("batter", "batter_id", "team", "opposing_pitcher",
                               "pitcher_id", "model_p", "recent_avg", "recent_ab",
                               "vs_hand_avg", "bvp_avg", "bvp_pa", "p_l3_h9",
                               "p_l3_baa", "p_form", "tags", "is_pick")
                   if c in df.columns]
            df[ctx] = df.groupby("batter_key")[ctx].transform(
                lambda s: s.ffill().bfill()
            )
            logger.info("%s: appended to %d existing rows", target, before)
        except Exception:
            logger.exception("%s: could not read %s, overwriting", target, path)

    df.to_parquet(path, index=False)
    closing = closing_prices(df)
    logger.info(
        "%s: %d prices this pass (%d joined to the board), %d rows total, "
        "%d batters with a pre-game close -> %s",
        target, len(rows), len(seen), len(df), len(closing), path,
    )
    return path


def closing_prices(df: pd.DataFrame) -> pd.DataFrame:
    """The last quote taken before each batter's own first pitch.

    This is the benchmark that matters. Beating an opening line mostly means
    being early; beating the close means being right, because the close is
    where the market has absorbed everything it is going to absorb — lineups,
    weather, scratches, and whatever the sharp money knew.

    Rows with no ``mins_to_start`` (game time unknown) fall back to the last
    capture for that batter, which is the best available guess.
    """
    if df.empty:
        return df
    key = "batter_key" if "batter_key" in df else "batter"
    if key not in df:
        return df
    d = df.copy()
    if "mins_to_start" not in d:
        return d.drop_duplicates(subset=[key], keep="last")
    pre = d[d["mins_to_start"].isna() | d["mins_to_start"].gt(0)]
    if pre.empty:
        return pre
    # Smallest positive mins_to_start = closest to the bell without passing it.
    pre = pre.sort_values(
        [key, "mins_to_start"], ascending=[True, True], na_position="last"
    )
    return pre.drop_duplicates(subset=[key], keep="first").reset_index(drop=True)


def summarise(outdir: Path) -> None:
    """Print what's been recorded so far — the job summary's payload."""
    files = sorted(outdir.glob("odds_*.parquet"))
    if not files:
        print(f"no snapshots in {outdir}")
        return
    df = pd.read_parquet(files[-1])
    close = closing_prices(df)
    kcol = "batter_key" if "batter_key" in df else "batter"
    print(f"{files[-1].name}: {len(df)} rows, {df[kcol].nunique()} batters")
    if "captured_at" in df and df["captured_at"].notna().any():
        passes = sorted(df["captured_at"].dropna().unique())
        print(f"passes today: {len(passes)}")
        for p in passes:
            n = int((df["captured_at"] == p).sum())
            print(f"   {p}  {n} prices")
    if "implied_p" in close:
        q = close["implied_p"].quantile([0.1, 0.5, 0.9]).round(3)
        print(f"closing implied   p10 {q.iloc[0]}   median {q.iloc[1]}   p90 {q.iloc[2]}")
    if "is_pick" in close:
        print(f"screen picks with a closing price: {int(close['is_pick'].sum())}")
    if "mins_to_start" in close and close["mins_to_start"].notna().any():
        m = close["mins_to_start"].dropna()
        print(f"closes taken {m.min():.0f}–{m.max():.0f} min before first pitch "
              f"(median {m.median():.0f})")

    # Line movement is only visible once there's more than one pass.
    if "captured_at" in df and df["captured_at"].nunique() > 1:
        kk = "batter_key" if "batter_key" in df else "batter"
        first = df.sort_values("captured_at").drop_duplicates(kk, keep="first")
        j = first[[kk, "implied_p"]].merge(
            close[[kk, "implied_p"]], on=kk, suffixes=("_open", "_close")
        )
        if len(j):
            drift = (j["implied_p_close"] - j["implied_p_open"]) * 100
            print(f"open->close drift: mean {drift.mean():+.2f} pts, "
                  f"|drift| median {drift.abs().median():.2f} pts, n={len(j)}")

    print(f"\n{len(files)} days of odds history recorded "
          f"({files[0].name[5:15]} .. {files[-1].name[5:15]})")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--state", default="CO")
    ap.add_argument("--summary", action="store_true",
                    help="report what's recorded instead of fetching")
    ap.add_argument("--prices-only", action="store_true",
                    help="skip the board join — seconds instead of minutes, "
                         "for the closing passes")
    args = ap.parse_args()

    if args.summary:
        summarise(args.dir)
        return
    written = snapshot(
        date.fromisoformat(args.date), args.dir, args.state, args.prices_only
    )
    if written is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
