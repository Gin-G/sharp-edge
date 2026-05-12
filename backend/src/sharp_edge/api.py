"""FastAPI application — REST API for the Sharp Edge frontend."""

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .db import create_database, BetDatabase
from .fanduel.auth import FanDuelAuth
from .fanduel.client import FanDuelClient
from .analysis import score_bet, generate_insights
from .chat import chat as chat_with_claude

logger = logging.getLogger(__name__)

# Per-user FanDuel auth, keyed by session uid. Replaces the old singleton so
# different visitors can each log in to their own FanDuel account.
_db: Optional[BetDatabase] = None
_fd_auth: dict[str, FanDuelAuth] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db
    _db = await create_database(settings.database_url)

    # Kick off the batter-screen scrape in the background so the first browser
    # request after a pod restart doesn't have to wait several minutes. If the
    # models extras aren't installed (lighter prod image, dev sandbox, etc.)
    # we skip it silently — the endpoint will surface the import error itself.
    try:
        from .batters import warm_async
        warm_async()
        logger.info("batters: background warm-up scheduled")
    except ImportError:
        logger.info("batters: models extras not installed, skipping prewarm")
    except Exception as e:
        logger.warning("batters: prewarm failed to schedule: %s", e)

    yield
    await _db.close()


app = FastAPI(title="Sharp Edge", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie=settings.session_cookie_name,
    max_age=settings.session_max_age,
    same_site="lax",
    https_only=False,  # set True behind TLS in prod via env override
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db() -> BetDatabase:
    return _db


def get_uid(request: Request) -> str:
    """Pull the visitor's uid from the signed session cookie, minting one on
    first contact. Every per-user endpoint depends on this."""
    uid = request.session.get("uid")
    if not uid:
        uid = uuid.uuid4().hex
        request.session["uid"] = uid
    return uid


# ------------------------------------------------------------------
# Auth (FanDuel — per-user)
# ------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: str
    password: str


class ManualTokenRequest(BaseModel):
    token: str


@app.post("/auth/login")
async def login(req: LoginRequest, uid: str = Depends(get_uid)):
    auth = FanDuelAuth(req.email, req.password)
    try:
        await auth.login()
        _fd_auth[uid] = auth
        return {
            "status": "ok",
            "expires_in": int(auth._token_exp - __import__("time").time()),
        }
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/auth/token")
async def set_manual_token(req: ManualTokenRequest, uid: str = Depends(get_uid)):
    """Set a manually-captured JWT from browser DevTools."""
    auth = _fd_auth.get(uid)
    if not auth:
        auth = FanDuelAuth("", "")
        _fd_auth[uid] = auth
    auth.set_manual_token(req.token)
    return {
        "status": "ok",
        "expires_in": int(auth._token_exp - __import__("time").time()),
    }


@app.get("/auth/status")
async def auth_status(uid: str = Depends(get_uid)):
    auth = _fd_auth.get(uid)
    if not auth or not auth.token:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "expired": auth.is_expired,
    }


@app.post("/auth/logout")
async def logout(request: Request, uid: str = Depends(get_uid)):
    """Clear FanDuel auth for this session and rotate the session id."""
    _fd_auth.pop(uid, None)
    request.session.clear()
    return {"status": "ok"}


# ------------------------------------------------------------------
# Bets
# ------------------------------------------------------------------

@app.post("/bets/sync")
async def sync_bets(
    uid: str = Depends(get_uid), db: BetDatabase = Depends(get_db)
):
    auth = _fd_auth.get(uid)
    if not auth or not auth.token:
        raise HTTPException(400, "Not authenticated with FanDuel")

    token = await auth.ensure_token()
    fd = FanDuelClient(auth_token=token, state=settings.fanduel_state)
    try:
        raw_bets = await fd.fetch_all_settled_bets()
        count = 0
        for raw in raw_bets:
            norm = fd.normalize_bet(raw)
            await db.upsert_bet(uid, norm)
            count += 1
        return {"status": "ok", "bets_synced": count}
    finally:
        await fd.close()


class ImportCSVRequest(BaseModel):
    csv_path: str


@app.post("/bets/import")
async def import_csv(
    req: ImportCSVRequest,
    uid: str = Depends(get_uid),
    db: BetDatabase = Depends(get_db),
):
    from pathlib import Path
    if not Path(req.csv_path).exists():
        raise HTTPException(404, f"File not found: {req.csv_path}")
    count = await db.import_pikkit_csv(uid, req.csv_path)
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
async def get_history(
    q: BetQuery,
    uid: str = Depends(get_uid),
    db: BetDatabase = Depends(get_db),
):
    bets = await db.query_bets(uid, **q.model_dump(exclude_none=True))
    return {"count": len(bets), "bets": bets}


