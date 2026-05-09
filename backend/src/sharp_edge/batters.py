"""MLB batter screen — recent form + BvP + handedness splits + pitcher form.

Returns three frames:

  hot_bats : everyone hitting > min_recent_avg over last N days
  today    : every batter in today's lineups, enriched with recent form,
             splits vs the opposing SP's handedness, BvP, and the SP's
             last-3-starts ERA — with edge flags so you can slice it
             however you want
  picks    : hot bats facing an advantageous matchup (≥1 edge type)

Edges:
  bvp_edge       : bvp_avg ≥ min_bvp_avg & bvp_pa ≥ min_bvp_pa
  hand_slump_edge: vs_hand_avg ≥ min_hand_avg & vs_hand_pa ≥ min_hand_pa
                   & opposing SP last-3 ERA ≥ min_slump_era

deps: install with `pip install -e ".[models]"` (pybaseball, MLB-StatsAPI, pandas)
"""

from __future__ import annotations

import functools
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import pybaseball as pb
import statsapi

pb.cache.enable()


@dataclass
class ScreenResult:
    picks: pd.DataFrame
    hot_bats: pd.DataFrame
    today: pd.DataFrame

    def __repr__(self) -> str:
        return (
            f"ScreenResult(picks={len(self.picks)}, "
            f"hot_bats={len(self.hot_bats)}, today={len(self.today)})"
        )


DEAD_STATES_SUBSTRINGS = ("Postponed", "Cancelled", "Canceled", "Suspended")


def _local_time_str(iso_utc: str) -> str:
    if not iso_utc:
        return ""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%-I:%M %p").lstrip("0")
    except Exception:
        return iso_utc


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return s.lower().replace(".", "").replace("'", "").replace("-", " ").strip()


def _ip_to_outs(ip_str) -> int:
    # MLB IP notation: '6.1' = 6 + 1/3 outs, '6.2' = 6 + 2/3 outs.
    if ip_str in (None, ""):
        return 0
    s = str(ip_str)
    if "." in s:
        whole, frac = s.split(".")
        return int(whole) * 3 + int(frac)
    return int(s) * 3


# AB = PA - (BB + HBP + SF + SH + catcher interference)
_NON_AB_EVENTS = frozenset({
    "walk", "intent_walk", "hit_by_pitch",
    "sac_fly", "sac_bunt",
    "sac_fly_double_play", "sac_bunt_double_play",
    "catcher_interf",
})
_HIT_EVENTS = frozenset({"single", "double", "triple", "home_run"})

_statcast_cache: pd.DataFrame | None = None
_statcast_lock = threading.Lock()


def _load_statcast(years_back: int = 3) -> pd.DataFrame:
    global _statcast_cache
    with _statcast_lock:
        if _statcast_cache is not None:
            return _statcast_cache

        today = date.today()
        frames = []
        for year in range(today.year - years_back + 1, today.year + 1):
            start_dt = f"{year}-03-15"
            end_dt = today.isoformat() if year == today.year else f"{year}-11-30"
            print(f"[statcast] loading {year} ({start_dt} -> {end_dt})...")
            df = pb.statcast(start_dt=start_dt, end_dt=end_dt, verbose=False)
            df = df[df["events"].notna()][
                ["batter", "pitcher", "events", "game_date"]
            ]
            frames.append(df)

        _statcast_cache = pd.concat(frames, ignore_index=True)
        print(f"[statcast] loaded {len(_statcast_cache):,} PA-ending events")
        return _statcast_cache


def _bvp(batter_id: int, pitcher_id: int) -> dict | None:
    df = _load_statcast()
    pa_df = df[(df["batter"] == batter_id) & (df["pitcher"] == pitcher_id)]
    if pa_df.empty:
        return None
    pa = len(pa_df)
    ab = pa - int(pa_df["events"].isin(_NON_AB_EVENTS).sum())
    hits = int(pa_df["events"].isin(_HIT_EVENTS).sum())
    if ab == 0:
        return None
    return {"pa": int(pa), "ab": ab, "hits": hits, "avg": round(hits / ab, 3)}


