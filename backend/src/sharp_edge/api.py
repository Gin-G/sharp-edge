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

from . import books
from .config import settings
from .db import create_database, BetDatabase
from .fanduel.auth import (
    FanDuelAuth,
    FanDuelBotBlocked,
    FanDuelMFARequired,
)
from .analysis import score_bet, generate_insights
from .chat import chat as chat_with_claude, verify_key, DEFAULT_MODEL

logger = logging.getLogger(__name__)

# Per-user book sessions, keyed by (book, session uid). Was a FanDuel-only map
# until DraftKings arrived; the book in the key is the only thing that changed,
# and FanDuel's persisted rows keep their historical sync_state key so nobody
# is logged out by the generalisation. The in-memory map is a cache over the
# DB-persisted session, so a pod restart rehydrates it (and its refresh token)
# instead of forcing a fresh login.
_db: Optional[BetDatabase] = None
_auth: dict[tuple[str, str], object] = {}


async def _persist_auth(book: str, uid: str, auth) -> None:
    """Save a user's session for one book (token + refresh token, no password)."""
    import json
    try:
        await _db.set_sync_state(
            uid, books.session_key(book), json.dumps(auth.to_state())
        )
    except Exception as e:
        logger.warning("failed to persist %s session: %s", book, e)


async def _load_auth(book: str, uid: str):
    """Return the user's auth for one book, rehydrating from the DB if the
    in-memory cache was lost to a restart."""
    auth = _auth.get((book, uid))
    if auth is not None:
        return auth
    entry = books.get_book(book)
    if entry.state_factory is None:
        return None
    import json
    try:
        raw = await _db.get_sync_state(uid, books.session_key(book))
    except Exception:
        raw = None
    if not raw:
        return None
    try:
        auth = entry.state_factory(json.loads(raw))
    except Exception as e:
        logger.warning("failed to rehydrate %s session: %s", book, e)
        return None
    _auth[(book, uid)] = auth
    return auth


def _require_book(key: str, need: str = "login") -> books.Book:
    """Resolve a book, refusing clearly when it can't do what was asked.

    A capability the book doesn't have is a 501 naming the reason, not a 500
    from somewhere deep in a request that was never going to work.
    """
    try:
        entry = books.get_book(key)
    except ValueError as e:
        raise HTTPException(404, str(e))
    supported = entry.supports_login if need == "login" else entry.supports_sync
    if not supported:
        raise HTTPException(
            501,
            entry.unsupported_reason
            or f"{entry.name} does not support {need} yet.",
        )
    return entry


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

    # NFL tracking needs only the database — its writes happen on the request
    # path rather than in a warm-up thread, so there is no loop to hand over.
    try:
        from .nfl import tracking as nfl_tracking
        nfl_tracking.configure(_db)
        logger.info("nfl tracking: configured")
    except Exception as e:
        logger.warning("nfl tracking: configure failed: %s", e)

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


@app.get("/books")
async def list_books(uid: str = Depends(get_uid)):
    """Every book the app knows, what it can do, and whether you're logged in.

    The frontend builds its settings panel off this rather than hard-coding
    FanDuel, so a book whose login isn't wired up yet renders as an honest
    "not available" instead of a form that can't work.
    """
    out = []
    for key, entry in books.BOOKS.items():
        auth = await _load_auth(key, uid) if entry.supports_login else None
        out.append({
            "key": key,
            "name": entry.name,
            "supports_login": entry.supports_login,
            "supports_sync": entry.supports_sync,
            # Prices can arrive through the aggregator even when the book's
            # own API is unreachable, which is exactly the DraftKings case.
            "supports_odds": entry.odds_api_key is not None,
            "unsupported_reason": entry.unsupported_reason,
            "authenticated": bool(auth and auth.token),
            "expired": bool(auth and auth.token and auth.is_expired),
        })
    return {"books": out, "default": books.DEFAULT_BOOK}


@app.post("/auth/{book}/login")
async def login_book(book: str, req: LoginRequest, uid: str = Depends(get_uid)):
    entry = _require_book(book)
    # Reuse the stored session when there is one: FanDuel keys device
    # verification to an installation id, so a new id means a new MFA code
    # every login. Any book with the same notion gets the same treatment.
    prior = await _load_auth(book, uid)
    auth = entry.auth_factory(req.email, req.password, prior)
    try:
        await auth.login()
        _auth[(book, uid)] = auth
        await _persist_auth(book, uid, auth)
        return _session_payload(auth)
    except FanDuelMFARequired as e:
        # Keep the credentials so the mfa route can finish the login.
        _auth[(book, uid)] = auth
        return {"status": "mfa_required", "message": str(e)}
    except FanDuelBotBlocked as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))


