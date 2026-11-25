# Disaster Recovery

## Targets (initial)
RPO ≤ 5 minutes (Postgres PITR), RTO ≤ 1 hour for control plane. Realtime plane: in-flight calls on a failed worker are lost (callers redial); new calls route to healthy workers within seconds.

## Backups
Postgres continuous WAL archiving + daily snapshots, 30-day retention, encrypted; object storage versioning for recordings/documents; secrets backed up in the secret manager; restore tested monthly.

## Failure scenarios
| Scenario | Response |
|---|---|
| Worker crash | LiveKit reassigns new jobs to other workers; call state in Redis allows partial recovery for reconnects. |
| LiveKit deployment down | Fail over phone numbers to a standby deployment (carrier-side secondary SIP URI); status page. |
| Postgres down | Workers keep serving calls with cached snapshots; writes buffered to Redis/outbox and replayed; dashboard read-only/down. |
| Provider outage (LLM/STT/TTS) | Fallback provider (post-MVP); else safe message + transfer. |
| Carrier outage | Secondary carrier per number where configured (BYOC). |
| Region loss (later) | Regional deployments, DNS failover. |

## Runbooks
Incident severity levels, on-call, communication templates, post-incident review. Maintained alongside this file.