def debug_bvp_raw(batter_name: str, pitcher_name: str) -> dict:
    bs = statsapi.lookup_player(batter_name)
    ps = statsapi.lookup_player(pitcher_name)
    if not bs:
        raise ValueError(f"batter not found: {batter_name!r}")
    if not ps:
        raise ValueError(f"pitcher not found: {pitcher_name!r}")
    bid, pid = bs[0]["id"], ps[0]["id"]

    df = _load_statcast()
    pa_df = df[(df["batter"] == bid) & (df["pitcher"] == pid)].copy()
    pa_df = pa_df.sort_values("game_date").reset_index(drop=True)

    return {
        "batter": bs[0]["fullName"],
        "batter_id": bid,
        "pitcher": ps[0]["fullName"],
        "pitcher_id": pid,
        "events": pa_df[["game_date", "events"]].to_dict(orient="records"),
        "bvp": _bvp(bid, pid),
    }


@functools.lru_cache(maxsize=None)
def _pitcher_info(pitcher_id: int) -> dict:
    try:
        data = statsapi.get("person", {"personId": pitcher_id})
        person = (data.get("people") or [{}])[0]
        return {
            "hand": person.get("pitchHand", {}).get("code"),
            "name": person.get("fullName"),
        }
    except Exception:
        return {"hand": None, "name": None}


@functools.lru_cache(maxsize=None)
def _pitcher_last_3(pitcher_id: int, season: int) -> dict:
    try:
        data = statsapi.get(
            "people",
            {
                "personIds": str(pitcher_id),
                "hydrate": (
                    f"stats(group=[pitching],type=[gameLog],"
                    f"season={season},sportId=1)"
                ),
            },
        )
    except Exception:
        return {"era": None, "ip": 0.0, "er": 0, "starts": 0}

    games = []
    for person in data.get("people", []):
        for block in person.get("stats", []):
            for split in block.get("splits", []):
                stat = split.get("stat", {})
                if stat.get("gamesStarted") == 1:
                    games.append({
                        "date": split.get("date", ""),
                        "ip_str": stat.get("inningsPitched", "0.0"),
                        "er": stat.get("earnedRuns", 0),
                    })
    games.sort(key=lambda g: g["date"], reverse=True)
    last3 = games[:3]
    if not last3:
        return {"era": None, "ip": 0.0, "er": 0, "starts": 0}
    outs = sum(_ip_to_outs(g["ip_str"]) for g in last3)
    er = sum(g["er"] for g in last3)
    era = round((er * 27 / outs), 2) if outs else None
    return {"era": era, "ip": round(outs / 3, 1), "er": er, "starts": len(last3)}


@functools.lru_cache(maxsize=None)
def _handedness_splits(batter_id: int) -> dict:
    out = {"vs_R_avg": None, "vs_R_pa": 0, "vs_L_avg": None, "vs_L_pa": 0}

    for stat_type in ("careerStatSplits", "statSplits"):
        try:
            data = statsapi.get(
                "people",
                {
                    "personIds": str(batter_id),
                    "hydrate": (
                        f"stats(group=[hitting],type=[{stat_type}],"
                        f"sitCodes=[vr,vl],sportId=1)"
                    ),
                },
            )
        except Exception:
            continue

        for person in data.get("people", []):
            for block in person.get("stats", []):
                for split in block.get("splits", []):
                    code = split.get("split", {}).get("code", "")
                    stat = split.get("stat", {})
                    pa = stat.get("plateAppearances", 0)
                    if pa == 0:
                        continue
                    avg = float(stat.get("avg", "0") or 0)
                    if code == "vr" and pa > out["vs_R_pa"]:
                        out["vs_R_avg"], out["vs_R_pa"] = avg, pa
                    elif code == "vl" and pa > out["vs_L_pa"]:
                        out["vs_L_avg"], out["vs_L_pa"] = avg, pa

        if out["vs_R_pa"] > 0 or out["vs_L_pa"] > 0:
            break

    return out