@app.get("/bets/stats")
async def get_stats(
    league: Optional[str] = None,
    sportsbook: Optional[str] = None,
    bet_type: Optional[str] = None,
    since: Optional[str] = None,
    uid: str = Depends(get_uid),
    db: BetDatabase = Depends(get_db),
):
    return await db.get_summary_stats(
        uid, league=league, sportsbook=sportsbook, bet_type=bet_type, since=since
    )


@app.get("/bets/breakdown/{group_by}")
async def get_breakdown(
    group_by: str,
    since: Optional[str] = None,
    uid: str = Depends(get_uid),
    db: BetDatabase = Depends(get_db),
):
    try:
        return await db.get_breakdown(uid, group_by=group_by, since=since)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/bets/calendar")
async def get_calendar(
    since: Optional[str] = None,
    until: Optional[str] = None,
    uid: str = Depends(get_uid),
    db: BetDatabase = Depends(get_db),
):
    return await db.get_calendar_data(uid, since=since, until=until)


class ScoreBetRequest(BaseModel):
    league: str
    bet_type: str = "straight"
    market: Optional[str] = None
    odds: float = Field(gt=1.0)
    stake: float = Field(default=1.25, gt=0)
    leg_count: int = Field(default=1, ge=1)
    description: Optional[str] = None


@app.post("/bets/score")
async def score_proposed_bet(
    req: ScoreBetRequest,
    uid: str = Depends(get_uid),
    db: BetDatabase = Depends(get_db),
):
    history = await db.query_bets(uid, limit=5000)
    return score_bet(req.model_dump(), history)


@app.get("/bets/insights")
async def get_insights(
    since: Optional[str] = None,
    league: Optional[str] = None,
    uid: str = Depends(get_uid),
    db: BetDatabase = Depends(get_db),
):
    history = await db.query_bets(uid, league=league, since=since, limit=5000)
    return {"insights": generate_insights(history)}


# ------------------------------------------------------------------
# Batters — MLB hot-bat / BvP screen (not user-scoped — public data)
# ------------------------------------------------------------------

def _df_to_records(df) -> list[dict]:
    """pandas DataFrame → JSON-safe records (NaN/NaT → None)."""
    import math
    records = df.to_dict(orient="records")
    for row in records:
        for k, v in row.items():
            if isinstance(v, float) and math.isnan(v):
                row[k] = None
    return records


@app.get("/batters/screen")
async def batter_screen():
    """Today's MLB batter screen: hot bats, today's matchups, picks.

    Backed by a per-day in-memory cache that's pre-warmed at pod startup.
    If the cache isn't ready yet (cold pod, scrape still running), returns
    503 with a Retry-After header so the frontend can poll. Once warm, every
    subsequent call is instant for the rest of the day.
    """
    try:
        from .batters import get_cached, warm_async, warm_status
    except ImportError as e:
        raise HTTPException(
            500,
            f"models extras not installed (pip install -r requirements.txt): {e}",
        )

    cached = get_cached()
    if cached is None:
        state = warm_async()  # triggers a scrape if one isn't already running
        status = warm_status()
        if status["last_error"]:
            raise HTTPException(500, f"warm-up failed: {status['last_error']}")
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "15"},
            content={
                "status": state["status"],
                "elapsed_seconds": status["elapsed_seconds"],
                "message": "Scraping today's MLB data — try again in a few seconds.",
            },
        )

    hot = cached.hot_bats.rename(columns={"Name": "batter", "Tm": "team"})
    return {
        "picks": _df_to_records(cached.picks),
        "hot_bats": _df_to_records(hot),
        "today": _df_to_records(cached.today),
        "as_of": warm_status()["cached_date"],
    }


@app.get("/batters/screen/status")
async def batter_screen_status():
    """Probe for the warm-up state — used by the frontend's polling loop."""
    try:
        from .batters import warm_status
    except ImportError:
        return {"available": False}
    return {"available": True, **warm_status()}


# ------------------------------------------------------------------
# Chat
# ------------------------------------------------------------------

class ChatRequest(BaseModel):
    messages: list[dict]
    model: str = "claude-sonnet-4-20250514"


@app.post("/chat")
async def chat_endpoint(
    req: ChatRequest,
    uid: str = Depends(get_uid),
    db: BetDatabase = Depends(get_db),
):
    if not settings.anthropic_api_key:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured")
    result = await chat_with_claude(
        messages=req.messages,
        db=db,
        api_key=settings.anthropic_api_key,
        user_id=uid,
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
