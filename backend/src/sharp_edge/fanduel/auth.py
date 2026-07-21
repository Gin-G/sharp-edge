"""FanDuel session authentication — direct login, new-device MFA, refresh.

Login flow (matches the browser's calls to api.fanduel.com):

  1. POST /sessions with email/password/product and the static app key from
     FanDuel's JS bundle sent as ``Authorization: Basic <key>`` (configure
     via FANDUEL_BASIC_AUTH — capture it once from DevTools on the login
     request; unlike session tokens it does not expire).
  2. On success the session token comes back in the body (or an
     x-authentication response header) and is sent as ``x-authentication``
     on subsequent requests. The JWT's exp claim is ~1 hour out.
  3. If FanDuel doesn't recognize the device it demands a verification code
     (emailed to the account) — surfaced here as FanDuelMFARequired; submit
     the code via submit_mfa_code() and login is retried. Once the device
     is verified, later logins skip the code.
  4. ensure_token() re-logins automatically with the stored credentials
     whenever the token is stale, so the hourly expiry never surfaces.

If FanDuel's bot protection (PerimeterX) rejects the request outright,
FanDuelBotBlocked is raised — that layer can't be negotiated with from a
plain HTTP client, and the manual-token path remains the fallback.
"""

import logging
import time
from typing import Optional

import httpx
import jwt

logger = logging.getLogger(__name__)

FD_SESSION_URL = "https://api.fanduel.com/sessions"
FD_MFA_URL = "https://api.fanduel.com/users/mfa/new-device"

BROWSER_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Origin": "https://sportsbook.fanduel.com",
    "Referer": "https://sportsbook.fanduel.com/",
}

_MFA_MARKERS = ("mfa", "new-device", "new_device", "verification", "verify")
_BOT_MARKERS = ("perimeterx", "px-captcha", "_px", "captcha", "blockscript")


class FanDuelAuthError(Exception):
    """Login failed for a reason the caller can show to the user."""


class FanDuelMFARequired(FanDuelAuthError):
    """FanDuel wants a device-verification code (emailed to the account)."""


class FanDuelBotBlocked(FanDuelAuthError):
    """Bot protection rejected the request before credentials were checked."""


class FanDuelAuth:
    """Manages FanDuel session tokens for one user."""

    def __init__(
        self,
        email: str,
        password: str,
        basic_auth: str = "",
        product: str = "SB",
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.email = email
        self.password = password
        self.basic_auth = basic_auth.removeprefix("Basic ").strip()
        self.product = product
        self._transport = transport  # injected in tests
        self._token: Optional[str] = None
        self._token_exp: float = 0

    @property
    def token(self) -> Optional[str]:
        return self._token

    @property
    def can_relogin(self) -> bool:
        return bool(self.email and self.password)

    @property
    def is_expired(self) -> bool:
        if not self._token:
            return True
        # Refresh 5 minutes before actual expiry
        return time.time() > (self._token_exp - 300)

    def _headers(self) -> dict:
        headers = dict(BROWSER_HEADERS)
        if self.basic_auth:
            headers["Authorization"] = f"Basic {self.basic_auth}"
        return headers

    async def login(self) -> str:
        """Authenticate with email/password and store the session token.

        Raises FanDuelMFARequired when a device-verification code is needed,
        FanDuelBotBlocked when bot protection intervenes, FanDuelAuthError
        for anything else (bad credentials, unexpected response shape).
        """
        async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
            resp = await client.post(
                FD_SESSION_URL,
                json={
                    "email": self.email,
                    "password": self.password,
                    "product": self.product,
                },
                headers=self._headers(),
            )
        return self._handle_login_response(resp)

    def _handle_login_response(self, resp: httpx.Response) -> str:
        body_text = resp.text[:2000]
        lowered = body_text.lower()

        if resp.status_code == 403 and any(m in lowered for m in _BOT_MARKERS):
            raise FanDuelBotBlocked(
                "FanDuel's bot protection blocked the login attempt from this "
                "network. Direct login isn't possible here — use the manual "
                "token path instead."
            )
        if resp.status_code in (401, 403, 409, 428) and any(
            m in lowered for m in _MFA_MARKERS
        ):
            raise FanDuelMFARequired(
                "FanDuel emailed a verification code to this account "
                "(new device). Submit it to finish logging in."
            )
        if resp.status_code == 401:
            raise FanDuelAuthError(f"Login rejected (401): {body_text[:300]}")
        if not resp.is_success:
            raise FanDuelAuthError(
                f"Login failed ({resp.status_code}): {body_text[:300]}"
            )

        try:
            data = resp.json()
        except ValueError:
            data = {}

        sessions = data.get("sessions") or []
        token = (
            resp.headers.get("x-authentication")
            or resp.headers.get("x-auth-token")
            or data.get("token")
            or data.get("accessToken")
            or data.get("sessionToken")
            or (sessions[0].get("id") if sessions else None)
        )
        if not token:
            logger.error(
                "No token in login response. Status=%s keys=%s headers=%s",
                resp.status_code, list(data.keys()), list(resp.headers.keys()),
            )
            raise FanDuelAuthError(
                "Login succeeded but no session token was found in the "
                f"response (keys: {list(data.keys())})"
            )

        self._set_token(token)
        logger.info("FanDuel login successful")
        return token

    async def submit_mfa_code(self, code: str) -> str:
        """Verify this device with the emailed code, then retry login.

        The request shape mirrors the captured new-device endpoint; if
        FanDuel rejects it, the response body is included in the error so
        one DevTools capture of the browser's MFA POST is enough to adjust.
        """
        async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
            resp = await client.post(
                FD_MFA_URL,
                json={
                    "email": self.email,
                    "code": code.strip(),
                    "product": self.product,
                },
                headers=self._headers(),
            )
        if not resp.is_success:
            raise FanDuelAuthError(
                f"MFA verification failed ({resp.status_code}): {resp.text[:300]}"
            )
        return await self.login()

    async def ensure_token(self, force: bool = False) -> str:
        """Get a valid token, re-logging in when stale (or forced)."""
        if force or self.is_expired:
            if not self.can_relogin:
                raise FanDuelAuthError(
                    "Session token expired and no stored credentials to "
                    "re-login — log in with email/password or paste a fresh "
                    "token."
                )
            await self.login()
        return self._token

    def set_manual_token(self, token: str) -> None:
        """Set a manually-captured token (from browser DevTools)."""
        self._set_token(token)

    def _set_token(self, token: str) -> None:
        self._token = token
        try:
            # Decode without verification to read exp claim
            payload = jwt.decode(token, options={"verify_signature": False})
            self._token_exp = payload.get("exp", time.time() + 3600)
            logger.info(
                "Token expires at %s (%ds)",
                self._token_exp, int(self._token_exp - time.time()),
            )
        except jwt.DecodeError:
            # Opaque (non-JWT) session id — assume the usual 1h lifetime.
            self._token_exp = time.time() + 3600
