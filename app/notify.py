"""Breach-trend notification job — run from cron / a systemd timer.

    # hourly check (no-op unless a scheme has newly started deteriorating):
    0 * * * *  cd /opt/foi && . .venv/bin/activate && \
      FOI_NOTIFY_ENABLED=true FOI_NOTIFY_PROVIDER=smtp \
      FOI_NOTIFY_RECIPIENTS=ig@oxfordshire.gov.uk \
      FOI_SMTP_HOST=smtp.internal FOI_SMTP_USERNAME=... FOI_SMTP_PASSWORD=... \
      python -m app.notify

Honours FOI_NOTIFY_ENABLED (no-ops when off). Use the admin endpoint to force a
run on demand.
"""
from __future__ import annotations

from .database import SessionLocal, init_db
from .services import notifications


def run() -> None:
    init_db()
    db = SessionLocal()
    try:
        result = notifications.check_and_notify(db)
        alerted = ", ".join(result["alerted"]) or "none"
        resolved = ", ".join(result["resolved"]) or "none"
        print(f"notify [{result['status']}] provider={result.get('provider','-')} "
              f"alerted: {alerted} · resolved: {resolved}")
    finally:
        db.close()


if __name__ == "__main__":
    run()
