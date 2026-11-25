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

    database_url: PostgresDsn
    redis_url: RedisDsn

    app_secret_key: SecretStr

    @model_validator(mode="after")
    def _check_production_safety(self) -> Settings:
        if self.app_env in (Environment.STAGING, Environment.PRODUCTION):
            if len(self.app_secret_key.get_secret_value()) < 32:
                raise ValueError("APP_SECRET_KEY must be at least 32 characters outside local/test")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env is Environment.PRODUCTION


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Tests override via FastAPI dependency overrides."""
    return Settings()  # values come from the environment / .env
