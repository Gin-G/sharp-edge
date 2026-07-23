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

    # Anthropic keys are supplied per-request by each visitor (chat spends
    # their credits, not the operator's), so none is configured server-side.

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"
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

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")


settings = Settings()
