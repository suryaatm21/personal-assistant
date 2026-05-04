# personal-agent

A personal AI second brain you talk to over SMS. Text a Twilio number in
plain English; Claude (Haiku 4.5) parses your intent and either logs
something, queries it, schedules a reminder, or sends an SMS on your
behalf.

It's multi-tenant: every user is identified by their phone number, so
friends and family can share an instance — or fork the repo and run their
own.

---

## Example exchange

```
you      add to-do: fix the Unity null-ref bug
agent    Got it.

you      remind me tomorrow at 3pm to call mom
agent    Reminder set for tomorrow 3:00 PM.

you      idea: build a recipe app with eSIM backup plan
agent    Saved.

you      what are my todos?
agent    1. fix the Unity null-ref bug

you      add contact Jordan +14155551234
agent    Added Jordan.

you      text Jordan that I'm running late
agent    Sent to Jordan.
```

---

## Read this before deploying — costs and trust

A single deployed instance is **multi-tenant**: every user texting your
Twilio number is stored in your Supabase. But all users share **one** set
of provider accounts:

| Resource                 | Whose account | Who pays                |
| ------------------------ | ------------- | ----------------------- |
| Anthropic API key        | yours         | you                     |
| Twilio number + per-SMS  | yours         | you                     |
| Supabase database        | yours         | you (free tier likely)  |
| Render hosting           | yours         | you (free or $7/mo)     |

So pick one of these models on purpose:

1. **Household / friends-and-family.** You run one instance and give the
   number to people you trust. You eat the costs. Data isolation is
   enforced by the app + Postgres RLS.
2. **Each-friend-self-hosts.** Your friend forks this repo, creates their
   own Supabase / Twilio / Anthropic / Render accounts, and deploys their
   own copy with their own keys. Multi-tenant code is still useful so
   *they* can share *their* instance with *their* friends.

Don't accidentally give a stranger your Twilio number — every inbound
SMS calls Anthropic and Twilio on your dime.

---

## Prerequisites

You'll need accounts at:

