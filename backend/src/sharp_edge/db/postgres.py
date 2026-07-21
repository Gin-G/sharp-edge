"""PostgreSQL backend — for production via CloudNativePG."""

import csv
import json
from datetime import datetime
from typing import Optional

import asyncpg

from .base import BetDatabase


def _to_dt(v):
    """Coerce ISO-8601 strings to datetime; asyncpg refuses str for timestamptz.
    SQLite tolerated raw ISO strings; Postgres does not."""
    if v is None or isinstance(v, datetime):
        return v
    if isinstance(v, str):
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    raise TypeError(f"unsupported timestamp type: {type(v).__name__}")


SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
    user_id TEXT NOT NULL,
    bet_id TEXT NOT NULL,
    sportsbook TEXT NOT NULL,
    bet_type TEXT NOT NULL,
    status TEXT NOT NULL,
    odds DOUBLE PRECISION,
    closing_line DOUBLE PRECISION,
    ev DOUBLE PRECISION,
    stake DOUBLE PRECISION NOT NULL,
    profit DOUBLE PRECISION DEFAULT 0.0,
    time_placed TIMESTAMPTZ,
    time_settled TIMESTAMPTZ,
    sport TEXT,
    league TEXT,
    bet_info TEXT,
    legs JSONB,
    leg_count INTEGER DEFAULT 1,
    tags TEXT,
    source TEXT DEFAULT 'fanduel',
    raw_json JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, bet_id)
);
CREATE TABLE IF NOT EXISTS bankroll_log (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    sportsbook TEXT, balance DOUBLE PRECISION,
    deposit DOUBLE PRECISION DEFAULT 0.0,
    withdrawal DOUBLE PRECISION DEFAULT 0.0, note TEXT
);
CREATE TABLE IF NOT EXISTS sync_state (
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS model_picks (
    screen TEXT NOT NULL,
    pick_date DATE NOT NULL,
    batter_id INTEGER NOT NULL,
    batter TEXT,
    team TEXT,
    pitcher_id INTEGER,
    opposing_pitcher TEXT,
    venue TEXT,
    score DOUBLE PRECISION,
    rank INTEGER,
    tags TEXT,
    metrics JSONB,
    source TEXT DEFAULT 'live',
    result TEXT,
    hr_actual INTEGER,
    hits_actual INTEGER,
    pa_actual INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    PRIMARY KEY (screen, pick_date, batter_id)
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_bets_user_id ON bets(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status)",
    "CREATE INDEX IF NOT EXISTS idx_bets_league ON bets(league)",
    "CREATE INDEX IF NOT EXISTS idx_bets_sportsbook ON bets(sportsbook)",
    "CREATE INDEX IF NOT EXISTS idx_bets_time_placed ON bets(time_placed)",
    "CREATE INDEX IF NOT EXISTS idx_bets_bet_type ON bets(bet_type)",
    "CREATE INDEX IF NOT EXISTS idx_bankroll_user_id ON bankroll_log(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_bets_tags ON bets USING gin(to_tsvector('english', coalesce(tags, '')))",
    "CREATE INDEX IF NOT EXISTS idx_model_picks_screen_date ON model_picks(screen, pick_date)",
    "CREATE INDEX IF NOT EXISTS idx_model_picks_result ON model_picks(result)",
]


async def _has_user_id_column(conn: asyncpg.Connection, table: str) -> bool:
    row = await conn.fetchrow(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = $1 AND column_name = 'user_id'",
        table,
    )
    return row is not None


async def _table_exists(conn: asyncpg.Connection, table: str) -> bool:
    row = await conn.fetchrow(
        "SELECT 1 FROM information_schema.tables WHERE table_name = $1", table
    )
    return row is not None


class PostgresDatabase(BetDatabase):
    def __init__(self, url: str):
        self.url = url
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self.url, min_size=2, max_size=10)
        async with self._pool.acquire() as conn:
            # One-shot wipe when the old (single-user) schema is detected.
            if await _table_exists(conn, "bets") and not await _has_user_id_column(
                conn, "bets"
            ):
                await conn.execute(
                    "DROP TABLE IF EXISTS bets CASCADE; "
                    "DROP TABLE IF EXISTS bankroll_log CASCADE; "
                    "DROP TABLE IF EXISTS sync_state CASCADE;"
                )
            await conn.execute(SCHEMA)
            for idx in INDEXES:
                try:
                    await conn.execute(idx)
                except Exception:
                    pass  # GIN index may fail on empty table, that's fine

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    async def upsert_bet(self, user_id: str, bet: dict) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO bets (
                    user_id, bet_id, sportsbook, bet_type, status, odds, closing_line, ev,
                    stake, profit, time_placed, time_settled, sport, league,
                    bet_info, legs, leg_count, tags, source, raw_json, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,NOW())
                ON CONFLICT(user_id, bet_id) DO UPDATE SET
                    status=EXCLUDED.status, profit=EXCLUDED.profit,
                    time_settled=EXCLUDED.time_settled, closing_line=EXCLUDED.closing_line,
                    ev=EXCLUDED.ev, raw_json=EXCLUDED.raw_json, updated_at=NOW()
                """,
                user_id, bet["bet_id"], bet["sportsbook"], bet["bet_type"], bet["status"],
                bet.get("odds"), bet.get("closing_line"), bet.get("ev"),
                bet["stake"], bet.get("profit", 0),
                _to_dt(bet.get("time_placed")), _to_dt(bet.get("time_settled")),
                bet.get("sport"), bet.get("league"), bet.get("bet_info"),
                bet.get("legs"), bet.get("leg_count", 1),
                bet.get("tags"), bet.get("source", "fanduel"), bet.get("raw_json"),
            )

    async def upsert_bets(self, user_id: str, bets: list[dict]) -> int:
        for bet in bets:
            await self.upsert_bet(user_id, bet)
        return len(bets)

    async def query_bets(self, user_id: str, **kwargs) -> list[dict]:
        conditions, params = self._build_filters(**kwargs)
        all_conditions, all_params = self._renumber(user_id, conditions, params)
        where = f"WHERE {' AND '.join(all_conditions)}"
        limit = kwargs.get("limit", 500)
        offset = kwargs.get("offset", 0)
        n = len(all_params)
        query = f"SELECT * FROM bets {where} ORDER BY time_placed DESC LIMIT ${n+1} OFFSET ${n+2}"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *all_params, limit, offset)
            return [dict(r) for r in rows]

    async def get_summary_stats(self, user_id: str, **kwargs) -> dict:
        conditions = ["user_id = $1", "status IN ('SETTLED_WIN', 'SETTLED_LOSS')"]
        params = [user_id]
        idx = 2
        for key in ("league", "sportsbook", "bet_type"):
            if kwargs.get(key):
                conditions.append(f"{key} = ${idx}")
                params.append(kwargs[key])
                idx += 1
        if kwargs.get("since"):
            conditions.append(f"time_placed >= ${idx}")
            params.append(_to_dt(kwargs["since"]))
            idx += 1
        where = f"WHERE {' AND '.join(conditions)}"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""SELECT COUNT(*) as total_bets,
                    SUM(CASE WHEN status='SETTLED_WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN status='SETTLED_LOSS' THEN 1 ELSE 0 END) as losses,
                    SUM(stake) as total_wagered, SUM(profit) as net_profit,
                    AVG(odds) as avg_odds, AVG(stake) as avg_stake,
                    MIN(time_placed) as first_bet, MAX(time_placed) as last_bet
                FROM bets {where}""",
                *params,
            )
            d = dict(row)
            wag = d.get("total_wagered") or 0
            d["roi_pct"] = round((d["net_profit"] / wag) * 100, 2) if wag > 0 else 0.0
            return d

    async def get_breakdown(
        self, user_id: str, group_by: str = "league", since: Optional[str] = None
    ) -> list[dict]:
        valid = {"league", "sportsbook", "bet_type", "sport"}
        if group_by not in valid:
            raise ValueError(f"group_by must be one of {valid}")
        conditions = ["user_id = $1", "status IN ('SETTLED_WIN', 'SETTLED_LOSS')"]
        params = [user_id]
        if since:
            conditions.append("time_placed >= $2")
            params.append(_to_dt(since))
        where = f"WHERE {' AND '.join(conditions)}"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT {group_by}, COUNT(*) as total_bets,
                    SUM(CASE WHEN status='SETTLED_WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(stake) as total_wagered, SUM(profit) as net_profit
                FROM bets {where} GROUP BY {group_by} ORDER BY net_profit DESC""",
                *params,
            )
        results = []
        for row in rows:
            d = dict(row)
            d["losses"] = d["total_bets"] - d["wins"]
            d["win_pct"] = round(d["wins"] / d["total_bets"] * 100, 1) if d["total_bets"] else 0
            d["roi_pct"] = round(d["net_profit"] / d["total_wagered"] * 100, 2) if d["total_wagered"] else 0
            results.append(d)
        return results

    async def get_calendar_data(
        self, user_id: str, since: Optional[str] = None, until: Optional[str] = None
    ) -> list[dict]:
        conditions = ["user_id = $1", "status IN ('SETTLED_WIN', 'SETTLED_LOSS')"]
        params = [user_id]
        idx = 2
        if since:
            conditions.append(f"time_placed >= ${idx}")
            params.append(_to_dt(since))
            idx += 1
        if until:
            conditions.append(f"time_placed <= ${idx}")
            params.append(_to_dt(until))
            idx += 1
        where = f"WHERE {' AND '.join(conditions)}"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT date(time_placed) as day, COUNT(*) as total_bets,
                    SUM(CASE WHEN status='SETTLED_WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(stake) as wagered, SUM(profit) as net_profit
                FROM bets {where} GROUP BY date(time_placed) ORDER BY day""",
                *params,
            )
        return [dict(r) for r in rows]

    async def get_sync_state(self, user_id: str, key: str) -> Optional[str]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM sync_state WHERE user_id = $1 AND key = $2",
                user_id, key,
            )
            return row["value"] if row else None

    async def set_sync_state(self, user_id: str, key: str, value: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO sync_state (user_id, key, value, updated_at) "
                "VALUES ($1, $2, $3, NOW()) "
                "ON CONFLICT(user_id, key) DO UPDATE SET "
                "value=EXCLUDED.value, updated_at=NOW()",
                user_id, key, value,
            )

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
        from datetime import date as _date
        inserted = 0
        async with self._pool.acquire() as conn:
            for r in rows:
                status = await conn.execute(
                    """INSERT INTO model_picks (
                        screen, pick_date, batter_id, batter, team, pitcher_id,
                        opposing_pitcher, venue, score, rank, tags, metrics, source
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                    ON CONFLICT (screen, pick_date, batter_id) DO NOTHING""",
                    r["screen"], _date.fromisoformat(r["pick_date"]), r["batter_id"],
                    r.get("batter"), r.get("team"), r.get("pitcher_id"),
                    r.get("opposing_pitcher"), r.get("venue"), r.get("score"),
                    r.get("rank"), r.get("tags"), r.get("metrics"),
                    r.get("source", "live"),
                )
                # asyncpg returns e.g. "INSERT 0 1"; 0 rows means conflict-skipped
                inserted += int(status.rsplit(" ", 1)[-1])
        return inserted

    async def list_picks(
        self,
        screen: str,
        since: Optional[str] = None,
        until: Optional[str] = None,
        unresolved_only: bool = False,
    ) -> list[dict]:
        from datetime import date as _date
        conditions = ["screen = $1"]
        params: list = [screen]
        idx = 2
        if since:
            conditions.append(f"pick_date >= ${idx}")
            params.append(_date.fromisoformat(since))
            idx += 1
        if until:
            conditions.append(f"pick_date <= ${idx}")
            params.append(_date.fromisoformat(until))
            idx += 1
        if unresolved_only:
            conditions.append("result IS NULL")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM model_picks WHERE {' AND '.join(conditions)} "
                "ORDER BY pick_date DESC, rank ASC",
                *params,
            )
        out = []
        for row in rows:
            d = dict(row)
            d["pick_date"] = d["pick_date"].isoformat()
            out.append(d)
        return out

    async def update_pick_results(
        self, screen: str, pick_date: str, results: list[dict]
    ) -> int:
        from datetime import date as _date
        updated = 0
        pd_date = _date.fromisoformat(pick_date)
        async with self._pool.acquire() as conn:
            for r in results:
                status = await conn.execute(
                    "UPDATE model_picks SET result = $1, hr_actual = $2, "
                    "hits_actual = $3, pa_actual = $4, resolved_at = NOW() "
                    "WHERE screen = $5 AND pick_date = $6 AND batter_id = $7",
                    r["result"], r.get("hr_actual"), r.get("hits_actual"),
                    r.get("pa_actual"), screen, pd_date, r["batter_id"],
                )
                updated += int(status.rsplit(" ", 1)[-1])
        return updated

    def _renumber(self, user_id: str, conditions: list[str], params: list) -> tuple[list[str], list]:
        """Take filter conditions that use $1.. numbering and renumber them to
        start at $2 (since $1 is reserved for user_id). Returns the full
        condition list (with user_id first) and the full param list."""
        all_params = [user_id, *params]
        # Each condition contains exactly one placeholder of the form $N.
        # Bump N by 1 across the board.
        renumbered = []
        for i, cond in enumerate(conditions):
            renumbered.append(cond.replace(f"${i+1}", f"${i+2}"))
        return ["user_id = $1", *renumbered], all_params

    def _build_filters(self, **kwargs) -> tuple[list[str], list]:
        conditions, params = [], []
        idx = 1
        for key in ("status", "league", "sportsbook", "bet_type", "sport"):
            if kwargs.get(key):
                conditions.append(f"{key} = ${idx}")
                params.append(kwargs[key])
                idx += 1
        if kwargs.get("since"):
            conditions.append(f"time_placed >= ${idx}")
            params.append(_to_dt(kwargs["since"]))
            idx += 1
        if kwargs.get("until"):
            conditions.append(f"time_placed <= ${idx}")
            params.append(_to_dt(kwargs["until"]))
            idx += 1
        return conditions, params