class MFARequest(BaseModel):
    code: str


@app.post("/auth/{book}/mfa")
async def submit_mfa_book(book: str, req: MFARequest, uid: str = Depends(get_uid)):
    """Finish a login the book held for new-device verification."""
    _require_book(book)
    auth = _auth.get((book, uid))
    if not auth or not auth.mfa_pending:
        raise HTTPException(400, f"No pending login — start with /auth/{book}/login")
    try:
        await auth.submit_mfa_code(req.code)
        await _persist_auth(book, uid, auth)
        return _session_payload(auth)
    except FanDuelBotBlocked as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/auth/{book}/status")
async def auth_status_book(book: str, uid: str = Depends(get_uid)):
    try:
        books.get_book(book)
    except ValueError as e:
        raise HTTPException(404, str(e))
    auth = await _load_auth(book, uid)
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


@app.post("/auth/{book}/logout")
async def logout_book(book: str, request: Request, uid: str = Depends(get_uid)):
    """Clear one book's auth. The session cookie is only rotated when the last
    book is logged out — dropping it while another book is still signed in
    would strand that session's other credentials."""
    try:
        books.get_book(book)
    except ValueError as e:
        raise HTTPException(404, str(e))
    _auth.pop((book, uid), None)
    try:
        await _db.set_sync_state(uid, books.session_key(book), "")
    except Exception:
        pass
    if not any(k[1] == uid for k in _auth):
        request.session.clear()
    return {"status": "ok"}


# ------------------------------------------------------------------
# FanDuel's original un-namespaced routes.
#
# Kept as delegations rather than redirects: they are what the shipped
# frontend calls, and a 307 on a POST is the kind of thing that works in
# every client except the one you forgot about.
# ------------------------------------------------------------------

@app.post("/auth/login")
async def login(req: LoginRequest, uid: str = Depends(get_uid)):
    return await login_book(books.DEFAULT_BOOK, req, uid)


@app.post("/auth/mfa")
async def submit_mfa(req: MFARequest, uid: str = Depends(get_uid)):
    return await submit_mfa_book(books.DEFAULT_BOOK, req, uid)


@app.get("/auth/status")
async def auth_status(uid: str = Depends(get_uid)):
    return await auth_status_book(books.DEFAULT_BOOK, uid)


@app.post("/auth/logout")
async def logout(request: Request, uid: str = Depends(get_uid)):
    return await logout_book(books.DEFAULT_BOOK, request, uid)


@app.post("/auth/token")
async def set_manual_token(req: ManualTokenRequest, uid: str = Depends(get_uid)):
    """Set a manually-captured JWT from browser DevTools. FanDuel only — it is
    the fallback for when bot protection blocks a real login."""
    auth = _auth.get((books.DEFAULT_BOOK, uid))
    if not auth:
        auth = FanDuelAuth("", "")
        _auth[(books.DEFAULT_BOOK, uid)] = auth
    auth.set_manual_token(req.token)
    return _session_payload(auth)


# ------------------------------------------------------------------
# Bets
# ------------------------------------------------------------------

