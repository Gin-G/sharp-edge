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

    # Anthropic (for chat endpoint)
    anthropic_api_key: str = ""

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:3000"]

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")


settings = Settings()
