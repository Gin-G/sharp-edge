"""The aggregator feed: pivoting, devigging, quota, and multi-book pricing."""

import httpx
import pytest

from sharp_edge import books, pricing
from sharp_edge.oddsapi import props
from sharp_edge.oddsapi.client import (
    QUOTA_FLOOR,
    OddsAPI,
    OddsAPIError,
    OddsAPIQuotaExhausted,
    _same_day,
)


def _norm(s):
    return s.lower().strip()


def _event(bookmakers):
    return {
        "id": "evt1",
        "commence_time": "2026-09-23T23:05:00Z",
        "home_team": "Boston Red Sox",
        "away_team": "Oakland Athletics",
        "bookmakers": bookmakers,
    }


def _hits_market(over=-180, under=140, player="Rafael Devers"):
    return {
        "key": "batter_hits",
        "outcomes": [
            {"name": "Over", "description": player, "price": over, "point": 0.5},
            {"name": "Under", "description": player, "price": under, "point": 0.5},
        ],
    }


# --------------------------------------------------------------------------
# Devig
# --------------------------------------------------------------------------

def test_devig_removes_the_margin_and_reports_it():
    # -180 / +140 implies .643 + .417 = 1.060 — a six-point hold.
    fair = props.two_way_devig(-180, 140)
    assert fair["overround"] == pytest.approx(1.060, abs=1e-3)
    assert fair["p_over"] + fair["p_under"] == pytest.approx(1.0, abs=1e-3)
    # The favourite stays the favourite, just without the vig on top.
    assert fair["p_over"] == pytest.approx(0.607, abs=1e-3)


def test_devig_needs_both_sides():
    """A one-sided quote is exactly what this cannot improve on, so it says so
    rather than inventing a margin."""
    assert props.two_way_devig(-180, None) is None
    assert props.two_way_devig(None, 140) is None


# --------------------------------------------------------------------------
# Pivot
# --------------------------------------------------------------------------

def test_parse_event_pivots_to_book_market_player():
    parsed = props.parse_event(
        _event([{"key": "draftkings", "markets": [_hits_market()]}]),
        {"batter_hits": "hits"},
        _norm,
    )
    entry = parsed["draftkings"]["hits"]["rafael devers"]
    assert entry["over"] == -180
    assert entry["under"] == 140
    assert entry["line"] == 0.5
    assert entry["event"] == "Oakland Athletics @ Boston Red Sox"
    assert entry["p_over"] == pytest.approx(0.607, abs=1e-3)


def test_parse_event_keeps_books_separate():
    """The whole point of the aggregator: the same leg at two prices."""
    parsed = props.parse_event(
        _event([
            {"key": "draftkings", "markets": [_hits_market(over=-180)]},
            {"key": "fanduel", "markets": [_hits_market(over=-155)]},
        ]),
        {"batter_hits": "hits"},
        _norm,
    )
    assert parsed["draftkings"]["hits"]["rafael devers"]["over"] == -180
    assert parsed["fanduel"]["hits"]["rafael devers"]["over"] == -155


def test_yes_no_folds_to_the_over_side():
    """Anytime-TD is quoted Yes/No and to-record-a-hit Over/Under, but both ask
    whether the thing happens — so both land on `over`."""
    parsed = props.parse_event(
        _event([{"key": "draftkings", "markets": [{
            "key": "player_anytime_td",
            "outcomes": [
                {"name": "Yes", "description": "Bijan Robinson", "price": -110},
                {"name": "No", "description": "Bijan Robinson", "price": -120},
            ],
        }]}]),
        {"player_anytime_td": "anytime_td"},
        _norm,
    )
    entry = parsed["draftkings"]["anytime_td"]["bijan robinson"]
    assert entry["over"] == -110
    assert entry["under"] == -120


