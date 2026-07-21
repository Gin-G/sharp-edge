"""Hermetic tests for FanDuel direct login, MFA, and token refresh.

All HTTP is mocked via httpx.MockTransport — no network, no credentials.
"""

import asyncio
import base64
import json
import time

import httpx
import jwt
import pytest

from sharp_edge.fanduel.auth import (
    FanDuelAuth,
    FanDuelAuthError,
    FanDuelBotBlocked,
    FanDuelMFARequired,
)
from sharp_edge.fanduel.client import FanDuelClient


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _jwt(exp_offset: int = 3600) -> str:
    return jwt.encode({"exp": int(time.time()) + exp_offset}, "k", algorithm="HS256")


def _auth(handler, **kw) -> FanDuelAuth:
    return FanDuelAuth(
        "user@example.com", "hunter2",
        basic_auth="c3RhdGljLWtleQ==",
        transport=httpx.MockTransport(handler),
        **kw,
    )


def test_login_success_sessions_shape():
    """DFS-style response: token under sessions[0].id."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth_header"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"sessions": [{"id": _jwt()}]})

    auth = _auth(handler)
    token = _run(auth.login())
    assert token and not auth.is_expired
    assert seen["auth_header"] == "Basic c3RhdGljLWtleQ=="
    assert seen["body"]["email"] == "user@example.com"


def test_login_success_header_token():
    def handler(request):
        return httpx.Response(200, json={}, headers={"x-authentication": _jwt()})

    auth = _auth(handler)
    assert _run(auth.login())


def test_login_opaque_token_gets_default_expiry():
    def handler(request):
        return httpx.Response(201, json={"sessions": [{"id": "not-a-jwt"}]})

    auth = _auth(handler)
    _run(auth.login())
    assert not auth.is_expired  # assumed 1h lifetime


def test_login_mfa_required():
    def handler(request):
        return httpx.Response(
            401, json={"error": "new_device_verification_required"}
        )

    with pytest.raises(FanDuelMFARequired):
        _run(_auth(handler).login())


def test_login_bot_blocked():
    def handler(request):
        return httpx.Response(403, text='{"blockScript": "/px/captcha.js"}')

    with pytest.raises(FanDuelBotBlocked):
        _run(_auth(handler).login())


def test_login_bad_credentials():
    def handler(request):
        return httpx.Response(401, json={"error": "invalid_credentials"})

    with pytest.raises(FanDuelAuthError):
        _run(_auth(handler).login())


def test_mfa_code_then_login():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if "mfa" in request.url.path:
            assert json.loads(request.content)["code"] == "123456"
            return httpx.Response(200, json={})
        return httpx.Response(201, json={"sessions": [{"id": _jwt()}]})

    auth = _auth(handler)
    token = _run(auth.submit_mfa_code("123456 "))
    assert token and calls == ["/users/mfa/new-device", "/sessions"]


def test_ensure_token_relogins_when_expired():
    tokens = [_jwt(-100), _jwt(3600)]  # first login yields an expired token

    def handler(request):
        return httpx.Response(201, json={"sessions": [{"id": tokens.pop(0)}]})

    auth = _auth(handler)

    async def flow():
        await auth.login()
        assert auth.is_expired
        return await auth.ensure_token()

    assert _run(flow()) and not auth.is_expired


def test_login_captures_refresh_token():
    def handler(request):
        return httpx.Response(
            201, json={"sessions": [{"id": _jwt(), "refreshToken": "R-1"}]}
        )

    auth = _auth(handler)
    _run(auth.login())
    assert auth.can_refresh and auth._refresh_token == "R-1"


def test_ensure_token_prefers_refresh_over_relogin():
    """A stale token renews from the refresh token without touching
    /sessions (so no credentials, no MFA)."""
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/refresh"):
            assert json.loads(request.content)["refreshToken"] == "R-1"
            return httpx.Response(200, json={"token": _jwt(3600), "refreshToken": "R-2"})
        return httpx.Response(201, json={"sessions": [{"id": _jwt(-100), "refreshToken": "R-1"}]})

    auth = _auth(handler)
    _run(auth.login())
    assert auth.is_expired
    _run(auth.ensure_token())
    assert paths == ["/sessions", "/sessions/refresh"]
    assert auth._refresh_token == "R-2" and not auth.is_expired  # rotated


def test_ensure_token_falls_back_to_relogin_when_refresh_fails():
    def handler(request):
        if request.url.path.endswith("/refresh"):
            return httpx.Response(401, json={"error": "refresh_expired"})
        return httpx.Response(201, json={"sessions": [{"id": _jwt(3600), "refreshToken": "R-x"}]})

    auth = _auth(handler)
    auth._token = _jwt(-100)
    auth._refresh_token = "stale-refresh"
    _run(auth.ensure_token())
    assert not auth.is_expired  # credential re-login succeeded


def test_state_roundtrip_preserves_refresh_token_not_password():
    def handler(request):
        return httpx.Response(201, json={"sessions": [{"id": _jwt(), "refreshToken": "R-9"}]})

    auth = _auth(handler)
    _run(auth.login())
    state = auth.to_state()
    assert "password" not in state and state["refresh_token"] == "R-9"

    restored = FanDuelAuth.from_state(
        state, basic_auth="x", transport=httpx.MockTransport(handler)
    )
    assert restored.can_refresh and restored.token == auth.token
    assert not restored.can_relogin  # password intentionally not persisted


def test_ensure_token_without_credentials_raises():
    auth = FanDuelAuth("", "")
    auth.set_manual_token(_jwt(-100))
    with pytest.raises(FanDuelAuthError):
        _run(auth.ensure_token())


def test_client_retries_after_401():
    """A token expiring mid-sync triggers one re-login and a retry."""
    state = {"logins": 0, "fetches": 0}

    def auth_handler(request):
        state["logins"] += 1
        return httpx.Response(201, json={"sessions": [{"id": _jwt()}]})

    def api_handler(request):
        state["fetches"] += 1
        if state["fetches"] == 1:
            return httpx.Response(401)
        assert request.headers["x-authentication"]  # fresh token applied
        return httpx.Response(200, json={"moreAvailable": False, "bets": []})

    auth = _auth(auth_handler)
    client = FanDuelClient(
        auth_token="stale", auth=auth,
        transport=httpx.MockTransport(api_handler),
    )
    data = _run(client.fetch_bets())
    assert data["bets"] == []
    assert state == {"logins": 1, "fetches": 2}
    _run(client.close())
