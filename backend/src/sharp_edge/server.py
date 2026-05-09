"""MCP server — same tools as the API, exposed via MCP for Claude Desktop/Code."""

import json
import os
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP, Context
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional

from .config import settings
from .db import create_database
from .fanduel.client import FanDuelClient
from .analysis import score_bet, generate_insights


@asynccontextmanager
async def app_lifespan(server):
    db = await create_database(settings.database_url)
    yield {"db": db}
    await db.close()


mcp = FastMCP("sharp_edge_mcp", lifespan=app_lifespan)


def _get_db(ctx):
    return ctx.request_context.lifespan_context["db"]


# --- Tools (same logic as API endpoints) ---

class StatsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    league: Optional[str] = Field(default=None)
    sportsbook: Optional[str] = Field(default=None)
    bet_type: Optional[str] = Field(default=None)
    since: Optional[str] = Field(default=None)


@mcp.tool(name="se_stats", annotations={"readOnlyHint": True})
async def get_stats(params: StatsInput, ctx: Context) -> str:
    """Get aggregate betting stats: win rate, P/L, ROI with optional filters."""
    db = _get_db(ctx)
    stats = await db.get_summary_stats(
        settings.mcp_user_id, **params.model_dump(exclude_none=True)
    )
    return json.dumps(stats, default=str)


class BreakdownInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_by: str = Field(default="league")
    since: Optional[str] = Field(default=None)


@mcp.tool(name="se_breakdown", annotations={"readOnlyHint": True})
async def get_breakdown(params: BreakdownInput, ctx: Context) -> str:
    """P/L breakdown grouped by league, sportsbook, bet_type, or sport."""
    db = _get_db(ctx)
    result = await db.get_breakdown(
        settings.mcp_user_id, group_by=params.group_by, since=params.since
    )
    return json.dumps(result, default=str)


class ScoreInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    league: str = Field(...)
    bet_type: str = Field(default="straight")
    market: Optional[str] = Field(default=None)
    odds: float = Field(..., gt=1.0)
    leg_count: int = Field(default=1, ge=1)
    description: Optional[str] = Field(default=None)


@mcp.tool(name="se_score_bet", annotations={"readOnlyHint": True})
async def score_proposed_bet(params: ScoreInput, ctx: Context) -> str:
    """Score a proposed bet against your historical patterns."""
    db = _get_db(ctx)
    history = await db.query_bets(settings.mcp_user_id, limit=5000)
    result = score_bet(params.model_dump(), history)
    return json.dumps(result, default=str)


class HistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    league: Optional[str] = Field(default=None)
    bet_type: Optional[str] = Field(default=None)
    status: Optional[str] = Field(default=None)
    since: Optional[str] = Field(default=None)
    limit: int = Field(default=50, ge=1, le=500)


@mcp.tool(name="se_history", annotations={"readOnlyHint": True})
async def query_history(params: HistoryInput, ctx: Context) -> str:
    """Query stored bet history with optional filters."""
    db = _get_db(ctx)
    bets = await db.query_bets(
        settings.mcp_user_id, **params.model_dump(exclude_none=True)
    )
    return json.dumps({"count": len(bets), "bets": bets}, default=str)


@mcp.tool(name="se_insights", annotations={"readOnlyHint": True})
async def get_insights(ctx: Context) -> str:
    """Generate actionable insights from your complete betting history."""
    db = _get_db(ctx)
    history = await db.query_bets(settings.mcp_user_id, limit=5000)
    return json.dumps({"insights": generate_insights(history)})


class SyncInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_pages: int = Field(default=50, ge=0, le=200)


@mcp.tool(name="se_sync_fanduel", annotations={"readOnlyHint": False, "openWorldHint": True})
async def sync_fanduel(params: SyncInput, ctx: Context) -> str:
    """Pull latest bet history from FanDuel API."""
    token = os.environ.get("FANDUEL_AUTH_TOKEN")
    if not token:
        return json.dumps({"error": "FANDUEL_AUTH_TOKEN not set"})
    db = _get_db(ctx)
    fd = FanDuelClient(auth_token=token, state=settings.fanduel_state)
    try:
        raw_bets = await fd.fetch_all_settled_bets(max_pages=params.max_pages or 500)
        count = 0
        for raw in raw_bets:
            await db.upsert_bet(settings.mcp_user_id, fd.normalize_bet(raw))
            count += 1
        return json.dumps({"status": "ok", "bets_synced": count})
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        await fd.close()


class ImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    csv_path: str = Field(...)


@mcp.tool(name="se_import_csv", annotations={"readOnlyHint": False})
async def import_csv(params: ImportInput, ctx: Context) -> str:
    """Import bet history from a Pikkit CSV export."""
    db = _get_db(ctx)
    count = await db.import_pikkit_csv(settings.mcp_user_id, params.csv_path)
    return json.dumps({"status": "ok", "bets_imported": count})


def main():
    mcp.run()