def test_unrequested_markets_are_dropped():
    parsed = props.parse_event(
        _event([{"key": "draftkings", "markets": [
            _hits_market(),
            {"key": "batter_total_bases", "outcomes": [
                {"name": "Over", "description": "Rafael Devers", "price": 100},
            ]},
        ]}]),
        {"batter_hits": "hits"},
        _norm,
    )
    assert set(parsed["draftkings"]) == {"hits"}


def test_outcomes_without_a_player_are_skipped():
    """A market-level outcome carries no `description`; it is not a prop."""
    parsed = props.parse_event(
        _event([{"key": "draftkings", "markets": [{
            "key": "batter_hits",
            "outcomes": [{"name": "Over", "price": -180, "point": 0.5}],
        }]}]),
        {"batter_hits": "hits"},
        _norm,
    )
    assert parsed.get("draftkings", {}).get("hits", {}) == {}


# --------------------------------------------------------------------------
# Screen-facing projection
# --------------------------------------------------------------------------

def test_hit_odds_projects_one_book_onto_the_screens_shape():
    slate = props.parse_event(
        _event([{"key": "draftkings", "markets": [_hits_market()]}]),
        {"batter_hits": "hits"},
        _norm,
    )
    out = props.hit_odds(slate, "draftkings")
    assert out["rafael devers"]["odds"] == -180
    assert out["rafael devers"]["devig_p"] == pytest.approx(0.607, abs=1e-3)
    # No bet-slip handles exist in an aggregator quote, and the screens read
    # these keys — so they are present and explicitly None rather than absent.
    assert out["rafael devers"]["market_id"] is None
    assert out["rafael devers"]["selection_id"] is None


def test_hit_odds_for_a_book_that_did_not_quote_is_empty_not_an_error():
    slate = props.parse_event(
        _event([{"key": "draftkings", "markets": [_hits_market()]}]),
        {"batter_hits": "hits"},
        _norm,
    )
    assert props.hit_odds(slate, "betmgm") == {}


def test_books_in_ignores_the_meta_key():
    assert props.books_in({"draftkings": {}, "fanduel": {}, "_meta": {}}) == [
        "draftkings", "fanduel"
    ]


# --------------------------------------------------------------------------
# Quota
# --------------------------------------------------------------------------

def test_quota_is_read_off_the_response_headers():
    api = OddsAPI("k")
    api._read_quota(httpx.Response(200, headers={
        "x-requests-remaining": "431", "x-requests-used": "69",
    }))
    assert (api.remaining, api.used) == (431, 69)


def test_quota_floor_stops_the_fetcher_before_the_month_is_spent():
    api = OddsAPI("k")
    api.remaining = QUOTA_FLOOR
    with pytest.raises(OddsAPIQuotaExhausted):
        api._check_quota()


def test_quota_check_passes_before_any_call_has_landed():
    """`remaining` is None until the first response — which is not zero, and
    must not be treated as it."""
    OddsAPI("k")._check_quota()


def test_a_missing_key_fails_loudly_at_construction():
    with pytest.raises(OddsAPIError):
        OddsAPI("")


@pytest.mark.asyncio
async def test_401_is_reported_as_key_or_quota():
    api = OddsAPI("k", transport=httpx.MockTransport(
        lambda req: httpx.Response(401, text="unauthorized")
    ))
    async with httpx.AsyncClient(transport=api._transport) as client:
        with pytest.raises(OddsAPIError, match="quota"):
            await api._get(client, "/sports/x/events", {})


@pytest.mark.asyncio
async def test_one_events_failure_does_not_sink_the_slate():
    """A game whose props are not posted yet costs that game's prices only."""
    api = OddsAPI("k", transport=httpx.MockTransport(
        lambda req: httpx.Response(404, text="nope")
    ))
    async with httpx.AsyncClient(transport=api._transport) as client:
        assert await api.fetch_event_props(client, "s", "e", ["batter_hits"]) == {}


# --------------------------------------------------------------------------
# Date handling
# --------------------------------------------------------------------------

