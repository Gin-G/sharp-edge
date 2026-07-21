"""FanDuel session authentication — direct login, new-device MFA, refresh.

Modeled on the token flow that worked for the Hydrow integration: log in
once with username/password, keep the returned refresh token, and mint new
access tokens from it so credentials (and MFA) are never needed again until
the refresh token itself expires. The whole session serializes to/from the
database so a pod restart reuses it instead of bouncing back to login.

Login flow (matches the browser's calls to api.fanduel.com):

  1. POST /sessions with email/password/product and the static app key from
     FanDuel's JS bundle sent as ``Authorization: Basic <key>`` (configure
     via FANDUEL_BASIC_AUTH — capture it once from DevTools on the login
     request; unlike session tokens it does not expire).
  2. On success the session token comes back in the body (or an
     x-authentication response header) and is sent as ``x-authentication``
     on subsequent requests. The JWT's exp claim is ~1 hour out. A refresh
     token, when present, is stored for step 4.
  3. If FanDuel doesn't recognize the device it demands a verification code
     (emailed to the account) — surfaced here as FanDuelMFARequired; submit
     the code via submit_mfa_code() and login is retried. Once the device
     is verified, later logins skip the code.
  4. ensure_token() renews a stale token from the refresh token first
     (POST /sessions/refresh), falling back to a full credential re-login,
     so the hourly expiry never surfaces to the user.

If FanDuel's bot protection (PerimeterX) rejects the request outright,
FanDuelBotBlocked is raised — that layer can't be negotiated with from a
plain HTTP client, and the manual-token path remains the fallback.

NOTE: the /sessions and /sessions/refresh response shapes are parsed
defensively across the field names FanDuel and community clients have used
(token / accessToken / sessionToken / sessions[].id; refresh under
refreshToken / refresh_token). One DevTools capture of the real login
response confirms which apply — see _extract_tokens for the union handled.
"""

import logging
import time
from typing import Optional

import httpx
import jwt

FD_REFRESH_URL = "https://api.fanduel.com/sessions/refresh"

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
        self._refresh_token: Optional[str] = None

    @property
    def token(self) -> Optional[str]:
        return self._token

    @property
    def can_relogin(self) -> bool:
        return bool(self.email and self.password)

    @property
    def can_refresh(self) -> bool:
        return bool(self._refresh_token)

    @property
    def can_renew(self) -> bool:
        """True when a stale token can be renewed without user input."""
        return self.can_refresh or self.can_relogin

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

        token, refresh = self._extract_tokens(resp, data)
        if not token:
            logger.error(
                "No token in login response. Status=%s keys=%s headers=%s",
                resp.status_code, list(data.keys()), list(resp.headers.keys()),
            )
            raise FanDuelAuthError(
                "Login succeeded but no session token was found in the "
                f"response (keys: {list(data.keys())})"
            )

        self._set_token(token, refresh)
        logger.info("FanDuel login successful (refresh_token=%s)", bool(refresh))
        return token

    @staticmethod
    def _extract_tokens(resp: httpx.Response, data: dict) -> tuple[Optional[str], Optional[str]]:
        """Pull (session_token, refresh_token) from a /sessions-style
        response, tolerating the field names FanDuel and community clients
        have variously used."""
        sessions = data.get("sessions") or []
        first = sessions[0] if sessions else {}
        token = (
            resp.headers.get("x-authentication")
            or resp.headers.get("x-auth-token")
            or data.get("token")
            or data.get("accessToken")
            or data.get("sessionToken")
            or first.get("id")
            or first.get("token")
        )
        refresh = (
            data.get("refreshToken")
            or data.get("refresh_token")
            or first.get("refreshToken")
            or first.get("refresh_token")
        )
        return token, refresh

    async def refresh(self) -> str:
        """Mint a fresh session token from the stored refresh token — no
        credentials, no MFA. Raises FanDuelAuthError if there's no refresh
        token or FanDuel rejects it (caller falls back to credential login)."""
        if not self._refresh_token:
            raise FanDuelAuthError("No refresh token available")
        async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
            resp = await client.post(
                FD_REFRESH_URL,
                json={"refreshToken": self._refresh_token, "product": self.product},
                headers=self._headers(),
            )
        if not resp.is_success:
            raise FanDuelAuthError(
                f"Token refresh failed ({resp.status_code}): {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        token, refresh = self._extract_tokens(resp, data)
        if not token:
            raise FanDuelAuthError("Refresh succeeded but returned no token")
        # FanDuel may or may not rotate the refresh token; keep the old one
        # if the response doesn't carry a new one.
        self._set_token(token, refresh or self._refresh_token)
        logger.info("FanDuel token refreshed")
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
        """Get a valid token, renewing when stale (or forced).

        Prefers the refresh token (silent, MFA-free) and falls back to a
        full credential re-login. Raises FanDuelAuthError only when neither
        renewal path is available."""
        if not (force or self.is_expired):
            return self._token
        if self.can_refresh:
            try:
                return await self.refresh()
            except FanDuelAuthError as e:
                logger.info("refresh failed (%s); trying credential login", e)
        if self.can_relogin:
            await self.login()
            return self._token
        raise FanDuelAuthError(
            "Session token expired and can't be renewed — log in with "
            "email/password or paste a fresh token."
        )

    # ------------------------------------------------------------------
    # Serialization — persist the session across restarts (no password)
    # ------------------------------------------------------------------

    def to_state(self) -> dict:
        """Serialize the session for storage. Excludes the password; the
        refresh token is what enables MFA-free renewal after a restart."""
        return {
            "email": self.email,
            "token": self._token,
            "token_exp": self._token_exp,
            "refresh_token": self._refresh_token,
            "product": self.product,
            "state": None,
        }

    @classmethod
    def from_state(
        cls,
        state: dict,
        password: str = "",
        basic_auth: str = "",
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> "FanDuelAuth":
        auth = cls(
            state.get("email", ""),
            password,
            basic_auth=basic_auth,
            product=state.get("product", "SB"),
            transport=transport,
        )
        auth._token = state.get("token")
        auth._token_exp = state.get("token_exp", 0) or 0
        auth._refresh_token = state.get("refresh_token")
        return auth

    def set_manual_token(self, token: str) -> None:
        """Set a manually-captured token (from browser DevTools)."""
        self._set_token(token)

    def _set_token(self, token: str, refresh: Optional[str] = None) -> None:
        self._token = token
        if refresh is not None:
            self._refresh_token = refresh
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
