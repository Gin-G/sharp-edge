"""The sportsbooks this app can talk to, and what each one can actually do.

Every book surface splits into four capabilities that fail independently:

    login     hold a session for one user
    sync      read that user's bet history
    odds      read prices
    betslip   build a link that loads a card into the slip

FanDuel has all four. DraftKings, as things stand, has exactly one, and the
reasons are worth recording next to the code rather than in a commit message:

    login/sync  possible, but the request shapes are not captured yet. The
                FanDuel modules were built from a real browser capture and say
                so; the same is needed here. See DRAFTKINGS_CAPTURE.md. The
                open question that shapes the whole adapter is whether a
                DraftKings session is a bearer token (as FanDuel's is, which is
                what FanDuelAuth's whole refresh design rests on) or a cookie.
    odds        not from DraftKings. Its public board answers 403 from Akamai's
                edge on every host and path tried, from a dev machine and a
                datacenter egress alike. Prices come from the aggregator in
                oddsapi/ instead, which licenses them.
    betslip     does not exist. DraftKings generates a share link only after a
                bet is placed; there is no public pre-placement equivalent to
                FanDuel's addToBetslip, so a card cannot be handed over loaded.
                The honest fallback is a link to the event page.

A capability that is absent is declared absent here, so the API answers "not
supported at this book" instead of failing somewhere deep in a request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol, runtime_checkable


@runtime_checkable
class BookAuth(Protocol):
    """What an auth adapter has to provide to be usable by the auth routes.

    ``FanDuelAuth`` is the reference implementation; a DraftKings adapter that
    satisfies this drops into the registry below with no route changes. The
    surface is deliberately the smaller half of FanDuelAuth — the parts the
    HTTP layer actually touches.
    """

    # Identity and lifecycle
    @property
    def token(self) -> Optional[str]: ...
    @property
    def is_expired(self) -> bool: ...
    @property
    def expires_in(self) -> int: ...
    @property
    def expiry_assumed(self) -> bool: ...
    @property
    def mfa_pending(self) -> bool: ...
    @property
    def can_refresh(self) -> bool: ...
    @property
    def can_renew(self) -> bool: ...

    async def login(self) -> str: ...
    async def submit_mfa_code(self, code: str) -> str: ...
    async def ensure_token(self, force: bool = False) -> str: ...

    # Persistence. to_state must never include the password.
    def to_state(self) -> dict: ...


@dataclass(frozen=True)
class Book:
    """One book's registry entry.

    A ``None`` factory means the capability is not implemented, which the
    routes surface as a 501 naming ``unsupported_reason`` — deliberately not a
    500, and deliberately not silence.
    """

    key: str
    name: str
    # (email, password, prior) -> BookAuth
    auth_factory: Optional[Callable] = None
    # (state: dict) -> BookAuth. Rebuilds a session persisted by to_state()
    # after a restart, without the password — which is never stored, and is
    # why a book with no refresh token cannot renew itself across one.
    state_factory: Optional[Callable] = None
    # (token, auth) -> client exposing fetch_all_settled_bets()/normalize_bet()
    client_factory: Optional[Callable] = None
    # The aggregator's key for this book, when its prices come from there.
    odds_api_key: Optional[str] = None
    unsupported_reason: str = ""

    @property
    def supports_login(self) -> bool:
        return self.auth_factory is not None

    @property
    def supports_sync(self) -> bool:
        return self.client_factory is not None


def _fanduel_auth(email: str, password: str, prior=None):
    from .config import settings
    from .fanduel.auth import FanDuelAuth

    return FanDuelAuth(
        email, password,
        basic_auth=settings.fanduel_basic_auth,
        state=settings.fanduel_state,
        # FanDuel keys device verification to the installation id, so reusing
        # a stored one is what stops every login triggering a fresh MFA code.
        installation_id=getattr(prior, "installation_id", None),
    )


def _fanduel_from_state(state: dict):
    from .config import settings
    from .fanduel.auth import FanDuelAuth

    auth = FanDuelAuth.from_state(state, basic_auth=settings.fanduel_basic_auth)
    auth.state = auth.state or settings.fanduel_state
    return auth


def _fanduel_client(token: str, auth=None):
    from .config import settings
    from .fanduel.client import FanDuelClient

    return FanDuelClient(auth_token=token, state=settings.fanduel_state, auth=auth)


BOOKS: dict[str, Book] = {
    "fanduel": Book(
        key="fanduel",
        name="FanDuel",
        auth_factory=_fanduel_auth,
        state_factory=_fanduel_from_state,
        client_factory=_fanduel_client,
        odds_api_key="fanduel",
    ),
    "draftkings": Book(
        key="draftkings",
        name="DraftKings",
        # Both left unset on purpose. Wiring a guessed request shape in here
        # would produce a login that fails in a way no error message explains;
        # an explicit 501 pointing at the capture doc is worth more.
        auth_factory=None,
        client_factory=None,
        odds_api_key="draftkings",
        unsupported_reason=(
            "DraftKings login and bet sync are not wired up yet — the session "
            "and bet-history request shapes still need capturing from a "
            "browser. See DRAFTKINGS_CAPTURE.md. DraftKings odds are already "
            "available via the multi-book board."
        ),
    ),
}

DEFAULT_BOOK = "fanduel"


def get_book(key: str) -> Book:
    """Look a book up, or raise ValueError naming the ones that exist."""
    book = BOOKS.get((key or "").strip().lower())
    if book is None:
        raise ValueError(
            f"unknown book {key!r} — known books: {', '.join(sorted(BOOKS))}"
        )
    return book


def session_key(book: str) -> str:
    """The sync_state key a book's session is persisted under.

    FanDuel keeps its historical ``fanduel_session`` rather than moving to a
    namespaced one, because a rename would log every existing user out for no
    benefit — the stored row is keyed by this string.
    """
    return f"{book}_session"
