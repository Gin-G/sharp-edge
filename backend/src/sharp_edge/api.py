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
from .chat import chat as chat_with_claude, verify_key, DEFAULT_MODEL

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


def _session_payload(auth: FanDuelAuth) -> dict:
    """What the UI needs to describe a session honestly.

    expiry_assumed matters: without an exp claim the countdown is a 1h
    guess, so a confident "valid for 60 min" would be made up. can_refresh
    matters because the password is deliberately never persisted — after a
    pod restart a session with no refresh token cannot renew itself.
    """
    return {
        "status": "ok",
        "expires_in": auth.expires_in,
        "expiry_assumed": auth.expiry_assumed,
        "can_refresh": auth.can_refresh,
    }


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
        return _session_payload(auth)
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
        return _session_payload(auth)
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
    return _session_payload(auth)


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
        # can_renew is true whenever the password is still in memory, which
        # hides whether renewal survives a restart. can_refresh is the part
        # that does, since the password is never persisted.
        "can_refresh": auth.can_refresh,
        "expires_in": auth.expires_in,
        "expiry_assumed": auth.expiry_assumed,
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
    """Today's MLB batter board: hot bats, today's matchups, picks.

    ``picks`` is the board ranked by the probability the batter records a
    hit, one per game; ``bundle`` is the top two of that list as a parlay.
    The hot-bat and BvP tags still ride along on every row for display, but
    they no longer select anything.

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

    # Non-blocking, self-guarded: refreshes in the background only when the
    # cache is aged past its TTL (picks up mid-day probable/lineup changes) or
    # is serving degraded fallback data — otherwise a cheap no-op.
    warm_async()
    status = warm_status()
    hot = cached.hot_bats.rename(columns={"Name": "batter", "Tm": "team"})
    picks = _df_to_records(cached.picks)
    today = _df_to_records(cached.today)

    # Prices are the difference between "the screen is right two thirds of the
    # time" and "the screen makes money" — at -207 those are the same number.
    # Never let a FanDuel hiccup take the board down with it: no odds just
    # means the EV columns come back null.
    odds_meta = {"age_seconds": None, "error": "not attempted", "count": 0}
    try:
        from .fanduel.odds import cached_hit_odds
        from . import pricing

        got = await cached_hit_odds(state=settings.fanduel_state)
        pricing.enrich_records(picks, got["odds"])
        pricing.enrich_records(today, got["odds"])
        odds_meta = {
            "age_seconds": got["age_seconds"],
            "error": got["error"],
            "count": len(got["odds"]),
        }
    except Exception as e:
        logger.warning("odds enrichment failed: %s", e)
        odds_meta["error"] = str(e)

    # The day's bundle: the two most likely to record a hit, one leg per
    # game, plus every other leg that pays for the risk it adds — and a link
    # that loads it straight into the bet slip. The first two are chosen on
    # probability, which is the only place the model can separate the board;
    # everything past them is chosen on what it pays. No cap.
    from . import bundle as _bundle

    legs = _bundle.build(picks)

    # Freeze the day's card the first time it is built, then serve the frozen
    # one. Rebuilding live looks right and isn't: FanDuel pulls the market on
    # every game that starts, so a card re-derived in the afternoon is made of
    # whoever is left rather than what was recommended. The MIN_LEGS guard is
    # what stops a late first request freezing that residue as the day's card.
    # tracking's helpers are *sync* and reach the database through
    # tracking._run_db, which schedules onto this loop with
    # run_coroutine_threadsafe and blocks on the result. That is safe from a
    # worker thread, which is what its docstring says, and a deadlock from
    # here: the loop cannot run the coroutine it is blocked waiting for, so it
    # sits until the 300s timeout and every other request on the process waits
    # with it. Always cross to a thread first.
    from . import tracking as _tracking
    try:
        if len(legs) >= _bundle.MIN_LEGS:
            await asyncio.to_thread(
                _tracking.freeze_parlay,
                date.fromisoformat(status["cached_date"]), legs,
                _bundle.summarise(legs),
            )
        frozen = await asyncio.to_thread(
            _tracking.get_parlay, date.fromisoformat(status["cached_date"])
        )
        if frozen and frozen.get("legs"):
            open_now = {
                r.get("batter_id") for r in picks
                if r.get("fd_market_id") is not None
            }
            legs = [
                {**leg, "market_open": leg.get("batter_id") in open_now}
                for leg in frozen["legs"]
            ]
    except Exception as e:
        logger.warning("parlay freeze/read failed: %r", e)
        frozen = None

    return {
        "picks": picks,
        "hot_bats": _df_to_records(hot),
        "today": today,
        "as_of": status["cached_date"],
        "stale": bool(status.get("stale")),
        "odds": odds_meta,
        "bundle": {
            "legs": legs,
            "frozen_at": (frozen or {}).get("created_at"),
            "result": (frozen or {}).get("result"),
            "summary": _bundle.summarise(legs),
            "betslip_url": _bundle.betslip_url(legs),
            # From the whole board: the card is a handful of legs at most,
            # so the next-best runners-up are only visible here.
            "near_misses": _bundle.near_misses(today, legs),
        },
    }


@app.get("/batters/pitcher-form")
async def batter_pitcher_form(name: str, starts: int = 3, season: Optional[int] = None):
    """Recent-form line for one starting pitcher, plus the band the screen
    assigns him (SHARP / HITTABLE / NEUTRAL / UNKNOWN).

    The screen vetoes picks against a SHARP starter, so this is how you check
    a surprising pick — or a surprising absence — against the same numbers the
    screen used instead of inferring them from the board.
    """
    try:
        from .batters import lookup_pitcher_form
    except ImportError as e:
        raise HTTPException(500, f"models extras not installed: {e}")
    try:
        return await asyncio.to_thread(lookup_pitcher_form, name, starts, season)
    except ValueError as e:
        raise HTTPException(404, str(e))


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

    # Non-blocking, self-guarded: refreshes in the background only when the
    # cache is aged past its TTL (picks up mid-day probable/lineup changes) or
    # is serving degraded fallback data — otherwise a cheap no-op.
    warm_async()
    status = warm_status()
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


@app.get("/picks/parlay-record")
async def picks_parlay_record(since: Optional[str] = None):
    """Track record for the day's card, settled as one bet.

    A parlay is not an average of its legs — every leg wins or the ticket is
    dead — so this is the only honest way to score how the card actually did.
    Hit rate on the pick list answers a different question.
    """
    from . import tracking
    return await asyncio.to_thread(tracking.parlay_track_record, since)


@app.post("/picks/resolve")
async def picks_resolve():
    """Settle any unresolved picks from before today against actual results.
    Also runs automatically after each daily screen warm-up.

    Cards settle here too. Grading the legs and leaving the ticket they belong
    to unsettled is the kind of split that hides a stuck card for days, and
    the warm-up already runs the pair together."""
    tracking = _get_tracking()
    picks = await asyncio.to_thread(tracking.resolve_pending)
    cards = await asyncio.to_thread(tracking.resolve_parlays)
    return {**picks, "parlays": cards}


@app.post("/picks/regenerate-today")
async def picks_regenerate_today():
    """Replace today's still-pending picks with the current live board for each
    screen — forces the recorded picks to match the latest slate right away
    (e.g. after a probable pitcher change) instead of waiting for the next
    intra-day refresh."""
    tracking = _get_tracking()
    return await asyncio.to_thread(tracking.regenerate_today)


@app.post("/picks/reresolve-voids")
async def picks_reresolve_voids(since: Optional[str] = None, dry_run: bool = False):
    """Repair picks frozen as VOID by re-checking them against the box score.

    Fixes VOIDs written prematurely from lagging Statcast data: a pick on a
    player who actually started and took a plate appearance becomes WIN/LOSS.
    Genuine voids are left untouched. ``since`` (YYYY-MM-DD) limits the scan;
    ``dry_run=true`` reports the corrections without writing them."""
    tracking = _get_tracking()
    if since is not None:
        try:
            date.fromisoformat(since)
        except ValueError as e:
            raise HTTPException(400, f"bad since date: {e}")
    return await asyncio.to_thread(tracking.reresolve_voids, since, dry_run)


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
    # The visitor's own Anthropic key — Sharp Edge holds none of its own, so
    # chat spends the user's credits. Used for this request only, never stored.
    api_key: str
    model: str = DEFAULT_MODEL


class VerifyKeyRequest(BaseModel):
    api_key: str


def _anthropic_error(e: Exception) -> HTTPException:
    """Map an Anthropic SDK error to a status the frontend can act on,
    without leaking the key or a stack trace."""
    from anthropic import APIStatusError
    if isinstance(e, APIStatusError):
        # 401 bad key, 400 no credit, 429 rate limit — pass the status
        # through so the UI can tell "fix your key" from "try again later".
        detail = getattr(e, "message", None) or str(e)
        return HTTPException(e.status_code, detail)
    return HTTPException(502, f"Anthropic request failed: {type(e).__name__}")


@app.post("/chat/verify")
async def chat_verify(req: VerifyKeyRequest):
    """Confirm a pasted key works before the settings UI marks it connected."""
    if not req.api_key.strip():
        raise HTTPException(400, "No API key provided")
    try:
        await verify_key(req.api_key.strip())
    except Exception as e:
        raise _anthropic_error(e)
    return {"status": "ok"}


@app.post("/chat")
async def chat_endpoint(
    req: ChatRequest,
    uid: str = Depends(get_uid),
    db: BetDatabase = Depends(get_db),
):
    if not req.api_key.strip():
        raise HTTPException(400, "Connect your Anthropic API key to use chat.")
    try:
        return await chat_with_claude(
            messages=req.messages,
            db=db,
            api_key=req.api_key.strip(),
            user_id=uid,
            model=req.model,
        )
    except Exception as e:
        raise _anthropic_error(e)


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
        reload=settings.reload,
    )


if __name__ == "__main__":
    main()
