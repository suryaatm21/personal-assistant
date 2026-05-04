-- Personal SMS Agent — Supabase schema
-- Run this once in your Supabase project's SQL Editor.

create extension if not exists pgcrypto;

-- per-user settings (timezone, etc). Auto-created on first SMS.
create table if not exists users (
  user_id     text primary key,                       -- E.164 phone, e.g. +14155551234
  timezone    text not null default 'America/New_York',
  created_at  timestamptz not null default now()
);

create table if not exists todos (
  id          uuid primary key default gen_random_uuid(),
  user_id     text not null,
  content     text not null,
  status      text not null default 'active' check (status in ('active','done')),
  created_at  timestamptz not null default now()
);
create index if not exists todos_user_status_created on todos (user_id, status, created_at desc);

create table if not exists notes (
  id          uuid primary key default gen_random_uuid(),
  user_id     text not null,
  content     text not null,
  created_at  timestamptz not null default now()
);
create index if not exists notes_user_created on notes (user_id, created_at desc);

create table if not exists reminders (
  id          uuid primary key default gen_random_uuid(),
  user_id     text not null,
  content     text not null,
  remind_at   timestamptz not null,
  status      text not null default 'pending' check (status in ('pending','sent','cancelled')),
  created_at  timestamptz not null default now()
);
create index if not exists reminders_due on reminders (status, remind_at) where status = 'pending';
create index if not exists reminders_user_created on reminders (user_id, created_at desc);

create table if not exists contacts (
  id          uuid primary key default gen_random_uuid(),
  user_id     text not null,
  name        text not null,
  phone       text not null,                          -- E.164
  created_at  timestamptz not null default now()
);
create unique index if not exists contacts_user_lower_name on contacts (user_id, lower(name));
create index if not exists contacts_user on contacts (user_id);

-- one row per inbound SMS turn, for cost / quality / iteration-count analysis.
-- twilio_message_sid is also the dedup key — Render free tier cold-starts
-- (~30s) plus Twilio's 15s webhook timeout means Twilio will retry the
-- same MessageSid; the unique partial index lets us short-circuit retries.
create table if not exists events (
  id                   uuid primary key default gen_random_uuid(),
  user_id              text not null,
  created_at           timestamptz not null default now(),
  inbound_text         text not null,
  reply_text           text,
  iterations           int  not null default 0,
  tool_calls           jsonb not null default '[]'::jsonb,
  input_tokens         int,
  output_tokens        int,
  latency_ms           int,
  model                text,
  error                text,
  twilio_message_sid   text
);
create index if not exists events_user_created on events (user_id, created_at desc);
create index if not exists events_iterations on events (iterations);
create unique index if not exists events_twilio_sid
  on events (twilio_message_sid)
  where twilio_message_sid is not null;

-- Defense-in-depth: enable RLS on every table. The FastAPI server uses the
-- Supabase service role key, which bypasses RLS, so the app keeps working.
-- With RLS enabled and no policies defined, anon/authenticated keys cannot
-- read or write — so if anyone ever points a browser client at this DB, they
-- get nothing. (Application code still filters by user_id explicitly; this is
-- a second layer.)
alter table users     enable row level security;
alter table todos     enable row level security;
alter table notes     enable row level security;
alter table reminders enable row level security;
alter table contacts  enable row level security;
alter table events    enable row level security;
