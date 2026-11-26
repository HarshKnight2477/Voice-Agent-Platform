from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from voice_platform.providers.llm.factory import LLMConfigurationError, build_llm_provider
from voice_platform.providers.llm.ollama_provider import OllamaLLMProvider
from voice_platform.runtime.llm import (
    LLMRequest,
    LLMRequestError,
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
        "llm_provider": "ollama",
        "llm_model_fast": "small-model",
        "llm_model_balanced": "mid-model",
        "llm_model_deep": "big-model",
    }
    base.update(over)
    return Settings(**base)


def provider(handler: Any) -> tuple[OllamaLLMProvider, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return handler(request)

    client = httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(wrapped))
    return OllamaLLMProvider(settings(), client), seen


REQUEST = LLMRequest(
    system=(SystemBlock("policy"), SystemBlock("facts", cacheable=True)),
    messages=(
        Message(role="user", text="Open now?"),
        Message(role="assistant", tool_calls=(ToolCall("c1", "get_hours", {}),)),
        Message(role="user", tool_results=(ToolResult("c1", '{"ok": true}'),)),
    ),
    tools=(ToolSpec("get_hours", "Hours", {"type": "object", "properties": {}}),),
)


async def test_request_body_and_text_reply() -> None:
    p, seen = provider(
        lambda r: httpx.Response(
            200,
            json={
                "model": "mid-model",
                "message": {"role": "assistant", "content": "<think>hmm</think>Yes, we're open."},
                "done_reason": "stop",
                "prompt_eval_count": 50,
                "eval_count": 7,
            },
        )
    )
    resp = await p.complete(REQUEST)
    body = seen[0]
    assert body["model"] == "mid-model" and body["stream"] is False and body["think"] is False
    assert body["messages"][0] == {"role": "system", "content": "policy\n\nfacts"}
    assert body["messages"][2]["tool_calls"][0]["function"]["name"] == "get_hours"
    assert body["messages"][3] == {
        "role": "tool",
        "tool_call_id": "c1",
        "tool_name": "get_hours",
        "content": '{"ok": true}',
    }
    assert body["tools"][0]["function"]["name"] == "get_hours"
    assert resp.text == "Yes, we're open." and resp.stop_reason is StopReason.END
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (50, 7)


async def test_tool_calls_including_string_arguments() -> None:
    p, _ = provider(
        lambda r: httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "x1",
                            "function": {"name": "update_state", "arguments": {"intent": "hours"}},
                        },
                        {"function": {"name": "get_hours", "arguments": "{}"}},
                    ],
                },
                "done_reason": "stop",
            },
        )
    )
    resp = await p.complete(REQUEST)
    assert resp.stop_reason is StopReason.TOOL_USE
    assert resp.tool_calls[0] == ToolCall("x1", "update_state", {"intent": "hours"})
    assert resp.tool_calls[1].name == "get_hours" and resp.tool_calls[1].id.startswith("call_")


async def test_effort_turns_thinking_on() -> None:
    p, seen = provider(lambda r: httpx.Response(200, json={"message": {"content": "ok"}}))
    await p.complete(LLMRequest(system=(), messages=(Message("user", "hi"),), effort="medium"))
    assert seen[0]["think"] is True


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (lambda r: httpx.Response(503, text="busy"), LLMUnavailableError),
        (lambda r: httpx.Response(404, json={"error": "model not found"}), LLMRequestError),
    ],
)
async def test_http_errors_map_to_platform_errors(handler: Any, expected: type) -> None:
    p, _ = provider(handler)
    with pytest.raises(expected):
        await p.complete(REQUEST)


async def test_connection_failure_is_unavailable() -> None:
    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    p, _ = provider(boom)
    with pytest.raises(LLMUnavailableError):
        await p.complete(REQUEST)


def test_factory_selects_provider_from_settings() -> None:
    assert build_llm_provider(settings()).name == "ollama"
    with pytest.raises(LLMConfigurationError, match="ANTHROPIC_API_KEY"):
        build_llm_provider(settings(llm_provider="anthropic"))
    assert (
        build_llm_provider(
            settings(llm_provider="anthropic", anthropic_api_key=SecretStr("k"))
        ).name
        == "anthropic"
    )
