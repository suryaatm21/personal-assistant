"""
FastAPI app — Twilio webhook + health check.

Inbound SMS flow:
  Twilio POST /webhook
    -> validate X-Twilio-Signature
    -> user_id = signed `From` field
    -> run_agent(user_id, body)
    -> log to events table
    -> reply with TwiML <Response><Message>...</Message></Response>
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from xml.sax.saxutils import escape

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request, Response
from twilio.request_validator import RequestValidator

import db
from agent import run_agent
from config import settings
from worker.process_reminders import main as process_reminders

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("personal-agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = BackgroundScheduler()
    scheduler.add_job(process_reminders, "interval", minutes=1)
    scheduler.start()
    logger.info("reminder scheduler started")
    yield
    scheduler.shutdown()


app = FastAPI(title="personal-agent", lifespan=lifespan)
validator = RequestValidator(settings.TWILIO_AUTH_TOKEN)


def _twiml(message: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{escape(message)}</Message></Response>"
    )


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    form = await request.form()

    if not settings.SKIP_TWILIO_SIGNATURE_VALIDATION:
        signature = request.headers.get("X-Twilio-Signature", "")
        if not validator.validate(str(request.url), dict(form), signature):
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    user_id = (form.get("From") or "").strip()
    body = (form.get("Body") or "").strip()
    sid = (form.get("MessageSid") or "").strip() or None
    if not user_id or not body:
        raise HTTPException(status_code=400, detail="Missing From or Body")

    # Retry dedup: Render free-tier cold-starts can exceed Twilio's 15s
    # webhook timeout, so Twilio retries with the same MessageSid. If we've
    # already processed this SID, return the cached reply instead of running
    # the agent again (which would double-log a todo).
    if sid:
        cached = db.find_event_by_sid(sid)
        if cached:
            return Response(
                content=_twiml(
                    cached.get("reply_text") or "Still working on this — try again in a sec."
                ),
                media_type="application/xml",
            )

    t0 = time.monotonic()
    reply: str
    try:
        result = run_agent(user_id, body)
        latency_ms = int((time.monotonic() - t0) * 1000)
        reply = result.reply
        try:
            db.insert_event(
                user_id=user_id,
                inbound_text=body,
                reply_text=reply,
                iterations=result.iterations,
                tool_calls=result.tool_calls,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                latency_ms=latency_ms,
                model=settings.ANTHROPIC_MODEL,
                error=None,
                twilio_message_sid=sid,
            )
        except Exception:  # noqa: BLE001
            # Most likely cause is a unique-violation on twilio_message_sid
            # because a concurrent retry won the insert race — that's fine,
            # we still return our reply to this attempt.
            logger.exception("failed to write event row (non-fatal)")
    except Exception as e:  # noqa: BLE001
        latency_ms = int((time.monotonic() - t0) * 1000)
        logger.exception("agent failed for user_id=%s", user_id)
        reply = "Something went wrong. Try again in a sec."
        try:
            db.insert_event(
                user_id=user_id,
                inbound_text=body,
                reply_text=None,
                iterations=0,
                tool_calls=[],
                input_tokens=None,
                output_tokens=None,
                latency_ms=latency_ms,
                model=settings.ANTHROPIC_MODEL,
                error=repr(e),
                twilio_message_sid=sid,
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to write error event row")

    return Response(content=_twiml(reply), media_type="application/xml")
