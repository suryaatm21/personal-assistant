# Handoff — personal SMS agent

Picking this up in a fresh CC terminal session? Read this first. It captures
**what's done, what's deferred, what's next, and why** so the new session
can continue without re-deriving the design.

Source-of-truth references in this repo:
- [PLAN.md](./PLAN.md) — the approved build plan, with every design decision
  the user signed off on (trust model, RLS choice, per-user TZ, cron worker,
  iteration logging, dedup, etc.).
- [README.md](./README.md) — the public-facing setup guide for self-hosters.
- [schema.sql](./schema.sql) — the canonical Postgres schema. Apply this in
  Supabase first; everything else assumes these tables exist.

---

## Status snapshot (as of handoff)

**Boilerplate is fully written.** Every file in the plan exists and
syntax-checks (`python3 -m py_compile` clean across all 6 .py files).
**Nothing has been deployed or run end-to-end.**

```
personal-agent/
├── main.py                        # FastAPI: /health, /webhook
├── agent.py                       # Claude tool-use loop
├── tools.py                       # 12 tool schemas + dispatcher
├── db.py                          # Supabase queries (all user_id-scoped)
├── config.py                      # pydantic-settings env loader
├── worker/
│   ├── __init__.py
│   └── process_reminders.py       # Render cron job — every minute
├── schema.sql                     # 6 tables + RLS + dedup index
├── render.yaml                    # 2 services: web + cron
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md                      # Self-hosting guide
├── PLAN.md                        # Approved build plan
└── HANDOFF.md                     # This file
```

---

## Tech choices already locked in (don't re-litigate)

| Decision | Choice | Reason |
|---|---|---|
| LLM | Claude Haiku 4.5 (`claude-haiku-4-5`) | User-specified |
| Backend | FastAPI + uvicorn | User-specified |
| DB | Supabase Postgres, service-role key, RLS-on with no policies | Server-only access; RLS as defense-in-depth |
| Hosting | Render (web + cron services) | User picked from plan options |
| State | Stateless per SMS — no conversation history table | User picked from plan options |
| User identity | Twilio's signed `From` field (E.164) → `user_id` everywhere | No auth UI needed |
| Contact resolution | `contacts` table (name → phone), with E.164 fallback | User picked from plan options |
| Clear-by-match | Server-side `ILIKE`; ambiguity returns matches without clearing | User picked from plan options |
| Reminders | One-shot only (no recurring); per-user TZ via `users.timezone` | Plan scope |
| Notes ≡ Ideas | Single `notes` table; Claude treats both verbs as the same intent | User added in round 2 |
| Iteration cap | `MAX_TOOL_ITERATIONS=5` (env-tunable) + `events` table records iterations / tokens / latency for tuning | User added in round 2 |
| Cold-start dedup | Twilio `MessageSid` cached in `events`; retries return cached reply | Added per advisor review (see below) |

---

## What's been deferred (and the reasoning)

### Async-route blocking under concurrency
`main.py:webhook` is `async def` but calls **sync** `run_agent` (sync
Anthropic + sync Supabase). Concurrent inbound SMS will serialize behind
each other on the event loop. Acceptable for a household-scale instance.

If concurrency becomes an issue, the cleanest fix is wrapping the agent call
in a thread:
```python
import asyncio
result = await asyncio.to_thread(run_agent, user_id, body)
```
Or drop the `async` and let FastAPI run the route in its threadpool. Don't
introduce an async Anthropic / async Supabase client unless there's a real
reason — the sync clients are fine.

### Per-user rate limiting
Out of scope for v1. The `events` table has everything needed (rows per
user, timestamps); a `count > N` check in `main.py` before calling the
agent is ~10 lines. Add when there's an actual abuse case.

### Recurring reminders
Out of scope. `reminders.status` is `pending|sent|cancelled` — no
`recurrence_rule` column. Adding RRULE support means a real `dateutil`
dependency and a re-schedule step in the worker; do it when needed.

### Web dashboard / auth UI
Out of scope. The plan deliberately cut this so non-technical friends can
self-host without React.

---

## Open issues from advisor review (resolved + remaining)

| # | Issue | Status |
|---|---|---|
| 1 | Render terminates TLS; `request.url.scheme` would be `http`, breaking Twilio signature validation | **Fixed** in `render.yaml` via `--forwarded-allow-ips="*"` |
| 2 | Render free-tier ~30s cold start exceeds Twilio's 15s webhook timeout → Twilio retries → duplicate todos | **Fixed** via `twilio_message_sid` unique partial index + cache-lookup in `main.py:webhook` |
| 3 | Async route + sync agent serializes concurrent requests | **Deferred** (see "Async-route" above) |

---

## What the next session needs to do

The remaining work is **deployment + smoke testing**, not code. In rough
order:

### 1. Local sanity check (~15 min)
```bash
cd /Users/Surya/Projects/imessage-assistant
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Fill in real values from Supabase, Twilio, Anthropic.
echo 'SKIP_TWILIO_SIGNATURE_VALIDATION=true' >> .env
uvicorn main:app --reload
```
In another shell:
```bash
curl -X POST http://localhost:8000/webhook \
  -d 'From=+14155550100&Body=add to-do: handoff smoke test&MessageSid=SMtest1'
```
Expect TwiML reply containing "Got it" or similar; `todos` row in Supabase
under `user_id='+14155550100'`; `events` row with `iterations=2` (one tool
call + final text), `input_tokens` and `output_tokens` set.