@app.post("/bets/sync")
async def sync_bets(
    book: str = books.DEFAULT_BOOK,
    uid: str = Depends(get_uid),
    db: BetDatabase = Depends(get_db),
):
    """Pull settled bet history from one book into the local store.

    Defaults to FanDuel so the existing frontend call keeps working unchanged.
    """
    entry = _require_book(book, need="sync")
    auth = await _load_auth(book, uid)
    if not auth or not auth.token:
        raise HTTPException(400, f"Not authenticated with {entry.name}")

    try:
        token = await auth.ensure_token()
    except Exception as e:
        raise HTTPException(401, str(e))
    # Persist any renewed token/refresh token so the next restart reuses it.
    await _persist_auth(book, uid, auth)
    client = entry.client_factory(token, auth)
    try:
        raw_bets = await client.fetch_all_settled_bets()
        count = 0
        for raw in raw_bets:
            await db.upsert_bet(uid, client.normalize_bet(raw))
            count += 1
        return {"status": "ok", "book": book, "bets_synced": count}
    finally:
        await _persist_auth(book, uid, auth)  # client may have refreshed on a 401
        await client.close()


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

    # Every other book's price for the same leg, through the aggregator.
    #
    # Additive by design: FanDuel above still sets fd_odds and the ids the
    # bet-slip link is built from, and this only attaches a `books` map and
    # names the best price. So a missing or unconfigured key costs the
    # comparison and nothing else — the board prices exactly as it did before.
    books_meta = {"error": None, "age_seconds": None, "books": [], "quota": {}}
    try:
        from . import pricing as _pricing
        from .oddsapi import cache as _oddscache
        from .oddsapi.props import books_in

        if _oddscache.configured():
            got = await _oddscache.cached_slate("mlb")
            _pricing.attach_book_prices(picks, got["slate"], market="hits")
            _pricing.attach_book_prices(today, got["slate"], market="hits")
            books_meta = {
                "error": got["error"],
                "age_seconds": got["age_seconds"],
                "books": books_in(got["slate"]),
                "quota": got["quota"],
            }
        else:
            books_meta["error"] = "no Odds API key configured"
    except Exception as e:
        logger.warning("multi-book pricing failed: %s", e)
        books_meta["error"] = str(e)

    # The day's bundle: the two most likely to record a hit, one leg per
    # game, plus every other leg that pays for the risk it adds — and a link
    # that loads it straight into the bet slip. The first two are chosen on
    # probability, which is the only place the model can separate the board;
    # everything past them is chosen on what it pays. No cap.
    from . import bundle as _bundle

    # Batters who did not bat in the last few days. They VOID again 44% of the
    # time and hit 5 points worse when they do play, so they are dropped before
    # the card is ranked rather than after.
    from . import tracking as _tr
    try:
        scratched = await _tr.recently_voided("batter", _bundle.VOID_LOOKBACK_DAYS)
    except Exception as e:
        logger.warning("recent-VOID filter unavailable: %s", e)
        scratched = set()
    legs = _bundle.build(picks, exclude_ids=scratched)

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
        "books": books_meta,
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


@app.get("/odds/books")
async def odds_books(sport: str = "mlb", force: bool = False):
    """The multi-book board for a sport, straight from the aggregator.

    Exposed on its own as well as folded into the screens because it answers a
    question the screens can't: what a market costs everywhere, including for
    players the model didn't pick. ``force`` skips the cache TTL but not the
    quota floor.
    """
    from .oddsapi import cache as _oddscache
    from .oddsapi.props import books_in

    # Sport first: a typo should be reported as a typo rather than disappear
    # behind the missing-key message.
    if sport not in _oddscache._FETCHERS:
        raise HTTPException(
            404,
            f"unknown sport {sport!r} — known sports: "
            f"{', '.join(sorted(_oddscache._FETCHERS))}",
        )
    if not _oddscache.configured():
        raise HTTPException(
            501,
            "No Odds API key configured. Set ODDS_API_KEY to price DraftKings "
            "and the other books — DraftKings' own board is unreachable "
            "(Akamai blocks it), so this is the supported route to its prices.",
        )
    got = await _oddscache.cached_slate(sport, force=force)
    return {
        "sport": sport,
        "books": books_in(got["slate"]),
        "slate": {k: v for k, v in got["slate"].items() if k != "_meta"},
        "age_seconds": got["age_seconds"],
        "error": got["error"],
        "quota": got["quota"],
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
# NFL — weekly prop board (not user-scoped — public data)
# ------------------------------------------------------------------

@app.get("/nfl/screen")
async def nfl_screen(force: bool = False):
    """This week's NFL prop board: projections against FanDuel's posted lines.

    Every row carries both the raw projection-minus-line gap and the residual
    after the week's projections are rescaled onto the market's scale. Only
    the residual drives ``signal``; the raw one is there because the two
    disagree a lot and the difference is worth watching rather than trusting
    — see ``nfl.model`` for the measurement.

    Backed by a per-week cache warmed in the background. A cold process
    returns 503 with Retry-After so the frontend polls, exactly like the
    batter screen; once warm every call is instant.
    """
    try:
        from .nfl import screen as nfl
    except ImportError as e:
        raise HTTPException(500, f"NFL extras not installed: {e}")

    board = nfl.get_cached()
    if board is None or force:
        nfl.warm_async(force=force)
        status = nfl.warm_status()
        # A forced rebuild must not serve — or freeze — the board it was asked
        # to replace. Returning the stale one here looks harmless and is not:
        # the freeze happens further down, so `?force=true` after an upstream
        # projection fix wrote the *pre-fix* card and made it permanent, since
        # the card insert is deliberately once-per-week. Answer 503 and let the
        # caller poll for the new board instead.
        if board is None or force:
            if status["last_error"]:
                raise HTTPException(500, f"board build failed: {status['last_error']}")
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "10"},
                content={
                    "status": "warming",
                    "elapsed_seconds": status["elapsed_seconds"],
                    "message": "Building this week's NFL board — try again shortly.",
                },
            )

    # Self-guarded: a cheap no-op unless the board has aged past its TTL, so
    # mid-week line moves land without a reader ever waiting for them.
    nfl.warm_async()
    payload = nfl.as_payload(board)
    payload["stale"] = bool(nfl.warm_status().get("stale"))

    # Record the week on the way past. Both writes are idempotent — picks
    # upsert while unresolved, the card insert is a no-op once the week has one
    # — so wiring this into the read path is what guarantees the week is
    # captured before kickoff without needing a scheduler to be right.
    try:
        from .nfl import tracking as nfl_tracking
        payload["frozen"] = await nfl_tracking.freeze_week(payload)
    except Exception as e:
        logger.warning("nfl freeze failed: %r", e)
        payload["frozen"] = {"error": str(e)}
    return payload


