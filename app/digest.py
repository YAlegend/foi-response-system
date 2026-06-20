"""Per-department SLA digest job — run weekly from cron / a systemd timer.

    # 08:00 every Monday: one SLA summary per department
    0 8 * * 1  cd /opt/foi && . .venv/bin/activate && \
      FOI_DIGEST_ENABLED=true FOI_NOTIFY_PROVIDER=smtp \
      FOI_DIGEST_RECIPIENTS="Highways=highways@oxfordshire.gov.uk,Environment=env@oxfordshire.gov.uk" \
      FOI_NOTIFY_RECIPIENTS=ig@oxfordshire.gov.uk \
      FOI_SMTP_HOST=smtp.internal FOI_SMTP_USERNAME=... FOI_SMTP_PASSWORD=... \
      python -m app.digest

Honours FOI_DIGEST_ENABLED (no-ops when off) and sends one digest per department
per ISO week. Use the admin endpoint to force a run on demand.
"""
from __future__ import annotations

from .database import SessionLocal, init_db
from .services import digests


def run() -> None:
    init_db()
    db = SessionLocal()
    try:
        result = digests.send_department_digests(db)
        sent = ", ".join(result["sent"]) or "none"
        print(f"digest [{result['status']}] period={result.get('period','-')} "
              f"provider={result.get('provider','-')} sent: {sent}")
    finally:
        db.close()


if __name__ == "__main__":
    run()
