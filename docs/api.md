# API Architecture

FastAPI, OpenAPI 3.1, Pydantic v2 schemas. Base path `/api/v1`.

## Resources
`/auth/*` · `/me` · `/organizations` · `/organizations/{id}/members` · `/agents` · `/agents/{id}/configuration` · `/agents/{id}/versions` · `/agents/{id}/publish` · `/agents/{id}/rollback` · `/agent-templates` · `/calls` · `/calls/{id}` (inspector: timeline, transcript, tools, latency, cost) · `/phone-numbers` · `/carriers` · `/knowledge` (bases, documents, search-test) · `/tools` · `/workflows` · `/integrations` · `/analytics` · `/billing` · `/test-sessions` (mint browser test call token) · `/webhooks/{provider}` (inbound).
Internal (worker → api, mTLS/token, not public): `/internal/v1/calls/resolve`, `/internal/v1/agent-versions/{id}/snapshot`.

## Conventions
- Auth: session cookie (dashboard) or `Authorization: Bearer <api key>`.
- Errors: RFC 9457 problem+json `{type, title, status, detail, code, request_id}`; no stack traces.
- Pagination: keyset `?limit=&cursor=`; response `{data, next_cursor}`.
- Filtering: explicit whitelisted query params.
- Idempotency: `Idempotency-Key` header on POSTs that create side effects (publish, number purchase, outbound call).
- Rate limits per API key/session/org (Redis token bucket), `429` + `Retry-After`.
- `X-Request-ID` propagated into logs and traces.
- Versioning: breaking changes → `/api/v2`; additive changes in place.

## Domain events
Envelope: `{id, type, version, occurred_at, organization_id, trace_id, data}`. Written to `outbox_events` in the same transaction as the state change, dispatched by `jobs` at-least-once; consumers idempotent by `id`.
Events: `agent.published`, `agent.rolled_back`, `call.started`, `call.ended`, `tool.executed`, `workflow.completed`, `handoff.requested`, `recording.created`, `transcript.completed`, `usage.recorded`, `integration.failed`, `knowledge.document.indexed`.
Outbound customer webhooks (post-MVP): signed HMAC, retries with backoff.
