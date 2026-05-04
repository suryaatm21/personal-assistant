# Personal SMS Agent — Build Plan

## Context

You want a personal AI second brain you interact with via SMS. Text a Twilio number; Claude Haiku 4.5 parses intent, calls one of a few tools, and replies. Multi-tenant from day one (user identity = phone number) so friends can share your instance, and self-hostable so they can also fork and run their own.

Adding to the original spec based on follow-up:
- **Reminders/scheduling** is in scope (was originally out).
- **Notes ≡ ideas** — one table named `notes`, with Claude treating "idea", "note", "thought", "remember this" as the same intent.
- **Event logging** — every inbound SMS records iteration count, tools called, tokens, and latency so `MAX_TOOL_ITERATIONS` can be tuned from real data.
- **RLS on by default** — defense-in-depth alongside server-side `user_id` scoping.

Working directory `/Users/Surya/Projects/imessage-assistant/` is empty — full greenfield build.

---

## Trust & cost model (read this before deploying)

A single deployed instance is **multi-tenant**: every user texting your Twilio number is identified by their phone number and stored in your Supabase. But all users share **one** set of provider accounts:

| Resource | Whose account | Who pays |
|---|---|---|
| Anthropic API key | yours | you |
| Twilio number + per-SMS fees | yours | you |
| Supabase database | yours | you (free tier likely fine) |
| Render hosting | yours | you (free or $7/mo) |

So the two sensible deployment models are:

1. **Household / friends-and-family** — you run one instance, give the number to people you trust; you pay; data isolation is enforced by the app + RLS.
2. **Each-friend-self-hosts** — your friend forks the repo, creates their own Supabase / Twilio / Anthropic / Render accounts, deploys their own instance with their own keys. Multi-tenant code is still useful because *they* can then share *their* instance with *their* friends.

The README must call this out plainly so a friend doesn't accidentally rack up charges on your account.

---

## Architecture

```
Twilio inbound SMS  ──► POST /webhook (FastAPI, Render web service)
                          │
                          ├─ validate Twilio signature
                          ├─ user_id = From (E.164)
                          ├─ Claude Haiku 4.5 (tool-use loop)
                          │     ├─ log_todo / log_note
                          │     ├─ list_todos / list_notes
                          │     ├─ clear_todos
                          │     ├─ schedule_reminder / list_reminders / cancel_reminder
                          │     ├─ add_contact / list_contacts
                          │     ├─ set_timezone
                          │     └─ send_sms (Twilio REST)
                          ├─ Supabase Postgres (RLS on, every query also scoped by user_id)
                          ├─ events table (one row per inbound SMS)
                          └─ TwiML reply

Render cron job (every minute) ──► python -m worker.process_reminders
                          │
                          ├─ SELECT reminders WHERE status='pending' AND remind_at <= now()
                          ├─ Twilio REST messages.create(from=TWILIO_NUMBER, to=user_id, body=…)
                          └─ UPDATE status='sent'
```

**Tool-use loop**: standard Anthropic pattern — Claude returns `tool_use` blocks; server executes each, returns `tool_result`; Claude either calls more tools or returns final text. Hard cap `MAX_TOOL_ITERATIONS=5` (env-tunable) so a confused turn can't burn unbounded tokens. Real iteration count is logged per request so the cap can be tuned later.

**Stateless per SMS**: each inbound SMS is one independent Claude turn. No conversation history table.

---

## Data model — `schema.sql`

