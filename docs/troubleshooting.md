# Troubleshooting (living document)

| Symptom | Check |
|---|---|
| Inbound call never reaches agent | Carrier points to LiveKit SIP host; inbound trunk `numbers` match E.164; trunk auth/allowed addresses; dispatch rule `trunk_ids`/`inbound_numbers`; `room_config.agents` agent_name = worker `agent_name`; worker registered (logs); SIP ports 5060 + RTP 10000-20000 reachable. |
| One-way or no audio | RTP port range open, `use_external_ip`, NAT, codec support of carrier. |
| Agent joins but silent | Provider keys present in worker env; STT/TTS errors in logs; snapshot load failure. |
| High latency | Inspector timeline: which segment (EOU, STT, TTFT, tool, TTS)? Region distance between worker, LiveKit and providers; prompt size; tool slowness. |
| Agent talks over caller / cuts off | Interruption mode/min_words; endpointing min/max delay; false interruption settings. |
| Transfer fails | Call answered?; carrier supports REFER (Twilio: enable; Plivo: default); `transfer_to` format; `sip_transfer_reason` in error metadata. |
| Cross-tenant data seen | Treat as SEV-1: disable affected endpoint, check RLS policy and `app.org_id` setting, run isolation suite. |
| Webhook rejected | Signature secret, clock skew, duplicate event id. |
