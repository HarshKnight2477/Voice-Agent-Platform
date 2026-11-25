from __future__ import annotations

import pytest
from pydantic import SecretStr

from voice_platform.shared.config import Environment, Settings


@pytest.fixture
def settings() -> Settings:
    """Settings that do not depend on the developer's .env file."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env=Environment.TEST,
        log_json=True,
        database_url="postgresql://test:test@127.0.0.1:5432/test",  # type: ignore[arg-type]
        redis_url="redis://127.0.0.1:6379/0",  # type: ignore[arg-type]
        app_secret_key=SecretStr("test-secret"),
    )