```sql
create extension if not exists pgcrypto;

-- per-user settings (timezone, etc). Auto-created on first SMS.
create table users (
  user_id     text primary key,                       -- E.164 phone, e.g. +14155551234
  timezone    text not null default 'America/New_York',
  created_at  timestamptz not null default now()
);

create table todos (
  id          uuid primary key default gen_random_uuid(),
  user_id     text not null,
  content     text not null,
  status      text not null default 'active' check (status in ('active','done')),
  created_at  timestamptz not null default now()
);
create index todos_user_status_created on todos (user_id, status, created_at desc);

create table notes (
  id          uuid primary key default gen_random_uuid(),
  user_id     text not null,
  content     text not null,
  created_at  timestamptz not null default now()
);
create index notes_user_created on notes (user_id, created_at desc);

create table reminders (
  id          uuid primary key default gen_random_uuid(),
  user_id     text not null,
  content     text not null,
  remind_at   timestamptz not null,
  status      text not null default 'pending' check (status in ('pending','sent','cancelled')),
  created_at  timestamptz not null default now()
);
create index reminders_due on reminders (status, remind_at) where status = 'pending';
create index reminders_user_created on reminders (user_id, created_at desc);

create table contacts (
  id          uuid primary key default gen_random_uuid(),
  user_id     text not null,
  name        text not null,
  phone       text not null,                          -- E.164
  created_at  timestamptz not null default now()
);
create unique index contacts_user_lower_name on contacts (user_id, lower(name));
create index contacts_user on contacts (user_id);

-- one row per inbound SMS turn, for cost / quality / iteration-count analysis.
create table events (
  id              uuid primary key default gen_random_uuid(),
  user_id         text not null,
  created_at      timestamptz not null default now(),
  inbound_text    text not null,
  reply_text      text,
  iterations      int  not null default 0,            -- tool-use loop count
  tool_calls      jsonb not null default '[]'::jsonb, -- [{name, args, result_summary}]
  input_tokens    int,
  output_tokens   int,
  latency_ms      int,
  model           text,
  error           text                                -- null if successful
);
create index events_user_created on events (user_id, created_at desc);
create index events_iterations on events (iterations);
```

### RLS (defense-in-depth)

The FastAPI server uses the Supabase **service role key**, which bypasses RLS by default. So application-side `WHERE user_id = $1` filtering is the *primary* safeguard. RLS is added on top so that if a future bug ever omits the `user_id` filter, the database itself still refuses cross-tenant access.

Implementation:

```sql
alter table users     enable row level security;
alter table todos     enable row level security;
alter table notes     enable row level security;
alter table reminders enable row level security;
alter table contacts  enable row level security;
alter table events    enable row level security;

-- Service role is exempt from RLS by default (Supabase grants it BYPASSRLS),
-- so the FastAPI app continues to work. To opt into RLS-enforced reads even
-- from the service role, the app would need to SET LOCAL request.user_id and
-- the policies below would gate on it. We keep the simpler model: app filters
-- by user_id, RLS catches mistakes for any *non-service-role* connection.

-- Default policy: deny all for anon / authenticated roles.
-- (No grants are added; absence of a permissive policy = deny.)
```

In other words: RLS being **on with no policies** means the only role that can read/write is the service role (which bypasses RLS). If anyone ever points a Supabase frontend client at this DB with the anon key, they get nothing. The README explains this and tells advanced users how to add per-user JWT policies if they want browser access later.

---

## Tool definitions — `tools.py`

Tool schemas given to Claude (Anthropic tool-use format) and corresponding Python implementations. Every tool implementation receives `user_id` injected by the dispatcher in [agent.py](agent.py) — Claude never passes `user_id` itself.

