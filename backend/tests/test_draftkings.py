"""The DraftKings adapter: session state, payload walking, normalisation.

Nothing here launches a browser or touches the network. The Playwright-driven
paths are exercised against fakes; what is tested is the logic that decides
what a page became and what a payload meant, which is where the bugs live.
"""

import json
import time

import pytest

from sharp_edge.books import BOOKS
from sharp_edge.draftkings import client as dk_client
from sharp_edge.draftkings.auth import (
    DraftKingsAuth,
    DraftKingsAuthError,
    _is_session_cookie,
    _read_expiry,
)


# --------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------

def _storage(cookies):
    return {"cookies": cookies, "origins": []}


def test_a_fresh_auth_is_not_authenticated():
    auth = DraftKingsAuth("a@b.c", "pw")
    assert auth.token is None
    assert auth.is_expired
    assert not auth.can_refresh
    # Credentials are in memory, so it can still get a session.
    assert auth.can_renew


def test_token_is_a_digest_not_a_credential():
    """Status responses must not leak live cookies, but 'is this user logged
    in' still has to be answerable."""
    auth = DraftKingsAuth("a@b.c", "pw")
    auth._storage = _storage([{"name": "dksession", "value": "SECRET-VALUE"}])
    token = auth.token
    assert token and token.startswith("dk_")
    assert "SECRET-VALUE" not in token


def test_the_same_cookies_give_a_stable_token():
    a, b = DraftKingsAuth("x", ""), DraftKingsAuth("x", "")
    cookies = _storage([{"name": "dksession", "value": "v"}])
    a._storage, b._storage = cookies, dict(cookies)
    assert a.token == b.token


def test_stored_cookies_are_what_survives_a_restart():
    """can_refresh is the honest answer to 'will this still work after a
    redeploy' — the password is never persisted."""
    auth = DraftKingsAuth("a@b.c", "pw")
    auth._storage = _storage([{"name": "dksession", "value": "v"}])
    revived = DraftKingsAuth.from_state(auth.to_state())
    assert revived.can_refresh
    assert not revived.can_relogin       # no password came across
    assert revived.token == auth.token


def test_to_state_never_carries_the_password():
    state = DraftKingsAuth("a@b.c", "hunter2").to_state()
    assert "hunter2" not in json.dumps(state)


def test_expiry_is_read_from_the_longest_lived_session_cookie():
    soon, later = time.time() + 3600, time.time() + 86400
    expires_at, assumed = _read_expiry(_storage([
        {"name": "dksession", "expires": soon},
        {"name": "dk_auth_token", "expires": later},
    ]))
    assert expires_at == pytest.approx(later)
    assert not assumed


def test_expiry_is_flagged_assumed_when_no_cookie_carries_one():
    """A pure session cookie has no expiry, so the countdown is a guess and
    the UI must be told not to state it as fact."""
    _, assumed = _read_expiry(_storage([{"name": "dksession", "value": "v"}]))
    assert assumed


def test_akamai_cookies_are_not_mistaken_for_the_session():
    """_abck and bm_sz ride along on every response; treating one as the
    login would make a blocked request look authenticated."""
    assert not _is_session_cookie("_abck")
    assert not _is_session_cookie("bm_sz")
    assert not _is_session_cookie("ak_bmsc")
    assert _is_session_cookie("dksession")
    assert _is_session_cookie("dk_auth_token")


def test_expiry_ignores_akamai_cookies_even_when_long_lived():
    """_abck is set a year out; reading it as the session expiry would keep
    reporting a valid login long after it died."""
    _, assumed = _read_expiry(_storage([
        {"name": "_abck", "expires": time.time() + 31536000},
    ]))
    assert assumed


@pytest.mark.asyncio
async def test_mfa_code_without_a_pending_login_is_refused():
    with pytest.raises(DraftKingsAuthError, match="No pending verification"):
        await DraftKingsAuth("a@b.c", "pw").submit_mfa_code("123456")


@pytest.mark.asyncio
async def test_mfa_after_the_browser_was_reaped_says_so():
    """The context is gone, so the code is stale — say that rather than
    silently starting a login the user didn't ask for."""
    auth = DraftKingsAuth("a@b.c", "pw", session_key="dk:gone")
    auth._mfa_required = True
    with pytest.raises(DraftKingsAuthError, match="expired"):
        await auth.submit_mfa_code("123456")


@pytest.mark.asyncio
async def test_login_without_credentials_fails_before_launching_anything():
    with pytest.raises(DraftKingsAuthError, match="required"):
        await DraftKingsAuth("", "").login()


# --------------------------------------------------------------------------
# Payload walking — the endpoint is discovered, so the shape is unknown
# --------------------------------------------------------------------------

def _bet(**over):
    base = {"betId": "1", "stake": 10.0, "status": "Won", "payout": 25.0}
    base.update(over)
    return base


def test_harvest_finds_bets_however_they_are_nested():
    payload = {"data": {"page": {"results": [_bet(betId="a"), _bet(betId="b")]}}}
    found = dk_client._harvest_bets(payload)
    assert {b["betId"] for b in found} == {"a", "b"}


