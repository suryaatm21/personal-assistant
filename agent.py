"""
Claude tool-use loop. One inbound SMS = one call to run_agent().

Claude is stateless per SMS — no conversation history table. The system
prompt injects the user's current local time + timezone so Claude can
parse 'tomorrow at 3pm' into an ISO timestamp.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import anthropic

import db
import tools
from config import settings

client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are a personal assistant for a single user, communicating over SMS.
Current time for this user: {now_local}  (timezone: {tz})

You have tools to log todos and notes, list them, clear them, schedule \
reminders, manage contacts, set timezone, and send SMS to others.

Style:
- Replies under ~160 chars when possible. Concatenated SMS works but short is better.
- Lists: numbered, one item per line, no extra prose. Example: "1. fix Unity bug\\n2. email Jordan".
- Treat "idea", "note", "thought", "remember this", "save" -> log_note.
- Treat "to-do", "todo", "remind me to <verb>", "task" -> log_todo.
- Treat "remind me at/on/in <time> to ..." or "set a reminder" -> schedule_reminder \
with remind_at_iso resolved to ISO 8601 in the user's timezone.
- If a tool returns ambiguous matches, ask the user which one in one short line.
- If the request is unclear, ask one short clarifying question; don't guess.
- Never invent IDs, contact phone numbers, or timestamps - use only tool outputs.
- After a successful action, reply with a short confirmation, not a recap of what the user said.
"""


@dataclass
class AgentResult:
    reply: str
    iterations: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


def _summarize_result(result: dict[str, Any]) -> dict[str, Any]:
    """Compact tool result for the events log — drops large list payloads."""
    summary: dict[str, Any] = {}
    for k, v in result.items():
        if isinstance(v, list):
            summary[k] = {"count": len(v)}
        else:
            summary[k] = v
    return summary


def run_agent(user_id: str, user_text: str) -> AgentResult:
    user = db.get_or_create_user(user_id)
    now_local = datetime.now(ZoneInfo(user["timezone"])).isoformat(timespec="seconds")
    system = SYSTEM_PROMPT.format(now_local=now_local, tz=user["timezone"])

    messages: list[dict[str, Any]] = [{"role": "user", "content": user_text}]
    tool_calls_log: list[dict[str, Any]] = []
    iterations = 0
    in_tokens = 0
    out_tokens = 0

    for _ in range(settings.MAX_TOOL_ITERATIONS):
        iterations += 1
        resp = client.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=512,
            system=system,
            tools=tools.TOOL_SCHEMAS,
            messages=messages,
        )
        in_tokens += resp.usage.input_tokens
        out_tokens += resp.usage.output_tokens

        if resp.stop_reason != "tool_use":
            text = "".join(
                block.text for block in resp.content if block.type == "text"
            ).strip()
            return AgentResult(
                reply=text or "Done.",
                iterations=iterations,
                tool_calls=tool_calls_log,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
            )

        # Echo Claude's response back into the conversation.
        messages.append({"role": "assistant", "content": resp.content})

        tool_results: list[dict[str, Any]] = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            result = tools.dispatch(block.name, dict(block.input), user_id)
            tool_calls_log.append(
                {
                    "name": block.name,
                    "args": block.input,
                    "result": _summarize_result(result),
                }
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                }
            )
        messages.append({"role": "user", "content": tool_results})

    return AgentResult(
        reply="Sorry, I got confused. Try rephrasing?",
        iterations=iterations,
        tool_calls=tool_calls_log,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
    )