| Tool | Args | Behavior |
|------|------|----------|
| `log_todo` | `content` | Insert into `todos`, status=active. |
| `log_note` | `content` | Insert into `notes`. (Triggered by "idea", "note", "thought", "remember", "save".) |
| `list_todos` | `filter_hours?: int` | Active only, optional time window, cap 50, newest first. |
| `list_notes` | `filter_hours?: int` | Cap 50, newest first. |
| `clear_todos` | `target: "all"` *or* `match_text: str` | `"all"` → mark all active done. `match_text` → `ILIKE '%match%'` on active. 1 match → done. 0 → `cleared_count=0`. 2+ → return matches without clearing so Claude can ask which. |
| `schedule_reminder` | `content: str, remind_at_iso: str` | Claude resolves natural language ("tomorrow at 3pm") to ISO 8601 with offset using the user's timezone (injected into system prompt). Server validates parses as future timestamp. |
| `list_reminders` | `filter: "pending"\|"all" = "pending"` | Pending sorted by `remind_at` asc. Cap 50. |
| `cancel_reminder` | `match_text: str` | Same matching semantics as `clear_todos`. Sets status='cancelled'. |
| `set_timezone` | `tz: str` | IANA name (e.g. `America/Los_Angeles`). Validate via `zoneinfo.ZoneInfo`. Upsert into `users`. |
| `add_contact` | `name, phone` | Normalize phone to E.164 via `phonenumbers`. Upsert on `(user_id, lower(name))`. |
| `list_contacts` | — | All contacts for user. |
| `send_sms` | `to, message` | If `to` is E.164 → use directly. Else lookup in contacts (case-insensitive exact, then unique substring). Ambiguous → return matches; Claude asks which. Then Twilio REST `messages.create`. |

---

## File-by-File

### [config.py](config.py)
`pydantic-settings` loading from `.env`:
```
ANTHROPIC_API_KEY
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
TWILIO_PHONE_NUMBER
DEFAULT_TIMEZONE=America/New_York
MAX_TOOL_ITERATIONS=5
ANTHROPIC_MODEL=claude-haiku-4-5
SKIP_TWILIO_SIGNATURE_VALIDATION=false  # only flip on for local curl testing
```

### [db.py](db.py)
Single `supabase: Client` from `create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)`. Thin helpers — `get_or_create_user`, `insert_todo`, `select_active_todos`, `mark_todos_done_ilike`, `mark_all_todos_done`, `insert_note`, `insert_reminder`, `select_due_reminders`, `mark_reminder_sent`, `cancel_reminder_ilike`, `upsert_contact`, `find_contact`, `insert_event`. Every helper takes `user_id` first and includes `.eq('user_id', user_id)`. The reminder *worker* helper (`select_due_reminders`) is the one query *not* scoped by user_id — by design; it scans across all users for due rows.

### [tools.py](tools.py)
Two exports:
1. `TOOL_SCHEMAS` — `tools=[...]` list for `client.messages.create(...)`.
2. `dispatch(name, args, user_id) -> dict` — pure function, easily unit-testable. Returns dict that becomes the `tool_result` content.

### [agent.py](agent.py)
```python
async def run_agent(user_id: str, user_text: str) -> tuple[str, EventLog]:
    user = db.get_or_create_user(user_id)            # ensures users row + timezone
    now_local = datetime.now(ZoneInfo(user.timezone)).isoformat()
    system = SYSTEM_PROMPT.format(now_local=now_local, tz=user.timezone)

    messages = [{"role": "user", "content": user_text}]
    tool_calls_log = []
    iterations = 0
    in_tokens = out_tokens = 0

    for _ in range(MAX_TOOL_ITERATIONS):
        iterations += 1
        resp = client.messages.create(
            model=ANTHROPIC_MODEL, system=system,
            tools=TOOL_SCHEMAS, max_tokens=512, messages=messages,
        )
        in_tokens  += resp.usage.input_tokens
        out_tokens += resp.usage.output_tokens

        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            return text or "Done.", EventLog(iterations, tool_calls_log, in_tokens, out_tokens)

        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = tools.dispatch(block.name, block.input, user_id)
                tool_calls_log.append({"name": block.name, "args": block.input,
                                       "result_summary": _summarize(result)})
                tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                     "content": json.dumps(result)})
        messages.append({"role": "user", "content": tool_results})

    return "Sorry, I got confused. Try rephrasing?", EventLog(iterations, tool_calls_log, in_tokens, out_tokens)
```

System prompt:

