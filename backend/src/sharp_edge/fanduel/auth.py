"""FanDuel session authentication — login and token refresh."""

import time
import logging
from typing import Optional

import httpx
import jwt

logger = logging.getLogger(__name__)

FD_SESSION_URL = "https://api.fanduel.com/sessions"
FD_MFA_URL = "https://api.fanduel.com/users/mfa/new-device"


class FanDuelAuth:
    """Manages FanDuel session tokens.

    Login flow (from browser capture):
      1. POST api.fanduel.com/sessions with email/password
      2. Receive JWT in response (x-authentication header on subsequent requests)
      3. Token exp claim ~1 hour TTL
      4. Refresh by re-posting to /sessions before expiry
    """

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self._token: Optional[str] = None
        self._token_exp: float = 0

    @property
    def token(self) -> Optional[str]:
        return self._token

    @property
    def is_expired(self) -> bool:
        if not self._token:
            return True
        # Refresh 5 minutes before actual expiry
        return time.time() > (self._token_exp - 300)

    async def login(self) -> str:
        """Authenticate with FanDuel and obtain a session token.

        NOTE: The exact request body format may need adjustment based on
        FanDuel's login endpoint. The browser uses a form POST.
        Capture the actual /sessions POST body from DevTools to confirm.
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                FD_SESSION_URL,
                json={
                    "email": self.email,
                    "password": self.password,
                    "product": "SB",
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Origin": "https://co.sportsbook.fanduel.com",
                },
            )
            resp.raise_for_status()
            data = resp.json()

            # Token location — check response body and headers
            # From the browser capture, the JWT is returned and then used as x-authentication
            token = (
                data.get("token")
                or data.get("accessToken")
                or data.get("sessionToken")
                or resp.headers.get("x-authentication")
            )

            if not token:
                # May need to check response structure more carefully
                logger.error(f"No token in login response. Keys: {list(data.keys())}")
                raise ValueError("Could not extract token from login response")

            self._set_token(token)
            logger.info("FanDuel login successful")
            return token

    async def ensure_token(self) -> str:
        """Get a valid token, refreshing if expired."""
        if self.is_expired:
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
            logger.info(f"Token expires at {self._token_exp} ({int(self._token_exp - time.time())}s)")
        except jwt.DecodeError:
            self._token_exp = time.time() + 3600
