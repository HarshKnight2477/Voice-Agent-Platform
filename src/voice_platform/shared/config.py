"""Typed application settings, loaded from environment variables and an optional .env file.

Secrets are wrapped in SecretStr so they never appear in logs or reprs.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Environment = Environment.LOCAL
    app_name: str = "voice-platform"
    log_level: str = "INFO"
    log_json: bool = True

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)

    # Runtime connection: the restricted app role, subject to row-level security.
    database_url: PostgresDsn
    # Owner connection used only by migrations and `db-setup`. Never used by the API.
    migration_database_url: PostgresDsn | None = None
    # Password assigned to the app role by `db-setup` (must match DATABASE_URL).
    app_db_password: SecretStr | None = None
    redis_url: RedisDsn

    app_secret_key: SecretStr

    # Where the dashboard lives; used to build links in account emails.
    public_app_url: str = "http://127.0.0.1:3000"
    # Browser origins allowed to call the API with cookies. Defaults to public_app_url.
    cors_allowed_origins: list[str] = Field(default_factory=list)
    # Requests per minute per signed-in session (or per IP when signed out).
    api_rate_limit_per_minute: int = Field(default=600, ge=10)
    session_ttl_hours: int = Field(default=24 * 14, ge=1, le=24 * 90)
    # Secure cookies need HTTPS. Defaults on; .env sets it false only for local http.
    cookie_secure: bool = True

    @model_validator(mode="after")
    def _check_production_safety(self) -> Settings:
        if self.app_env in (Environment.STAGING, Environment.PRODUCTION):
            if len(self.app_secret_key.get_secret_value()) < 32:
                raise ValueError("APP_SECRET_KEY must be at least 32 characters outside local/test")
            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be true outside local/test")
        return self

    @property
    def allowed_origins(self) -> list[str]:
        return self.cors_allowed_origins or [self.public_app_url.rstrip("/")]

    @property
    def is_production(self) -> bool:
        return self.app_env is Environment.PRODUCTION


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Tests override via FastAPI dependency overrides."""
    return Settings()  # values come from the environment / .env
