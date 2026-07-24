#!/usr/bin/env python3
"""One-time repair for picks wrongly frozen as VOID.

Background
----------
Outcome resolution used to count a pick's plate appearances from Statcast,
which can trail the box score by a day. A pick resolved before Statcast
published the game's rows saw pa == 0 and was settled VOID. Because
``resolve_pending`` only ever revisits unresolved (result IS NULL) rows, that
premature VOID was frozen for good — even after the data landed. Real WINs
(e.g. Clement and Springer each recording a hit) showed up as VOID.

The code fix (tracking.reresolve_voids) re-checks every VOID against the
authoritative, lag-free box score batting line and rewrites the ones where the
player actually started and took a PA. This script wires up the app's database
the same way the FastAPI lifespan does and runs that repair.

Usage
-----
Run inside an environment that can reach the database (e.g. the API pod), with
the package installed and DATABASE_URL pointing at the same DB as the app:

    # preview what would change, nothing written
    python backend/scripts/repair_void_picks.py --dry-run

    # apply the corrections
    python backend/scripts/repair_void_picks.py

    # limit the scan to recent dates
    python backend/scripts/repair_void_picks.py --since 2026-07-01

By default it also runs a normal resolve sweep afterwards to settle any picks
still pending; pass --no-resolve to skip that.
"""

from __future__ import annotations

import argparse
import asyncio
import threading

from sharp_edge import tracking
from sharp_edge.config import settings
from sharp_edge.db import create_database


def _start_loop() -> asyncio.AbstractEventLoop:
    """Run an event loop in a background thread.

    tracking's DB calls hop onto a loop via run_coroutine_threadsafe and block
    on the result, so the loop must live in a *different* thread than the caller
    — mirroring how the API serves requests off worker threads.
    """
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    return loop


def _print_report(summary: dict, dry_run: bool) -> int:
    total = 0
    verb = "Would fix" if dry_run else "Fixed"
    for screen, info in summary.items():
        fixed = info["fixed"]
        total += fixed
        print(f"\n[{screen}] {verb} {fixed} of the VOID picks "
              f"across {info['dates']} date(s)")
        for c in info["corrections"]:
            print(
                f"    {c['pick_date']}  {c['batter'] or c['batter_id']:<22} "
                f"{c['was']} -> {c['now']}  "
                f"(pa={c['pa']} hits={c['hits']} hr={c['hr']})"
            )
    print(f"\n{verb} {total} pick(s) total"
          + (" — dry run, nothing written." if dry_run else "."))
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", metavar="YYYY-MM-DD",
                    help="only scan picks on or after this date")
    ap.add_argument("--dry-run", action="store_true",
                    help="report corrections without writing them")
    ap.add_argument("--no-resolve", action="store_true",
                    help="skip the follow-up resolve_pending sweep")
    args = ap.parse_args()

    kind = "PostgreSQL" if settings.is_postgres else "SQLite"
    print(f"Connecting to {kind}: {settings.database_url}")

    loop = _start_loop()
    db = asyncio.run_coroutine_threadsafe(
        create_database(settings.database_url), loop
    ).result()
    tracking.configure(db, loop)

    try:
        summary = tracking.reresolve_voids(since=args.since, dry_run=args.dry_run)
        _print_report(summary, args.dry_run)

        if not args.dry_run and not args.no_resolve:
            print("\nRunning resolve sweep for anything still pending...")
            print(tracking.resolve_pending())
    finally:
        loop.call_soon_threadsafe(loop.stop)


if __name__ == "__main__":
    main()
