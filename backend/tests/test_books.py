"""The book registry, and the book-keyed auth routes built on it."""

import pytest
from fastapi.testclient import TestClient

from sharp_edge import books
from sharp_edge.api import app


@pytest.fixture
def client():
    return TestClient(app)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def test_unknown_book_names_the_ones_that_exist():
    with pytest.raises(ValueError, match="draftkings"):
        books.get_book("bovada")


def test_book_lookup_is_case_and_space_insensitive():
    assert books.get_book("  DraftKings ").key == "draftkings"


def test_fanduel_can_do_everything():
    fd = books.get_book("fanduel")
    assert fd.supports_login and fd.supports_sync
    assert fd.auth_factory and fd.state_factory and fd.client_factory


def test_draftkings_declares_what_it_cannot_do_and_why():
    """An absent capability is declared absent, so the API can answer 'not
    supported here' instead of failing deep inside a request."""
    dk = books.get_book("draftkings")
    assert not dk.supports_login
    assert not dk.supports_sync
    assert "capturing" in dk.unsupported_reason
    # Prices are a separate capability and DraftKings does have them — just
    # through the aggregator rather than its own board.
    assert dk.odds_api_key == "draftkings"


def test_fanduel_keeps_its_historical_session_key():
    """Renaming it would log every existing user out for no benefit."""
    assert books.session_key("fanduel") == "fanduel_session"
    assert books.session_key("draftkings") == "draftkings_session"


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def test_books_endpoint_describes_each_book(client):
    body = client.get("/books").json()
    assert body["default"] == "fanduel"
    by_key = {b["key"]: b for b in body["books"]}

    assert by_key["fanduel"]["supports_login"] is True
    assert by_key["draftkings"]["supports_login"] is False
    # Odds are available at DraftKings even though login is not.
    assert by_key["draftkings"]["supports_odds"] is True
    assert by_key["draftkings"]["unsupported_reason"]


def test_draftkings_login_refuses_with_501_not_a_crash(client):
    r = client.post(
        "/auth/draftkings/login", json={"email": "a@b.c", "password": "x"}
    )
    assert r.status_code == 501
    assert "DRAFTKINGS_CAPTURE.md" in r.json()["detail"]


def test_draftkings_sync_refuses_with_501(client):
    r = client.post("/bets/sync?book=draftkings")
    assert r.status_code == 501


def test_unknown_book_is_a_404(client):
    r = client.post("/auth/bovada/login", json={"email": "a@b.c", "password": "x"})
    assert r.status_code == 404


def test_status_for_a_book_nobody_is_logged_into(client):
    assert client.get("/auth/draftkings/status").json() == {"authenticated": False}


def test_the_original_fanduel_routes_still_answer(client):
    """The shipped frontend calls these un-namespaced paths; they delegate
    rather than redirect, because a 307 on a POST breaks somebody's client."""
    assert client.get("/auth/status").json() == {"authenticated": False}
    assert client.post("/auth/logout").json() == {"status": "ok"}


def test_sync_without_a_session_is_a_400_naming_the_book(client):
    r = client.post("/bets/sync")
    assert r.status_code == 400
    assert "FanDuel" in r.json()["detail"]
