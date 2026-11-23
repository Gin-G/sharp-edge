"""Season audit: is any part of the NFL record beating the price?

Read-only. Pulls the graded record from a running sharp-edge API and prints,
for every recommendation and every distinct paper-lotto leg, the hit rate
against the chance the book's price implied and the chance our model claimed.
A pick only earns its place by beating the *implied* number; beating 50% is
not the bar. Finding 15 in EXPERIMENTS_NFL.md is the first run.

    python backend/scripts/season_audit.py [--base https://sharp-edge.nickknows.net/api] [--season 2026]

Picks share games, players and weeks, so the ± printed (binomial, 95%) is
narrower than the truth. Treat a gap inside about 1.5x of it as noise.
"""
import argparse
import json
import math
import urllib.request
from collections import defaultdict


def fetch(base, path):
    req = urllib.request.Request(f"{base}/{path}", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def implied(odds):
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def line(name, rows, p_key):
    if not rows:
        return
    n = len(rows)
    hit = sum(r["y"] for r in rows) / n
    imp = sum(r["imp"] for r in rows) / n
    ps = [r[p_key] for r in rows if r.get(p_key) is not None]
    mp = sum(ps) / len(ps) if ps else float("nan")
    ci = 1.96 * math.sqrt(hit * (1 - hit) / n)
    units = sum(r.get("units") or 0 for r in rows)
    print(f"  {name:30s} n={n:4d}  hit {hit:5.1%} ±{ci:4.1%}  implied {imp:5.1%}  "
          f"ours {mp:5.1%}  units {units:+7.2f}")


def split(title, rows, key, p_key):
    print(f"-- {title}")
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    for k in sorted(groups, key=str):
        line(str(k), groups[k], p_key)


def brier(name, rows):
    rows = [r for r in rows if r.get("model_p") is not None]
    if len(rows) < 2:
        return
    d = [(r["model_p"] - r["y"]) ** 2 - (r["imp"] - r["y"]) ** 2 for r in rows]
    m = sum(d) / len(d)
    sd = math.sqrt(sum((v - m) ** 2 for v in d) / (len(d) - 1))
    print(f"  Brier {name}: ours minus book {m:+.4f} ±{1.96 * sd / math.sqrt(len(d)):.4f} "
          f"(n={len(d)}; positive = the book's price was the better forecast)")


def edge_band(e):
    e = e or 0
    return "<4" if e < 4 else "4-8" if e < 8 else "8-12" if e < 12 else "12+"


def price_band(o):
    return "short (-200 or less)" if o <= -200 else "-199 to -100" if o < 100 else "plus money"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://sharp-edge.nickknows.net/api")
    ap.add_argument("--season", type=int, default=2026)
    a = ap.parse_args()

    rec = fetch(a.base, f"nfl/recommendations?season={a.season}")["ledger"]
    rows = [r for r in rec if r["result"] in ("WIN", "LOSS") and r["odds"] is not None]
    for r in rows:
        r["y"] = int(r["result"] == "WIN")
        r["imp"] = implied(r["odds"])
        p = f" {r['pick'].lower()} "
        r["side"] = "over" if " over " in p else "under" if " under " in p else "-"
    print("RECOMMENDATIONS")
    line("all settled", rows, "model_p")
    split("by type", rows, lambda r: r["type"], "model_p")
    props = [r for r in rows if r["type"] == "prop"]
    split("props, by market and side", props, lambda r: f"{r['market']} {r['side']}", "model_p")
    split("props, under the current rules?", props, lambda r: r["current_rules"], "model_p")
    split("props, by edge points", props, lambda r: edge_band(r["edge_pts"]), "model_p")
    split("props, by our probability", props,
          lambda r: f"{min(int((r['model_p'] or 0) * 20) / 20, .75):.2f}+", "model_p")
    tds = [r for r in rows if r["type"] == "touchdown"]
    split("touchdowns, by price", tds,
          lambda r: "<+200" if r["odds"] < 200 else "+200 to +399" if r["odds"] < 400 else "+400+",
          "model_p")
    brier("props", props)
    brier("touchdowns", tds)

    paper = fetch(a.base, f"nfl/paper?season={a.season}")
    legs = {}
    for s in paper["strategies"]:
        for t in s["tickets"]:
            for leg in t["legs"]:
                if leg.get("result") in ("WIN", "LOSS") and leg.get("odds") is not None:
                    k = (t["week"], leg["player"], leg["market"], leg.get("threshold"), leg["side"])
                    legs[k] = dict(leg, week=t["week"], y=int(leg["result"] == "WIN"),
                                   imp=implied(leg["odds"]))
    legs = list(legs.values())
    print("\nPAPER LOTTO LEGS (each distinct leg once, across every strategy)")
    for s in paper["strategies"]:
        sm = s["summary"]
        print(f"  ticket {s['label']:36s} {sm['wins']}-{sm['losses']}  units {sm['units']:+.2f}")
    line("all legs", legs, "p_cal")
    split("legs, by market", legs, lambda r: r["market"], "p_cal")
    split("legs, by week", legs, lambda r: r["week"], "p_cal")
    split("legs, by price", legs, lambda r: price_band(r["odds"]), "p_cal")

    placed = fetch(a.base, f"nfl/placed?season={a.season}")["summary"]
    print(f"\nPLACED  {placed['wins']}-{placed['losses']} ({placed['pending']} pending)  "
          f"units {placed['units']:+.2f}")
    t = fetch(a.base, f"nfl/track-record?season={a.season}")["open_vs_lock"]
    print(f"OPEN vs LOCK  ROI at open {t['at_open']['roi']}%, at lock {t['at_lock']['roi']}%, "
          f"lines that moved: {t['line_moved']}, toward us {t['moved_to_us']}%")


if __name__ == "__main__":
    main()
