"""Choose the LLMProvider from settings (LLM_PROVIDER). Callers never import a vendor class."""

from __future__ import annotations

from voice_platform.runtime.llm import LLMProvider
from voice_platform.shared.config import Settings


class LLMConfigurationError(RuntimeError):
    pass


def build_llm_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "ollama":
        from voice_platform.providers.llm.ollama_provider import OllamaLLMProvider

        return OllamaLLMProvider(settings)
    if settings.llm_provider == "anthropic":
        if not settings.anthropic_api_key:
            raise LLMConfigurationError("LLM_PROVIDER=anthropic needs ANTHROPIC_API_KEY")
        from voice_platform.providers.llm.anthropic_provider import AnthropicLLMProvider

        return AnthropicLLMProvider(settings)
    raise LLMConfigurationError(f"unknown LLM_PROVIDER: {settings.llm_provider}")
