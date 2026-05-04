"""
Reminder worker. Render runs this once per minute via the cron service in
render.yaml.

Selects every reminder where status='pending' and remind_at <= now(),
sends each via Twilio, marks them sent. If Twilio fails for one row, that
row stays pending and gets retried next minute.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient

import db
from config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reminder-worker")


def main() -> int:
    twilio_client = TwilioClient(
        settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    due = db.select_due_reminders(now_iso=now_iso, limit=100)
    if not due:
        logger.info("no due reminders")
        return 0

    sent = 0
    failed = 0
    for r in due:
        try:
            twilio_client.messages.create(
                from_=settings.TWILIO_PHONE_NUMBER,
                to=r["user_id"],
                body=f"Reminder: {r['content']}",
            )
            db.mark_reminder_sent(r["id"])
            sent += 1
        except TwilioRestException as e:
            failed += 1
            logger.error(
                "twilio failed for reminder %s (user %s): %s",
                r["id"],
                r["user_id"],
                e.msg,
            )
        except Exception:  # noqa: BLE001
            failed += 1
            logger.exception("reminder %s failed", r["id"])

    logger.info("processed: sent=%d failed=%d total=%d", sent, failed, len(due))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
