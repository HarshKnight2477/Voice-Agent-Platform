"""Live tests against the local Ollama model. Free, but needs Ollama running with the model
pulled. Run with:  uv run pytest -m live_llm

These check what matters for a phone agent: the model calls tools when it should, answers
from tool results, stays short, and a full CallSession works end to end.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from voice_platform.agents.config_schema import AgentConfig
from voice_platform.agents.templates import TEMPLATES
from voice_platform.providers.llm.factory import build_llm_provider
from voice_platform.runtime.llm import LLMRequest, Message, StopReason, SystemBlock, ToolSpec
from voice_platform.runtime.session import CallSession, SessionIdentity, TurnAction
from voice_platform.shared.config import Settings, get_settings
from voice_platform.tools.base import ToolContext
from voice_platform.tools.builtin import BUILTIN_TOOLS
from voice_platform.tools.gateway import ToolGateway
from voice_platform.tools.registry import ToolRegistry

pytestmark = pytest.mark.live_llm


@pytest.fixture
def local_settings() -> Settings:
    s = get_settings()
    if s.llm_provider != "ollama":
        pytest.skip("LLM_PROVIDER is not ollama")
    try:
        httpx.get(f"{s.ollama_base_url}/api/tags", timeout=2).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("Ollama is not running")
    return s


class Sink:
    def __init__(self) -> None:
        self.saved: list[dict[str, str]] = []

    async def save(self, ctx: ToolContext, message: dict[str, str]) -> str:
        self.saved.append(message)
        return "ok"


class NullAudit:
    async def record(self, record: object) -> None:
        return None


async def test_model_calls_a_tool_then_answers(local_settings: Settings) -> None:
    llm = build_llm_provider(local_settings)
    tools = (
        ToolSpec(
            "get_hours",
            "Get opening hours and whether the business is open now.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
    )
    first = await llm.complete(
        LLMRequest(
            system=(SystemBlock("You answer a business phone line. Use tools for hours."),),
            messages=(Message("user", "Are you open right now?"),),
            tools=tools,
        )
    )
    assert first.stop_reason is StopReason.TOOL_USE
    assert first.tool_calls[0].name == "get_hours"


async def test_full_call_session_with_local_model(local_settings: Settings) -> None:
    base = TEMPLATES["generic_receptionist"].build_config("Joe's Diner", "Asia/Kolkata")
    data = base.model_dump(mode="json")
    days = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    data["business"]["hours"] = {d: [{"open": "00:00", "close": "23:59"}] for d in days}
    config = AgentConfig.model_validate(data)
    registry = ToolRegistry(BUILTIN_TOOLS)
    sink = Sink()
    session = CallSession(
        SessionIdentity(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "Front desk", "live-1"),
        config,
        build_llm_provider(local_settings),
        registry,
        ToolGateway(registry, NullAudit()),
        services={"message_sink": sink},
        clock=lambda: datetime.now(UTC),
    )
    session.greeting()

    started = time.perf_counter()
    r1 = await session.handle_user_turn("Hi, are you open right now?")
    first_turn_s = time.perf_counter() - started
    print(f"turn 1 events: {[e for e in r1.events if e['type'] in ('tool', 'llm_response')]}")
    assert r1.action is TurnAction.CONTINUE and r1.say
    assert "open" in r1.say.lower()  # from get_hours or the business facts; never guessed
    assert len(r1.say) <= 420
    print(f"\nturn 1 ({first_turn_s:.2f}s): {r1.say}")

    r2 = await session.handle_user_turn(
        "Can you take a message? I'm Asha and my number is +919800000000. "
        "Please ask the manager to call me back about a booking."
    )
    print(f"turn 2: {r2.say}  events={[e for e in r2.events if e['type'] == 'tool']}")
    assert sink.saved == []  # must read back and wait for a yes first
    r3 = await session.handle_user_turn("Yes, that's right.")
    print(f"turn 3: {r3.say}  events={[e for e in r3.events if e['type'] == 'tool']}")
    assert len(sink.saved) == 1
    assert sink.saved[0]["callback_number"].replace(" ", "").endswith("9800000000")
