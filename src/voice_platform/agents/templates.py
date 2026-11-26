"""Industry templates. A template is data: default configuration for a new agent.

Templates never contain code. Businesses customize the copy in their agent's draft; the
template itself is untouched, so one engine serves every industry.
MVP ships two templates. More (dental, healthcare, real estate, hotel, ...) follow later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from voice_platform.agents.config_schema import AgentConfig


@dataclass(frozen=True)
class Template:
    key: str
    name: str
    industry: str
    description: str
    defaults: dict[str, Any]

    def build_config(self, business_name: str, timezone: str) -> AgentConfig:
        data = {**self.defaults, "business": {"name": business_name, "timezone": timezone}}
        return AgentConfig.model_validate(data)


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri")

TEMPLATES: dict[str, Template] = {
    t.key: t
    for t in (
        Template(
            key="generic_receptionist",
            name="Receptionist",
            industry="general",
            description="Answers questions about the business, takes messages and transfers "
            "callers to a person when needed.",
            defaults={
                "purpose": "Answer common questions, take messages, and connect callers "
                "to the right person.",
                "greeting": "Hi, thanks for calling. How can I help you today?",
                "tools": [
                    {"name": "get_hours"},
                    {"name": "transfer_to_human"},
                    {"name": "take_message", "scopes": ["messages:create"]},
                ],
            },
        ),
        Template(
            key="restaurant_receptionist",
            name="Restaurant Receptionist",
            industry="restaurant",
            description="Takes table reservations, answers menu and hours questions, and "
            "hands off to staff for anything else.",
            defaults={
                "purpose": "Book, change and cancel table reservations, and answer questions "
                "about hours, menu and location.",
                "greeting": "Hi, thanks for calling. Would you like to book a table, "
                "or can I help with something else?",
                "business_rules": [
                    "Reservations are for parties of 1 to 10. Larger groups: offer a callback.",
                    "Always confirm date, time, party size and name before booking.",
                ],
                "tools": [
                    {"name": "get_hours"},
                    {"name": "get_menu"},
                    {"name": "check_availability", "scopes": ["reservations:read"]},
                    {"name": "create_reservation", "scopes": ["reservations:create"]},
                    {"name": "cancel_reservation", "scopes": ["reservations:cancel"]},
                    {"name": "send_confirmation", "scopes": ["messages:send"]},
                    {"name": "transfer_to_human"},
                ],
            },
        ),
    )
}


def get_template(key: str) -> Template | None:
    return TEMPLATES.get(key)


__all__ = ["TEMPLATES", "Template", "get_template"]
