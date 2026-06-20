"""Breach-trend deterioration notifications.

When a scheme's breaches start trending up (the same "deteriorating" signal the
dashboard shows), notify a distribution list out-of-band. This is a deliberate,
side-effecting job — run it from cron (`python -m app.notify`) or trigger it from
the admin UI; it never fires on a page load.

Off by default and **no egress** unless configured: the default "stub" provider
records exactly what *would* be emailed (visible via GET /admin/notifications)
without sending anything, so it is safe in tests and demos. Wire FOI_NOTIFY_
PROVIDER=smtp + credentials for real delivery.

Idempotent: a scheme already in an alerted state is not emailed again until it
recovers (a "resolved" event) and deteriorates anew — so cron can run hourly
without spamming.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import SchemeNotification
from ..projects import owning_department


class NotificationProvider:
    def send(self, subject: str, body: str, recipients: list[str]) -> bool:  # pragma: no cover
        raise NotImplementedError


class _StubProvider(NotificationProvider):
    """Records only — no network. The message body is persisted on the log row
    so the demo/tests can inspect exactly what would have been sent."""

    def send(self, subject: str, body: str, recipients: list[str]) -> bool:
        return True


class _SMTPProvider(NotificationProvider):  # pragma: no cover - needs a real server
    def __init__(self, s):
        self.s = s

    def send(self, subject: str, body: str, recipients: list[str]) -> bool:
        import smtplib
        from email.message import EmailMessage
        if not recipients:
            return False
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, self.s.notify_from, ", ".join(recipients)
        msg.set_content(body)
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=30) as srv:
            if self.s.smtp_use_tls:
                srv.starttls()
            if self.s.smtp_username:
                srv.login(self.s.smtp_username, self.s.smtp_password)
            srv.send_message(msg)
        return True


def get_notifier() -> NotificationProvider:
    return _SMTPProvider(get_settings()) if get_settings().notify_provider == "smtp" else _StubProvider()


def _active_alert_schemes(db: Session) -> set[str]:
    """Schemes whose most recent notification event is 'alerted' (still active)."""
    rows = db.execute(
        select(SchemeNotification).order_by(SchemeNotification.created_at.desc(),
                                            SchemeNotification.id.desc())).scalars().all()
    latest: dict[str, str] = {}
    for r in rows:
        latest.setdefault(r.scheme, r.event)
    return {k for k, e in latest.items() if e == "alerted"}


def _render(row: dict, s) -> tuple[str, str]:
    dept = owning_department(row["key"]) or "—"
    subject = f"[FOI SLA] Breach trend rising — {row['label']}"
    body = (
        f"The FOI breach trend for the {row['label']} scheme is rising.\n\n"
        f"  Recent 4 weeks : {row['recent_breaches']} breaches\n"
        f"  Prior 4 weeks  : {row['prior_breaches']} breaches\n"
        f"  Breach rate    : {row['breach_rate']}%\n"
        f"  Overdue now    : {row['overdue']}\n"
        f"  Owning dept    : {dept}\n\n"
        f"Review the {row['label']} cases in the FOI dashboard and prioritise the "
        f"overdue ones to avoid further statutory breaches.\n\n"
        f"— {s.council_name} FOI response system"
    )
    return subject, body


def check_and_notify(db: Session, force: bool = False) -> dict:
    """Notify on schemes that have newly started deteriorating; record recoveries.

    Returns a summary. With the stub provider nothing is sent — the rendered
    message is stored on each log row instead. ``force`` runs even when
    FOI_NOTIFY_ENABLED is off (used by the admin trigger)."""
    s = get_settings()
    if not (s.notify_enabled or force):
        return {"status": "disabled", "alerted": [], "resolved": [],
                "detail": "Set FOI_NOTIFY_ENABLED=true (or trigger manually)."}

    # Reuse the dashboard's per-scheme SLA + deterioration computation verbatim.
    from ..routers.analytics import analytics
    rows = analytics(db=db, user=None)["sla_by_scheme"]
    deteriorating = {r["key"]: r for r in rows if r["deteriorating"]}
    active = _active_alert_schemes(db)
    labels = {r["key"]: r["label"] for r in rows}

    notifier = get_notifier()
    recipients = [x.strip() for x in s.notify_recipients.split(",") if x.strip()]
    alerted, resolved = [], []

    # Newly deteriorating -> notify once.
    for key, row in deteriorating.items():
        if key in active:
            continue
        subject, body = _render(row, s)
        ok = notifier.send(subject, body, recipients)
        db.add(SchemeNotification(
            scheme=key, label=row["label"], event="alerted",
            recent=row["recent_breaches"], prior=row["prior_breaches"],
            breach_rate=row["breach_rate"], channel=s.notify_provider,
            recipients=",".join(recipients), subject=subject,
            detail=body if s.notify_provider == "stub" else ("sent" if ok else "send-failed")))
        alerted.append(key)

    # Recovered (was alerted, no longer deteriorating) -> clear so it can re-alert.
    for key in active - set(deteriorating):
        db.add(SchemeNotification(scheme=key, label=labels.get(key, key), event="resolved",
                                  channel=s.notify_provider, detail="No longer deteriorating."))
        resolved.append(key)

    db.commit()
    return {"status": "ok", "provider": s.notify_provider, "recipients": recipients,
            "alerted": alerted, "resolved": resolved,
            "deteriorating": list(deteriorating)}
