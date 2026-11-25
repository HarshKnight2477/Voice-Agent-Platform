"""The agent configuration: everything a business sets for one AI phone agent.

This is the platform's own domain model. It contains no LiveKit, carrier or vendor SDK
types; adapters translate it later. Every field is validated and size-bounded, and unknown
fields are rejected, so a stored configuration is always safe to compile into a version.

Basic-mode fields are what the wizard shows. `advanced` holds settings only shown when
Advanced Mode is on.
"""

from __future__ import annotations

import re
from datetime import time
from enum import StrEnum
from typing import Annotated, Literal
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_TIMEZONES = frozenset(available_timezones())
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_LANG = re.compile(r"^[a-z]{2,3}(-[A-Z]{2})?$")

ShortText = Annotated[str, Field(min_length=1, max_length=200)]
LongText = Annotated[str, Field(max_length=4000)]

# Agents choose a model *tier*, not a vendor model id. The LLM provider layer maps each
# tier to a concrete model id from server settings (LLM_MODEL_FAST / _BALANCED / _DEEP),
# so model upgrades are a config change and stored agent versions never name a vendor model.
SUPPORTED_LLM_MODELS = ("fast", "balanced", "deep")
DEFAULT_LLM_MODEL = "balanced"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Tone(StrEnum):
    FRIENDLY = "friendly"
    PROFESSIONAL = "professional"
    CALM = "calm"
    UPBEAT = "upbeat"


class Verbosity(StrEnum):
    BRIEF = "brief"
    BALANCED = "balanced"


class Personality(_Strict):
    tone: Tone = Tone.FRIENDLY
    verbosity: Verbosity = Verbosity.BRIEF
    persona_name: str = Field(default="", max_length=60)


class DayHours(_Strict):
    open: time
    close: time

    @model_validator(mode="after")
    def _order(self) -> DayHours:
        if self.close <= self.open:
            raise ValueError("close must be after open (split overnight hours across two days)")
        return self


Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class BusinessInfo(_Strict):
    name: ShortText
    timezone: str = "UTC"
    # Days missing from the map are closed. Up to two ranges per day (e.g. lunch, dinner).
    hours: dict[Weekday, list[DayHours]] = Field(default_factory=dict)
    address: str = Field(default="", max_length=300)
    website: str = Field(default="", max_length=300)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        if v not in _TIMEZONES:
            raise ValueError("unknown time zone; use an IANA name such as Asia/Kolkata")
        return v

    @field_validator("hours")
    @classmethod
    def _ranges(cls, v: dict[str, list[DayHours]]) -> dict[str, list[DayHours]]:
        for day, ranges in v.items():
            if len(ranges) > 2:
                raise ValueError(f"{day}: at most two opening ranges")
        return v


class VoiceSettings(_Strict):
    """Provider-neutral voice choice. Validated against real provider catalogs in Phase 3."""

    provider: Literal["cartesia", "elevenlabs", "deepgram", "livekit_inference"] = "cartesia"
    voice_id: str = Field(default="", max_length=100)
    speaking_rate: float = Field(default=1.0, ge=0.7, le=1.3)


class LLMSettings(_Strict):
    provider: Literal["anthropic"] = "anthropic"
    model: str = DEFAULT_LLM_MODEL
    temperature: float = Field(default=0.3, ge=0.0, le=1.0)
    max_output_tokens: int = Field(default=300, ge=50, le=2000)

    @field_validator("model")
    @classmethod
    def _supported(cls, v: str) -> str:
        if v not in SUPPORTED_LLM_MODELS:
            raise ValueError(f"model must be one of {', '.join(SUPPORTED_LLM_MODELS)}")
        return v


class EscalationTrigger(StrEnum):
    CALLER_REQUESTS_HUMAN = "caller_requests_human"
    REPEATED_FAILURE = "repeated_failure"
    TOOL_UNAVAILABLE = "tool_unavailable"
    HIGH_FRUSTRATION = "high_frustration"
    SENSITIVE_REQUEST = "sensitive_request"


class Escalation(_Strict):
    enabled: bool = True
    transfer_number: str = ""  # E.164; empty = take a message / offer a callback instead
    triggers: list[EscalationTrigger] = Field(
        default_factory=lambda: [
            EscalationTrigger.CALLER_REQUESTS_HUMAN,
            EscalationTrigger.REPEATED_FAILURE,
            EscalationTrigger.TOOL_UNAVAILABLE,
        ]
    )
    after_hours_message: str = Field(default="", max_length=500)

    @field_validator("transfer_number")
    @classmethod
    def _e164(cls, v: str) -> str:
        if v and not _E164.match(v):
            raise ValueError("transfer_number must be in E.164 format, e.g. +14155550123")
        return v


class Safety(_Strict):
    # Spoken near the start of every call where the law or the business requires it.
    ai_disclosure: str = Field(default="Just so you know, I'm an AI assistant.", max_length=300)
    recording_notice: str = Field(default="", max_length=300)
    forbidden_topics: list[Annotated[str, Field(max_length=100)]] = Field(
        default_factory=list, max_length=20
    )


class Limits(_Strict):
    max_call_seconds: int = Field(default=900, ge=60, le=3600)
    max_tool_iterations_per_turn: int = Field(default=4, ge=1, le=8)
    max_silence_reprompts: int = Field(default=2, ge=0, le=5)


class ToolGrant(_Strict):
    """A tool the agent may request, with the explicit permission scopes it is granted."""

    name: str
    scopes: list[Annotated[str, Field(max_length=64)]] = Field(default_factory=list, max_length=10)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _TOOL_NAME.match(v):
            raise ValueError("tool names are lowercase letters, digits and underscores")
        return v


class Advanced(_Strict):
    """Only shown with Advanced Mode on."""

    extra_instructions: LongText = ""
    llm: LLMSettings = Field(default_factory=LLMSettings)
    interruptions_enabled: bool = True
    min_interruption_words: int = Field(default=2, ge=0, le=10)
    endpointing_min_delay_ms: int = Field(default=400, ge=100, le=3000)
    endpointing_max_delay_ms: int = Field(default=3000, ge=500, le=8000)

    @model_validator(mode="after")
    def _delays(self) -> Advanced:
        if self.endpointing_max_delay_ms < self.endpointing_min_delay_ms:
            raise ValueError("endpointing_max_delay_ms must be >= endpointing_min_delay_ms")
        return self


class AgentConfig(_Strict):
    schema_version: Literal[1] = 1
    purpose: str = Field(default="Answer calls for the business.", max_length=500)
    language: str = "en-US"
    greeting: str = Field(default="Hi, thanks for calling. How can I help?", max_length=400)
    personality: Personality = Field(default_factory=Personality)
    business: BusinessInfo
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    business_rules: list[Annotated[str, Field(max_length=300)]] = Field(
        default_factory=list, max_length=30
    )
    escalation: Escalation = Field(default_factory=Escalation)
    safety: Safety = Field(default_factory=Safety)
    limits: Limits = Field(default_factory=Limits)
    tools: list[ToolGrant] = Field(default_factory=list, max_length=30)
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=10)
    advanced: Advanced = Field(default_factory=Advanced)

    @field_validator("language")
    @classmethod
    def _lang(cls, v: str) -> str:
        if not _LANG.match(v):
            raise ValueError("language must look like en-US or hi-IN")
        return v

    @field_validator("tools")
    @classmethod
    def _unique_tools(cls, v: list[ToolGrant]) -> list[ToolGrant]:
        names = [t.name for t in v]
        if len(names) != len(set(names)):
            raise ValueError("each tool can be listed once")
        return v