```
You are a personal assistant for a single user, communicating over SMS.
Current time for this user: {now_local}  (timezone: {tz})

Tools let you log todos and notes, list them, clear them, schedule reminders,
manage contacts, set timezone, and send SMS to others.

Style:
- Replies under ~160 chars when possible. Concatenated SMS works but short is better.
- Lists: numbered, one per line, no extra prose ("1. fix Unity bug\n2. email Jordan").
- Treat "idea", "note", "thought", "remember this", "save" → log_note.
- Treat "to-do", "todo", "remind me to <verb>", "task" → log_todo.
- Treat "remind me at/on/in <time> to …" or "set a reminder" → schedule_reminder
  with remind_at_iso resolved to ISO 8601 in the user's timezone.
- If a tool returns ambiguous matches, ask the user which one in one short line.
- If the request is unclear, ask one short clarifying question; don't guess.
- Never invent IDs, contact phone numbers, or timestamps — use only tool outputs.
```

### [main.py](main.py)
```python
@app.post("/webhook")
async def webhook(request: Request):
    form = await request.form()
    if not SKIP_TWILIO_SIGNATURE_VALIDATION:
        if not RequestValidator(TWILIO_AUTH_TOKEN).validate(
            str(request.url), dict(form), request.headers.get("X-Twilio-Signature","")):
            raise HTTPException(403)
    user_id = form["From"]
    body    = form["Body"].strip()
    t0 = time.monotonic()
    try:
        reply, log = await run_agent(user_id, body)
        latency_ms = int((time.monotonic() - t0) * 1000)
        db.insert_event(user_id=user_id, inbound_text=body, reply_text=reply,
                        iterations=log.iterations, tool_calls=log.tool_calls,
                        input_tokens=log.in_tokens, output_tokens=log.out_tokens,
                        latency_ms=latency_ms, model=ANTHROPIC_MODEL, error=None)
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        db.insert_event(user_id=user_id, inbound_text=body, reply_text=None,
                        iterations=0, tool_calls=[], input_tokens=None,
                        output_tokens=None, latency_ms=latency_ms,
                        model=ANTHROPIC_MODEL, error=repr(e))
        reply = "Something went wrong. Try again in a sec."
    return Response(content=twiml(reply), media_type="application/xml")

@app.get("/health")
def health(): return {"ok": True}
```

### [worker/process_reminders.py](worker/process_reminders.py)
Standalone script invoked by Render cron once per minute:
```python
def main():
    due = db.select_due_reminders(limit=100)   # status=pending and remind_at <= now()
    for r in due:
        try:
            twilio.messages.create(from_=TWILIO_PHONE_NUMBER, to=r.user_id,
                                   body=f"⏰ Reminder: {r.content}")
            db.mark_reminder_sent(r.id)
        except Exception as e:
            log.exception("reminder send failed for %s", r.id)  # leave pending; retry next minute

if __name__ == "__main__": main()
```
The default reminder text is the only place an emoji appears; it's optional and easy to remove if you'd rather avoid them.

### [tools.py](tools.py)
See tool table above. Validation details:
- `schedule_reminder`: parse `remind_at_iso` with `datetime.fromisoformat`; reject if `< now() + 30s`. If `tzinfo is None`, attach the user's timezone before storing.
- `set_timezone`: `ZoneInfo(tz)` raises if invalid → return `{ok: false, error: "unknown timezone"}`.

### [requirements.txt](requirements.txt)
```
fastapi
uvicorn[standard]
anthropic>=0.40
supabase>=2.0
twilio>=9.0
phonenumbers
pydantic-settings
python-multipart
tzdata          # ensures zoneinfo works on slim base images
```
Pin minor versions in the actual file. Implementation should consult the `claude-api` skill and `mcp__plugin_context7_context7__query-docs` for the current Anthropic SDK tool-use shape before coding (the SDK evolves; Haiku 4.5 model id and `messages.create` signature should be verified against current docs).

