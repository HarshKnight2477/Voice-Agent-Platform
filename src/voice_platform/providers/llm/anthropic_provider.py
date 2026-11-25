"""LLMProvider backed by the official Anthropic Python SDK (Messages API).

Used by the runtime outside LiveKit (text-mode tests, the test console, post-call jobs).
During live calls LiveKit's own plugin talks to the same API; both read the same settings.

Choices that follow the current API:
- No sampling parameters (current models reject temperature); `output_config.effort` sets
  how much the model thinks. Thinking stays at the model default (adaptive).
- `tool_choice` is always "auto" (forcing a specific tool is rejected by current models).
- Stable system blocks carry `cache_control` so repeated turns reuse the cached prefix.
- Server-side refusal fallback (beta) is on by default: a declined request is re-run on a
  fallback model inside the same call. A refusal that survives it is reported as REFUSAL.
"""

from __future__ import annotations

import time
from typing import Any

import anthropic

from voice_platform.runtime.llm import (
    LLMRequest,
    LLMRequestError,
    LLMResponse,
    LLMUnavailableError,
    Message,
    ModelTier,
    StopReason,
    ToolCall,
    Usage,
)
from voice_platform.shared.config import Settings
from voice_platform.shared.logging import get_logger

log = get_logger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

_STOP_REASONS = {
    "end_turn": StopReason.END,
    "stop_sequence": StopReason.END,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "refusal": StopReason.REFUSAL,
}


class LLMNotConfiguredError(LLMRequestError):
    pass


def resolve_model(settings: Settings, tier: ModelTier) -> str:
    model = {
        "fast": settings.llm_model_fast,
        "balanced": settings.llm_model_balanced,
        "deep": settings.llm_model_deep,
    }[tier]
    if not model:
        raise LLMNotConfiguredError(f"no model configured for tier '{tier}' (LLM_MODEL_*)")
    return model


def _to_api_messages(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "assistant":
            # Replay the provider's own blocks unchanged when we have them (required for
            # thinking blocks); otherwise rebuild text + tool_use blocks.
            if m.provider_content is not None:
                out.append({"role": "assistant", "content": m.provider_content})
                continue
            blocks: list[dict[str, Any]] = []
            if m.text:
                blocks.append({"type": "text", "text": m.text})
            blocks += [
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                for c in m.tool_calls
            ]
            out.append({"role": "assistant", "content": blocks or m.text})
        else:
            if m.tool_results:
                # All results for one assistant turn go back in a single user message.
                content: list[dict[str, Any]] = [
                    {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content,
                     "is_error": r.is_error}
                    for r in m.tool_results
                ]  # fmt: skip
                if m.text:
                    content.append({"type": "text", "text": m.text})
                out.append({"role": "user", "content": content})
            else:
                out.append({"role": "user", "content": m.text})
    return out


class AnthropicLLMProvider:
    name = "anthropic"

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client or anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value()
            if settings.anthropic_api_key
            else None,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    def build_params(self, request: LLMRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": resolve_model(self._settings, request.tier),
            "max_tokens": request.max_output_tokens,
            "system": [
                {"type": "text", "text": b.text}
                | ({"cache_control": {"type": "ephemeral"}} if b.cacheable else {})
                for b in request.system
            ],
            "messages": _to_api_messages(request.messages),
            "output_config": {"effort": request.effort},
        }
        if request.tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in request.tools
            ]
            params["tool_choice"] = {"type": "auto"}
        if self._settings.llm_refusal_fallback:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

    async def complete(self, request: LLMRequest) -> LLMResponse:
        params = self.build_params(request)
        started = time.perf_counter()
        try:
            if "betas" in params:
                resp = await self._client.beta.messages.create(**params)
            else:
                resp = await self._client.messages.create(**params)
        except (anthropic.APITimeoutError, anthropic.APIConnectionError,
                anthropic.RateLimitError, anthropic.InternalServerError) as exc:  # fmt: skip
            log.warning("llm.unavailable", provider=self.name, error=type(exc).__name__)
            raise LLMUnavailableError(type(exc).__name__) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                log.warning("llm.unavailable", provider=self.name, status=exc.status_code)
                raise LLMUnavailableError(str(exc.status_code)) from exc
            log.error("llm.request_rejected", provider=self.name, status=exc.status_code,
                      request_id=getattr(exc, "request_id", None))  # fmt: skip
            raise LLMRequestError(f"status {exc.status_code}") from exc
        latency_ms = (time.perf_counter() - started) * 1000
        return self._parse(resp, latency_ms)

    @staticmethod
    def _parse(resp: Any, latency_ms: float) -> LLMResponse:
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input)))
        stop = _STOP_REASONS.get(resp.stop_reason or "", StopReason.OTHER)
        category = None
        if stop is StopReason.REFUSAL and getattr(resp, "stop_details", None):
            category = resp.stop_details.category
        u = resp.usage
        return LLMResponse(
            text="".join(texts).strip(),
            stop_reason=stop,
            tool_calls=tuple(calls),
            usage=Usage(
                input_tokens=u.input_tokens or 0,
                output_tokens=u.output_tokens or 0,
                cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            ),
            model=resp.model,
            latency_ms=latency_ms,
            refusal_category=category,
            # Replayed unchanged on the next request (keeps thinking/fallback blocks intact).
            provider_content=[b.model_dump(exclude_none=True) for b in resp.content],
        )
