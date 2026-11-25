"""Optional smoke test against the real LLM API. Costs a fraction of a cent.

Runs only with:  uv run pytest -m live_llm   (and ANTHROPIC_API_KEY + LLM_MODEL_FAST in .env)
"""

from __future__ import annotations

import pytest

from voice_platform.providers.llm.anthropic_provider import AnthropicLLMProvider
from voice_platform.runtime.llm import LLMRequest, Message, StopReason, SystemBlock
from voice_platform.shared.config import get_settings

pytestmark = pytest.mark.live_llm


async def test_fast_tier_answers_briefly() -> None:
    settings = get_settings()
    if not settings.anthropic_api_key or not settings.llm_model_fast:
        pytest.skip("ANTHROPIC_API_KEY / LLM_MODEL_FAST not configured")
    provider = AnthropicLLMProvider(settings)
    resp = await provider.complete(
        LLMRequest(
            system=(SystemBlock("Answer in at most five words."),),
            messages=(Message(role="user", text="What color is the sky on a clear day?"),),
            tier="fast",
            effort="low",
            max_output_tokens=200,
        )
    )
    assert resp.stop_reason is StopReason.END
    assert resp.text
    assert resp.usage.output_tokens > 0
