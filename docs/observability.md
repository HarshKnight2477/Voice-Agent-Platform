# Observability Architecture

## Correlation ids
`request_id`, `tenant_id`, `agent_id`, `agent_version_id`, `call_id`, `conversation_id`, `tool_execution_id`, `workflow_execution_id`, `trace_id`. Bound to a contextvar logger in every process.

## Logs
structlog JSON; levels; no transcripts, no secrets, caller numbers masked (last 4). Error tracking via Sentry (or equivalent).

## Metrics (per call and aggregated)
STT, EOU, LLM TTFT, TTS TTFB, tool latency, end-to-end per turn (from LiveKit `ChatMessage.metrics` and per-plugin `metrics_collected`), interruptions/false interruptions, retries, errors by provider, call duration, tokens, audio seconds, TTS characters, AI and telephony cost. Stored in `messages`/`call_events` for the inspector and exported as OTel metrics for dashboards/alerts.

## Traces
OpenTelemetry: API requests, jobs, and LiveKit session traces (LiveKit Agents exports OTel traces — exporter setup verified in Phase 3). Each call is one trace root with spans per turn/tool.

## Call inspector timeline
Built from `call_events`: caller speech start/end → STT final → LLM start/first token → tool start/end → TTS first byte → playback end, plus interruptions, transfers, errors.

## Alerts
p95 e2e latency over budget, provider error rate, failed calls rate, transfer spikes, worker capacity, budget thresholds, webhook failures.
