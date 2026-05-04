# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Local development

```bash
cp .env.example .env
# fill in values, then:
pip install -r requirements.txt
echo 'SKIP_TWILIO_SIGNATURE_VALIDATION=true' >> .env
uvicorn main:app --reload
```

Test the webhook locally:
```bash
curl -X POST http://localhost:8000/webhook \
  -d 'From=+14155550100&Body=add to-do: test it works'
```

Test the reminder worker:
```bash
python -m worker.process_reminders
```

## Architecture

**Request path:** Twilio POST `/webhook` → validate signature → extract `user_id` from signed `From` field → `run_agent()` → log to `events` table → TwiML reply.

**Agent loop** (`agent.py`): Stateless per SMS. Calls Claude with tool schemas, executes tools server-side, loops up to `MAX_TOOL_ITERATIONS` times. No conversation history between messages.

**Security invariant:** `user_id` is never exposed to Claude — it's injected by `tools.dispatch()` from the verified Twilio `From` field. Tool schemas have no `user_id` parameter, making cross-tenant prompt injection impossible by design.

**Retry dedup:** Twilio retries on timeout (common on Render free-tier cold starts). The `events` table has a unique index on `twilio_message_sid`; the webhook checks this before running the agent to avoid double-logging.

**Reminder worker** (`worker/process_reminders.py`): Deployed as a Render cron job (every minute). Queries `reminders` where `status='pending'` and `remind_at <= now()`, sends SMS via Twilio, marks `status='sent'`.

## Key files

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app, Twilio webhook + health check |
| `agent.py` | Claude tool-use loop, `run_agent()` |
| `tools.py` | Tool schemas (sent to Claude) + `dispatch()` (runs server-side) |
| `db.py` | All Supabase queries, user upsert |
| `config.py` | `Settings` via pydantic-settings (reads `.env`) |
| `schema.sql` | Full DB schema with RLS — run this in Supabase SQL Editor |
| `worker/process_reminders.py` | Cron job for due reminders |
| `render.yaml` | Render Blueprint — two services: web + cron |

## Configuration

All tuning via env vars (see `config.py`):
- `ANTHROPIC_MODEL` — default `claude-haiku-4-5`
- `MAX_TOOL_ITERATIONS` — default `5`; raise if events table shows many rows hitting the cap
- `DEFAULT_TIMEZONE` — default `America/New_York`
- `SKIP_TWILIO_SIGNATURE_VALIDATION` — set `true` for local curl testing only

## Observability

Every inbound SMS writes one row to the `events` table (`inbound_text`, `iterations`, `latency_ms`, `input_tokens`, `output_tokens`, `error`). Query it directly in Supabase to debug or tune.
