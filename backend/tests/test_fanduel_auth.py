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
