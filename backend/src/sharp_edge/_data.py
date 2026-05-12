"""Shared MLB data helpers — Statcast loader, StatsAPI wrappers, utilities.

Used by both batters.py and homers.py.  Nothing in here is app-specific;
it's all about fetching and shaping raw data.
"""

from __future__ import annotations

import functools
import threading
import unicodedata
from datetime import date
from typing import Optional

import pandas as pd
import pybaseball as pb
import statsapi

pb.cache.enable()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEAD_STATES_SUBSTRINGS = ("Postponed", "Cancelled", "Canceled", "Suspended")

_NON_AB_EVENTS = frozenset({
    "walk", "intent_walk", "hit_by_pitch",
    "sac_fly", "sac_bunt",
    "sac_fly_double_play", "sac_bunt_double_play",
    "catcher_interf",
})
_HIT_EVENTS = frozenset({"single", "double", "triple", "home_run"})

# Statcast columns loaded for the shared cache.  "barrel" is optional and
# appended only when the data export includes it.
_STATCAST_COLS = [
    "batter", "pitcher", "events", "game_date",
    "launch_speed", "launch_angle", "hc_x", "hc_y",
    "stand", "p_throws",
]

# ---------------------------------------------------------------------------
# Shared Statcast cache
# ---------------------------------------------------------------------------

_statcast_cache: Optional[pd.DataFrame] = None
_statcast_lock = threading.Lock()


def _load_statcast(years_back: int = 3) -> pd.DataFrame:
    """Load and cache ``years_back`` seasons of Statcast PA-ending events.

    The returned DataFrame contains batted-ball columns (launch_speed,
    launch_angle, hc_x, hc_y, stand, p_throws, barrel) in addition to the
    base columns used by batters.py (batter, pitcher, events, game_date).
    Callers that only need the base columns simply ignore the extras.
    """
    global _statcast_cache
    with _statcast_lock:
        if _statcast_cache is not None:
            return _statcast_cache

        import gc
        today = date.today()
        frames: list[pd.DataFrame] = []
        for year in range(today.year - years_back + 1, today.year + 1):
            start_dt = f"{year}-03-15"
            end_dt = today.isoformat() if year == today.year else f"{year}-11-30"
            print(f"[statcast] loading {year} ({start_dt} -> {end_dt})...")
            raw = pb.statcast(start_dt=start_dt, end_dt=end_dt, verbose=False)

            mask = raw["events"].notna()
            keep = [c for c in _STATCAST_COLS if c in raw.columns]
            if "barrel" in raw.columns:
                keep.append("barrel")

            # .loc[mask, keep].copy() selects rows AND columns in one step so
            # the wide full-year DataFrame (90 cols) can be freed immediately —
            # keeping two full-year frames in RAM simultaneously caused the OOM.
            filtered = raw.loc[mask, keep].copy()
            del raw
            gc.collect()

            # Downcast to cut per-year memory ~3×:
            #   float64 → float32 for batted-ball metrics
            #   batter/pitcher IDs → int32 (MLBAM IDs fit; max ~800K)
            for col in ("launch_speed", "launch_angle", "hc_x", "hc_y"):
                if col in filtered.columns:
                    filtered[col] = filtered[col].astype("float32")
            if "barrel" in filtered.columns:
                filtered["barrel"] = pd.to_numeric(
                    filtered["barrel"], errors="coerce"
                ).astype("float32")
            for col in ("batter", "pitcher"):
                if col in filtered.columns:
                    filtered[col] = (
                        pd.to_numeric(filtered[col], errors="coerce")
                        .fillna(0)
                        .astype("int32")
                    )

            frames.append(filtered)

        _statcast_cache = pd.concat(frames, ignore_index=True)
        del frames
        gc.collect()

        if "barrel" not in _statcast_cache.columns:
            _statcast_cache["barrel"] = float("nan")

        _statcast_cache["game_date"] = pd.to_datetime(
            _statcast_cache["game_date"], errors="coerce"
        )
        print(f"[statcast] loaded {len(_statcast_cache):,} PA-ending events")
        return _statcast_cache


# ---------------------------------------------------------------------------
# Small text / inning utilities
# ---------------------------------------------------------------------------

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
    """Convert MLB inning-pitched notation ('6.2') to integer out count."""
    if ip_str in (None, ""):
        return 0
    s = str(ip_str)
    if "." in s:
        whole, frac = s.split(".")
        return int(whole) * 3 + int(frac)
    return int(s) * 3


# ---------------------------------------------------------------------------
# MLB StatsAPI helpers (LRU-cached per-session)
# ---------------------------------------------------------------------------

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
    """ERA / IP / ER / starts over the pitcher's last 3 game-log starts."""
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


def _roster_batters(team_id: int) -> list[tuple[int, str]]:
    """Return (mlbam_id, full_name) for every non-pitcher on the active roster."""
    try:
        data = statsapi.get("team_roster", {"teamId": team_id, "rosterType": "active"})
    except Exception:
        return []
    return [
        (p["person"]["id"], p["person"]["fullName"])
        for p in data.get("roster", [])
        if p.get("position", {}).get("abbreviation") != "P"
    ]


def fetch_today_schedule() -> list[dict]:
    """Return the raw game list for today with probablePitcher hydrated."""
    raw = statsapi.get("schedule", {
        "sportId": 1,
        "date": date.today().strftime("%Y-%m-%d"),
        "hydrate": "probablePitcher,linescore",
    })
    games: list[dict] = []
    for d in raw.get("dates", []):
        games.extend(d.get("games", []))
    return games
