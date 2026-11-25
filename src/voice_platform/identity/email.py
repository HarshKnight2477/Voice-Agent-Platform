"""Outgoing account emails behind a small port.

No email provider is connected yet. Locally, LocalDevEmailSender writes the link to the
console so sign-up and password reset can be tried end to end. It refuses to run in
staging or production, where a real provider must be configured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from voice_platform.shared.config import Environment, Settings
from voice_platform.shared.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class AccountEmail:
    to: str
    kind: str  # "verify_email" | "reset_password"
    link: str


class EmailSender(Protocol):
    async def send(self, email: AccountEmail) -> None: ...


class LocalDevEmailSender:
    def __init__(self, settings: Settings) -> None:
        if settings.app_env not in (Environment.LOCAL, Environment.TEST):
            raise RuntimeError("LocalDevEmailSender must not be used outside local/test")

    async def send(self, email: AccountEmail) -> None:
        # Local development only: the link contains a live single-use token.
        print(f"\n[dev email] {email.kind} for {email.to}: {email.link}\n")
        log.info("email.sent_dev", kind=email.kind)
