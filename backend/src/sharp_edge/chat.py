"""Claude chat integration — proxies to Anthropic API with betting tools.

The chat panel in the frontend sends messages here. We forward them to Claude
with tool definitions that map to our backend functions, then stream the
response back. Claude can query bet history, run analysis, score bets, etc.
"""

import json
import logging

from anthropic import AsyncAnthropic

from .db.base import BetDatabase
from .analysis import score_bet, generate_insights

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Sharp Edge, a sports betting analyst assistant. You have access to the user's complete betting history and analytical tools. Your job is to help them improve their ROI through data-driven analysis.

Key context about this user:
- Primary sportsbook: FanDuel
- Strongest markets: MLB player props (total bases, hits + moneyline parlays), 3-4 leg parlays
- Known leaks: NFL yardage props, totals/O/U, 6+ leg parlays, NCAAFB
- MLB season is active — they do batter-vs-pitcher research
- NBA: Celtics fan, runs Celtics ML + Brown/Tatum PRA parlays (break-even)

Be direct and analytical. When they ask about a potential bet, use the score_bet tool to check their historical edge. Don't sugarcoat — if the data says avoid, say avoid."""

TOOLS = [
    {
        "name": "query_bets",
        "description": "Query the user's bet history with filters. Returns matching bets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "league": {"type": "string", "description": "Filter: MLB, NFL, NBA, NHL, NCAAM"},
                "bet_type": {"type": "string", "description": "Filter: straight or parlay"},
                "status": {"type": "string", "description": "Filter: SETTLED_WIN or SETTLED_LOSS"},
                "since": {"type": "string", "description": "ISO date, e.g. '2025-01-01'"},
                "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
            },
        },
    },
    {
        "name": "get_stats",
        "description": "Get aggregate stats: total bets, win rate, P/L, ROI. Supports filters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "league": {"type": "string"},
                "sportsbook": {"type": "string"},
                "bet_type": {"type": "string"},
                "since": {"type": "string"},
            },
        },
    },
    {
        "name": "get_breakdown",
        "description": "P/L breakdown grouped by a dimension (league, sportsbook, bet_type, sport).",
        "input_schema": {
            "type": "object",
            "properties": {
                "group_by": {"type": "string", "enum": ["league", "sportsbook", "bet_type", "sport"]},
                "since": {"type": "string"},
            },
            "required": ["group_by"],
        },
    },
    {
        "name": "score_bet",
        "description": "Score a proposed bet against historical patterns. Returns edge/leak assessment per dimension.",
        "input_schema": {
            "type": "object",
            "properties": {
                "league": {"type": "string", "description": "MLB, NFL, NBA, etc."},
                "bet_type": {"type": "string", "default": "straight"},
                "market": {"type": "string", "description": "moneyline, total_bases, hits, home_run, anytime_td, pra_combo, yardage_props, spread, totals"},
                "odds": {"type": "number", "description": "Decimal odds"},
                "leg_count": {"type": "integer", "default": 1},
                "description": {"type": "string", "description": "Free text bet description"},
            },
            "required": ["league", "odds"],
        },
    },
    {
        "name": "get_insights",
        "description": "Generate actionable insights from betting history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "since": {"type": "string"},
                "league": {"type": "string"},
            },
        },
    },
]


async def handle_tool_call(
    tool_name: str, tool_input: dict, db: BetDatabase, user_id: str
) -> str:
    """Execute a tool call and return the result as a string."""
    if tool_name == "query_bets":
        bets = await db.query_bets(user_id, **tool_input)
        return json.dumps({"count": len(bets), "bets": bets}, default=str)

    elif tool_name == "get_stats":
        stats = await db.get_summary_stats(user_id, **tool_input)
        return json.dumps(stats, default=str)

    elif tool_name == "get_breakdown":
        breakdown = await db.get_breakdown(user_id, **tool_input)
        return json.dumps(breakdown, default=str)

    elif tool_name == "score_bet":
        history = await db.query_bets(user_id, limit=5000)
        result = score_bet(tool_input, history)
        return json.dumps(result, default=str)

    elif tool_name == "get_insights":
        history = await db.query_bets(
            user_id,
            league=tool_input.get("league"),
            since=tool_input.get("since"),
            limit=5000,
        )
        insights = generate_insights(history)
        return json.dumps({"insights": insights})

    return json.dumps({"error": f"Unknown tool: {tool_name}"})


async def chat(
    messages: list[dict],
    db: BetDatabase,
    api_key: str,
    user_id: str,
    model: str = "claude-sonnet-4-20250514",
) -> dict:
    """Send a chat message to Claude with betting tools, scoped to user_id."""
    client = AsyncAnthropic(api_key=api_key)

    response = await client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=messages,
    )

    while response.stop_reason == "tool_use":
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = await handle_tool_call(block.name, block.input, db, user_id)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        response = await client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

    text = "".join(block.text for block in response.content if hasattr(block, "text"))
    messages.append({"role": "assistant", "content": response.content})

    return {"response": text, "messages": messages}
