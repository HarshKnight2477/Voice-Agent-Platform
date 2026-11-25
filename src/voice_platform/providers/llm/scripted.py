"""A deterministic LLMProvider for tests and offline development.

Replays a script of responses and records every request, so runtime behaviour can be tested
without network access, API keys or cost.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from voice_platform.runtime.llm import LLMError, LLMRequest, LLMResponse

Step = LLMResponse | LLMError | Callable[[LLMRequest], LLMResponse]


class ScriptedLLMProvider:
    name = "scripted"

    def __init__(self, steps: Iterable[Step]) -> None:
        self._steps = list(steps)
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._steps:
            raise AssertionError("ScriptedLLMProvider ran out of scripted responses")
        step = self._steps.pop(0)
        if isinstance(step, LLMError):
            raise step
        if callable(step):
            return step(request)
        return step
