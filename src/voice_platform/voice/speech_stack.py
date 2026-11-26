"""Builds the speech-to-text and text-to-speech models for a call from settings.

"local" uses free open-source servers from compose.yaml (profile "speech"): Whisper
(faster-whisper via Speaches) and Kokoro, both behind OpenAI-compatible APIs, which LiveKit's
OpenAI plugin can talk to (pattern from docs.livekit.io/agents/models/tts/kokoro, checked
2026-11-26). Hosted providers (Deepgram, Cartesia, ...) are added here later; nothing else
in the platform changes when they are.
"""

from __future__ import annotations

from typing import Any

# LiveKit plugins must be imported on the main thread at startup, never inside a job.
from livekit.plugins import openai

from voice_platform.agents.config_schema import AgentConfig
from voice_platform.shared.config import Settings


def build_stt(settings: Settings, config: AgentConfig) -> Any:
    return openai.STT(
        model=settings.local_stt_model,
        base_url=settings.local_stt_url,
        api_key="not-needed",  # local server; the client library refuses an empty key
        language=config.language.split("-")[0],
        use_realtime=False,  # plain transcription requests; VAD decides when speech ends
    )


def build_tts(settings: Settings, config: AgentConfig) -> Any:
    voice = config.voice.voice_id or settings.local_tts_voice
    return openai.TTS(
        model="kokoro",
        voice=voice,
        speed=config.voice.speaking_rate,
        base_url=settings.local_tts_url,
        api_key="not-needed",
        response_format="pcm",  # raw 24 kHz PCM: playback can start before synthesis ends
    )
