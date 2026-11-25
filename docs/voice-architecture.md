# Voice Architecture

## Per-turn pipeline
```
Caller audio → (Krisp, optional) → VAD (Silero) → STT (streaming) → turn detector (end of utterance)
 → Runtime: update state · decide (workflow step / knowledge / tool) → LLM (streaming)
 → [tool request → Tool Gateway → result → LLM] → TTS (streaming) → caller
 ↺ barge-in: user speech during TTS → interruption handling (adaptive mode) → stop playback → new turn
```

## Latency budget (targets, to be validated by measurement)
| Segment | Target p50 | Source metric |
|---|---|---|
| End-of-utterance detection | 200-400 ms | EOU metrics |
| STT final | ≤ 300 ms after speech end | STT metrics |
| LLM time-to-first-token | 400-700 ms | `llm_node_ttft` |
| TTS time-to-first-byte | 150-250 ms | `tts_node_ttfb` |
| **End-to-end (user stops → agent audio)** | **≤ 1.0 s p50, ≤ 1.6 s p95** | `e2e_latency` |
| Tool call | ≤ 800 ms, else speak a filler ("One sec, checking that.") | our tool metrics |

No tuning by assumption: every feature (Krisp, preemptive generation, endpointing delays) is A/B measured on recorded test calls before becoming a default.

## Voice configuration (per agent version)
`stt {provider, model, language, keywords}`, `tts {provider, model, voice_id, speed}`, `llm {provider, model, temperature, max_tokens}`, `vad {provider, params}`, `turn {mode, min_delay, max_delay}`, `interruption {enabled, mode, min_words, min_duration}`, `noise {krisp}`, `fillers {enabled}`, `max_call_seconds`.
Basic mode exposes only voice, language, speaking speed. Everything else is Advanced mode.

## Conversational rules (enforced by prompt layer + output filter)
- 1-2 short sentences per turn; one question at a time; no lists read aloud; no markdown.
- Never re-ask known slots (state-driven prompting: the prompt shows collected and missing slots).
- Confirm before state-changing actions (read back key details).
- Spoken formatting: numbers, dates, times, prices, phone numbers normalised for TTS.
- Graceful errors: never speak internal errors; use the fallback phrase and offer transfer.

## Failure handling
| Failure | Behaviour |
|---|---|
| STT/TTS provider error | Plugin retry; fallback provider (post-MVP); else apologise + transfer/end |
| LLM timeout (> 5 s) | Filler, one retry with smaller context; else fallback model; else transfer |
| Tool timeout | "I'm having trouble accessing that right now" + offer transfer |
| Silence | Re-prompt twice, then polite goodbye |
| Caller frustration (repeat failures, explicit request) | Escalate to human per policy |
