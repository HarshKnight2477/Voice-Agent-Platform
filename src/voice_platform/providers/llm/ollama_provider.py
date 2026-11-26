"""LLMProvider backed by a local Ollama server (free, open models, no API key).

Uses Ollama's native chat endpoint (POST /api/chat), verified against Ollama 0.40 on this
machine: request `tools` in OpenAI function format; the reply carries `message.tool_calls`
with `id`, `function.name` and `function.arguments` (an object); tool results go back as
`{"role": "tool", "tool_call_id", "tool_name", "content"}`. `think: false` turns off the
reasoning phase of hybrid models (e.g. Qwen3), which matters for voice latency.

Meant for development. The platform's production model is configured separately, and
switching is a settings change (LLM_PROVIDER), not a code change.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import httpx

from voice_platform.runtime.llm import (
    LLMRequest,
    LLMRequestError,
    LLMResponse,
    LLMUnavailableError,
    Message,
    StopReason,
    ToolCall,
    Usage,
)
from voice_platform.shared.config import Settings
from voice_platform.shared.logging import get_logger

from .anthropic_provider import resolve_model

log = get_logger(__name__)

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)


def to_ollama_messages(request: LLMRequest) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    system = "\n\n".join(b.text for b in request.system)
    if system:
        out.append({"role": "system", "content": system})
    names: dict[str, str] = {}  # tool call id -> tool name, for the tool result messages
    for m in request.messages:
        out.extend(_convert(m, names))
    return out


def _convert(m: Message, names: dict[str, str]) -> list[dict[str, Any]]:
    if m.role == "assistant":
        msg: dict[str, Any] = {"role": "assistant", "content": m.text}
        if m.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "function": {"name": c.name, "arguments": c.arguments}}
                for c in m.tool_calls
            ]
            names.update({c.id: c.name for c in m.tool_calls})
        return [msg]
    msgs = [
        {
            "role": "tool",
            "tool_call_id": r.call_id,
            "tool_name": names.get(r.call_id, ""),
            "content": r.content,
        }
        for r in m.tool_results
    ]
    if m.text or not m.tool_results:
        msgs.append({"role": "user", "content": m.text})
    return msgs


class OllamaLLMProvider:
    name = "ollama"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            base_url=settings.ollama_base_url,
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=2.0),
        )

    def build_body(self, request: LLMRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": resolve_model(self._settings, request.tier),
            "messages": to_ollama_messages(request),
            "stream": False,
            # Thinking only when the agent asks for more effort; it adds seconds on a voice call.
            "think": request.effort != "low",
            "keep_alive": self._settings.ollama_keep_alive,
            "options": {"num_predict": request.max_output_tokens},
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in request.tools
            ]
        return body

    async def complete(self, request: LLMRequest) -> LLMResponse:
        body = self.build_body(request)
        started = time.perf_counter()
        try:
            resp = await self._client.post("/api/chat", json=body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            log.warning("llm.unavailable", provider=self.name, error=type(exc).__name__)
            raise LLMUnavailableError(type(exc).__name__) from exc
        if resp.status_code >= 500:
            log.warning("llm.unavailable", provider=self.name, status=resp.status_code)
            raise LLMUnavailableError(str(resp.status_code))
        if resp.status_code >= 400:
            log.error(
                "llm.request_rejected",
                provider=self.name,
                status=resp.status_code,
                detail=resp.text[:200],
            )
            raise LLMRequestError(f"status {resp.status_code}")
        latency_ms = (time.perf_counter() - started) * 1000
        return self._parse(resp.json(), latency_ms)

    @staticmethod
    def _parse(data: dict[str, Any], latency_ms: float) -> LLMResponse:
        msg = data.get("message") or {}
        text = _THINK_BLOCK.sub("", msg.get("content") or "").strip()
        calls: list[ToolCall] = []
        for raw in msg.get("tool_calls") or []:
            fn = raw.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):  # some models return a JSON string
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_unparseable": args[:200]}
            calls.append(
                ToolCall(
                    id=raw.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    name=str(fn.get("name", "")),
                    arguments=dict(args),
                )
            )
        if calls:
            stop = StopReason.TOOL_USE
        elif data.get("done_reason") == "length":
            stop = StopReason.MAX_TOKENS
        else:
            stop = StopReason.END
        return LLMResponse(
            text=text,
            stop_reason=stop,
            tool_calls=tuple(calls),
            usage=Usage(
                input_tokens=int(data.get("prompt_eval_count") or 0),
                output_tokens=int(data.get("eval_count") or 0),
            ),
            model=str(data.get("model", "")),
            latency_ms=latency_ms,
        )

    async def warm_up(self, tier: str = "balanced") -> None:
        """Load the model into GPU memory so the first caller doesn't wait for it."""
        model = resolve_model(self._settings, tier)  # type: ignore[arg-type]
        await self._client.post(
            "/api/generate",
            json={"model": model, "keep_alive": self._settings.ollama_keep_alive},
            timeout=180.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
