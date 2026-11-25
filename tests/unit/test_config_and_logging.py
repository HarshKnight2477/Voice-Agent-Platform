from __future__ import annotations

import json

import pytest
import structlog
from pydantic import SecretStr, ValidationError

from voice_platform.shared.config import Environment, Settings
from voice_platform.shared.logging import configure_logging, get_logger


def test_secret_is_not_exposed_in_repr(settings: Settings) -> None:
    assert "test-secret" not in repr(settings)
    assert settings.app_secret_key.get_secret_value() == "test-secret"


def test_short_secret_rejected_in_production(settings: Settings) -> None:
    with pytest.raises(ValidationError, match="APP_SECRET_KEY"):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            app_env=Environment.PRODUCTION,
            database_url=settings.database_url,
            redis_url=settings.redis_url,
            app_secret_key=SecretStr("short"),
        )


def test_missing_database_url_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, redis_url="redis://x", app_secret_key="k")  # type: ignore[call-arg, arg-type]


def test_logs_are_json_and_mask_sensitive_keys(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json_output=True, cache_loggers=False)
    try:
        get_logger("t").info("login.attempt", password="hunter2", api_key="k123", user="u1")
    finally:
        output = capsys.readouterr().out
        structlog.reset_defaults()
    line = json.loads(output.strip().splitlines()[-1])
    assert line["event"] == "login.attempt"
    assert line["password"] == "***"
    assert line["api_key"] == "***"
    assert line["user"] == "u1"
