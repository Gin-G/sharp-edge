"""CLI for the MLB HR probability screen.  Logic lives in `sharp_edge.homers`.

Usage
-----
  python test_hr.py           # full run — loads 3 seasons of Statcast (~minutes)
  python test_hr.py --stub    # skeleton run — skips Statcast download, all
                              # API calls (schedule / rosters / pitchers) still
                              # execute; power metrics show as null
"""

from __future__ import annotations

import sys

import pandas as pd

from sharp_edge.homers import (
    HR_LAST_15D_HOT,
    screen_today_hr,
)

LEGEND = """\
=== COLUMN LEGEND ===

  IDENTITY
    batter            : player name
    team              : MLB team
    opp_pitcher       : starting pitcher facing this batter today
    hand              : opposing pitcher throws R or L
    game_time         : local start time

  BATTER POWER METRICS (Statcast, last 3 seasons + season-to-date)
    iso_career        : ISO (SLG – AVG) over the 3-season Statcast window
    iso_season        : ISO current season (Statcast)
    iso_vs_hand       : ISO vs opposing SP's handedness (≥50 PA to surface)
    barrel_pct        : barrel% of batted balls (Statcast field; simplified EV/LA
                        threshold of 98 mph / 26–50° when field is absent)
    hard_hit_pct      : hard-hit% (EV ≥ 95 mph)
    hr_15d, hr_30d    : home runs in last 15 / 30 days
    pa_15d            : plate appearances in last 15 days
    pull_air_pct      : pulled fly-ball% (pulled FBs ÷ all batted balls)

  PITCHER METRICS (Statcast + season gamelog)
    p_hr9             : SP HR/9 current season (official game log)
    p_hr9_l3          : SP HR/9 over last 3 starts (official game log)
    p_barrel_pct      : barrel% allowed (Statcast)
    p_hard_hit_pct    : hard-hit% allowed (Statcast)
    p_fb_pct          : fly-ball% (LA ≥ 25°, Statcast)
    p_hr9_vs_hand     : HR/27PA vs batter's hand (Statcast, approximate)

  BvP — vs THIS pitcher (Statcast, last 3 seasons)
    bvp_hr            : home runs hit vs this pitcher
    bvp_pa            : plate appearances vs this pitcher

  PARK
    park_factor       : HR park factor for batter's hand (100 = league average;
                        hardcoded 2023-2025 Baseball Savant averages)
    venue             : ballpark name (resolved from schedule payload, not team home)

  EDGES (booleans collapsed into 'tags')
    POWER+HAND  : iso_vs_hand ≥ .250 (≥50 PA) AND opp SP p_hr9_l3 ≥ 1.5
    BARREL-EDGE : batter barrel% ≥ 12 AND opp SP barrel% allowed ≥ 10
    PARK-BOOST  : park_factor ≥ 110 AND batter iso_career ≥ .200
    BVP-HR      : bvp_hr ≥ 1 AND bvp_pa ≥ 5
    HOT-POP     : hr_last_15d ≥ 2

  PICKS = HOT-POP AND ≥1 of {POWER+HAND, BARREL-EDGE, PARK-BOOST, BVP-HR}
=== """

DISPLAY_RENAME = {
    "opposing_pitcher": "opp_pitcher",
    "p_hand": "hand",
    "hr_last_15d": "hr_15d",
    "hr_last_30d": "hr_30d",
    "pa_last_15d": "pa_15d",
    "p_hr9_season": "p_hr9",
    "p_barrel_pct": "p_barrel%",
    "p_hard_hit_pct": "p_hh%",
    "p_fb_pct": "p_fb%",
    "barrel_pct": "barrel%",
    "hard_hit_pct": "hh%",
    "pull_air_pct": "pull_air%",
    "bvp_barrel_pct": "bvp_barrel%",
}


def _inject_stub() -> None:
    """Pre-populate the shared Statcast cache with an empty schema-correct
    DataFrame so _load_statcast() short-circuits without downloading anything.

    All schedule / roster / pitcher API calls still run.  Power metrics (ISO,
    barrel%, etc.) will be null/zero because no batter matches the empty cache.
    """
    import sharp_edge._data as _d

    with _d._statcast_lock:
        _d._statcast_cache = pd.DataFrame({
            "batter":       pd.array([], dtype="int32"),
            "pitcher":      pd.array([], dtype="int32"),
            "events":       pd.array([], dtype="object"),
            "game_date":    pd.array([], dtype="datetime64[ns]"),
            "launch_speed": pd.array([], dtype="float32"),
            "launch_angle": pd.array([], dtype="float32"),
            "hc_x":         pd.array([], dtype="float32"),
            "hc_y":         pd.array([], dtype="float32"),
            "stand":        pd.array([], dtype="object"),
            "p_throws":     pd.array([], dtype="object"),
            "barrel":       pd.array([], dtype="float32"),
        })


