from __future__ import annotations

import pytest
from pydantic import ValidationError

from voice_platform.agents.config_schema import AgentConfig
from voice_platform.agents.service import canonical_hash
from voice_platform.agents.templates import TEMPLATES


def base(**over: object) -> dict[str, object]:
    return {"business": {"name": "Joe's Diner", "timezone": "Asia/Kolkata"}, **over}


@pytest.mark.parametrize("key", list(TEMPLATES))
def test_every_template_builds_a_valid_config(key: str) -> None:
    cfg = TEMPLATES[key].build_config("Joe's Diner", "America/New_York")
    assert cfg.business.name == "Joe's Diner"
    assert cfg.tools, "templates ship with tools"


def test_restaurant_template_grants_explicit_scopes() -> None:
    cfg = TEMPLATES["restaurant_receptionist"].build_config("x", "UTC")
    scopes = {t.name: t.scopes for t in cfg.tools}
    assert scopes["create_reservation"] == ["reservations:create"]
    assert scopes["check_availability"] == ["reservations:read"]  # read does not imply write


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"language": "english"}, "language"),
        ({"business": {"name": "x", "timezone": "Mars/Base"}}, "time zone"),
        ({"escalation": {"transfer_number": "555-1234"}}, "E.164"),
        ({"advanced": {"llm": {"model": "gpt-4o"}}}, "model must be one of"),
        ({"tools": [{"name": "Bad Name"}]}, "lowercase"),
        ({"tools": [{"name": "get_menu"}, {"name": "get_menu"}]}, "once"),
        ({"unknown_field": 1}, "Extra inputs"),
        ({"business": {"name": "x", "hours": {"mon": [{"open": "18:00", "close": "09:00"}]}}},
         "close must be after open"),
        ({"advanced": {"endpointing_min_delay_ms": 2000, "endpointing_max_delay_ms": 1000}},
         "endpointing"),
    ],
)  # fmt: skip
def test_invalid_configs_are_rejected(override: dict[str, object], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        AgentConfig.model_validate(base(**override))


def test_hash_is_stable_regardless_of_key_order() -> None:
    assert canonical_hash({"a": 1, "b": [1, 2]}) == canonical_hash({"b": [1, 2], "a": 1})
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})
