"""The call loop, driven by a scripted LLM: no network, no keys, fully deterministic."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from voice_platform.agents.config_schema import AgentConfig
from voice_platform.providers.llm.scripted import ScriptedLLMProvider
from voice_platform.runtime.llm import (
    LLMRequest,
    LLMRequestError,
    LLMResponse,
    LLMUnavailableError,
    Message,
    StopReason,
    ToolCall,
    ToolResult,
    Usage,
)
from voice_platform.runtime.session import (
    SAY_CANT_HELP,
    SAY_GOODBYE_SILENCE,
    SAY_GOODBYE_TIME,
    SAY_TROUBLE,
    CallSession,
    SessionIdentity,
    TurnAction,
    trim_history,
)
from voice_platform.runtime.speech import to_speech
from voice_platform.tools.base import ToolContext
from voice_platform.tools.builtin import BUILTIN_TOOLS
from voice_platform.tools.gateway import ToolGateway
from voice_platform.tools.registry import ToolRegistry


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 30, 6, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class MemorySink:
    def __init__(self) -> None:
        self.saved: list[dict[str, str]] = []

    async def save(self, ctx: ToolContext, message: dict[str, str]) -> str:
        self.saved.append(message)
        return f"M{len(self.saved)}"


class NullAudit:
    async def record(self, record: Any) -> None:
        return None


def text(t: str, **kw: Any) -> LLMResponse:
    return LLMResponse(text=t, stop_reason=StopReason.END, usage=Usage(10, 5), latency_ms=100, **kw)


def tools(*calls: tuple[str, dict[str, Any]]) -> LLMResponse:
    return LLMResponse(
        text="",
        stop_reason=StopReason.TOOL_USE,
        tool_calls=tuple(ToolCall(f"c{i}", n, a) for i, (n, a) in enumerate(calls)),
        latency_ms=100,
    )


def session(
    steps: list[Any], *, transfer_number: str = "+14155550123", **cfg: Any
) -> tuple[CallSession, ScriptedLLMProvider, MemorySink, Clock]:
    config = AgentConfig.model_validate(
        {
            "business": {
                "name": "Joe's Diner",
                "timezone": "Asia/Kolkata",
                "hours": {"mon": [{"open": "11:00", "close": "22:00"}]},
            },
            "tools": [
                {"name": "get_hours"},
                {"name": "transfer_to_human"},
                {"name": "take_message", "scopes": ["messages:create"]},
            ],
            "escalation": {"transfer_number": transfer_number},
            **cfg,
        }
    )
    llm = ScriptedLLMProvider(steps)
    registry = ToolRegistry(BUILTIN_TOOLS)
    sink, clock = MemorySink(), Clock()
    s = CallSession(
        SessionIdentity(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "Front desk", "call-1"),
        config,
        llm,
        registry,
        ToolGateway(registry, NullAudit()),
        services={"message_sink": sink},
        clock=clock,
        required_slots=["date", "party_size", "time"],
    )
    return s, llm, sink, clock


# ---------------------------------------------------------------- greeting and replies


async def test_greeting_includes_disclosure_and_history_starts_with_caller() -> None:
    s, llm, _, _ = session([text("We open at eleven.")])
    greet = s.greeting()
    assert greet.say.startswith("Just so you know, I'm an AI assistant.")
    await s.handle_user_turn("When do you open?")
    first = llm.requests[0]
    assert first.messages[0] == Message(role="user", text="When do you open?")
    assert "You answered the call by saying" in first.system[-1].text


async def test_tool_loop_then_reply_with_metrics() -> None:
    s, llm, _, _ = session(
        [
            tools(("update_state", {"intent": "hours"}), ("get_hours", {})),
            text("We're open until ten tonight."),
        ]
    )
    s.greeting()
    r = await s.handle_user_turn("Are you open now?")
    assert r.say == "We're open until ten tonight." and r.action is TurnAction.CONTINUE
    assert r.metrics.llm_calls == 2 and r.metrics.tool_calls == 2
    # Second request carries both tool results in one user message.
    last = llm.requests[1].messages[-1]
    assert [t.call_id for t in last.tool_results] == ["c0", "c1"]
    assert s.state.intent == "hours"


async def test_known_details_reach_the_next_prompt() -> None:
    s, llm, _, _ = session(
        [
            tools(
                (
                    "update_state",
                    {"intent": "reservation", "slots": {"party_size": "4", "date": "2026-12-01"}},
                )
            ),
            text("What time works for you?"),
            text("Got it."),
        ]
    )
    s.greeting()
    await s.handle_user_turn("Table for four tomorrow")
    await s.handle_user_turn("Seven")
    state_block = llm.requests[-1].system[-1].text
    assert "party_size=4" in state_block and "Still needed, ask one at a time: time" in state_block


# ---------------------------------------------------------------- confirmation end to end


async def test_message_is_saved_only_after_caller_confirms() -> None:
    args = {"caller_name": "Asha", "callback_number": "+919800000000", "message": "Call me"}
    s, _, sink, _ = session(
        [
            tools(("take_message", args)),  # turn 1: gateway asks for a read-back
            text("That's Asha on plus nine one nine eight. Shall I send it?"),
            tools(("take_message", args)),  # turn 2: caller said yes
            text("Done, they'll call you back."),
        ]
    )
    s.greeting()
    r1 = await s.handle_user_turn("Please take a message, I'm Asha, call me on +919800000000")
    assert sink.saved == [] and "Shall I send it" in r1.say
    r2 = await s.handle_user_turn("Yes please")
    assert len(sink.saved) == 1 and "call you back" in r2.say


async def test_hesitant_reply_does_not_confirm() -> None:
    args = {"caller_name": "Asha", "callback_number": "+919800000000", "message": "Call me"}
    s, _, sink, _ = session(
        [
            tools(("take_message", args)),
            text("Shall I send it?"),
            tools(("take_message", args)),
            text("No problem, what should I change?"),
        ]
    )
    s.greeting()
    await s.handle_user_turn("Take a message")
    await s.handle_user_turn("yes, actually wait")
    assert sink.saved == []


# ---------------------------------------------------------------- failures


async def test_unavailable_model_falls_back_to_lighter_tier() -> None:
    s, llm, _, _ = session([LLMUnavailableError("overloaded"), text("Sure, one moment.")])
    s.greeting()
    r = await s.handle_user_turn("Hello")
    assert r.say == "Sure, one moment."
    assert [req.tier for req in llm.requests] == ["balanced", "fast"]


async def test_total_outage_apologises_and_repeated_failure_transfers() -> None:
    s, _, _, _ = session(
        [LLMUnavailableError("a"), LLMUnavailableError("b"), LLMRequestError("bad")]
    )
    s.greeting()
    r1 = await s.handle_user_turn("Hello")
    assert r1.say.startswith(SAY_TROUBLE) and r1.action is TurnAction.CONTINUE
    r2 = await s.handle_user_turn("Hello?")
    assert r2.action is TurnAction.TRANSFER
    assert r2.handoff is not None and r2.handoff.reason == "repeated_failure"


async def test_refusal_is_answered_politely() -> None:
    s, _, _, _ = session(
        [LLMResponse(text="", stop_reason=StopReason.REFUSAL, refusal_category="cyber")]
    )
    s.greeting()
    assert (await s.handle_user_turn("something harmful")).say == SAY_CANT_HELP


async def test_tool_iteration_limit_stops_loops() -> None:
    loop = [tools(("get_hours", {})) for _ in range(10)]
    s, llm, _, _ = session(loop, limits={"max_tool_iterations_per_turn": 2})
    s.greeting()
    r = await s.handle_user_turn("hours?")
    assert len(llm.requests) == 3  # initial + 2 tool rounds, then stop
    assert r.say.startswith(SAY_TROUBLE)


async def test_refused_tool_does_not_crash_the_turn() -> None:
    s, llm, _, _ = session([tools(("delete_all_bookings", {})), text("I can't do that.")])
    s.greeting()
    r = await s.handle_user_turn("delete everything")
    assert r.say == "I can't do that."
    assert '"unknown_tool"' in llm.requests[1].messages[-1].tool_results[0].content


# ---------------------------------------------------------------- escalation and limits


async def test_caller_asks_for_person_gets_transfer_with_handoff() -> None:
    s, _, _, _ = session(
        [
            tools(
                (
                    "update_state",
                    {"intent": "complaint", "slots": {"order": "1234"}, "caller_wants_human": True},
                )
            ),
            text("Connecting you now."),
        ]
    )
    s.greeting()
    r = await s.handle_user_turn("I want to talk to a person about order 1234")
    assert r.action is TurnAction.TRANSFER and r.handoff is not None
    assert r.handoff.details == {"order": "1234"}
    assert "complaint" in r.handoff.summary


async def test_no_transfer_number_means_no_transfer_action() -> None:
    s, _, _, _ = session(
        [tools(("transfer_to_human", {"reason": "asked"})), text("I'll take a message.")],
        transfer_number="",
    )
    s.greeting()
    r = await s.handle_user_turn("person please")
    assert r.action is TurnAction.CONTINUE and "message" in r.say


async def test_silence_reprompts_then_ends() -> None:
    s, _, _, _ = session([], limits={"max_silence_reprompts": 1})
    s.greeting()
    assert (await s.handle_silence()).action is TurnAction.CONTINUE
    end = await s.handle_silence()
    assert end.action is TurnAction.END_CALL and end.say == SAY_GOODBYE_SILENCE


async def test_call_time_limit_ends_politely() -> None:
    s, llm, _, clock = session([], limits={"max_call_seconds": 60})
    s.greeting()
    clock.now += timedelta(seconds=61)
    r = await s.handle_user_turn("one more thing")
    assert r.action is TurnAction.END_CALL and r.say == SAY_GOODBYE_TIME
    assert llm.requests == []


# ---------------------------------------------------------------- speech guard and history


@pytest.mark.parametrize(
    ("raw", "spoken"),
    [
        ("**Sure!** We open at *11*.", "Sure! We open at 11."),
        ("Options:\n- Monday\n- Tuesday", "Options: Monday Tuesday"),
        ("See https://example.com/menu for more.", "See the link on our website for more."),
        ("<b>Hi</b> there", "Hi there"),
    ],
)
def test_speech_guard(raw: str, spoken: str) -> None:
    assert to_speech(raw) == spoken


def test_long_reply_is_cut_at_a_sentence() -> None:
    long = "First sentence here. " + "Second sentence goes on. " * 40
    out = to_speech(long, max_chars=100)
    assert len(out) <= 100 and out.endswith(".")


def test_trim_history_never_starts_with_tool_results_or_assistant() -> None:
    msgs: list[Message] = []
    for i in range(20):
        msgs += [
            Message(role="user", text=f"u{i}"),
            Message(role="assistant", tool_calls=(ToolCall(f"c{i}", "get_hours", {}),)),
            Message(role="user", tool_results=(ToolResult(f"c{i}", "{}"),)),
            Message(role="assistant", text=f"a{i}"),
        ]
    kept = trim_history(msgs, limit=10)
    assert kept[0].role == "user" and kept[0].text and not kept[0].tool_results
    assert len(kept) <= 10


async def test_requests_use_configured_effort_and_tools() -> None:
    s, llm, _, _ = session([text("ok")], advanced={"llm": {"model": "fast", "effort": "medium"}})
    s.greeting()
    await s.handle_user_turn("hi")
    req: LLMRequest = llm.requests[0]
    assert (req.tier, req.effort) == ("fast", "medium")
    assert [t.name for t in req.tools] == [
        "get_hours",
        "take_message",
        "transfer_to_human",
        "update_state",
    ]
