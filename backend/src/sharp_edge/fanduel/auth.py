"""FanDuel session authentication — direct login, new-device MFA, refresh.

Modeled on the token flow that worked for the Hydrow integration: log in
once with username/password, keep the returned refresh token, and mint new
access tokens from it so credentials (and MFA) are never needed again until
the refresh token itself expires. The whole session serializes to/from the
database so a pod restart reuses it instead of bouncing back to login.

Login flow (matches the browser's calls to api.fanduel.com):

  1. POST /sessions with email/password/product/location, the static app key
     from FanDuel's JS bundle sent as ``Authorization: Basic <key>``, and the
     browser's routing headers (vendor Accept type, X-Brand, X-Product-Region,
     X-Installation-Id, per-state Origin/Referer). All of these come from a
     capture of the real login — the plain application/json shape FanDuel's
     older DFS API accepted is not what the sportsbook client sends.
  2. On success the session token comes back in the body (or an
     x-authentication response header) and is sent as ``x-authentication``
     on subsequent requests. The JWT's exp claim is ~1 hour out. A refresh
     token, when present, is stored for step 4.
  3. If FanDuel doesn't recognize the device it demands a verification code
     (emailed to the account) — surfaced here as FanDuelMFARequired. The
     challenge response carries a short-lived new_device_token; submit the
     code via submit_mfa_code() and it goes back to /sessions together with
     that token, which is what returns the real session. Verification is
     keyed to X-Installation-Id, so keeping that id stable is what stops
     FanDuel asking again on every login.
  4. ensure_token() renews a stale token from the refresh token first
     (POST /sessions/refresh), falling back to a full credential re-login,
     so the hourly expiry never surfaces to the user.

If FanDuel's bot protection (PerimeterX) rejects the request outright,
FanDuelBotBlocked is raised — that layer can't be negotiated with from a
plain HTTP client, and the manual-token path remains the fallback.

NOTE: the request shapes here are confirmed against a real capture, but the
*response* shapes are not — they are parsed defensively across the field
names FanDuel and community clients have used (token / accessToken /
sessionToken / sessions[].id; refresh under refreshToken / refresh_token;
see _extract_tokens and _extract_device_token for the union handled). One
capture of a login response would let this be narrowed.

Unresolved: the browser also sends an x-px-context PerimeterX token, which
cannot be minted outside a real browser session. Whether FanDuel enforces
it on /sessions is untested — if it does, FanDuelBotBlocked is raised and
the manual-token path remains the fallback.
"""

import logging
import time
import uuid
from typing import Optional

import httpx
import jwt

FD_REFRESH_URL = "https://api.fanduel.com/sessions/refresh"

logger = logging.getLogger(__name__)

FD_SESSION_URL = "https://api.fanduel.com/sessions"

# The browser's login page is per-state: account.co.sportsbook.fanduel.com
# for Colorado, .nj. for New Jersey, and so on. Origin/Referer have to match
# or the request looks cross-site.
FD_ACCOUNT_ORIGIN = "https://account.{state}.sportsbook.fanduel.com"

# Captured from a real login. The vendor Accept type selects the trimmed user
# payload the web client asks for; x-brand / x-product-region are required
# routing headers.
BROWSER_HEADERS = {
    "Accept": "application/vnd.fanduel.reduced_user_view+json",
    "Content-Type": "application/json;charset=UTF-8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    "X-Brand": "FANDUEL",
    "X-Geo-Region": "",
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
        state: str = "CO",
        installation_id: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.email = email
        self.password = password
        self.basic_auth = basic_auth.removeprefix("Basic ").strip()
        self.product = product
        self.state = (state or "CO").upper()
        # Identifies this "device" to FanDuel. It has to stay stable across
        # logins — that's what makes a verified device stay verified, so it
        # is persisted with the session rather than regenerated per process.
        self.installation_id = installation_id or str(uuid.uuid4())
        self._transport = transport  # injected in tests
        self._token: Optional[str] = None
        self._token_exp: float = 0
        self._refresh_token: Optional[str] = None
        # Short-lived (~5 min) challenge token handed back when FanDuel wants
        # a device-verification code; posted back alongside the code.
        self._device_token: Optional[str] = None

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
    def mfa_pending(self) -> bool:
        """True between a device-verification challenge and its code."""
        return bool(self._device_token)

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
        origin = FD_ACCOUNT_ORIGIN.format(state=self.state.lower())
        headers = dict(BROWSER_HEADERS)
        headers.update({
            "Origin": origin,
            "Referer": f"{origin}/",
            "X-Installation-Id": self.installation_id,
            "X-Product-Region": self.state,
        })
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
                    "location": self.state,
                },
                headers=self._headers(),
            )
        return self._handle_login_response(resp)

    def _handle_login_response(self, resp: httpx.Response) -> str:
        body_text = resp.text[:2000]
        lowered = body_text.lower()

        try:
            data = resp.json()
        except ValueError:
            data = {}
        # The MFA challenge carries the device token we have to echo back
        # with the code, so capture it before the error branches below.
        device = self._extract_device_token(data)
        if device:
            self._device_token = device

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

    @staticmethod
    def _extract_device_token(data: dict) -> Optional[str]:
        sessions = data.get("sessions") or []
        first = sessions[0] if sessions else {}
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        return (
            data.get("new_device_token")
            or data.get("newDeviceToken")
            or first.get("new_device_token")
            or error.get("new_device_token")
        )

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
        """Finish a held login by posting the emailed code.

        Verification is not a separate endpoint: the code goes back to
        /sessions along with the new_device_token from the challenge, and
        that call returns the real session token. The device token is only
        valid for about five minutes, so a slow user has to start over.
        """
        if not self._device_token:
            raise FanDuelAuthError(
                "No pending device verification — log in again to request a "
                "new code."
            )
        async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
            resp = await client.post(
                FD_SESSION_URL,
                json={
                    "code": code.strip(),
                    "new_device_token": self._device_token,
                    "product": self.product,
                    "location": self.state,
                },
                headers=self._headers(),
            )
        token = self._handle_login_response(resp)
        self._device_token = None
        return token

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
            "state": self.state,
            "installation_id": self.installation_id,
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
            state=state.get("state") or "CO",
            installation_id=state.get("installation_id"),
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
