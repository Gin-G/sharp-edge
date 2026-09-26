"""The sportsbooks this app can talk to, and what each one can actually do.

Every book surface splits into four capabilities that fail independently:

    login     hold a session for one user
    sync      read that user's bet history
    odds      read prices
    betslip   build a link that loads a card into the slip

FanDuel has all four. DraftKings has three, and the reasons are worth
recording next to the code rather than in a commit message:

    login/sync  yes, but through a real browser rather than an HTTP client.
                Its /v1/auth/* endpoints are behind Akamai Bot Manager, whose
                _abck cookie only validates once the sensor JS has posted
                telemetry — and Akamai rejects headless Chromium too, so the
                browser runs headed under Xvfb. All measured; see DRAFTKINGS.md
                for the table and draftkings/browser.py for the mechanism.
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


def _draftkings_auth(email: str, password: str, prior=None):
    from .config import settings
    from .draftkings.auth import DraftKingsAuth

    return DraftKingsAuth(
        email, password,
        state=settings.draftkings_state,
        # Reuse the prior session key so a re-login lands on the same browser
        # context slot rather than orphaning one.
        session_key=getattr(prior, "session_key", None),
    )


def _draftkings_from_state(state: dict):
    from .draftkings.auth import DraftKingsAuth

    return DraftKingsAuth.from_state(state)


def _draftkings_client(token: str, auth=None):
    from .draftkings.client import DraftKingsClient

    # The token is a digest, not a credential — the session lives in the
    # stored cookies, which is what the browser context needs.
    return DraftKingsClient(
        storage_state=getattr(auth, "storage_state", None),
        auth=auth,
        session_key=f"{getattr(auth, 'session_key', 'dk')}:sync",
    )


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
        # Driven through a real headless browser, not an HTTP client. Not a
        # stylistic choice: DraftKings' /v1/auth/* endpoints are behind Akamai
        # Bot Manager, whose _abck cookie only validates after its sensor JS
        # posts telemetry. Measured from a primed cookie jar with browser
        # headers and the right Origin — still "Access Denied". The browser
        # satisfies it by being one. See draftkings/browser.py.
        auth_factory=_draftkings_auth,
        state_factory=_draftkings_from_state,
        client_factory=_draftkings_client,
        odds_api_key="draftkings",
    ),
}

DEFAULT_BOOK = "fanduel"


# Each book raises its own exception types, and the routes have to treat them
# alike: an MFA challenge is an MFA prompt whoever raised it. Resolved lazily
# into tuples rather than unified behind a base class, because retro-fitting a
# base onto FanDuel's working exceptions would churn code for no behaviour.


def mfa_errors() -> tuple:
    """Exceptions that mean "the book wants a verification code"."""
    from .draftkings.auth import DraftKingsMFARequired
    from .fanduel.auth import FanDuelMFARequired

    return (FanDuelMFARequired, DraftKingsMFARequired)


def blocked_errors() -> tuple:
    """Exceptions that mean bot protection refused before credentials were
    even considered — a 502, since the failure is not the user's."""
    from .draftkings.auth import DraftKingsBotBlocked
    from .fanduel.auth import FanDuelBotBlocked

    return (FanDuelBotBlocked, DraftKingsBotBlocked)


def unavailable_errors() -> tuple:
    """Exceptions that mean this deployment can't do it — Playwright or its
    Chromium missing from the image. A 501, not a failed login."""
    from .draftkings.browser import PlaywrightUnavailable

    return (PlaywrightUnavailable,)


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
