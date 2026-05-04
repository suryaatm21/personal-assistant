"""
Tool schemas (passed to Claude) and dispatcher (executes them server-side).

Claude never sees user_id — it's injected by the dispatcher from the signed
Twilio `From` field. No tool argument named user_id exists, so a prompt
injection cannot make Claude write to a different tenant's data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import phonenumbers
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient

import db
from config import settings

twilio_client = TwilioClient(
    settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN
)


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "log_todo",
        "description": "Add a new todo (task) for the user. Use for 'remind me to <verb>', 'add to-do: ...', 'task: ...'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The todo text, as the user would read it back.",
                }
            },
            "required": ["content"],
        },
    },
    {
        "name": "log_note",
        "description": "Save a note / idea / thought. Use for 'note:', 'idea:', 'remember this:', 'save:'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The note text."}
            },
            "required": ["content"],
        },
    },
    {
        "name": "list_todos",
        "description": "List the user's active todos, newest first. Use for 'what are my todos?', 'what do I have to do?'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_hours": {
                    "type": "integer",
                    "description": "Only return todos created within the last N hours. Omit for all.",
                }
            },
        },
    },
    {
        "name": "list_notes",
        "description": "List the user's notes, newest first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_hours": {
                    "type": "integer",
                    "description": "Only return notes created within the last N hours. Omit for all.",
                }
            },
        },
    },
    {
        "name": "clear_todos",
        "description": (
            "Mark todos as done. Pass target='all' to clear everything, or "
            "match_text to clear by content. If match_text matches multiple, "
            "the tool returns the matches without clearing so you can ask "
            "the user which one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["all"],
                    "description": "Pass 'all' to clear every active todo.",
                },
                "match_text": {
                    "type": "string",
                    "description": "Substring of the todo to clear (case-insensitive).",
                },
            },
        },
    },
    {
        "name": "schedule_reminder",
        "description": (
            "Schedule a one-shot reminder. The user will receive an SMS at "
            "remind_at_iso with the content. Resolve relative times "
            "('tomorrow at 3pm', 'in 2 hours') to ISO 8601 with offset, "
            "using the current time and timezone provided in the system "
            "prompt. Must be in the future."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "What the reminder is about.",
                },
                "remind_at_iso": {
                    "type": "string",
                    "description": "ISO 8601 timestamp with timezone offset, e.g. 2026-05-04T15:00:00-04:00.",
                },
            },
            "required": ["content", "remind_at_iso"],
        },
    },
    {
        "name": "list_reminders",
        "description": "List the user's pending reminders, soonest first.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_reminder",
        "description": (
            "Cancel a pending reminder by content match. If multiple match, "
            "returns the matches so you can ask which one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "match_text": {
                    "type": "string",
                    "description": "Substring of the reminder to cancel.",
                }
            },
            "required": ["match_text"],
        },
    },
    {
        "name": "set_timezone",
        "description": (
            "Set the user's timezone. Accepts an IANA name like "
            "'America/Los_Angeles'. Use this when the user says things like "
            "'set timezone to Pacific' (resolve 'Pacific' -> "
            "'America/Los_Angeles')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tz": {
                    "type": "string",
                    "description": "IANA timezone name.",
                }
            },
            "required": ["tz"],
        },
    },
    {
        "name": "add_contact",
        "description": "Save a contact for the user so they can later text the contact by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "phone": {
                    "type": "string",
                    "description": "Phone number — any common format; will be normalized to E.164.",
                },
            },
            "required": ["name", "phone"],
        },
    },
    {
        "name": "list_contacts",
        "description": "List the user's saved contacts.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_sms",
        "description": (
            "Send an SMS to someone else. `to` may be an E.164 phone number "
            "or a contact name (resolved against the user's contacts). If "
            "the name matches multiple contacts, the tool returns the "
            "matches without sending."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "E.164 number or contact name.",
                },
                "message": {"type": "string"},
            },
            "required": ["to", "message"],
        },
    },
]


def _normalize_phone(raw: str) -> str | None:
    raw = raw.strip()
    try:
        # Default region only used if no '+' prefix.
        parsed = phonenumbers.parse(raw, "US")
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _hours_ago_iso(hours: int) -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=hours)
    ).isoformat()


def dispatch(name: str, args: dict[str, Any], user_id: str) -> dict[str, Any]:
    """Run the tool. Always returns a JSON-serializable dict."""
    try:
        if name == "log_todo":
            row = db.insert_todo(user_id, args["content"])
            return {"ok": True, "id": row["id"]}

        if name == "log_note":
            row = db.insert_note(user_id, args["content"])
            return {"ok": True, "id": row["id"]}

        if name == "list_todos":
            since = (
                _hours_ago_iso(args["filter_hours"])
                if args.get("filter_hours")
                else None
            )
            rows = db.select_active_todos(user_id, since_iso=since)
            return {
                "todos": [
                    {"id": r["id"], "content": r["content"]} for r in rows
                ]
            }

        if name == "list_notes":
            since = (
                _hours_ago_iso(args["filter_hours"])
                if args.get("filter_hours")
                else None
            )
            rows = db.select_notes(user_id, since_iso=since)
            return {
                "notes": [
                    {"id": r["id"], "content": r["content"]} for r in rows
                ]
            }

        if name == "clear_todos":
            if args.get("target") == "all":
                count = db.mark_all_todos_done(user_id)
                return {"ok": True, "cleared_count": count}
            match_text = args.get("match_text")
            if not match_text:
                return {
                    "ok": False,
                    "error": "Provide either target='all' or match_text.",
                }
            matches = db.find_active_todos_ilike(user_id, match_text)
            if len(matches) == 0:
                return {"ok": True, "cleared_count": 0}
            if len(matches) == 1:
                db.mark_todo_done(user_id, matches[0]["id"])
                return {
                    "ok": True,
                    "cleared_count": 1,
                    "cleared_content": matches[0]["content"],
                }
            return {
                "ok": False,
                "ambiguous": True,
                "matches": [
                    {"id": m["id"], "content": m["content"]} for m in matches
                ],
            }

        if name == "schedule_reminder":
            user = db.get_or_create_user(user_id)
            tz = ZoneInfo(user["timezone"])
            try:
                dt = datetime.fromisoformat(args["remind_at_iso"])
            except ValueError:
                return {
                    "ok": False,
                    "error": "Could not parse remind_at_iso. Use ISO 8601 with offset.",
                }
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            now = datetime.now(timezone.utc)
            if dt <= now:
                return {"ok": False, "error": "Reminder must be in the future."}
            row = db.insert_reminder(user_id, args["content"], dt.isoformat())
            return {
                "ok": True,
                "id": row["id"],
                "remind_at": dt.isoformat(),
            }

        if name == "list_reminders":
            rows = db.select_pending_reminders(user_id)
            return {
                "reminders": [
                    {
                        "id": r["id"],
                        "content": r["content"],
                        "remind_at": r["remind_at"],
                    }
                    for r in rows
                ]
            }

        if name == "cancel_reminder":
            matches = db.find_pending_reminders_ilike(user_id, args["match_text"])
            if len(matches) == 0:
                return {"ok": True, "cancelled_count": 0}
            if len(matches) == 1:
                db.cancel_reminder(user_id, matches[0]["id"])
                return {
                    "ok": True,
                    "cancelled_count": 1,
                    "cancelled_content": matches[0]["content"],
                }
            return {
                "ok": False,
                "ambiguous": True,
                "matches": [
                    {
                        "id": m["id"],
                        "content": m["content"],
                        "remind_at": m["remind_at"],
                    }
                    for m in matches
                ],
            }

        if name == "set_timezone":
            try:
                ZoneInfo(args["tz"])
            except ZoneInfoNotFoundError:
                return {
                    "ok": False,
                    "error": f"Unknown timezone: {args['tz']}. Use an IANA name like America/Los_Angeles.",
                }
            db.set_user_timezone(user_id, args["tz"])
            return {"ok": True, "timezone": args["tz"]}

        if name == "add_contact":
            phone = _normalize_phone(args["phone"])
            if not phone:
                return {
                    "ok": False,
                    "error": "Could not parse phone number.",
                }
            db.upsert_contact(user_id, args["name"].strip(), phone)
            return {"ok": True, "name": args["name"].strip(), "phone": phone}

        if name == "list_contacts":
            rows = db.list_contacts(user_id)
            return {"contacts": rows}

        if name == "send_sms":
            to = args["to"].strip()
            # Treat as phone if it parses; otherwise look up as a contact name.
            looks_like_phone = to.startswith("+") or to.lstrip("+").replace(
                " ", ""
            ).replace("-", "").isdigit()
            resolved = _normalize_phone(to) if looks_like_phone else None
            if not resolved:
                matches = db.find_contacts(user_id, to)
                if len(matches) == 0:
                    return {
                        "ok": False,
                        "error": f"No contact named '{to}'. Add one with add_contact, or use a phone number.",
                    }
                if len(matches) > 1:
                    return {
                        "ok": False,
                        "ambiguous": True,
                        "matches": [
                            {"name": m["name"], "phone": m["phone"]}
                            for m in matches
                        ],
                    }
                resolved = matches[0]["phone"]
            try:
                twilio_client.messages.create(
                    from_=settings.TWILIO_PHONE_NUMBER,
                    to=resolved,
                    body=args["message"],
                )
            except TwilioRestException as e:
                return {"ok": False, "error": f"Twilio error: {e.msg}"}
            return {"ok": True, "resolved_to": resolved}

        return {"ok": False, "error": f"Unknown tool: {name}"}

    except KeyError as e:
        return {"ok": False, "error": f"Missing argument: {e.args[0]}"}
