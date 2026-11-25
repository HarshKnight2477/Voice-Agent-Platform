# Security Architecture

## Threat model highlights
SQLi, XSS, CSRF, SSRF, broken access control, tenant leakage, prompt injection, tool abuse, credential theft, webhook spoofing/replay, rate-limit abuse, malicious uploads, data exfiltration, session theft, privilege escalation, toll fraud on outbound SIP.

## Multi-tenancy
- Tenant context is derived **only** from the authenticated session/API key → `TenantContext(org_id, user_id, role, permissions)`. Any `organization_id` in a request body or path is checked against it, never trusted.
- Defense in depth: route dependency → service-layer authorization → tenant-scoped repository → Postgres RLS.
- Realtime plane: worker derives tenant from `phone_number_id`/`agent_version_id` lookup, not from caller-supplied data.

## RBAC
| Role | Summary |
|---|---|
| OWNER | Everything incl. billing, deletion of org, ownership transfer |
| ADMIN | Everything except ownership/billing ownership |
| MANAGER | Manage agents, numbers, knowledge, members (non-admin) |
| AGENT_MANAGER | Create/edit/test/publish agents, knowledge, tools config |
| ANALYST | Read calls, transcripts (if permitted), analytics |
| VIEWER | Read-only dashboards; no transcripts/recordings by default |
Permissions are fine-grained strings (`agents.publish`, `calls.transcript.read`, `recordings.download`…); roles map to permission sets; checks are server-side only.

## Authentication
Email+password (argon2id), email verification, password reset (single-use, short-lived tokens), TOTP MFA, server-side sessions in httpOnly Secure SameSite=Lax cookies + CSRF token for state-changing requests, session/device list and revoke. API keys: hashed, prefixed, scoped, org-bound. OAuth/SSO later.

## Tool permission model
- Tools declare required permission scopes (`customer:read`, `customer:write`, `reservation:create`…).
- Agent versions hold explicit grants. No implication between scopes (read ≠ write ≠ delete).
- State-changing tools require `pending_confirmation` satisfied in state (business-rule layer) unless policy waives it.
- Every execution audited. Model output can never widen grants.

## Prompt-injection defense
Hierarchy: platform policy > tenant policy > agent policy > workflow rules > caller input > retrieved data.
- Retrieved docs, tool outputs, MCP responses wrapped and labelled as untrusted data; instruction-like text is not executed.
- Ingestion scanner flags documents containing instruction patterns for review.
- Permissions are enforced by the gateway, so injected text cannot cause unauthorised actions even if the model is fooled.
- Adversarial test scenarios in the eval suite (see testing.md).

## Webhooks (inbound)
Verify signature (LiveKit signed JWT; carrier/integration HMAC), check timestamp window (≤ 5 min), reject replays via `processed_webhooks(event_id)`, schema-validate, rate-limit, process idempotently.

## Secrets & data
- Secrets from environment/secret manager; `.env` git-ignored; `.env.example` only. Pre-commit secret scan (gitleaks).
- Integration credentials envelope-encrypted (data key per record, master key from KMS / local key file in dev).
- Provider keys never reach browsers.
- SSRF: outbound fetches (website ingestion, custom REST tools, MCP) go through an egress guard blocking private/link-local/metadata IPs and enforcing allow-lists.
- Uploads: type sniffing, size limits, parsing in isolated job process, no execution.
- Security headers (CSP, HSTS, frame-ancestors), strict CORS.
- Dependency scanning (pip-audit, npm audit) and SAST in CI.

## Recordings & PII
Per-tenant/agent settings: recording on/off, transcript on/off, retention days, PII redaction (transcript), access/download/export/delete permissions. Retention purge job. Logs never contain transcripts or secrets.

## Compliance
No HIPAA, PCI, SOC 2 or GDPR claims until the controls are implemented and independently verified. Regulated-industry templates ship with configurable controls (no recording, short retention, redaction, stricter verification) instead.
