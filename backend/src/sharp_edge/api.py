"""FastAPI application — REST API for the Sharp Edge frontend."""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .db import create_database, BetDatabase
from .fanduel.auth import (
    FanDuelAuth,
    FanDuelBotBlocked,
    FanDuelMFARequired,
)
from .fanduel.client import FanDuelClient
from .analysis import score_bet, generate_insights
from .chat import chat as chat_with_claude

logger = logging.getLogger(__name__)

# Per-user FanDuel auth, keyed by session uid. Replaces the old singleton so
# different visitors can each log in to their own FanDuel account. The in-
# memory map is a cache over the DB-persisted session (sync_state), so a pod
# restart rehydrates the session (and its refresh token) instead of forcing
# a fresh login.
_db: Optional[BetDatabase] = None
_fd_auth: dict[str, FanDuelAuth] = {}
_FD_SESSION_KEY = "fanduel_session"


async def _persist_fd_auth(uid: str, auth: FanDuelAuth) -> None:
    """Save a user's FanDuel session (token + refresh token, no password)."""
    import json
    try:
        await _db.set_sync_state(uid, _FD_SESSION_KEY, json.dumps(auth.to_state()))
    except Exception as e:
        logger.warning("failed to persist FanDuel session: %s", e)


async def _load_fd_auth(uid: str) -> Optional[FanDuelAuth]:
    """Return the user's FanDuel auth, rehydrating from the DB if the in-
    memory cache was lost to a restart."""
    auth = _fd_auth.get(uid)
    if auth is not None:
        return auth
    import json
    try:
        raw = await _db.get_sync_state(uid, _FD_SESSION_KEY)
    except Exception:
        raw = None
    if not raw:
        return None
    try:
        auth = FanDuelAuth.from_state(
            json.loads(raw), basic_auth=settings.fanduel_basic_auth
        )
        auth.state = auth.state or settings.fanduel_state
    except Exception as e:
        logger.warning("failed to rehydrate FanDuel session: %s", e)
        return None
    _fd_auth[uid] = auth
    return auth


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db
    _db = await create_database(settings.database_url)

    # Wire pick tracking (persistence + outcome resolution) to the db and
    # event loop so the screens' warm-up threads can record their picks.
    try:
        from . import tracking
        tracking.configure(_db, asyncio.get_running_loop())
        # Build the season's pick history without anyone having to press a
        # button: once the Statcast cache is warm, generate + settle any day
        # that isn't recorded yet. Already-recorded days are skipped, so a
        # restart costs one query.
        tracking.schedule_catchup()
        logger.info("tracking: configured, catch-up scheduled")
    except ImportError:
        logger.info("tracking: models extras not installed, skipping")
    except Exception as e:
        logger.warning("tracking: configure failed: %s", e)

    # Kick off the batter-screen scrape in the background so the first browser
    # request after a pod restart doesn't have to wait several minutes. If the
    # models extras aren't installed (lighter prod image, dev sandbox, etc.)
    # we skip it silently — the endpoint will surface the import error itself.
    try:
        from .batters import warm_async as warm_batters
        warm_batters()
        logger.info("batters: background warm-up scheduled")
    except ImportError:
        logger.info("batters: models extras not installed, skipping prewarm")
    except Exception as e:
        logger.warning("batters: prewarm failed to schedule: %s", e)

    try:
        from .homers import warm_async as warm_homers
        warm_homers()
        logger.info("homers: background warm-up scheduled")
    except ImportError:
        logger.info("homers: models extras not installed, skipping prewarm")
    except Exception as e:
        logger.warning("homers: prewarm failed to schedule: %s", e)

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
    # Reuse the stored installation id when there is one: FanDuel keys device
    # verification to it, so a new id means a new MFA code every login.
    prior = await _load_fd_auth(uid)
    auth = FanDuelAuth(
        req.email, req.password,
        basic_auth=settings.fanduel_basic_auth,
        state=settings.fanduel_state,
        installation_id=prior.installation_id if prior else None,
    )
    try:
        await auth.login()
        _fd_auth[uid] = auth
        await _persist_fd_auth(uid, auth)
        return {
            "status": "ok",
            "expires_in": int(auth._token_exp - __import__("time").time()),
        }
    except FanDuelMFARequired as e:
        # Keep the credentials so /auth/mfa can finish the login.
        _fd_auth[uid] = auth
        return {"status": "mfa_required", "message": str(e)}
    except FanDuelBotBlocked as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))


class MFARequest(BaseModel):
    code: str


@app.post("/auth/mfa")
async def submit_mfa(req: MFARequest, uid: str = Depends(get_uid)):
    """Finish a login that FanDuel held for new-device verification."""
    auth = _fd_auth.get(uid)
    if not auth or not auth.mfa_pending:
        raise HTTPException(400, "No pending login — start with /auth/login")
    try:
        await auth.submit_mfa_code(req.code)
        await _persist_fd_auth(uid, auth)
        return {
            "status": "ok",
            "expires_in": int(auth._token_exp - __import__("time").time()),
        }
    except FanDuelBotBlocked as e:
        raise HTTPException(status_code=502, detail=str(e))
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
    auth = await _load_fd_auth(uid)
    if not auth or not auth.token:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "expired": auth.is_expired,
        # A stale token renews silently on the next sync when we hold a
        # refresh token or stored credentials, so "expired" is only terminal
        # for a bare manual-token session.
        "can_renew": auth.can_renew,
    }