### [render.yaml](render.yaml)
```yaml
services:
  - type: web
    name: personal-agent
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn main:app --host 0.0.0.0 --port $PORT
    healthCheckPath: /health
    envVars: &shared
      - key: ANTHROPIC_API_KEY
        sync: false
      - key: SUPABASE_URL
        sync: false
      - key: SUPABASE_SERVICE_ROLE_KEY
        sync: false
      - key: TWILIO_ACCOUNT_SID
        sync: false
      - key: TWILIO_AUTH_TOKEN
        sync: false
      - key: TWILIO_PHONE_NUMBER
        sync: false
      - key: DEFAULT_TIMEZONE
        value: America/New_York
      - key: ANTHROPIC_MODEL
        value: claude-haiku-4-5
      - key: MAX_TOOL_ITERATIONS
        value: "5"

  - type: cron
    name: reminder-worker
    runtime: python
    buildCommand: pip install -r requirements.txt
    schedule: "* * * * *"            # every minute
    command: python -m worker.process_reminders
    envVars: *shared
```

### [.env.example](.env.example)
All env vars listed with one-line comments and links to where to find each.

### [README.md](README.md)
Sections, written for a non-technical friend:
1. **What this is** — three sentences + 6-line example SMS exchange.
2. **Trust & costs** — the table from "Trust & cost model" above. Plainly state: if you share your number, your friends' SMS spend money on your accounts.
3. **Prerequisites** — accounts: Supabase, Twilio (paid number ~$1/mo), Anthropic, Render, GitHub.
4. **Setup**:
   1. Fork the repo.
   2. Supabase: new project, SQL Editor, paste `schema.sql`, run.
   3. Twilio: buy a number.
   4. Anthropic: get API key.
   5. Render: New → Blueprint → connect fork → fill env vars when prompted. Render reads `render.yaml` and creates **two** services: a web service and a cron worker. Both share env vars via the YAML anchor.
   6. Copy the deployed URL.
   7. In Twilio Console → your number → "A message comes in" → POST to `https://…/webhook`.
5. **Try it** — text the number `add to-do: test it works` then `what's on my list?`.
6. **Local development** — `cp .env.example .env`, set `SKIP_TWILIO_SIGNATURE_VALIDATION=true`, `pip install -r requirements.txt`, `uvicorn main:app --reload`. For real Twilio testing locally, `ngrok http 8000` and point Twilio at the ngrok URL (and re-enable signature validation).
7. **Tuning** — `events` table has `iterations`, `latency_ms`, `tool_calls`. Run a quick query to see how often turns hit the `MAX_TOOL_ITERATIONS` cap; raise/lower as needed.
8. **Costs / limits** — Twilio number ~$1/mo + per-SMS, Anthropic per-token (Haiku 4.5 is cheap), Supabase free tier, Render free web tier (cold-starts on idle) or $7/mo always-on. Cron service runs on Render's cron tier (free).

---

## SMS examples (expected behavior)

| User SMS | Tools Claude calls | Reply |
|----------|--------------------|-------|
| `remind me to email Jordan` | `log_todo("email Jordan")` | `Got it.` |
| `note: build a recipe app` | `log_note("build a recipe app")` | `Saved.` |
| `idea — eSIM backup plan` | `log_note("eSIM backup plan")` | `Saved.` |
| `what are my todos?` | `list_todos()` | `1. email Jordan\n2. fix Unity bug` |
| `notes from this week` | `list_notes(filter_hours=168)` | numbered list |
| `clear my list` | `clear_todos(target="all")` | `Cleared 4.` |
| `clear the Unity bug task` | `clear_todos(match_text="Unity bug")` | `Done: fix Unity null ref bug` |
| `clear the bug` (2 matches) | `clear_todos(match_text="bug")` returns matches | `Which one?\n1. fix Unity bug\n2. fix login bug` |
| `remind me tomorrow at 3pm to call mom` | `schedule_reminder("call mom","2026-05-04T15:00:00-04:00")` | `Reminder set for tomorrow 3:00 PM.` |
| `what reminders do I have?` | `list_reminders()` | `1. call mom — Tue 3:00 PM` |
| `cancel the call mom reminder` | `cancel_reminder("call mom")` | `Cancelled.` |
| `set timezone to Pacific` | `set_timezone("America/Los_Angeles")` | `Timezone set to America/Los_Angeles.` |
| `add contact Jordan +14155551234` | `add_contact("Jordan","+14155551234")` | `Added Jordan.` |
| `text Jordan that I'm running late` | `send_sms("Jordan","I'm running late")` | `Sent to Jordan.` |
| `asdfgh` | none | `Not sure — try "what are my todos?" or "remind me to …"` |

