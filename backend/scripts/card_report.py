#!/usr/bin/env python
"""How the daily card is doing, against the baseline that actually matters.

The card's own hit rate means little on its own — the question is whether
picking those legs beat picking *any* legs off the same day's screen. That is
the comparison that found the old card was losing eleven points by sorting its
tail on price (56.0% against 67.1% for the screen it drew from), and it is the
one that will say whether the probability bar fixed it.

Three numbers per day, and the third is the one to read:

  card legs      did the batters on the card get a hit
  screen         did the batters on that day's board get a hit
  delta          card minus screen, in points

A positive delta means selection is adding something. Zero means the card is
no better than drawing at random off the board, which is where the model's own
ranking sat when last measured (AUC 0.507 on its own picks). Negative means it
is actively choosing worse bets, which is what the old tail did.

Usage:
    python scripts/card_report.py --since 2026-09-15
    python scripts/card_report.py --since 2026-09-15 --api http://localhost:8000
"""

from __future__ import annotations

import argparse
import statistics
from collections import defaultdict

import httpx

DEFAULT_API = "https://sharp-edge.nickknows.net/api"


def fetch(api: str) -> tuple[list[dict], list[dict]]:
    cards = httpx.get(f"{api}/picks/parlay-record", timeout=90).json()["parlays"]
    picks = httpx.get(f"{api}/picks/track-record",
                      params={"screen": "batter", "include_metrics": "true"},
                      timeout=90).json()["picks"]
    return cards, picks


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", required=True, help="YYYY-MM-DD, inclusive")
    ap.add_argument("--api", default=DEFAULT_API)
    args = ap.parse_args()

    cards, picks = fetch(args.api)
    result_of = {(p["pick_date"], (p.get("batter") or "").lower()): p.get("result")
                 for p in picks}
    screen_day: dict[str, list[bool]] = defaultdict(list)
    for p in picks:
        if p.get("result") in ("WIN", "LOSS"):
            screen_day[p["pick_date"]].append(p["result"] == "WIN")

    rows = [c for c in cards if c["pick_date"] >= args.since]
    if not rows:
        raise SystemExit(f"no cards on or after {args.since}")

    print(f"{'date':12s}{'legs':>5}{'swept':>7}{'card legs':>12}{'screen':>10}{'delta':>8}")
    print("-" * 54)
    deltas, swept, decided, lw, ln = [], 0, 0, 0, 0
    for c in sorted(rows, key=lambda x: x["pick_date"]):
        d = c["pick_date"]
        got = [result_of.get((d, (l.get("batter") or "").lower())) for l in c["legs"]]
        graded = [g for g in got if g in ("WIN", "LOSS")]
        day = screen_day.get(d) or []
        # Sweep comes from the card's OWN settled result, never from the legs
        # that happened to join. A leg whose pick row is missing drops out of
        # `graded` silently, and scoring the remainder turns a card that lost
        # 1-of-2 into a clean sweep — which is exactly what it did before this
        # was fixed.
        settled = c.get("result") in ("WIN", "LOSS")
        if not settled or not day:
            print(f"{d:12s}{c['leg_count']:>5}{'—':>7}{'pending':>12}")
            continue
        won = c.get("legs_won")
        of = c.get("legs_settled")
        if won is None or not of:
            if not graded:
                print(f"{d:12s}{c['leg_count']:>5}{'—':>7}{'no leg results':>14}")
                continue
            won, of = sum(g == "WIN" for g in graded), len(graded)
        card_rate = won / of
        screen_rate = statistics.mean(day)
        delta = 100 * (card_rate - screen_rate)
        deltas.append(delta)
        lw += won
        ln += of
        decided += 1
        is_sweep = c["result"] == "WIN"
        swept += is_sweep
        note = "" if of == c["leg_count"] else f"  ({of}/{c['leg_count']} legs graded)"
        print(f"{d:12s}{c['leg_count']:>5}{'yes' if is_sweep else 'no':>7}"
              f"{100*card_rate:>11.1f}%{100*screen_rate:>9.1f}%{delta:>+8.1f}{note}")

    if not decided:
        raise SystemExit("\nnothing settled yet")
    print("-" * 54)
    print(f"  cards settled      {decided}")
    print(f"  swept              {swept}/{decided} = {100*swept/decided:.1f}%")
    print(f"  card leg hit rate  {lw}/{ln} = {100*lw/ln:.1f}%")
    print(f"  mean delta vs screen {statistics.mean(deltas):+.1f} points"
          + (f"  (median {statistics.median(deltas):+.1f})" if decided > 2 else ""))
    print(f"  days the card beat its own screen: "
          f"{sum(1 for x in deltas if x > 0)}/{decided}")
    if decided < 7:
        print("\n  Too few days to conclude anything. For reference, the old card ran"
              "\n  -24.9 points against its screen over 11 September days, and the"
              "\n  whole reason for the change was that it should sit near zero.")


if __name__ == "__main__":
    main()
