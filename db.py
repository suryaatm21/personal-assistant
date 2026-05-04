"""
Supabase queries for the SMS agent.

Every helper takes user_id as the first argument and includes it in the
filter clause. Tool dispatch never gets user_id from Claude — the webhook
extracts it from the signed Twilio `From` field and threads it through.

The reminder worker is the one query that scans across users by design.
"""

from __future__ import annotations

from typing import Any

from supabase import Client, create_client

from config import settings

supabase: Client = create_client(
    settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY
)


def get_or_create_user(user_id: str) -> dict[str, Any]:
    existing = (
        supabase.table("users")
        .select("user_id, timezone, created_at")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if existing.data:
        return existing.data[0]

    inserted = (
        supabase.table("users")
        .insert({"user_id": user_id, "timezone": settings.DEFAULT_TIMEZONE})
        .execute()
    )
    return inserted.data[0]


def set_user_timezone(user_id: str, timezone: str) -> None:
    supabase.table("users").upsert(
        {"user_id": user_id, "timezone": timezone}, on_conflict="user_id"
    ).execute()


# ---------- todos ----------


def insert_todo(user_id: str, content: str) -> dict[str, Any]:
    return (
        supabase.table("todos")
        .insert({"user_id": user_id, "content": content})
        .execute()
        .data[0]
    )


def select_active_todos(
    user_id: str, since_iso: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    q = (
        supabase.table("todos")
        .select("id, content, created_at")
        .eq("user_id", user_id)
        .eq("status", "active")
        .order("created_at", desc=True)
        .limit(limit)
    )
    if since_iso:
        q = q.gte("created_at", since_iso)
    return q.execute().data


def mark_all_todos_done(user_id: str) -> int:
    result = (
        supabase.table("todos")
        .update({"status": "done"})
        .eq("user_id", user_id)
        .eq("status", "active")
        .execute()
    )
    return len(result.data)


def find_active_todos_ilike(user_id: str, match_text: str) -> list[dict[str, Any]]:
    pattern = f"%{match_text}%"
    return (
        supabase.table("todos")
        .select("id, content")
        .eq("user_id", user_id)
        .eq("status", "active")
        .ilike("content", pattern)
        .limit(10)
        .execute()
        .data
    )


def mark_todo_done(user_id: str, todo_id: str) -> None:
    supabase.table("todos").update({"status": "done"}).eq("user_id", user_id).eq(
        "id", todo_id
    ).execute()


# ---------- notes ----------


def insert_note(user_id: str, content: str) -> dict[str, Any]:
    return (
        supabase.table("notes")
        .insert({"user_id": user_id, "content": content})
        .execute()
        .data[0]
    )


def select_notes(
    user_id: str, since_iso: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    q = (
        supabase.table("notes")
        .select("id, content, created_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
    )
    if since_iso:
        q = q.gte("created_at", since_iso)
    return q.execute().data


# ---------- reminders ----------


def insert_reminder(user_id: str, content: str, remind_at_iso: str) -> dict[str, Any]:
    return (
        supabase.table("reminders")
        .insert(
            {"user_id": user_id, "content": content, "remind_at": remind_at_iso}
        )
        .execute()
        .data[0]
    )


def select_pending_reminders(user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    return (
        supabase.table("reminders")
        .select("id, content, remind_at")
        .eq("user_id", user_id)
        .eq("status", "pending")
        .order("remind_at", desc=False)
        .limit(limit)
        .execute()
        .data
    )


def find_pending_reminders_ilike(
    user_id: str, match_text: str
) -> list[dict[str, Any]]:
    pattern = f"%{match_text}%"
    return (
        supabase.table("reminders")
        .select("id, content, remind_at")
        .eq("user_id", user_id)
        .eq("status", "pending")
        .ilike("content", pattern)
        .limit(10)
        .execute()
        .data
    )


def cancel_reminder(user_id: str, reminder_id: str) -> None:
    supabase.table("reminders").update({"status": "cancelled"}).eq(
        "user_id", user_id
    ).eq("id", reminder_id).execute()


# Worker-only — NOT scoped to user_id (intentional; scans across all users).
def select_due_reminders(now_iso: str, limit: int = 100) -> list[dict[str, Any]]:
    return (
        supabase.table("reminders")
        .select("id, user_id, content, remind_at")
        .eq("status", "pending")
        .lte("remind_at", now_iso)
        .order("remind_at", desc=False)
        .limit(limit)
        .execute()
        .data
    )


def mark_reminder_sent(reminder_id: str) -> None:
    supabase.table("reminders").update({"status": "sent"}).eq(
        "id", reminder_id
    ).execute()


# ---------- contacts ----------


def upsert_contact(user_id: str, name: str, phone: str) -> dict[str, Any]:
    # Match the unique index on (user_id, lower(name)).
    existing = (
        supabase.table("contacts")
        .select("id, name, phone")
        .eq("user_id", user_id)
        .ilike("name", name)
        .limit(1)
        .execute()
        .data
    )
    if existing:
        return (
            supabase.table("contacts")
            .update({"phone": phone, "name": name})
            .eq("id", existing[0]["id"])
            .execute()
            .data[0]
        )
    return (
        supabase.table("contacts")
        .insert({"user_id": user_id, "name": name, "phone": phone})
        .execute()
        .data[0]
    )


def list_contacts(user_id: str) -> list[dict[str, Any]]:
    return (
        supabase.table("contacts")
        .select("name, phone")
        .eq("user_id", user_id)
        .order("name", desc=False)
        .execute()
        .data
    )


def find_contacts(user_id: str, query: str) -> list[dict[str, Any]]:
    """Case-insensitive: exact match first, then unique substring."""
    rows = (
        supabase.table("contacts")
        .select("id, name, phone")
        .eq("user_id", user_id)
        .ilike("name", query)
        .execute()
        .data
    )
    if rows:
        return rows
    return (
        supabase.table("contacts")
        .select("id, name, phone")
        .eq("user_id", user_id)
        .ilike("name", f"%{query}%")
        .execute()
        .data
    )


# ---------- events (telemetry) ----------


def insert_event(
    *,
    user_id: str,
    inbound_text: str,
    reply_text: str | None,
    iterations: int,
    tool_calls: list[dict[str, Any]],
    input_tokens: int | None,
    output_tokens: int | None,
    latency_ms: int,
    model: str,
    error: str | None,
    twilio_message_sid: str | None = None,
) -> None:
    supabase.table("events").insert(
        {
            "user_id": user_id,
            "inbound_text": inbound_text,
            "reply_text": reply_text,
            "iterations": iterations,
            "tool_calls": tool_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": latency_ms,
            "model": model,
            "error": error,
            "twilio_message_sid": twilio_message_sid,
        }
    ).execute()


def find_event_by_sid(twilio_message_sid: str) -> dict[str, Any] | None:
    """Look up a prior event row by Twilio MessageSid (for retry dedup)."""
    rows = (
        supabase.table("events")
        .select("reply_text")
        .eq("twilio_message_sid", twilio_message_sid)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None
