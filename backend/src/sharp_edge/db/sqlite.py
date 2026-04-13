"""SQLite backend — for local development."""

import csv
import json
from pathlib import Path
from typing import Optional

import aiosqlite

from .base import BetDatabase

SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
    bet_id TEXT PRIMARY KEY,
    sportsbook TEXT NOT NULL,
    bet_type TEXT NOT NULL,
    status TEXT NOT NULL,
    odds REAL,
    closing_line REAL,
    ev REAL,
    stake REAL NOT NULL,
    profit REAL DEFAULT 0.0,
    time_placed TEXT,
    time_settled TEXT,
    sport TEXT,
    league TEXT,
    bet_info TEXT,
    legs TEXT,
    leg_count INTEGER DEFAULT 1,
    tags TEXT,
    source TEXT DEFAULT 'fanduel',
    raw_json TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS bankroll_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    sportsbook TEXT, balance REAL,
    deposit REAL DEFAULT 0.0, withdrawal REAL DEFAULT 0.0, note TEXT
);
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY, value TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status);
CREATE INDEX IF NOT EXISTS idx_bets_league ON bets(league);
CREATE INDEX IF NOT EXISTS idx_bets_sportsbook ON bets(sportsbook);
CREATE INDEX IF NOT EXISTS idx_bets_time_placed ON bets(time_placed);
CREATE INDEX IF NOT EXISTS idx_bets_bet_type ON bets(bet_type);
"""


class SQLiteDatabase(BetDatabase):
    def __init__(self, url: str):
        # url: "sqlite:///path/to/db" or "sqlite:///~/.sharp-edge/bets.db"
        path_str = url.replace("sqlite:///", "")
        self.db_path = Path(path_str).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def upsert_bet(self, bet: dict) -> None:
        await self._db.execute(
            """INSERT INTO bets (
                bet_id, sportsbook, bet_type, status, odds, closing_line, ev,
                stake, profit, time_placed, time_settled, sport, league,
                bet_info, legs, leg_count, tags, source, raw_json, updated_at
            ) VALUES (
                :bet_id, :sportsbook, :bet_type, :status, :odds, :closing_line, :ev,
                :stake, :profit, :time_placed, :time_settled, :sport, :league,
                :bet_info, :legs, :leg_count, :tags, :source, :raw_json, datetime('now')
            ) ON CONFLICT(bet_id) DO UPDATE SET
                status=excluded.status, profit=excluded.profit,
                time_settled=excluded.time_settled, closing_line=excluded.closing_line,
                ev=excluded.ev, raw_json=excluded.raw_json, updated_at=datetime('now')
            """,
            bet,
        )
        await self._db.commit()

    async def upsert_bets(self, bets: list[dict]) -> int:
        for bet in bets:
            await self.upsert_bet(bet)
        return len(bets)

    async def query_bets(self, **kwargs) -> list[dict]:
        conditions, params = self._build_filters(**kwargs)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit = kwargs.get("limit", 500)
        offset = kwargs.get("offset", 0)
        query = f"SELECT * FROM bets {where} ORDER BY time_placed DESC LIMIT ? OFFSET ?"
        cursor = await self._db.execute(query, (*params, limit, offset))
        return [dict(row) for row in await cursor.fetchall()]

    async def get_summary_stats(self, **kwargs) -> dict:
        conditions = ["status IN ('SETTLED_WIN', 'SETTLED_LOSS')"]
        params = []
        for key in ("league", "sportsbook", "bet_type", "since"):
            val = kwargs.get(key)
            if val:
                col = "time_placed" if key == "since" else key
                op = ">=" if key == "since" else "="
                conditions.append(f"{col} {op} ?")
                params.append(val)
        where = f"WHERE {' AND '.join(conditions)}"
        cursor = await self._db.execute(
            f"""SELECT COUNT(*) as total_bets,
                SUM(CASE WHEN status='SETTLED_WIN' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN status='SETTLED_LOSS' THEN 1 ELSE 0 END) as losses,
                SUM(stake) as total_wagered, SUM(profit) as net_profit,
                AVG(odds) as avg_odds, AVG(stake) as avg_stake,
                MIN(time_placed) as first_bet, MAX(time_placed) as last_bet
            FROM bets {where}""",
            params,
        )
        row = dict(await cursor.fetchone())
        wag = row.get("total_wagered") or 0
        row["roi_pct"] = round((row["net_profit"] / wag) * 100, 2) if wag > 0 else 0.0
        return row

    async def get_breakdown(self, group_by: str = "league", since: Optional[str] = None) -> list[dict]:
        valid = {"league", "sportsbook", "bet_type", "sport"}
        if group_by not in valid:
            raise ValueError(f"group_by must be one of {valid}")
        conditions = ["status IN ('SETTLED_WIN', 'SETTLED_LOSS')"]
        params = []
        if since:
            conditions.append("time_placed >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(conditions)}"
        cursor = await self._db.execute(
            f"""SELECT {group_by}, COUNT(*) as total_bets,
                SUM(CASE WHEN status='SETTLED_WIN' THEN 1 ELSE 0 END) as wins,
                SUM(stake) as total_wagered, SUM(profit) as net_profit
            FROM bets {where} GROUP BY {group_by} ORDER BY net_profit DESC""",
            params,
        )
        results = []
        for row in await cursor.fetchall():
            d = dict(row)
            d["losses"] = d["total_bets"] - d["wins"]
            d["win_pct"] = round(d["wins"] / d["total_bets"] * 100, 1) if d["total_bets"] else 0
            d["roi_pct"] = round(d["net_profit"] / d["total_wagered"] * 100, 2) if d["total_wagered"] else 0
            results.append(d)
        return results

    async def get_calendar_data(self, since: Optional[str] = None, until: Optional[str] = None) -> list[dict]:
        conditions = ["status IN ('SETTLED_WIN', 'SETTLED_LOSS')"]
        params = []
        if since:
            conditions.append("time_placed >= ?")
            params.append(since)
        if until:
            conditions.append("time_placed <= ?")
            params.append(until)
        where = f"WHERE {' AND '.join(conditions)}"
        cursor = await self._db.execute(
            f"""SELECT date(time_placed) as day, COUNT(*) as total_bets,
                SUM(CASE WHEN status='SETTLED_WIN' THEN 1 ELSE 0 END) as wins,
                SUM(stake) as wagered, SUM(profit) as net_profit
            FROM bets {where} GROUP BY date(time_placed) ORDER BY day""",
            params,
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def get_sync_state(self, key: str) -> Optional[str]:
        cursor = await self._db.execute("SELECT value FROM sync_state WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row["value"] if row else None

    async def set_sync_state(self, key: str, value: str) -> None:
        await self._db.execute(
            "INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (key, value),
        )
        await self._db.commit()

    async def import_pikkit_csv(self, csv_path: str) -> int:
        count = 0
        with open(csv_path, "r") as f:
            for row in csv.DictReader(f):
                if row.get("status") not in ("SETTLED_WIN", "SETTLED_LOSS", "PLACED"):
                    continue
                legs = row.get("bet_info", "").split("|")
                await self.upsert_bet({
                    "bet_id": row["bet_id"],
                    "sportsbook": row.get("sportsbook", "unknown"),
                    "bet_type": row.get("type", "straight"),
                    "status": row["status"],
                    "odds": float(row["odds"]) if row.get("odds") else None,
                    "closing_line": float(row["closing_line"]) if row.get("closing_line") else None,
                    "ev": float(row["ev"]) if row.get("ev") else None,
                    "stake": float(row.get("amount", 0)),
                    "profit": float(row.get("profit", 0)),
                    "time_placed": row.get("time_placed_iso"),
                    "time_settled": row.get("time_settled_iso"),
                    "sport": row.get("sports"),
                    "league": row.get("leagues"),
                    "bet_info": row.get("bet_info"),
                    "legs": json.dumps(legs),
                    "leg_count": len(legs),
                    "tags": row.get("tags"),
                    "source": "pikkit",
                    "raw_json": None,
                })
                count += 1
        return count

    def _build_filters(self, **kwargs) -> tuple[list[str], list]:
        conditions, params = [], []
        for key in ("status", "league", "sportsbook", "bet_type", "sport"):
            if kwargs.get(key):
                conditions.append(f"{key} = ?")
                params.append(kwargs[key])
        if kwargs.get("since"):
            conditions.append("time_placed >= ?")
            params.append(kwargs["since"])
        if kwargs.get("until"):
            conditions.append("time_placed <= ?")
            params.append(kwargs["until"])
        return conditions, params
