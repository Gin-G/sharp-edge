"""FastAPI application — REST API for the Sharp Edge frontend."""

import logging
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import settings
from .db import create_database, BetDatabase
from .fanduel.auth import FanDuelAuth
from .fanduel.client import FanDuelClient
from .analysis import score_bet, generate_insights
from .chat import chat as chat_with_claude

logger = logging.getLogger(__name__)

# Singletons
_db: Optional[BetDatabase] = None
_fd_auth: Optional[FanDuelAuth] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db, _fd_auth
    _db = await create_database(settings.database_url)
    if settings.fanduel_email:
        _fd_auth = FanDuelAuth(settings.fanduel_email, settings.fanduel_password)
    yield
    await _db.close()


app = FastAPI(title="Sharp Edge", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db() -> BetDatabase:
    return _db


# ------------------------------------------------------------------
# Auth
# ------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: str
    password: str


class ManualTokenRequest(BaseModel):
    token: str


@app.post("/auth/login")
async def login(req: LoginRequest):
    global _fd_auth
    _fd_auth = FanDuelAuth(req.email, req.password)
    try:
        token = await _fd_auth.login()
        return {"status": "ok", "expires_in": int(_fd_auth._token_exp - __import__("time").time())}
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/auth/token")
async def set_manual_token(req: ManualTokenRequest):
    """Set a manually-captured JWT from browser DevTools."""
    global _fd_auth
    if not _fd_auth:
        _fd_auth = FanDuelAuth("", "")
    _fd_auth.set_manual_token(req.token)
    return {"status": "ok", "expires_in": int(_fd_auth._token_exp - __import__("time").time())}


@app.get("/auth/status")
async def auth_status():
    if not _fd_auth or not _fd_auth.token:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "expired": _fd_auth.is_expired,
    }


# ------------------------------------------------------------------
# Bets
# ------------------------------------------------------------------

@app.post("/bets/sync")
async def sync_bets(db: BetDatabase = Depends(get_db)):
    if not _fd_auth or not _fd_auth.token:
        raise HTTPException(400, "Not authenticated with FanDuel")

    token = await _fd_auth.ensure_token()
    fd = FanDuelClient(auth_token=token, state=settings.fanduel_state)
    try:
        raw_bets = await fd.fetch_all_settled_bets()
        count = 0
        for raw in raw_bets:
            norm = fd.normalize_bet(raw)
            await db.upsert_bet(norm)
            count += 1
        return {"status": "ok", "bets_synced": count}
    finally:
        await fd.close()


class ImportCSVRequest(BaseModel):
    csv_path: str


@app.post("/bets/import")
async def import_csv(req: ImportCSVRequest, db: BetDatabase = Depends(get_db)):
    from pathlib import Path
    if not Path(req.csv_path).exists():
        raise HTTPException(404, f"File not found: {req.csv_path}")
    count = await db.import_pikkit_csv(req.csv_path)
    return {"status": "ok", "bets_imported": count}


class BetQuery(BaseModel):
    league: Optional[str] = None
    sportsbook: Optional[str] = None
    bet_type: Optional[str] = None
    status: Optional[str] = None
    sport: Optional[str] = None
    since: Optional[str] = None
    until: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


@app.post("/bets/history")
async def get_history(q: BetQuery, db: BetDatabase = Depends(get_db)):
    bets = await db.query_bets(**q.model_dump(exclude_none=True))
    return {"count": len(bets), "bets": bets}


@app.get("/bets/stats")
async def get_stats(
    league: Optional[str] = None,
    sportsbook: Optional[str] = None,
    bet_type: Optional[str] = None,
    since: Optional[str] = None,
    db: BetDatabase = Depends(get_db),
):
    return await db.get_summary_stats(
        league=league, sportsbook=sportsbook, bet_type=bet_type, since=since
    )


@app.get("/bets/breakdown/{group_by}")
async def get_breakdown(
    group_by: str, since: Optional[str] = None, db: BetDatabase = Depends(get_db)
):
    try:
        return await db.get_breakdown(group_by=group_by, since=since)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/bets/calendar")
async def get_calendar(
    since: Optional[str] = None, until: Optional[str] = None,
    db: BetDatabase = Depends(get_db),
):
    return await db.get_calendar_data(since=since, until=until)


class ScoreBetRequest(BaseModel):
    league: str
    bet_type: str = "straight"
    market: Optional[str] = None
    odds: float = Field(gt=1.0)
    stake: float = Field(default=1.25, gt=0)
    leg_count: int = Field(default=1, ge=1)
    description: Optional[str] = None


@app.post("/bets/score")
async def score_proposed_bet(req: ScoreBetRequest, db: BetDatabase = Depends(get_db)):
    history = await db.query_bets(limit=5000)
    return score_bet(req.model_dump(), history)


@app.get("/bets/insights")
async def get_insights(
    since: Optional[str] = None, league: Optional[str] = None,
    db: BetDatabase = Depends(get_db),
):
    history = await db.query_bets(league=league, since=since, limit=5000)
    return {"insights": generate_insights(history)}


# ------------------------------------------------------------------
# Chat
# ------------------------------------------------------------------

class ChatRequest(BaseModel):
    messages: list[dict]
    model: str = "claude-sonnet-4-20250514"


@app.post("/chat")
async def chat_endpoint(req: ChatRequest, db: BetDatabase = Depends(get_db)):
    if not settings.anthropic_api_key:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured")
    result = await chat_with_claude(
        messages=req.messages,
        db=db,
        api_key=settings.anthropic_api_key,
        model=req.model,
    )
    return result


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


def main():
    uvicorn.run(
        "sharp_edge.api:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        reload=True,
    )


if __name__ == "__main__":
    main()
