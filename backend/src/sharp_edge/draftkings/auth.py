"""DraftKings session auth — email/password and MFA, driven through a browser.

The shape mirrors ``fanduel/auth.py`` deliberately: log in once, keep the
session, resume it after a restart, and surface a device-verification
challenge as an exception the API layer turns into an MFA prompt. What differs
is the mechanism underneath, and the difference is forced rather than chosen —
see ``browser.py`` for why an HTTP login cannot work here.

Two consequences fall out of the session being *cookies in a browser* rather
than a bearer token:

**There is no exp claim to read.** FanDuel hands back a JWT and
``FanDuelAuth._set_token`` decodes its expiry, falling back to an assumed hour.
Here the closest equivalent is the longest-lived DraftKings cookie in the
stored state, which is a decent proxy and is reported honestly through
``expiry_assumed`` when it isn't available.

**Renewal after a restart is the stored state, not a refresh token.** The
password is never persisted — same rule as FanDuel — so ``can_refresh``
reports whether the saved cookies can resume the session unaided.

Selectors are written as ordered candidate lists rather than one guess.
DraftKings' login is a React app that has been a single form and a stepped
email-then-password flow at different times, and its MFA input has appeared
both as one field and as six single-character boxes. Each is handled, because
a selector that silently matches nothing produces a timeout whose error
message explains nothing.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

from . import browser as _browser

logger = logging.getLogger(__name__)

# Per the login URL captured from a real session:
#   myaccount.draftkings.com/auth/login?product=sportsbook&intendedSiteExp=US-CO-SB
LOGIN_URL = (
    "https://myaccount.draftkings.com/auth/login"
    "?product=sportsbook&intendedSiteExp=US-{state}-SB"
)

# Hosts that mean the login finished. Leaving the auth page for any DraftKings
# surface is the signal; which surface depends on returnPath.
_SUCCESS_URL = re.compile(
    r"^https://(?:sportsbook|myaccount|www)\.draftkings\.com/(?!auth/login)", re.I
)

# Verified against the live login page. The ids come first because they are
# what the page actually uses; the rest are fallbacks for when it is
# redesigned, which is the only reason they are still here.
_EMAIL_SELECTORS = (
    "#login-username-input",
    'input[name="Email"]',
    'input[type="email"]',
    'input[id*="username" i]',
    'input[id*="email" i]',
)
_PASSWORD_SELECTORS = (
    "#login-password-input",
    'input[name="Password"]',
    'input[type="password"]',
    'input[autocomplete="current-password"]',
)

# Order is load-bearing here, and not obviously so.
#
# The login page carries TWO type="submit" buttons, and "Sign Up" is the one
# that comes first in the DOM. A bare `button[type="submit"]` .first therefore
# clicks Sign Up — sending the user into registration instead of logging them
# in, with no error to explain it. Text-matched selectors must win, and the
# generic one stays only as a last resort for a redesign that drops the label.
_SUBMIT_SELECTORS = (
    'button[type="submit"]:has-text("Log In")',
    'button:has-text("Log In")',
    'button:has-text("Sign In")',
    'button:has-text("Continue")',
    'button:has-text("Next")',
    'button[type="submit"]',
)
# One-field form of the code input. The boxed variant is found separately.
_MFA_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
    'input[name*="code" i]',
    'input[id*="otp" i]',
    'input[id*="code" i]',
)
_MFA_PAGE_MARKERS = re.compile(
    r"verification code|security code|we sent|check your (?:email|phone)|"
    r"two[- ]factor|enter the code",
    re.I,
)
_BOT_MARKERS = re.compile(r"access denied|reference\s*#\d+|errors\.edgesuite\.net", re.I)
_ERROR_SELECTORS = (
    '[role="alert"]',
    '[class*="error" i]',
    '[data-testid*="error" i]',
)


class DraftKingsAuthError(Exception):
    """Login failed for a reason worth showing the user."""


class DraftKingsMFARequired(DraftKingsAuthError):
    """DraftKings wants a verification code. The browser is held open."""


class DraftKingsBotBlocked(DraftKingsAuthError):
    """Akamai rejected the attempt before credentials were considered."""


class DraftKingsAuth:
    """One user's DraftKings session, obtained and held via a real browser."""

    def __init__(
        self,
        email: str,
        password: str,
        state: str = "CO",
        session_key: Optional[str] = None,
        storage_state: Optional[dict] = None,
    ):
        self.email = email
        self.password = password
        self.state = (state or "CO").upper()
        # Keys the live browser context. Stable per user so a login and its
        # MFA step find the same half-finished browser.
        self.session_key = session_key or f"dk:{email or 'anon'}"
        self._storage: Optional[dict] = storage_state
        self._mfa_required = False
        self._authenticated_at: float = 0.0
        self._expires_at: float = 0.0
        self._exp_assumed = True

    # ------------------------------------------------------------------
    # Protocol surface (books.BookAuth)
    # ------------------------------------------------------------------

    @property
    def token(self) -> Optional[str]:
        """There is no bearer token; the session *is* the stored cookies.

        A short digest stands in so the API's ``if not auth.token`` checks
        mean "is this user logged in", which is the question they are really
        asking. Returning cookie values here would put a live credential in
        every status response.
        """
        if not self._storage:
            return None
        import hashlib, json

        blob = json.dumps(self._storage.get("cookies", []), sort_keys=True)
        return "dk_" + hashlib.sha256(blob.encode()).hexdigest()[:16]

    @property
    def is_expired(self) -> bool:
        if not self._storage:
            return True
        return time.time() > (self._expires_at - 300)

    @property
    def expires_in(self) -> int:
        return max(0, int(self._expires_at - time.time()))

    @property
    def expiry_assumed(self) -> bool:
        return self._exp_assumed

    @property
    def mfa_pending(self) -> bool:
        return self._mfa_required

    @property
    def can_relogin(self) -> bool:
        return bool(self.email and self.password)

    @property
    def can_refresh(self) -> bool:
        """Stored cookies can resume the session with no user input — the
        part that survives a restart, since the password never does."""
        return bool(self._storage)

    @property
    def can_renew(self) -> bool:
        return self.can_refresh or self.can_relogin

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def login(self) -> str:
        """Drive the real login form.

        Raises DraftKingsMFARequired with the browser still open when a code
        is wanted, DraftKingsBotBlocked when Akamai intervenes anyway, and
        DraftKingsAuthError for a refused credential or a page we could not
        read.
        """
        if not self.can_relogin:
            raise DraftKingsAuthError("Email and password are required")

        session = await _browser.open_session(self.session_key)
        page = session.page
        try:
            await page.goto(
                LOGIN_URL.format(state=self.state),
                wait_until="domcontentloaded",
                timeout=self._timeout(),
            )
            await self._raise_if_blocked(page)

            await self._fill_first(page, _EMAIL_SELECTORS, self.email, "email")
            # Stepped flows hide the password until the email is submitted;
            # single-page ones show both at once. Try to fill directly and
            # only advance when there is nothing to fill.
            if not await self._try_fill(page, _PASSWORD_SELECTORS, self.password):
                await self._click_submit(page)
                await self._fill_first(
                    page, _PASSWORD_SELECTORS, self.password, "password"
                )
            await self._click_submit(page)

            return await self._settle(page)
        except DraftKingsAuthError:
            raise
        except Exception as e:
            await _browser.close_session(self.session_key)
            raise DraftKingsAuthError(f"DraftKings login failed: {e}") from e

    async def submit_mfa_code(self, code: str) -> str:
        """Finish a login DraftKings held for verification.

        The browser from ``login()`` is still open at the code prompt; if it
        was reaped for idleness the code is already expired anyway, so this
        says so rather than starting a login the user didn't ask for.
        """
        if not self._mfa_required:
            raise DraftKingsAuthError(
                "No pending verification — log in again to request a new code."
            )
        session = _browser.get_session(self.session_key)
        if session is None:
            self._mfa_required = False
            raise DraftKingsAuthError(
                "The verification window expired — log in again to get a new code."
            )
        page = session.page
        try:
            await self._fill_code(page, code.strip())
            await self._click_submit(page)
            token = await self._settle(page)
            self._mfa_required = False
            return token
        except DraftKingsAuthError:
            raise
        except Exception as e:
            raise DraftKingsAuthError(f"Verification failed: {e}") from e

    async def ensure_token(self, force: bool = False) -> str:
        """A usable session, renewed when stale.

        Stored cookies are tried first because they need no user input; a
        credential re-login is the fallback and may re-trigger MFA, which is
        why it is not the first move.
        """
        if not force and not self.is_expired and self._storage:
            return self.token
        if self.can_relogin:
            return await self.login()
        if self._storage:
            # Nothing else to try — let the caller discover whether the
            # cookies still work rather than refusing pre-emptively.
            return self.token
        raise DraftKingsAuthError(
            "DraftKings session expired and can't be renewed — log in again."
        )

    # ------------------------------------------------------------------
    # Page helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _timeout() -> int:
        from ..config import settings

        return settings.draftkings_timeout_ms

    async def _settle(self, page) -> str:
        """Work out what the page became after a submit.

        Three outcomes matter and they are racing: a navigation away (success),
        a code prompt (MFA), or an error message. Polling for all three beats
        waiting on any one of them, because waiting on the wrong one turns a
        clear failure into a timeout.
        """
        deadline = time.time() + (self._timeout() / 1000)
        last_error = None
        while time.time() < deadline:
            await page.wait_for_timeout(500)

            if _SUCCESS_URL.match(page.url or ""):
                return await self._capture(page)

            await self._raise_if_blocked(page)

            if await self._mfa_visible(page):
                self._mfa_required = True
                raise DraftKingsMFARequired(
                    "DraftKings sent a verification code. Enter it to finish "
                    "signing in."
                )

            found = await self._read_error(page)
            if found:
                last_error = found
                # An inline validation message can flash before the real
                # navigation, so don't fail on the first sight of one.
                if time.time() > deadline - 2:
                    break

        if last_error:
            raise DraftKingsAuthError(last_error)
        # A session cookie can exist even when the SPA hasn't navigated yet.
        if await self._has_session_cookie(page):
            return await self._capture(page)
        raise DraftKingsAuthError(
            "Timed out waiting for DraftKings to complete the login. The page "
            "may have changed shape — check the selectors in draftkings/auth.py."
        )

    async def _capture(self, page) -> str:
        """Freeze the authenticated session: cookies plus localStorage."""
        self._storage = await page.context.storage_state()
        self._authenticated_at = time.time()
        self._expires_at, self._exp_assumed = _read_expiry(self._storage)
        logger.info(
            "draftkings: login complete (cookies=%d, expiry_assumed=%s)",
            len(self._storage.get("cookies", [])), self._exp_assumed,
        )
        return self.token

    async def _raise_if_blocked(self, page) -> None:
        try:
            body = await page.inner_text("body", timeout=1000)
        except Exception:
            return
        if _BOT_MARKERS.search(body or "") and len(body or "") < 2000:
            raise DraftKingsBotBlocked(
                "DraftKings' bot protection blocked the login even from a real "
                "browser. Retry shortly; if it persists the browser may need to "
                "run headed (DRAFTKINGS_HEADLESS=false)."
            )

    async def _mfa_visible(self, page) -> bool:
        for sel in _MFA_SELECTORS:
            try:
                if await page.locator(sel).first.is_visible(timeout=200):
                    return True
            except Exception:
                continue
        if await self._boxed_code_inputs(page):
            return True
        try:
            body = await page.inner_text("body", timeout=500)
        except Exception:
            return False
        return bool(_MFA_PAGE_MARKERS.search(body or ""))

    @staticmethod
    async def _boxed_code_inputs(page):
        """The six-single-character variant of the code input.

        Returns the locator when the page uses it, so the code can be typed
        one digit per box — filling the first box with all six characters
        silently drops five of them.
        """
        try:
            boxes = page.locator('input[maxlength="1"]')
            count = await boxes.count()
            if count >= 4 and await boxes.first.is_visible(timeout=200):
                return boxes
        except Exception:
            pass
        return None

    async def _fill_code(self, page, code: str) -> None:
        boxes = await self._boxed_code_inputs(page)
        if boxes is not None:
            count = await boxes.count()
            for i, ch in enumerate(code[:count]):
                await boxes.nth(i).fill(ch)
            return
        if not await self._try_fill(page, _MFA_SELECTORS, code):
            raise DraftKingsAuthError(
                "Couldn't find the verification code field on the page."
            )

    async def _fill_first(self, page, selectors, value: str, what: str) -> None:
        if not await self._try_fill(page, selectors, value):
            raise DraftKingsAuthError(
                f"Couldn't find the {what} field on DraftKings' login page — "
                "its markup has probably changed."
            )

    @staticmethod
    async def _try_fill(page, selectors, value: str) -> bool:
        for sel in selectors:
            try:
                field = page.locator(sel).first
                if await field.is_visible(timeout=1500):
                    await field.fill(value)
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    async def _click_submit(page) -> None:
        for sel in _SUBMIT_SELECTORS:
            try:
                button = page.locator(sel).first
                if await button.is_visible(timeout=1000) and await button.is_enabled():
                    await button.click()
                    return
            except Exception:
                continue
        # Some flows have no button until the field validates; Enter works.
        await page.keyboard.press("Enter")

    @staticmethod
    async def _read_error(page) -> Optional[str]:
        for sel in _ERROR_SELECTORS:
            try:
                node = page.locator(sel).first
                if await node.is_visible(timeout=200):
                    text = (await node.inner_text()).strip()
                    if text and len(text) < 300:
                        return text
            except Exception:
                continue
        return None

    @staticmethod
    async def _has_session_cookie(page) -> bool:
        try:
            cookies = await page.context.cookies()
        except Exception:
            return False
        return any(_is_session_cookie(c.get("name", "")) for c in cookies)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_state(self) -> dict:
        """Persisted between restarts. No password, same rule as FanDuel."""
        return {
            "email": self.email,
            "state": self.state,
            "session_key": self.session_key,
            "storage_state": self._storage,
            "authenticated_at": self._authenticated_at,
            "expires_at": self._expires_at,
            "exp_assumed": self._exp_assumed,
        }

    @classmethod
    def from_state(cls, state: dict, password: str = "") -> "DraftKingsAuth":
        auth = cls(
            state.get("email", ""),
            password,
            state=state.get("state") or "CO",
            session_key=state.get("session_key"),
            storage_state=state.get("storage_state"),
        )
        auth._authenticated_at = state.get("authenticated_at", 0) or 0
        auth._expires_at = state.get("expires_at", 0) or 0
        auth._exp_assumed = bool(state.get("exp_assumed", True))
        return auth

    @property
    def storage_state(self) -> Optional[dict]:
        """The cookies the client needs to make authenticated requests."""
        return self._storage


def _is_session_cookie(name: str) -> bool:
    """Does this cookie look like the one that carries the login?

    Matched by shape rather than an exact name: DraftKings has used several,
    and the Akamai cookies that ride alongside must not be mistaken for one.
    """
    lowered = name.lower()
    if lowered.startswith(("_abck", "bm_", "ak_")):
        return False
    return any(k in lowered for k in ("session", "auth", "token", "dkid", "sid"))


def _read_expiry(storage: Optional[dict]) -> tuple[float, bool]:
    """When the session dies, and whether that is known or assumed.

    The longest-lived session-ish cookie is the best available proxy for a JWT
    exp claim. When none carries an expiry — a pure session cookie has none —
    a day is assumed and flagged, so the UI never states a countdown as fact.
    """
    cookies = (storage or {}).get("cookies") or []
    expiries = [
        c["expires"] for c in cookies
        if _is_session_cookie(c.get("name", ""))
        and isinstance(c.get("expires"), (int, float))
        and c["expires"] > 0
    ]
    if expiries:
        return float(max(expiries)), False
    return time.time() + 86400, True