def test_harvest_does_not_mistake_legs_for_bets():
    """A leg sits inside a bet; counting both would double the history."""
    payload = {"bets": [{
        "betId": "a", "stake": 10.0, "status": "Won",
        "legs": [{"id": "leg1", "selectionName": "x"}],
    }]}
    found = dk_client._harvest_bets(payload)
    assert len(found) == 1
    assert found[0]["betId"] == "a"


def test_harvest_ignores_objects_that_are_not_bets():
    payload = {"user": {"id": "u1", "name": "x"}, "config": {"id": "c"}}
    assert dk_client._harvest_bets(payload) == []


def test_dedupe_keeps_one_row_per_bet():
    """Scrolling refetches overlapping windows, so the same bet arrives more
    than once."""
    out = dk_client._dedupe([_bet(betId="a"), _bet(betId="a"), _bet(betId="b")])
    assert [b["betId"] for b in out] == ["a", "b"]


@pytest.mark.parametrize("names,expected", [
    ({"betId": "x"}, "x"),
    ({"bet_id": "x"}, "x"),
    ({"BetID": "x"}, "x"),
])
def test_first_is_case_and_underscore_insensitive(names, expected):
    assert dk_client._first(names, "betId") == expected


def test_first_skips_empty_values():
    assert dk_client._first({"a": "", "b": None, "c": "v"}, "a", "b", "c") == "v"


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

def _norm(raw):
    return dk_client.DraftKingsClient().normalize_bet(raw)


def test_a_win_records_profit_not_payout():
    """DraftKings' payout includes the stake back; recording it as profit
    would overstate every winning bet by its own stake."""
    row = _norm(_bet(stake=10.0, payout=25.0, status="Won"))
    assert row["status"] == "SETTLED_WIN"
    assert row["profit"] == 15.0


def test_a_loss_is_the_stake_back_out():
    row = _norm(_bet(stake=10.0, payout=0, status="Lost"))
    assert row["status"] == "SETTLED_LOSS"
    assert row["profit"] == -10.0


def test_a_push_settles_flat():
    row = _norm(_bet(stake=10.0, status="Push"))
    assert row["status"] == "VOID"
    assert row["profit"] == 0.0


def test_leg_count_decides_the_bet_type_when_nothing_declares_it():
    assert _norm(_bet())["bet_type"] == "straight"
    multi = _bet(legs=[{"selectionName": "a"}, {"selectionName": "b"}])
    assert _norm(multi)["bet_type"] == "parlay"


def test_a_declared_type_wins_over_the_leg_count():
    row = _norm(_bet(betType="Parlay", legs=[{"selectionName": "a"}]))
    assert row["bet_type"] == "parlay"


def test_legs_are_flattened_and_the_league_mapped():
    row = _norm(_bet(legs=[
        {"selectionName": "Aaron Judge", "marketName": "To Record A Hit",
         "eventName": "NYY @ BOS", "league": "MLB", "americanOdds": -150},
    ]))
    assert row["leg_count"] == 1
    assert row["league"] == "MLB"
    assert row["sport"] == "Baseball"
    assert "Aaron Judge" in row["bet_info"]
    assert json.loads(row["legs"])[0]["odds_american"] == -150.0


def test_the_raw_payload_is_always_kept():
    """The field mapping is a best guess against an uncaptured shape, so a
    wrong guess must cost a column rather than the row."""
    raw = _bet(somethingUnmapped={"a": 1})
    row = _norm(raw)
    assert json.loads(row["raw_json"])["somethingUnmapped"] == {"a": 1}
    assert row["source"] == "draftkings"
    assert row["sportsbook"] == "DraftKings"


def test_money_survives_string_formatting():
    assert dk_client._to_num("$1,250.50") == 1250.5
    assert dk_client._to_num("+150") == 150.0
    assert dk_client._to_num(None) is None
    assert dk_client._to_num("not a number") is None


def test_an_unrecognised_status_is_passed_through_not_swallowed():
    row = _norm(_bet(status="SomeNewStatus"))
    assert row["status"] == "SOMENEWSTATUS"


# --------------------------------------------------------------------------
# Registry wiring
# --------------------------------------------------------------------------

def test_the_registry_builds_a_draftkings_auth():
    auth = BOOKS["draftkings"].auth_factory("a@b.c", "pw", None)
    assert isinstance(auth, DraftKingsAuth)
    assert auth.can_relogin


def test_a_relogin_reuses_the_prior_browser_slot():
    """A new session key per login would orphan the previous context."""
    first = BOOKS["draftkings"].auth_factory("a@b.c", "pw", None)
    second = BOOKS["draftkings"].auth_factory("a@b.c", "pw", first)
    assert second.session_key == first.session_key


def test_the_client_is_built_from_the_stored_cookies_not_the_token():
    auth = DraftKingsAuth("a@b.c", "pw")
    auth._storage = _storage([{"name": "dksession", "value": "v"}])
    built = BOOKS["draftkings"].client_factory(auth.token, auth)
    assert built._storage == auth.storage_state