---

## Security

1. **Twilio signature validation** — `RequestValidator(TWILIO_AUTH_TOKEN).validate(...)` on every `/webhook`. Reject 403 on mismatch. Without this, anyone with the URL can spoof inbound SMS and dump every user's todos via crafted `From=` values.
2. **user_id scoping** — `user_id` derived server-side from Twilio's signed `From` field, injected into every `db.py` call. Tools never receive `user_id` from Claude — eliminates prompt-injection cross-tenant leakage.
3. **RLS** — enabled on every table, no policies → only service role (server) can read/write. Defense-in-depth if a future query forgets `WHERE user_id`.
4. **send_sms abuse** — out of v1 scope (mentioned in README as v2: per-user rate limiting via `events` count over the last hour). For now, trust boundary is whoever you give the number to.
5. **Service role key** — never logged, never returned in SMS, only read at startup.

---

## Verification

1. **Schema apply**: in a Supabase project, run `schema.sql` — six tables + indexes exist, RLS enabled on all (`SELECT relrowsecurity FROM pg_class WHERE relname IN (...)` → all `t`).
2. **Local server** (with `SKIP_TWILIO_SIGNATURE_VALIDATION=true`):
   ```
   curl -X POST localhost:8000/webhook \
        -d "From=+14155550100&Body=remind me to test"
   ```
   → TwiML reply containing "Got it" or similar; `todos` row inserted; `events` row inserted with `iterations=2` (one tool call + final text).
3. **List**: `Body=what are my todos?` → reply contains "test"; `events` shows `tool_calls: [{name:"list_todos",...}]`.
4. **Cross-user isolation**: same query with `From=+14155550999` → reply lists 0; the row from step 2 untouched.
5. **Clear ambiguity**: insert two todos containing "bug", send `Body=clear the bug` → reply asks which. Both still active.
6. **Reminder roundtrip**: send `Body=remind me in 2 minutes to test`. Verify `reminders` row with `remind_at ≈ now()+2min`. Run `python -m worker.process_reminders` after 2 minutes → row's status flips to `sent`; Twilio sandbox shows outbound SMS to the test number. (For local: stub Twilio client or use a Twilio test credential.)
7. **send_sms by name**: add a contact pointing at your own second phone, send `text TestName that hello` → second phone receives the SMS.
8. **Timezone**: `set timezone to America/Los_Angeles`, then `remind me tomorrow at 9am to test` → stored `remind_at` has `-07:00` (or `-08:00`) offset, not the default zone.
9. **Iteration cap**: temporarily set `MAX_TOOL_ITERATIONS=1`, send any tool-requiring command — reply is the "got confused" fallback; `events.iterations=1`, `error=null`.
10. **Deploy**: push to GitHub → Render auto-deploys both services → `/health` returns 200; cron service shows recent runs in Render dashboard; Twilio webhook hits the prod URL successfully.

---

## Out of scope (explicitly)

- Editing existing todos/notes (only add, list, clear-by-match).
- Recurring reminders ("every Monday at 9am") — only one-shot.
- MMS / media attachments.
- Multi-turn conversational memory.
- Per-user rate limiting (v2 — straightforward to add using `events` table).
- Web dashboard / auth UI.

These are intentionally cut to keep v1 small, predictable, and self-hostable.