def _roster_batters(team_id: int) -> list[tuple[int, str]]:
    try:
        data = statsapi.get(
            "team_roster", {"teamId": team_id, "rosterType": "active"}
        )
    except Exception:
        return []
    return [
        (p["person"]["id"], p["person"]["fullName"])
        for p in data.get("roster", [])
        if p.get("position", {}).get("abbreviation") != "P"
    ]


def lookup_bvp(batter_name: str, pitcher_name: str) -> dict:
    bs = statsapi.lookup_player(batter_name)
    ps = statsapi.lookup_player(pitcher_name)
    if not bs:
        raise ValueError(f"batter not found: {batter_name!r}")
    if not ps:
        raise ValueError(f"pitcher not found: {pitcher_name!r}")
    bid, pid = bs[0]["id"], ps[0]["id"]
    return {
        "batter": bs[0]["fullName"],
        "batter_id": bid,
        "pitcher": ps[0]["fullName"],
        "pitcher_id": pid,
        "bvp": _bvp(bid, pid),
    }


def screen_today(
    min_recent_avg: float = 0.300,
    min_recent_ab: int = 10,
    min_bvp_avg: float = 0.400,
    min_bvp_pa: int = 5,
    min_hand_avg: float = 0.400,
    min_hand_pa: int = 50,
    min_slump_era: float = 5.00,
    days: int = 7,
    workers: int = 12,
    verbose: bool = True,
) -> ScreenResult:
    today = date.today()
    end = today - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    season = today.year

    recent = pb.batting_stats_range(start.isoformat(), end.isoformat())
    cols = ["Name", "Tm", "BA", "AB", "H", "HR", "OBP", "OPS"]
    hot_df = recent[
        (recent["AB"] >= min_recent_ab) & (recent["BA"] >= min_recent_avg)
    ][cols].copy().rename(columns={"BA": "recent_avg", "AB": "recent_ab"})
    hot_df = hot_df.sort_values("recent_avg", ascending=False).reset_index(drop=True)

    all_recent = {
        _norm(row["Name"]): (float(row["BA"]), int(row["AB"]))
        for _, row in recent.iterrows()
    }

    # statsapi.schedule() omits probable pitcher IDs (only exposes names);
    # hit the raw endpoint with explicit hydrate so we can resolve IDs.
    raw = statsapi.get("schedule", {
        "sportId": 1,
        "date": today.strftime("%Y-%m-%d"),
        "hydrate": "probablePitcher,linescore",
    })
    games_kept = 0
    games_with_pp = 0
    targets = []
    raw_games = []
    for d in raw.get("dates", []):
        raw_games.extend(d.get("games", []))

    for game in raw_games:
        status = (game.get("status", {}) or {}).get("detailedState", "") or ""
        if any(bad in status for bad in DEAD_STATES_SUBSTRINGS):
            continue
        games_kept += 1

        away = game["teams"]["away"]
        home = game["teams"]["home"]
        away_team_id = away["team"]["id"]
        home_team_id = home["team"]["id"]
        away_team_name = away["team"].get("name", "")
        home_team_name = home["team"].get("name", "")

        away_pp = away.get("probablePitcher") or {}
        home_pp = home.get("probablePitcher") or {}
        away_pp_id, away_pp_name = away_pp.get("id"), away_pp.get("fullName", "")
        home_pp_id, home_pp_name = home_pp.get("id"), home_pp.get("fullName", "")

        if away_pp_id or home_pp_id:
            games_with_pp += 1

        gtime = game.get("gameDate", "")

        for batting_team_id, batting_team, opp_pid, opp_pname in (
            (away_team_id, away_team_name, home_pp_id, home_pp_name),
            (home_team_id, home_team_name, away_pp_id, away_pp_name),
        ):
            if not opp_pid:
                continue
            for bid, bname in _roster_batters(batting_team_id):
                targets.append((bid, bname, batting_team, opp_pid, opp_pname, gtime))

    if verbose:
        print(
            f"[screen] schedule={len(raw_games)} kept={games_kept} "
            f"with_probable_pitcher={games_with_pp} targets={len(targets)}"
        )

    def _scan(t):
        bid, _, _, ppid, _, _ = t
        return {
            "target": t,
            "bvp": _bvp(bid, ppid),
            "hand": _handedness_splits(bid),
            "pinfo": _pitcher_info(ppid),
            "p_l3": _pitcher_last_3(ppid, season),
        }

    if targets:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            scanned = list(pool.map(_scan, targets))
    else:
        scanned = []

    rows = []
    for r in scanned:
        bid, bname, team, ppid, ppname, gtime = r["target"]
        hand_split = r["hand"]
        p_hand = r["pinfo"]["hand"]
        p_l3 = r["p_l3"]
        bvp = r["bvp"] or {"avg": None, "pa": 0, "hits": 0}

        if p_hand == "R":
            vs_hand_avg = hand_split.get("vs_R_avg")
            vs_hand_pa = hand_split.get("vs_R_pa", 0)
        elif p_hand == "L":
            vs_hand_avg = hand_split.get("vs_L_avg")
            vs_hand_pa = hand_split.get("vs_L_pa", 0)
        else:
            vs_hand_avg, vs_hand_pa = None, 0

        rec = all_recent.get(_norm(bname))
        recent_avg = rec[0] if rec else None
        recent_ab = rec[1] if rec else 0

        is_hot = (
            recent_avg is not None
            and recent_avg >= min_recent_avg
            and recent_ab >= min_recent_ab
        )
        bvp_edge = (
            bvp["avg"] is not None
            and bvp["avg"] >= min_bvp_avg
            and bvp["pa"] >= min_bvp_pa
        )
        hand_slump_edge = (
            vs_hand_avg is not None
            and vs_hand_avg >= min_hand_avg
            and vs_hand_pa >= min_hand_pa
            and p_l3["era"] is not None
            and p_l3["era"] >= min_slump_era
            and p_l3["starts"] >= 3
        )

        tags = []
        if bvp_edge:
            tags.append("BvP")
        if hand_slump_edge:
            tags.append("HAND+SLUMP")

        rows.append({
            "batter": bname,
            "team": team,
            "opposing_pitcher": ppname,
            "p_hand": p_hand,
            "recent_avg": round(recent_avg, 3) if recent_avg is not None else None,
            "recent_ab": recent_ab,
            "vs_hand_avg": round(vs_hand_avg, 3) if vs_hand_avg is not None else None,
            "vs_hand_pa": vs_hand_pa,
            "bvp_avg": bvp["avg"],
            "bvp_pa": bvp["pa"],
            "bvp_hits": bvp["hits"],
            "p_l3_era": p_l3["era"],
            "p_l3_ip": p_l3["ip"],
            "p_l3_starts": p_l3["starts"],
            "is_hot": is_hot,
            "bvp_edge": bvp_edge,
            "hand_slump_edge": hand_slump_edge,
            "tags": ",".join(tags),
            "game_time": _local_time_str(gtime),
        })

    today_df = pd.DataFrame(rows)

    if today_df.empty:
        picks = pd.DataFrame()
    else:
        mask = today_df["is_hot"] & (today_df["bvp_edge"] | today_df["hand_slump_edge"])
        picks = today_df[mask].copy()
        if not picks.empty:
            picks = picks.sort_values(
                ["bvp_edge", "bvp_avg", "vs_hand_avg", "recent_avg"],
                ascending=[False, False, False, False],
            ).reset_index(drop=True)

    return ScreenResult(picks=picks, hot_bats=hot_df, today=today_df)


def find_value_bats(**kwargs) -> pd.DataFrame:
    """Backwards-compatible: returns just the picks frame."""
    return screen_today(**kwargs).picks