def main() -> None:
    pd.set_option("display.max_rows", 200)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", None)

    stub = "--stub" in sys.argv
    if stub:
        _inject_stub()
        print("[stub mode] Statcast download skipped — schedule/roster/pitcher APIs still live\n")

    print(LEGEND)
    res = screen_today_hr()

    # ------------------------------------------------------------------ HOT POP
    print(f"\n=== HOT POP — {len(res.hot_pop)} players with ≥{HR_LAST_15D_HOT} HR in last 15d ===")
    if res.hot_pop.empty:
        print("(none)")
    else:
        hot_cols = [
            "batter", "team", "hr_last_15d", "hr_last_30d",
            "iso_career", "barrel_pct", "game_time", "opposing_pitcher",
        ]
        print(
            res.hot_pop[hot_cols]
            .rename(columns=DISPLAY_RENAME)
            .sort_values("hr_15d", ascending=False)
            .to_string(index=False)
        )

    # ------------------------------------------------------------------ TODAY
    print(f"\n=== TODAY — {len(res.today)} batters in lineups ===")
    if res.today.empty:
        print("(empty) — no live matchups with probable pitchers")
    else:
        # POWER+HAND
        ph = res.today[res.today["power_hand_edge"]].sort_values("iso_vs_hand", ascending=False)
        print(f"\n  -- POWER+HAND edges (iso_vs_hand ≥ .250, p_hr9_l3 ≥ 1.5) — {len(ph)} --")
        if ph.empty:
            print("  (none)")
        else:
            cols = [
                "batter", "team", "opposing_pitcher", "p_hand",
                "iso_vs_hand", "iso_career", "p_hr9_l3", "p_hr9_season",
                "game_time",
            ]
            print(ph[cols].rename(columns=DISPLAY_RENAME).to_string(index=False))

        # BARREL-EDGE
        be = res.today[res.today["barrel_edge"]].sort_values("barrel_pct", ascending=False)
        print(f"\n  -- BARREL-EDGE (batter barrel% ≥ 12, SP barrel% allowed ≥ 10) — {len(be)} --")
        if be.empty:
            print("  (none)")
        else:
            cols = [
                "batter", "team", "opposing_pitcher", "p_hand",
                "barrel_pct", "p_barrel_pct", "hard_hit_pct", "p_hard_hit_pct",
                "game_time",
            ]
            print(be[cols].rename(columns=DISPLAY_RENAME).to_string(index=False))

        # PARK-BOOST
        pb_edge = res.today[res.today["park_boost_edge"]].sort_values("park_factor", ascending=False)
        print(f"\n  -- PARK-BOOST (park factor ≥ 110, iso_career ≥ .200) — {len(pb_edge)} --")
        if pb_edge.empty:
            print("  (none)")
        else:
            cols = [
                "batter", "team", "opposing_pitcher", "p_hand",
                "park_factor", "venue", "iso_career", "iso_vs_hand",
                "game_time",
            ]
            print(pb_edge[cols].rename(columns=DISPLAY_RENAME).to_string(index=False))

        # BVP-HR
        bvp = res.today[res.today["bvp_hr_edge"]].sort_values("bvp_hr", ascending=False)
        print(f"\n  -- BVP-HR (bvp_hr ≥ 1, bvp_pa ≥ 5) — {len(bvp)} --")
        if bvp.empty:
            print("  (none)")
        else:
            cols = [
                "batter", "team", "opposing_pitcher", "p_hand",
                "bvp_hr", "bvp_pa", "bvp_barrel_pct",
                "iso_vs_hand", "game_time",
            ]
            print(bvp[cols].rename(columns=DISPLAY_RENAME).to_string(index=False))

    # ------------------------------------------------------------------ PICKS
    from sharp_edge.homers import TOP_N_PICKS
    print(f"\n=== PICKS — top {TOP_N_PICKS} by hr_score (HOT-POP AND ≥1 edge) — {len(res.picks)} ===")
    if res.picks.empty:
        print("(none)")
    else:
        cols = [
            "hr_score",
            "batter", "team", "opposing_pitcher", "p_hand",
            "game_time", "venue", "park_factor",
            "iso_career", "iso_vs_hand",
            "barrel_pct", "hard_hit_pct",
            "hr_last_15d", "hr_last_30d",
            "p_hr9_season", "p_hr9_l3", "p_barrel_pct",
            "bvp_hr", "bvp_pa",
            "tags",
        ]
        print(res.picks[cols].rename(columns=DISPLAY_RENAME).to_string(index=False))


if __name__ == "__main__":
    main()