def test_same_day_accepts_a_late_start_that_stamps_as_tomorrow_utc():
    from datetime import date

    # 01:05 UTC on the 24th is a 7:05pm first pitch on the 23rd in Denver.
    assert _same_day("2026-09-24T01:05:00Z", date(2026, 9, 23))
    assert _same_day("2026-09-23T17:05:00Z", date(2026, 9, 23))
    assert not _same_day("2026-09-25T17:05:00Z", date(2026, 9, 23))
    assert not _same_day(None, date(2026, 9, 23))
    assert not _same_day("not-a-date", date(2026, 9, 23))


# --------------------------------------------------------------------------
# Multi-book pricing
# --------------------------------------------------------------------------

def _slate(dk=-180, fd=-155):
    return props.parse_event(
        _event([
            {"key": "draftkings", "markets": [_hits_market(over=dk)]},
            {"key": "fanduel", "markets": [_hits_market(over=fd)]},
        ]),
        {"batter_hits": "hits"},
        _norm,
    )


def test_attach_book_prices_names_the_book_paying_most():
    rows = [{"batter": "Rafael Devers", "model_p": 0.70}]
    pricing.attach_book_prices(rows, _slate(dk=-180, fd=-155), market="hits")
    r = rows[0]
    assert set(r["books"]) == {"draftkings", "fanduel"}
    # -155 pays more than -180 for the same outcome.
    assert r["best_book"] == "fanduel"
    assert r["best_odds"] == -155
    assert r["best_ev"] > r["books"]["draftkings"]["ev"]


def test_attach_book_prices_computes_edge_against_the_model():
    rows = [{"batter": "Rafael Devers", "model_p": 0.70}]
    pricing.attach_book_prices(rows, _slate(), market="hits")
    dk = rows[0]["books"]["draftkings"]
    # -180 implies 64.3%; the model says 70%, so ~5.7 points of edge.
    assert dk["edge_pts"] == pytest.approx(5.7, abs=0.2)
    assert dk["devig_p"] == pytest.approx(0.607, abs=1e-3)


def test_attach_book_prices_survives_a_row_the_model_could_not_score():
    """Ranking is on price, not EV, precisely so an unscored row still gets a
    best book rather than dropping out of the comparison."""
    rows = [{"batter": "Rafael Devers", "model_p": None}]
    pricing.attach_book_prices(rows, _slate(), market="hits")
    assert rows[0]["best_book"] == "fanduel"
    assert "ev" not in rows[0]["books"]["draftkings"]


def test_attach_book_prices_is_explicit_when_nobody_quoted_the_player():
    rows = [{"batter": "Nobody At All", "model_p": 0.70}]
    pricing.attach_book_prices(rows, _slate(), market="hits")
    assert rows[0]["books"] == {}
    assert rows[0]["best_book"] is None
    assert rows[0]["best_odds"] is None


def test_attach_book_prices_ignores_the_meta_key():
    """_meta rides in the slate alongside the books and is not one."""
    slate = {**_slate(), "_meta": {"quota_remaining": 400}}
    rows = [{"batter": "Rafael Devers", "model_p": 0.70}]
    pricing.attach_book_prices(rows, slate, market="hits")
    assert "_meta" not in rows[0]["books"]


def test_attach_book_prices_leaves_the_fanduel_columns_alone():
    """Additive by design: the board must price exactly as it did before, so a
    missing Odds API key costs the comparison and nothing else."""
    rows = [{"batter": "Rafael Devers", "model_p": 0.70,
             "fd_odds": -175, "fd_market_id": "708.1", "fd_selection_id": "999"}]
    pricing.attach_book_prices(rows, _slate(), market="hits")
    assert rows[0]["fd_odds"] == -175
    assert rows[0]["fd_market_id"] == "708.1"
    assert rows[0]["fd_selection_id"] == "999"