- [Supabase](https://supabase.com/) — free tier
- [Twilio](https://www.twilio.com/) — paid number, ~$1/mo + per-SMS
- [Anthropic](https://console.anthropic.com/) — pay-as-you-go (Haiku 4.5
  is cheap; expect cents per day for personal use)
- [Render](https://render.com/) — free web tier or $7/mo always-on
- [GitHub](https://github.com/) — to fork and connect to Render

---

## Setup

### 1. Fork and clone the repo

Click **Fork** on GitHub, then `git clone` your fork.

### 2. Set up Supabase

1. Create a new project at https://supabase.com/dashboard.
2. In the project, open **SQL Editor → New query**.
3. Paste the entire contents of [`schema.sql`](./schema.sql) and click
   **Run**. You should see *Success* — six tables created (`users`,
   `todos`, `notes`, `reminders`, `contacts`, `events`) with RLS enabled.
4. Open **Project Settings → API**. Copy two values you'll need later:
   - **Project URL** → goes into `SUPABASE_URL`.
   - **service_role secret** (not the `anon` key) → goes into
     `SUPABASE_SERVICE_ROLE_KEY`. This key bypasses RLS; treat it like a
     password and never put it in a browser.

### 3. Buy a Twilio number

1. Sign up at twilio.com and verify your account.
2. **Console → Phone Numbers → Manage → Buy a number.** Make sure SMS is
   enabled in the capabilities.
3. From the **Console Dashboard**, copy:
   - **Account SID** → `TWILIO_ACCOUNT_SID`
   - **Auth Token** → `TWILIO_AUTH_TOKEN`
   - The phone number you bought (E.164 format, like `+14155551234`) →
     `TWILIO_PHONE_NUMBER`

You'll point the webhook at your deployed URL in step 6.

### 4. Get an Anthropic API key

https://console.anthropic.com/settings/keys → **Create Key**. Save it for
`ANTHROPIC_API_KEY`.

### 5. Deploy to Render

1. Push your fork to GitHub (it's already a git repo).
2. In Render, click **New → Blueprint** and connect the GitHub repo.
   Render reads [`render.yaml`](./render.yaml) and will create **two**
   services:
   - `personal-agent` — the FastAPI web service.
   - `reminder-worker` — a cron job that fires every minute to send due
     reminders.
3. Render will prompt you for the secret env vars. Paste in:
   - `ANTHROPIC_API_KEY`
   - `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
   - `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`

   `DEFAULT_TIMEZONE`, `ANTHROPIC_MODEL`, and `MAX_TOOL_ITERATIONS` have
   sensible defaults baked into `render.yaml`.

   **Render Blueprints don't auto-share env vars between services.** You
   need to fill in the same six secrets on both `personal-agent` and
   `reminder-worker` — they each have their own env-var table in the
   dashboard. Get them once from a password manager and paste twice.

4. Wait for both services to come up green. Hit `https://<your-web-service>.onrender.com/health`
   in a browser — you should see `{"ok": true}`.

### 6. Point Twilio at your deployment

1. Twilio Console → **Phone Numbers → Manage → Active numbers** → click
   your number.
2. In the **Messaging** section, set **A message comes in** to
   **Webhook**, paste `https://<your-web-service>.onrender.com/webhook`,
   and pick **HTTP POST**.
3. Save.

### 7. Try it

Text the number `add to-do: test it works`, then `what's on my list?`.
You should get a reply within a few seconds.

If you get nothing back, check the Render logs (web service) and the
Twilio Monitor → Logs → Errors page. The first request after a free-tier
service has been idle has a cold-start of ~30 seconds.

---

## Local development

```bash
cp .env.example .env
# fill in the values, then:
pip install -r requirements.txt

# bypass Twilio signature validation so curl works:
echo 'SKIP_TWILIO_SIGNATURE_VALIDATION=true' >> .env

uvicorn main:app --reload
```

Then in another shell:

```bash
curl -X POST http://localhost:8000/webhook \
  -d 'From=+14155550100&Body=add to-do: test it works'
```

You should get a TwiML response and see a row in the `todos` table.

To test against real Twilio locally, run `ngrok http 8000`, point Twilio
at the ngrok URL, and **set `SKIP_TWILIO_SIGNATURE_VALIDATION=false`**
again — Twilio's signature includes the URL, so leaving it bypassed
defeats the only thing protecting your webhook.

To test the reminder worker:

```bash
python -m worker.process_reminders
```

---

## Tuning

Every inbound SMS writes one row to the `events` table:

```sql
select created_at, inbound_text, iterations, latency_ms,
       input_tokens, output_tokens, error
from events
order by created_at desc
limit 50;
```

Useful queries:

- **How often do turns hit the iteration cap?**
  `select count(*) from events where iterations = 5;` — if this is
  non-zero, raise `MAX_TOOL_ITERATIONS`.
- **What does each turn cost?** `input_tokens + output_tokens` × Haiku
  pricing (currently $1 / $5 per 1M tokens).
- **What did I send last week?** `select inbound_text from events where
  created_at > now() - interval '7 days' order by created_at desc;`

---

## Architecture

```
Twilio inbound SMS ──► POST /webhook  (FastAPI on Render)
                         │
                         ├─ verify Twilio signature
                         ├─ user_id = From (E.164)
                         ├─ Claude Haiku 4.5 tool-use loop
                         │     log_todo / log_note
                         │     list_todos / list_notes
                         │     clear_todos
                         │     schedule_reminder / list_reminders /
                         │       cancel_reminder
                         │     add_contact / list_contacts
                         │     set_timezone
                         │     send_sms
                         ├─ Supabase Postgres (RLS on, every query
                         │   also scoped by user_id)
                         ├─ events table (one row per inbound SMS)
                         └─ TwiML reply

Render cron (every minute) ──► python -m worker.process_reminders
                         ├─ select reminders where status='pending'
                         │   and remind_at <= now()
                         ├─ Twilio messages.create(...)
                         └─ mark status='sent'
```

`user_id` is set server-side from Twilio's signed `From` field and never
seen by Claude. Tool calls always run server-side with `user_id`
injected — so even if a clever inbound SMS prompt-injects Claude, it
can't read or write another user's data.

---

## What's intentionally not here (v1)

- Editing existing todos / notes (only add, list, clear-by-match).
- Recurring reminders ("every Monday at 9am") — only one-shot.
- MMS / media attachments.
- Multi-turn conversational memory (each SMS is independent).
- Per-user rate limiting (the `events` table has everything you'd need
  to add this in ~10 lines if you want to).
- A web dashboard.

These are deliberate cuts to keep v1 small and self-hostable. Add them
when you actually need them.
