# Database Design

PostgreSQL 16, `pgvector`, `pgcrypto`. SQLAlchemy 2 (async) + Alembic. UUIDv7 primary keys. `created_at/updated_at` everywhere; `deleted_at` soft delete where noted.

## Tenant isolation
- Every tenant-owned table has `organization_id uuid not null` + FK + index (usually leading a composite index).
- **RLS enabled and forced** on all tenant tables: `USING (organization_id = current_setting('app.org_id')::uuid)`.
- The API sets `SET LOCAL app.org_id` inside each request transaction from the authenticated session — never from request input.
- App role has no `BYPASSRLS`. A separate migration/admin role is used only by migrations and audited platform-admin tools.
- Composite FKs `(organization_id, id)` on child tables prevent cross-tenant references.
- Isolation test suite runs every endpoint with two tenants in CI.

## Entities

**Identity & tenancy**: `users` (email citext unique, password_hash argon2id, email_verified_at, mfa), `user_sessions`, `mfa_factors`, `api_keys` (hash only, prefix, scopes, org), `organizations` (name, slug, timezone, plan, settings, data_retention_days), `memberships` (user, org, role), `roles` (system roles OWNER, ADMIN, MANAGER, AGENT_MANAGER, ANALYST, VIEWER), `permissions`, `role_permissions`, `invitations`.

**Agents**: `agent_templates` (platform-owned, industry, defaults json), `agents` (org, name, template_id, published_version_id, status, deleted_at), `agent_configurations` (draft, 1:1 agent, json validated by schema), `voice_configurations`, `agent_versions` (agent, number, status testing|published|retired, snapshot jsonb, snapshot_hash, created_by, published_at; unique(agent_id, number); immutable via trigger).

**Knowledge**: `knowledge_bases`, `knowledge_documents` (source type, storage uri, status, checksum), `knowledge_chunks` (document, content, tokens, metadata, `embedding vector(n)`, HNSW index, org-scoped).

**Tools & workflows**: `tools` (platform or tenant-owned, name, version, input_schema, output_schema, permissions required, timeout_ms, retry_policy, rate_limit, side_effect bool), `tool_permissions` (agent_version, tool, granted scopes), `workflows`, `workflow_versions` (definition jsonb, immutable).

**Integrations**: `integrations` (org, type, status, scopes), `integration_credentials` (envelope-encrypted blob, key id), `mcp_servers` (approved list, url, allowed tools, auth ref).

**Telephony**: `livekit_deployments`, `carriers` (org or platform, provider type, sip config ref, capabilities: refer, tls), `phone_numbers` (E.164 unique, org, carrier, deployment, agent_id, routing rules, business hours), `sip_trunks` (our record ↔ LiveKit trunk id), `dispatch_rules`.

**Calls**: `calls` (org, agent_id, agent_version_id, phone_number_id, direction, from, to, status, started/ended, duration, outcome, intent, cost_estimate, livekit room/ids), `conversations`, `participants`, `messages` (role, text, timing, latency fields), `tool_executions` (call, tool, args redacted, result summary, status, latency, error), `workflow_executions`, `handoffs`, `recordings` (storage uri, retention_until), `transcripts`, `call_events` (timeline).

**Metering & billing**: `usage_records` (org, call, kind llm_tokens|stt_seconds|tts_chars|telephony_minutes|storage, quantity, unit_cost, provider, idempotency key), `budgets`, `subscriptions`.

**Platform**: `audit_logs` (append-only, actor, org, action, target, ip, request_id), `outbox_events` (type, version, payload, status), `processed_webhooks` (provider, event_id unique).

## Rules
- All list endpoints keyset-paginated; no unbounded queries.
- Large append tables (`messages`, `call_events`, `usage_records`, `audit_logs`) partitioned by month when volume requires.
- Transactions wrap state changes + outbox insert.
- PII columns (caller numbers in some tables, transcripts) subject to retention jobs; sensitive fields encrypted at application level where required.
