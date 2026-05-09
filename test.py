"""CLI for the MLB batter screen. Logic lives in `sharp_edge.batters`."""

from __future__ import annotations

import pandas as pd

from sharp_edge.batters import screen_today


LEGEND = """\
=== COLUMN LEGEND ===

  IDENTITY
    batter            : player name
    team              : MLB team
    opp_pitcher       : starting pitcher facing this batter today
    hand              : opposing pitcher throws R or L

  RECENT FORM (Baseball Reference, last 7 days)
    r7_avg, r7_ab     : batting average and at-bats over the trailing window

  HANDEDNESS SPLIT (career, multi-season)
    vs_hand_avg       : career AVG vs pitchers of today's SP's hand
    vs_hand_pa        : career plate appearances in that split

  BvP — career vs THIS pitcher (Statcast, last 3 seasons)
    bvp_avg           : H / AB
    bvp_pa, bvp_h     : plate appearances and hits

  PITCHER FORM (current season)
    l3_era            : ERA over the SP's last 3 starts
    l3_ip, l3_gs      : innings pitched, starts logged

  EDGES (booleans collapsed into 'tags')
    BvP               : bvp_avg ≥ 0.400 with ≥ 5 PA
    HAND+SLUMP        : vs_hand_avg ≥ 0.400 (≥50 PA) AND opp SP l3_era ≥ 5.00

  PICKS = hot bat (r7_avg ≥ 0.300, ≥10 AB) AND ≥1 edge
=== """

DISPLAY_RENAME = {
    "opposing_pitcher": "opp_pitcher",
    "p_hand": "hand",
    "recent_avg": "r7_avg",
    "recent_ab": "r7_ab",
    "bvp_hits": "bvp_h",
    "p_l3_era": "l3_era",
    "p_l3_ip": "l3_ip",
    "p_l3_starts": "l3_gs",
}


def main() -> None:
    pd.set_option("display.max_rows", 200)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", None)

    print(LEGEND)
    res = screen_today()

    print(f"\n=== HOT BATS — {len(res.hot_bats)} players >.300 over last 7d ===")
    hot_cols = ["Name", "Tm", "recent_avg", "recent_ab", "H", "OPS"]
    hot = res.hot_bats[hot_cols].rename(columns={
        "Name": "batter", "Tm": "team",
        "recent_avg": "r7_avg", "recent_ab": "r7_ab",
    })
    print(hot.head(50).to_string(index=False))

    print(f"\n=== TODAY — {len(res.today)} batters in lineups ===")
    if res.today.empty:
        print("(empty) — no live matchups with probable pitchers")
    else:
        bvp_only = res.today[res.today["bvp_edge"]].sort_values(
            "bvp_avg", ascending=False
        )
        print(f"\n  -- BvP edges (≥.400 vs today's SP) — {len(bvp_only)} --")
        if bvp_only.empty:
            print("  (none)")
        else:
            cols = ["batter", "team", "opposing_pitcher",
                    "bvp_avg", "bvp_pa", "bvp_hits",
                    "recent_avg", "recent_ab", "game_time"]
            print(bvp_only[cols].rename(columns=DISPLAY_RENAME).to_string(index=False))

        hs_only = res.today[res.today["hand_slump_edge"]].sort_values(
            "vs_hand_avg", ascending=False
        )
        print(f"\n  -- Hand+slump edges (≥.400 vs hand & SP L3 ERA ≥5.00) — {len(hs_only)} --")
        if hs_only.empty:
            print("  (none)")
        else:
            cols = ["batter", "team", "opposing_pitcher", "p_hand",
                    "vs_hand_avg", "vs_hand_pa",
                    "p_l3_era", "p_l3_ip", "p_l3_starts",
                    "recent_avg", "game_time"]
            print(hs_only[cols].rename(columns=DISPLAY_RENAME).to_string(index=False))

    print(f"\n=== PICKS (hot AND has edge) — {len(res.picks)} ===")
    if res.picks.empty:
        print("(none)")
    else:
        cols = ["batter", "team", "opposing_pitcher", "p_hand",
                "recent_avg", "recent_ab",
                "vs_hand_avg", "vs_hand_pa",
                "bvp_avg", "bvp_pa", "bvp_hits",
                "p_l3_era", "p_l3_ip", "p_l3_starts",
                "tags", "game_time"]
        print(res.picks[cols].rename(columns=DISPLAY_RENAME).to_string(index=False))


if __name__ == "__main__":
    main()
