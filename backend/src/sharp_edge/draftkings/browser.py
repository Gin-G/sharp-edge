"""A real browser on the backend, and the contexts that outlive one request.

DraftKings' auth endpoint sits behind Akamai Bot Manager. Loading the login
page seeds an ``_abck`` cookie, but Akamai only promotes it to validated once
its sensor JS has POSTed telemetry — device fingerprint, timing, input events.
An HTTP client cannot produce that, so a plain login lands on the block page
however well-formed the request is (measured: primed cookie jar, browser
headers, correct Origin — still ``403 AkamaiGHost``). A TLS fingerprint would
not change it either; the sensor payload is the gate, not the handshake.

So the login happens in an actual browser. The user still types their email,
password and MFA code into Sharp Edge's own form — the browser is invisible to
them — and the sensor runs for real because it *is* real.

Two things here are not obvious.

**A context has to survive between requests.** FanDuel's MFA only needs a
short-lived device token carried across two calls; here the half-finished
login lives in a browser, so the context is held open between ``login()`` and
``submit_mfa_code()``. That means a registry with an idle reaper, because a
leaked context is a leaked Chromium process. DraftKings' codes expire in
minutes, so the window is deliberately short.

**One browser, many contexts.** Launching Chromium per login would be absurd;
contexts are the isolation boundary and they are cheap. The browser is started
lazily on first use, so an instance nobody logs into DraftKings from never
pays for it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Chromium flags that matter in a container. --no-sandbox because the pod
# doesn't run privileged, and --disable-dev-shm-usage because Kubernetes gives
# /dev/shm 64MB by default, which Chromium will happily exhaust mid-page and
# then crash in a way that reads as a network error.
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
]

# Automation leaves one obvious tell: navigator.webdriver. This is a real
# browser doing a real login with the account holder's own credentials, so the
# flag misrepresents what is happening rather than describing it. Cleared on
# every page before any site script runs.
_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
"""

# How long a half-finished login may sit before its browser is reclaimed.
# Sized to DraftKings' MFA code lifetime rather than to politeness: a user who
# takes longer than this has an expired code anyway and has to start over.
IDLE_TIMEOUT_SECONDS = 600
_REAP_INTERVAL_SECONDS = 120


class PlaywrightUnavailable(RuntimeError):
    """Playwright or its Chromium build isn't present in this image."""


@dataclass
class LiveSession:
    """One user's in-flight browser, held between requests."""

    context: object          # playwright BrowserContext
    page: object             # playwright Page
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_used = time.time()

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_used


_playwright = None
_browser = None
_launch_lock = asyncio.Lock()
_sessions: dict[str, LiveSession] = {}
_reaper: Optional[asyncio.Task] = None


def _import_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise PlaywrightUnavailable(
            "Playwright isn't installed. DraftKings login needs a real browser "
            "because its auth endpoint is behind Akamai's bot sensor. Install "
            "with: pip install playwright && playwright install chromium"
        ) from e
    return async_playwright


async def _get_browser():
    """The shared Chromium, launched on first use.

    Guarded by a lock rather than started at app boot: an instance that never
    touches DraftKings should never pay for a browser, and lifespan startup is
    already slow enough with the Statcast warm-up.
    """
    global _playwright, _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    async with _launch_lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        from ..config import settings

        async_playwright = _import_playwright()
        if _playwright is None:
            _playwright = await async_playwright().start()
        try:
            _browser = await _playwright.chromium.launch(
                headless=settings.draftkings_headless,
                # The full Chromium build, not the headless-shell Playwright
                # otherwise defaults to. Measured against the live login page:
                #
                #   headless-shell            403 Access Denied
                #   new headless (channel)    403 Access Denied
                #   headed (channel)          200, form renders
                #
                # Akamai rejects both headless modes, so the shell is never
                # the right binary here even when something else is.
                channel="chromium",
                args=LAUNCH_ARGS,
            )
        except Exception as e:
            raise PlaywrightUnavailable(
                f"Couldn't launch Chromium ({e}). If Playwright is installed "
                "but the browser isn't, run: playwright install chromium. "
                "Running headed also needs a display — in a container that "
                "means Xvfb (the image wraps the process in xvfb-run)."
            ) from e
        logger.info("draftkings: chromium launched (headless=%s, display=%s)",
                    settings.draftkings_headless, os.environ.get("DISPLAY", "-"))
    _ensure_reaper()
    return _browser


async def open_session(key: str, storage_state: Optional[dict] = None) -> LiveSession:
    """Start a fresh context for one user, replacing any it already had.

    ``storage_state`` rehydrates a previously saved session (cookies plus
    localStorage), which is what lets a pod restart resume instead of sending
    the user back through MFA.
    """
    await close_session(key)
    browser = await _get_browser()
    context = await browser.new_context(
        storage_state=storage_state or None,
        locale="en-US",
        timezone_id="America/Denver",
        viewport={"width": 1280, "height": 900},
    )
    await context.add_init_script(_STEALTH_INIT)
    page = await context.new_page()
    session = LiveSession(context=context, page=page)
    _sessions[key] = session
    return session


def get_session(key: str) -> Optional[LiveSession]:
    session = _sessions.get(key)
    if session is not None:
        session.touch()
    return session


async def close_session(key: str) -> None:
    """Drop a user's context. Safe to call on one that isn't there."""
    session = _sessions.pop(key, None)
    if session is None:
        return
    try:
        await session.context.close()
    except Exception as e:  # a context whose browser already died
        logger.debug("draftkings: context close failed for %s: %s", key, e)


def _ensure_reaper() -> None:
    global _reaper
    if _reaper is None or _reaper.done():
        _reaper = asyncio.create_task(_reap_loop())


async def _reap_loop() -> None:
    """Close contexts nobody came back for.

    Without this an abandoned MFA — user closes the tab at the code prompt —
    holds a Chromium context open until the pod restarts, and a handful of
    those is real memory.
    """
    while True:
        await asyncio.sleep(_REAP_INTERVAL_SECONDS)
        try:
            stale = [
                k for k, s in _sessions.items()
                if s.idle_seconds > IDLE_TIMEOUT_SECONDS
            ]
            for key in stale:
                logger.info("draftkings: reaping idle session %s", key)
                await close_session(key)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("draftkings: reaper error: %s", e)


async def shutdown() -> None:
    """Close every context and the browser. Called from the app's lifespan."""
    global _browser, _playwright, _reaper
    if _reaper is not None:
        _reaper.cancel()
        _reaper = None
    for key in list(_sessions):
        await close_session(key)
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None


def session_count() -> int:
    """Live contexts — exposed so the API can report what's being held."""
    return len(_sessions)
