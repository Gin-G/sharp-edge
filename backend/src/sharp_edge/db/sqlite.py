"""SQLite backend — for local development."""

import csv
import json
from pathlib import Path
from typing import Optional

import aiosqlite

from .base import PARLAY_COLUMNS, PICK_COLUMNS, BetDatabase

SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
    user_id TEXT NOT NULL,
    bet_id TEXT NOT NULL,
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
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, bet_id)
);
CREATE TABLE IF NOT EXISTS bankroll_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    timestamp TEXT DEFAULT (datetime('now')),
    sportsbook TEXT, balance REAL,
    deposit REAL DEFAULT 0.0, withdrawal REAL DEFAULT 0.0, note TEXT
);
CREATE TABLE IF NOT EXISTS sync_state (
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS model_picks (
    screen TEXT NOT NULL,
    pick_date TEXT NOT NULL,
    batter_id INTEGER NOT NULL,
    batter TEXT,
    team TEXT,
    pitcher_id INTEGER,
    opposing_pitcher TEXT,
    venue TEXT,
    score REAL,
    rank INTEGER,
    tags TEXT,
    metrics TEXT,
    source TEXT DEFAULT 'live',
    result TEXT,
    hr_actual INTEGER,
    hits_actual INTEGER,
    pa_actual INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    resolved_at TEXT,
    PRIMARY KEY (screen, pick_date, batter_id)
);
CREATE TABLE IF NOT EXISTS model_parlays (
    pick_date TEXT PRIMARY KEY,
    legs TEXT NOT NULL,
    leg_count INTEGER NOT NULL,
    american INTEGER,
    decimal_odds REAL,
    model_p REAL,
    created_at TEXT DEFAULT (datetime('now')),
    result TEXT,
    legs_won INTEGER,
    legs_settled INTEGER,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS screen_runs (
    screen TEXT NOT NULL,
    pick_date TEXT NOT NULL,
    picks INTEGER NOT NULL DEFAULT 0,
    ran_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (screen, pick_date)
);
CREATE INDEX IF NOT EXISTS idx_model_picks_screen_date ON model_picks(screen, pick_date);
CREATE INDEX IF NOT EXISTS idx_model_picks_result ON model_picks(result);
CREATE INDEX IF NOT EXISTS idx_bets_user_id ON bets(user_id);
CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status);
CREATE INDEX IF NOT EXISTS idx_bets_league ON bets(league);
CREATE INDEX IF NOT EXISTS idx_bets_sportsbook ON bets(sportsbook);
CREATE INDEX IF NOT EXISTS idx_bets_time_placed ON bets(time_placed);
CREATE INDEX IF NOT EXISTS idx_bets_bet_type ON bets(bet_type);
CREATE INDEX IF NOT EXISTS idx_bankroll_user_id ON bankroll_log(user_id);
"""


async def _has_user_id_column(db: aiosqlite.Connection, table: str) -> bool:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    cols = [row["name"] for row in await cursor.fetchall()]
    return "user_id" in cols


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return await cursor.fetchone() is not None


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

        # Detect old schema (no user_id column on bets) and wipe so the new
        # multi-user schema can take its place. One-shot migration.
        if await _table_exists(self._db, "bets") and not await _has_user_id_column(
            self._db, "bets"
        ):
            await self._db.executescript(
                "DROP TABLE IF EXISTS bets;"
                "DROP TABLE IF EXISTS bankroll_log;"
                "DROP TABLE IF EXISTS sync_state;"
            )

        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def upsert_bet(self, user_id: str, bet: dict) -> None:
        params = {**bet, "user_id": user_id}
        await self._db.execute(
            """INSERT INTO bets (
                user_id, bet_id, sportsbook, bet_type, status, odds, closing_line, ev,
                stake, profit, time_placed, time_settled, sport, league,
                bet_info, legs, leg_count, tags, source, raw_json, updated_at
            ) VALUES (
                :user_id, :bet_id, :sportsbook, :bet_type, :status, :odds, :closing_line, :ev,
                :stake, :profit, :time_placed, :time_settled, :sport, :league,
                :bet_info, :legs, :leg_count, :tags, :source, :raw_json, datetime('now')
            ) ON CONFLICT(user_id, bet_id) DO UPDATE SET
                status=excluded.status, profit=excluded.profit,
                time_settled=excluded.time_settled, closing_line=excluded.closing_line,
                ev=excluded.ev, raw_json=excluded.raw_json, updated_at=datetime('now')
            """,
            params,
        )
        await self._db.commit()

    async def upsert_bets(self, user_id: str, bets: list[dict]) -> int:
        for bet in bets:
            await self.upsert_bet(user_id, bet)
        return len(bets)

    async def query_bets(self, user_id: str, **kwargs) -> list[dict]:
        conditions, params = self._build_filters(**kwargs)
        conditions.insert(0, "user_id = ?")
        params.insert(0, user_id)
        where = f"WHERE {' AND '.join(conditions)}"
        limit = kwargs.get("limit", 500)
        offset = kwargs.get("offset", 0)
        query = f"SELECT * FROM bets {where} ORDER BY time_placed DESC LIMIT ? OFFSET ?"
        cursor = await self._db.execute(query, (*params, limit, offset))
        return [dict(row) for row in await cursor.fetchall()]

    async def get_summary_stats(self, user_id: str, **kwargs) -> dict:
        conditions = ["user_id = ?", "status IN ('SETTLED_WIN', 'SETTLED_LOSS')"]
        params = [user_id]
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

    async def get_breakdown(
        self, user_id: str, group_by: str = "league", since: Optional[str] = None
    ) -> list[dict]:
        valid = {"league", "sportsbook", "bet_type", "sport"}
        if group_by not in valid:
            raise ValueError(f"group_by must be one of {valid}")
        conditions = ["user_id = ?", "status IN ('SETTLED_WIN', 'SETTLED_LOSS')"]
        params = [user_id]
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

    async def get_calendar_data(
        self, user_id: str, since: Optional[str] = None, until: Optional[str] = None
    ) -> list[dict]:
        conditions = ["user_id = ?", "status IN ('SETTLED_WIN', 'SETTLED_LOSS')"]
        params = [user_id]
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

    async def get_sync_state(self, user_id: str, key: str) -> Optional[str]:
        cursor = await self._db.execute(
            "SELECT value FROM sync_state WHERE user_id = ? AND key = ?",
            (user_id, key),
        )
        row = await cursor.fetchone()
        return row["value"] if row else None

    async def set_sync_state(self, user_id: str, key: str, value: str) -> None:
        await self._db.execute(
            "INSERT INTO sync_state (user_id, key, value, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(user_id, key) DO UPDATE SET "
            "value=excluded.value, updated_at=datetime('now')",
            (user_id, key, value),
        )
        await self._db.commit()

    async def import_pikkit_csv(self, user_id: str, csv_path: str) -> int:
        count = 0
        with open(csv_path, "r") as f:
            for row in csv.DictReader(f):
                if row.get("status") not in ("SETTLED_WIN", "SETTLED_LOSS", "PLACED"):
                    continue
                legs = row.get("bet_info", "").split("|")
                await self.upsert_bet(user_id, {
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

    async def insert_picks(self, rows: list[dict]) -> int:
        inserted = 0
        for r in rows:
            cursor = await self._db.execute(
                """INSERT INTO model_picks (
                    screen, pick_date, batter_id, batter, team, pitcher_id,
                    opposing_pitcher, venue, score, rank, tags, metrics, source
                ) VALUES (
                    :screen, :pick_date, :batter_id, :batter, :team, :pitcher_id,
                    :opposing_pitcher, :venue, :score, :rank, :tags, :metrics, :source
                ) ON CONFLICT(screen, pick_date, batter_id) DO NOTHING""",
                r,
            )
            inserted += cursor.rowcount if cursor.rowcount > 0 else 0
        await self._db.commit()
        return inserted

    async def insert_parlay(self, row: dict) -> bool:
        cursor = await self._db.execute(
            """INSERT INTO model_parlays (
                pick_date, legs, leg_count, american, decimal_odds, model_p
            ) VALUES (
                :pick_date, :legs, :leg_count, :american, :decimal_odds, :model_p
            ) ON CONFLICT(pick_date) DO NOTHING""",
            row,
        )
        await self._db.commit()
        return bool(cursor.rowcount)

    async def get_parlay(self, pick_date: str):
        cursor = await self._db.execute(
            f"SELECT {PARLAY_COLUMNS} FROM model_parlays WHERE pick_date = ?",
            (pick_date,),
        )
        r = await cursor.fetchone()
        return dict(r) if r else None

    async def list_parlays(self, since=None, limit: int = 400) -> list[dict]:
        sql = f"SELECT {PARLAY_COLUMNS} FROM model_parlays"
        args: list = []
        if since:
            sql += " WHERE pick_date >= ?"
            args.append(since)
        sql += " ORDER BY pick_date DESC LIMIT ?"
        args.append(limit)
        cursor = await self._db.execute(sql, args)
        return [dict(r) for r in await cursor.fetchall()]

    async def settle_parlay(
        self, pick_date: str, result: str, legs_won: int, legs_settled: int
    ) -> None:
        await self._db.execute(
            """UPDATE model_parlays
               SET result = ?, legs_won = ?, legs_settled = ?,
                   resolved_at = datetime('now')
             WHERE pick_date = ?""",
            (result, legs_won, legs_settled, pick_date),
        )
        await self._db.commit()

    async def delete_picks(
        self, screen: str, pick_date: str, unresolved_only: bool = True
    ) -> int:
        sql = "DELETE FROM model_picks WHERE screen = ? AND pick_date = ?"
        if unresolved_only:
            sql += " AND result IS NULL"
        cursor = await self._db.execute(sql, (screen, pick_date))
        await self._db.commit()
        return cursor.rowcount

    async def list_picks(
        self,
        screen: str,
        since: Optional[str] = None,
        until: Optional[str] = None,
        unresolved_only: bool = False,
        include_metrics: bool = False,
    ) -> list[dict]:
        conditions = ["screen = ?"]
        params: list = [screen]
        if since:
            conditions.append("pick_date >= ?")
            params.append(since)
        if until:
            conditions.append("pick_date <= ?")
            params.append(until)
        if unresolved_only:
            conditions.append("result IS NULL")
        cols = "*" if include_metrics else PICK_COLUMNS
        cursor = await self._db.execute(
            f"SELECT {cols} FROM model_picks WHERE {' AND '.join(conditions)} "
            "ORDER BY pick_date DESC, rank ASC",
            params,
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def record_screen_run(self, screen: str, pick_date: str, picks: int) -> None:
        await self._db.execute(
            """INSERT INTO screen_runs (screen, pick_date, picks)
               VALUES (?, ?, ?)
               ON CONFLICT (screen, pick_date)
               DO UPDATE SET picks = excluded.picks, ran_at = datetime('now')""",
            (screen, pick_date, picks),
        )
        await self._db.commit()

    async def screened_dates(self, screen: str) -> set[str]:
        cursor = await self._db.execute(
            """SELECT pick_date FROM screen_runs WHERE screen = ?
               UNION
               SELECT DISTINCT pick_date FROM model_picks WHERE screen = ?""",
            (screen, screen),
        )
        return {row[0] for row in await cursor.fetchall()}

    async def update_pick_results(
        self, screen: str, pick_date: str, results: list[dict]
    ) -> int:
        updated = 0
        for r in results:
            cursor = await self._db.execute(
                "UPDATE model_picks SET result = ?, hr_actual = ?, hits_actual = ?, "
                "pa_actual = ?, resolved_at = datetime('now') "
                "WHERE screen = ? AND pick_date = ? AND batter_id = ?",
                (
                    r["result"], r.get("hr_actual"), r.get("hits_actual"),
                    r.get("pa_actual"), screen, pick_date, r["batter_id"],
                ),
            )
            updated += cursor.rowcount
        await self._db.commit()
        return updated

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