### 2. Apply schema in Supabase
Open the project's SQL Editor and paste `schema.sql`. Verify:
```sql
select tablename, rowsecurity from pg_tables where schemaname='public';
-- All six tables should show rowsecurity = true.
```

### 3. Deploy to Render
- Push to GitHub.
- Render → New → Blueprint → connect repo. It creates two services from
  `render.yaml`: `personal-agent` (web) and `reminder-worker` (cron).
- **Render Blueprints don't share env vars across services** — fill in the
  six secrets (`ANTHROPIC_API_KEY`, `SUPABASE_URL`,
  `SUPABASE_SERVICE_ROLE_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
  `TWILIO_PHONE_NUMBER`) on **both** services. README step 5 documents this.

### 4. Wire up Twilio
Twilio Console → your number → Messaging → "A message comes in" → Webhook
POST `https://<your-web-service>.onrender.com/webhook`. Send a real SMS;
verify it round-trips and a row lands in both `todos` and `events`.

### 5. Verify the cron worker
- `python -m worker.process_reminders` locally (after schema + .env).
- After deploy: send `remind me in 2 minutes to test handoff`. Within 3
  minutes you should receive an SMS starting `Reminder: test handoff` from
  your Twilio number. Render dashboard → reminder-worker → Logs should
  show one row processed.

### 6. Spot-check cross-user isolation
```bash
curl -X POST <prod>/webhook -d 'From=+14155550999&Body=what are my todos?&...'
```
(With a valid Twilio signature — easiest to do by sending a real SMS from
a second phone, if available.) The reply should list 0 todos. Confirms
the `user_id` scoping works in production.

---

## Things to know that aren't obvious from the code

- **`user_id` is the raw E.164 phone number from Twilio.** No separate
  users-by-internal-id mapping. The string `+14155551234` is the primary
  key in `users` and the foreign key in every other table.
- **Claude never sees `user_id`.** Tool schemas have no `user_id` argument.
  The dispatcher injects it from the signed `From` field. Eliminates
  prompt-injection cross-tenant attacks.
- **The reminder worker's `select_due_reminders` is the only DB query
  not scoped by `user_id`** — by design; it scans across all users for due
  rows, then sends one SMS per user.
- **RLS is on with no policies.** That means: only the service role
  (which bypasses RLS) can read/write. If anyone ever points a browser
  client at this DB with the anon key, they get nothing. The application
  still filters by `user_id` explicitly — RLS is a second layer.
- **System-prompt time injection.** `agent.py` injects the user's current
  local time + timezone every turn so Claude can resolve "tomorrow at 3pm"
  to a real ISO timestamp. Defaults to `America/New_York` (overridable
  via `DEFAULT_TIMEZONE` env var); per-user override via
  `set_timezone` tool.
- **Iteration cap is `MAX_TOOL_ITERATIONS=5`.** Hitting it returns
  "Sorry, I got confused. Try rephrasing?" and logs the iteration count.
  Tune from real `events` data after a few days of use.

---

## Bug-watch list

Things that are correct now but easy to break in future edits:

1. **Don't move `user_id` into tool schemas.** It must come from the
   signed `From` field, never from Claude's tool input.
2. **Don't add a tool result to the assistant message.** `tool_result`
   blocks belong in a `user` message — Anthropic's tool-use protocol.
   `agent.py` does this correctly; preserve that.
3. **Don't call `get_or_create_user()` inside a tool.** It's already
   called once per webhook in `agent.run_agent`. Adding it elsewhere
   creates redundant round-trips.
4. **Don't drop `--forwarded-allow-ips="*"` from render.yaml.**
   Without it, Twilio signature validation 403s every real request
   because uvicorn sees `http://` instead of `https://`.
5. **Don't insert into `events` without a unique-conflict guard around
   the SID.** `main.py` catches the broad exception; that's the dedup
   safety net.

---

## Useful queries once you have data

```sql
-- Last 50 turns
select created_at, inbound_text, reply_text, iterations, latency_ms
from events order by created_at desc limit 50;

-- How often do we hit the iteration cap?
select count(*) from events where iterations >= 5;

-- Per-user volume in the last 24h
select user_id, count(*)
from events
where created_at > now() - interval '24 hours'
group by user_id order by 2 desc;

-- Slowest 10 requests
select inbound_text, latency_ms, iterations
from events order by latency_ms desc limit 10;

-- Token spend (Haiku 4.5 = $1/$5 per 1M)
select sum(input_tokens)*0.000001 as input_dollars,
       sum(output_tokens)*0.000005 as output_dollars
from events
where created_at > now() - interval '7 days';
```

---

## If something is broken on resume

| Symptom | First thing to check |
|---|---|
| `/webhook` 403s on real Twilio requests | `--forwarded-allow-ips="*"` in render.yaml; restart the web service |
| Curl-to-localhost 403s | `SKIP_TWILIO_SIGNATURE_VALIDATION=true` in `.env` |
| Reminders never fire | Render dashboard → reminder-worker → Logs. Free cron tier should run every minute. |
| Duplicate todos on cold-start request | Check `twilio_message_sid` is being read from form (`MessageSid`, not `messagesid`) |
| Claude returns "Sorry, I got confused" | Iteration cap. Check `events.iterations` for the row. Increase `MAX_TOOL_ITERATIONS`. |
| Cross-user todos visible | **Stop everything.** Verify every `db.py` helper has `.eq('user_id', user_id)`. RLS should be the second-layer catch but the app is the primary safeguard. |