@app.post("/auth/logout")
async def logout(request: Request, uid: str = Depends(get_uid)):
    """Clear FanDuel auth for this session and rotate the session id."""
    _fd_auth.pop(uid, None)
    try:
        await _db.set_sync_state(uid, _FD_SESSION_KEY, "")
    except Exception:
        pass
    request.session.clear()
    return {"status": "ok"}


# ------------------------------------------------------------------
# Bets
# ------------------------------------------------------------------

@app.post("/bets/sync")
async def sync_bets(
    uid: str = Depends(get_uid), db: BetDatabase = Depends(get_db)
):
    auth = await _load_fd_auth(uid)
    if not auth or not auth.token:
        raise HTTPException(400, "Not authenticated with FanDuel")

    try:
        token = await auth.ensure_token()
    except Exception as e:
        raise HTTPException(401, str(e))
    # Persist any renewed token/refresh token so the next restart reuses it.
    await _persist_fd_auth(uid, auth)
    fd = FanDuelClient(auth_token=token, state=settings.fanduel_state, auth=auth)
    try:
        raw_bets = await fd.fetch_all_settled_bets()
        count = 0
        for raw in raw_bets:
            norm = fd.normalize_bet(raw)
            await db.upsert_bet(uid, norm)
            count += 1
        return {"status": "ok", "bets_synced": count}
    finally:
        await _persist_fd_auth(uid, auth)  # client may have refreshed on a 401
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

    status = warm_status()
    if status.get("stale"):
        # Serving fallback data — kick a non-blocking refresh (cooldown-guarded)
        # so the screen self-heals once upstream recovers.
        warm_async()
    hot = cached.hot_bats.rename(columns={"Name": "batter", "Tm": "team"})
    return {
        "picks": _df_to_records(cached.picks),
        "hot_bats": _df_to_records(hot),
        "today": _df_to_records(cached.today),
        "as_of": status["cached_date"],
        "stale": bool(status.get("stale")),
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
# Homers — MLB home-run probability screen (not user-scoped — public data)
# ------------------------------------------------------------------

@app.get("/homers/screen")
async def homer_screen():
    """Today's MLB HR probability screen: picks, hot-pop, and full board.

    Backed by the same per-day cache + background warm-up pattern as
    /batters/screen — reuses the shared Statcast cache, so once that's warm
    the HR screen is just a few more API calls on top.
    """
    try:
        from .homers import get_cached, warm_async, warm_status
    except ImportError as e:
        raise HTTPException(
            500,
            f"models extras not installed (pip install -r requirements.txt): {e}",
        )

    cached = get_cached()
    if cached is None:
        state = warm_async()
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

    status = warm_status()
    if status.get("stale"):
        # Serving fallback data — kick a non-blocking refresh (cooldown-guarded)
        # so the screen self-heals once upstream recovers.
        warm_async()
    return {
        "picks": _df_to_records(cached.picks),
        "hot_pop": _df_to_records(cached.hot_pop),
        "today": _df_to_records(cached.today),
        "as_of": status["cached_date"],
        "stale": bool(status.get("stale")),
    }


@app.get("/homers/screen/status")
async def homer_screen_status():
    try:
        from .homers import warm_status
    except ImportError:
        return {"available": False}
    return {"available": True, **warm_status()}


# ------------------------------------------------------------------
# Picks tracking — persisted screen picks vs actual outcomes (public)
# ------------------------------------------------------------------

def _get_tracking():
    try:
        from . import tracking
        return tracking
    except ImportError as e:
        raise HTTPException(
            500,
            f"models extras not installed (pip install -r requirements.txt): {e}",
        )


@app.get("/picks/track-record")
async def picks_track_record(
    screen: str = "hr",
    since: Optional[str] = None,
    db: BetDatabase = Depends(get_db),
):
    """Hit-rate summary for a screen's persisted picks: overall, per edge
    tag, per source (live vs backfill), per day, plus the pick list itself
    with WIN / LOSS / VOID outcomes."""
    tracking = _get_tracking()
    if screen not in tracking.SCREENS:
        raise HTTPException(400, f"screen must be one of {tracking.SCREENS}")
    rows = await db.list_picks(screen=screen, since=since)
    return tracking.build_track_record(screen, rows)


@app.post("/picks/resolve")
async def picks_resolve():
    """Settle any unresolved picks from before today against actual results.
    Also runs automatically after each daily screen warm-up."""
    tracking = _get_tracking()
    return await asyncio.to_thread(tracking.resolve_pending)


class BackfillRequest(BaseModel):
    start: str
    end: Optional[str] = None
    screens: list[str] = ["hr", "batter"]


@app.post("/picks/backfill")
async def picks_backfill(req: BackfillRequest):
    """Retroactively generate and settle picks for a past date range using
    as-of stats. Runs in a background thread; poll /picks/backfill/status."""
    tracking = _get_tracking()
    try:
        start = date.fromisoformat(req.start)
        end = date.fromisoformat(req.end) if req.end else date.today() - timedelta(days=1)
    except ValueError as e:
        raise HTTPException(400, f"bad date: {e}")
    end = min(end, date.today() - timedelta(days=1))
    if start > end:
        raise HTTPException(400, "start must be on or before end (and before today)")
    bad = [s for s in req.screens if s not in tracking.SCREENS]
    if bad:
        raise HTTPException(400, f"unknown screens: {bad}")
    return tracking.start_backfill(start, end, req.screens)


@app.get("/picks/backfill/status")
async def picks_backfill_status():
    tracking = _get_tracking()
    return tracking.backfill_status()


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