@app.get("/nfl/track-record")
async def nfl_track_record(season: Optional[int] = None):
    """Hit rate and ROI for every NFL suggestion we have recorded.

    Split by market and by side, because those are where the model is most
    likely to be wrong in a way an overall number would hide — in particular
    the UNDER bar in nfl/card.py is a guess, and this is what confirms or
    kills it.
    """
    try:
        from .nfl import tracking as nfl_tracking
    except ImportError as e:
        raise HTTPException(500, f"NFL extras not installed: {e}")
    return await nfl_tracking.track_record(season)


@app.post("/nfl/settle")
async def nfl_settle(season: Optional[int] = None, week: Optional[int] = None,
                     regrade: bool = False):
    """Settle one week against nflverse actuals.

    Defaults to the most recent week that still has pending picks, so the
    daily job can call it with no arguments and do the right thing. nflverse
    publishes a day or two after the games, and a week with no actuals yet is
    reported as such rather than settled wrongly.
    """
    try:
        from .nfl import tracking as nfl_tracking
    except ImportError as e:
        raise HTTPException(500, f"NFL extras not installed: {e}")

    if season is None or week is None:
        pending = [p for p in await get_db().list_nfl_picks() if not p.get("result")]
        if not pending:
            return {"settled": 0, "message": "nothing pending"}
        target = max((p["season"], p["week"]) for p in pending)
        season, week = target
    return await nfl_tracking.settle_week(season, week, regrade=regrade)


@app.post("/nfl/picks/purge-late")
async def nfl_purge_late(season: Optional[int] = None, week: Optional[int] = None,
                         apply: bool = False):
    """Delete picks recorded after their own game started. Dry run by default.

    They are not predictions, and they distort every number the track record
    reports. The filter is the rule itself — created_at later than kickoff — so
    this cannot be aimed at a legitimate pick.
    """
    try:
        from .nfl import tracking as nfl_tracking
    except ImportError as e:
        raise HTTPException(500, f"NFL extras not installed: {e}")
    return await nfl_tracking.purge_late_picks(season, week, apply=apply)


@app.get("/nfl/games/record")
async def nfl_game_record(season: Optional[int] = None):
    """How the game model has done against results and against the market.

    Predictions only — no side is taken on these, and none should be until the
    live record says the model finds something the backtest did not.
    """
    try:
        from .nfl import tracking as nfl_tracking
    except ImportError as e:
        raise HTTPException(500, f"NFL extras not installed: {e}")
    return await nfl_tracking.game_model_record(season)


@app.post("/nfl/games/settle")
async def nfl_game_settle(season: Optional[int] = None, week: Optional[int] = None):
    """Attach final scores to game predictions whose games have finished."""
    try:
        from .nfl import tracking as nfl_tracking
    except ImportError as e:
        raise HTTPException(500, f"NFL extras not installed: {e}")
    return await nfl_tracking.settle_game_predictions(season, week)


@app.get("/nfl/screen/status")
async def nfl_screen_status():
    try:
        from .nfl import screen as nfl
    except ImportError:
        return {"available": False}
    return {"available": True, **nfl.warm_status()}


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
    include_metrics: bool = False,
    db: BetDatabase = Depends(get_db),
):
    """Hit-rate summary for a screen's persisted picks: overall, per edge
    tag, per source (live vs backfill), per day, plus the pick list itself
    with WIN / LOSS / VOID outcomes."""
    tracking = _get_tracking()
    if screen not in tracking.SCREENS:
        raise HTTPException(400, f"screen must be one of {tracking.SCREENS}")
    rows = await db.list_picks(screen=screen, since=since,
                               include_metrics=include_metrics)
    return tracking.build_track_record(screen, rows,
                                       include_metrics=include_metrics)


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
