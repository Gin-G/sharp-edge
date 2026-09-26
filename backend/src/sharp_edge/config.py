"""Application configuration — reads from env vars / .env file."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    # Database — SQLite for local dev, PostgreSQL for production (CNPG)
    # sqlite:  "sqlite:///~/.sharp-edge/bets.db"
    # postgres: "postgresql://user:pass@pgdb-rw.sharp-edge.svc:5432/sharpedge"
    database_url: str = "sqlite:///~/.sharp-edge/bets.db"

    # FanDuel
    fanduel_email: str = ""
    fanduel_password: str = ""
    fanduel_state: str = "CO"
    fanduel_api_key: str = "FhMFpcPWXMeyZxOx"
    # Static app key from FanDuel's JS bundle, sent as "Authorization: Basic"
    # on the /sessions login call. Public (it ships to every browser that
    # loads the login page) and it doesn't expire like session tokens, so it
    # is defaulted rather than configured. Decodes to a client id with an
    # empty secret. Override via FANDUEL_BASIC_AUTH if FanDuel rotates it —
    # recapture from DevTools on the POST to api.fanduel.com/sessions.
    fanduel_basic_auth: str = "ODc2YmQzOTE3ZWE3NjYwMjZhNjg5YzY2MTE5OGQxMmU6"

    # DraftKings
    #
    # Odds are deliberately not fetched from DraftKings: every host and path of
    # its public board answers 403 from Akamai's edge before reaching
    # DraftKings, from a dev machine and a datacenter egress alike, so prices
    # come through the aggregator below instead.
    #
    # Login and bet sync go through a real headless browser rather than an HTTP
    # client, because DraftKings' /v1/auth/* endpoints sit behind Akamai Bot
    # Manager: the _abck cookie only validates once Akamai's sensor JS has
    # POSTed telemetry, which no HTTP client can produce. Measured — a primed
    # cookie jar with browser headers and the correct Origin still comes back
    # "Access Denied". See draftkings/browser.py.
    #
    # No credentials here: they are entered per-user in the UI and never
    # persisted, exactly as FanDuel's are.
    draftkings_state: str = "CO"
    # Headed, and not as a debugging convenience — Akamai rejects headless
    # Chromium outright. Measured against the live login page:
    #
    #     headless-shell (Playwright default)   403 Access Denied
    #     new headless mode (channel=chromium)  403 Access Denied
    #     headed (channel=chromium)             200, form renders
    #
    # Headed needs a display, so the container runs the process under Xvfb.
    # Setting this True will get you blocked, not merely unsupported.
    draftkings_headless: bool = False
    # Generous on purpose: this covers a real page load plus a React app
    # settling over whatever the pod's egress looks like. A tight timeout here
    # surfaces as "login failed" when the truth is "the page was still coming".
    draftkings_timeout_ms: int = 45000

    # The Odds API — every book's price for a prop, through one licensed feed.
    #
    # This is how DraftKings (and BetMGM, and Caesars) get priced, and it makes
    # line shopping free since one call returns them all. Free key from
    # the-odds-api.com; without one the multi-book board is simply absent and
    # the FanDuel board carries on unaffected.
    #
    # Quota is the binding constraint, not rate limits: player props cost one
    # credit per market per event, so a 15-game slate is ~15-30 credits a
    # refresh against a 500-credit free month. oddsapi.client.QUOTA_FLOOR holds
    # a reserve back so the board degrades to stale prices instead of blanking.
    odds_api_key: str = ""
    # Comma-separated. Narrowing this is the cheapest way to cut spend.
    odds_api_books: str = "draftkings,fanduel,betmgm,caesars"

    # Anthropic keys are supplied per-request by each visitor (chat spends
    # their credits, not the operator's), so none is configured server-side.

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"
    # Uvicorn's auto-reloader. Off by default because this same entry point
    # runs in production, where it was doing real harm: WatchFiles polls /app
    # continuously, the reloader adds a second process, and any write under
    # the watched tree restarts the worker — which throws away the in-memory
    # day cache and sends the batter screen back to the start of a warm-up
    # that takes minutes of sequential network fetches. Set RELOAD=1 (or
    # reload=true in .env) for local development.
    reload: bool = False
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:3000"]

    # Sessions — signed cookie used to scope every user's data.
    # Override in production via SESSION_SECRET env var; generate with
    # `python -c 'import secrets; print(secrets.token_urlsafe(32))'`.
    session_secret: str = "dev-secret-change-me"
    session_cookie_name: str = "sharp_edge_sid"
    session_max_age: int = 60 * 60 * 24 * 365  # 1 year

    # MCP server is single-user (runs on the operator's machine), so it uses
    # a fixed user_id rather than HTTP sessions.
    mcp_user_id: str = "mcp-local"

    # NFL projections come from the NFL-API service rather than being refit
    # here — it already runs the nfl_projections model on a weekly cron and
    # holds the schedule with nflverse's closing lines. Public read-only API,
    # so no credential; override for a local instance.
    nfl_api_base: str = "https://nfl-api.nickknows.net"

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def odds_api_books_list(self) -> list[str]:
        """``odds_api_books`` as the aggregator wants it — lowercased keys,
        blanks dropped, so a stray trailing comma in .env can't request a book
        named empty string."""
        return [b.strip().lower() for b in self.odds_api_books.split(",") if b.strip()]


settings = Settings()
