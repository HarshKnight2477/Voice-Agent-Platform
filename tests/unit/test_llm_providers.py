from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2 as httpx
import pytest
from pydantic import SecretStr

from voice_platform.providers.llm.anthropic_provider import (
    AnthropicLLMProvider,
    LLMNotConfiguredError,
)
from voice_platform.providers.llm.scripted import ScriptedLLMProvider
from voice_platform.runtime.llm import (
    LLMRequest,
    LLMRequestError,
    LLMResponse,
    LLMUnavailableError,
    Message,
    StopReason,
    SystemBlock,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from voice_platform.shared.config import Environment, Settings


def settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "app_env": Environment.TEST,
        "database_url": "postgresql://x:y@localhost/z",
        "redis_url": "redis://localhost",
        "app_secret_key": SecretStr("k"),
        "anthropic_api_key": SecretStr("test-key"),
        "llm_model_fast": "model-fast",
        "llm_model_balanced": "model-balanced",
        "llm_model_deep": "model-deep",
    }
    base.update(over)
    return Settings(**base)


def block(**kw: Any) -> SimpleNamespace:
    ns = SimpleNamespace(**kw)
    ns.model_dump = lambda exclude_none=True: dict(kw)
    return ns


def fake_response(content: list[SimpleNamespace], stop: str, **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop,
        model="model-balanced",
        stop_details=extra.get("stop_details"),
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=80,
            cache_creation_input_tokens=0,
        ),
    )


class FakeClient:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **params: Any) -> Any:
        self.calls.append(params)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


REQUEST = LLMRequest(
    system=(SystemBlock("policy"), SystemBlock("facts", cacheable=True), SystemBlock("state")),
    messages=(
        Message(role="user", text="Table for four tomorrow?"),
        Message(role="assistant", tool_calls=(ToolCall("t1", "check_availability", {"n": 4}),)),
        Message(role="user", tool_results=(ToolResult("t1", "7pm free"),)),
    ),
    tier="balanced",
    effort="low",
    tools=(ToolSpec("check_availability", "Check tables", {"type": "object"}),),
)


async def test_request_shape_follows_current_api() -> None:
    client = FakeClient(fake_response([block(type="text", text="7 works.")], "end_turn"))
    resp = await AnthropicLLMProvider(settings(), client).complete(REQUEST)
    params = client.calls[0]
    assert params["model"] == "model-balanced"
    assert params["output_config"] == {"effort": "low"}
    assert "temperature" not in params and "thinking" not in params
    assert params["tool_choice"] == {"type": "auto"}
    assert [("cache_control" in b) for b in params["system"]] == [False, True, False]
    assert params["fallbacks"] == "default" and params["betas"]
    # All tool results for one turn travel in one user message.
    assert params["messages"][2]["content"][0]["type"] == "tool_result"
    assert resp.text == "7 works." and resp.stop_reason is StopReason.END
    assert resp.usage.cache_read_tokens == 80


async def test_tool_calls_and_refusals_are_mapped() -> None:
    client = FakeClient(
        fake_response([block(type="tool_use", id="c1", name="get_hours", input={})], "tool_use")
    )
    provider = AnthropicLLMProvider(settings(llm_refusal_fallback=False), client)
    resp = await provider.complete(REQUEST)
    assert resp.tool_calls == (ToolCall("c1", "get_hours", {}),)
    assert "betas" not in client.calls[0]

    refused = FakeClient(
        fake_response([], "refusal", stop_details=SimpleNamespace(category="cyber"))
    )
    r = await AnthropicLLMProvider(settings(), refused).complete(REQUEST)
    assert r.stop_reason is StopReason.REFUSAL and r.refusal_category == "cyber"


def _status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    req = httpx.Request("POST", "https://api.example/v1/messages")
    return cls("err", response=httpx.Response(code, request=req), body=None)


@pytest.mark.parametrize(
    ("make_exc", "expected"),
    [
        (
            lambda: anthropic.APITimeoutError(httpx.Request("POST", "https://x")),
            LLMUnavailableError,
        ),
        (lambda: _status_error(anthropic.RateLimitError, 429), LLMUnavailableError),
        (lambda: _status_error(anthropic.InternalServerError, 529), LLMUnavailableError),
        (lambda: _status_error(anthropic.BadRequestError, 400), LLMRequestError),
    ],
)
async def test_errors_are_mapped_to_platform_errors(
    make_exc: Any, expected: type[Exception]
) -> None:
    provider = AnthropicLLMProvider(settings(), FakeClient(make_exc()))
    with pytest.raises(expected):
        await provider.complete(REQUEST)


async def test_missing_model_mapping_is_a_configuration_error() -> None:
    provider = AnthropicLLMProvider(settings(llm_model_deep=None), FakeClient(None))
    with pytest.raises(LLMNotConfiguredError):
        await provider.complete(LLMRequest(system=(), messages=(), tier="deep"))


async def test_scripted_provider_replays_and_records() -> None:
    p = ScriptedLLMProvider(
        [LLMResponse(text="hi", stop_reason=StopReason.END), LLMUnavailableError("down")]
    )
    assert (await p.complete(REQUEST)).text == "hi"
    with pytest.raises(LLMUnavailableError):
        await p.complete(REQUEST)
    assert len(p.requests) == 2
