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


def test_draftkings_is_fully_wired():
    """Login and sync go through a headless browser; odds come from the
    aggregator, since DraftKings' own board is Akamai-blocked."""
    dk = books.get_book("draftkings")
    assert dk.supports_login and dk.supports_sync
    assert dk.auth_factory and dk.state_factory and dk.client_factory
    assert dk.odds_api_key == "draftkings"
    # Nothing left unsupported, so nothing left to explain.
    assert dk.unsupported_reason == ""


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

    for key in ("fanduel", "draftkings"):
        assert by_key[key]["supports_login"] is True
        assert by_key[key]["supports_sync"] is True
        assert by_key[key]["supports_odds"] is True
        assert by_key[key]["unsupported_reason"] == ""


def test_draftkings_sync_without_a_session_is_a_400(client):
    r = client.post("/bets/sync?book=draftkings")
    assert r.status_code == 400
    assert "DraftKings" in r.json()["detail"]


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
