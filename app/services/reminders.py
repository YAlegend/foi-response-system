"""Deadline reminders for a single case.

Resolves who is responsible for a case (its explicit owning department, else the
owning department of its scheme) and emails them a reminder that it is due soon
or overdue. Recipients are routed per department from FOI_DIGEST_RECIPIENTS,
falling back to the central FOI_NOTIFY_RECIPIENTS list — the same routing the
weekly digests use. Delivery goes through the pluggable notifier: the default
"stub" provider records what *would* be sent (no egress) so this is safe in the
demo and tests; set FOI_NOTIFY_PROVIDER=smtp to actually send.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..models import FOIRequest
from ..people import officer_for
from ..projects import label as project_label
from ..projects import owning_department as scheme_department
from ..sla import sla_state
from .digests import _recipient_map, _recipients_for
from .notifications import get_notifier


def responsible_for(req: FOIRequest) -> str:
    """The team on the hook for this case: its explicit owning department, else
    the owning department of its scheme. Empty string when unassigned."""
    return (req.owning_department or scheme_department(req.project) or "").strip()


def _timing(req: FOIRequest, s) -> tuple[dict, str]:
    st = sla_state(req.received_at, s.statutory_working_days, s.sla_amber_day,
                   s.sla_red_day, paused_days=req.clock_paused_days or 0,
                   paused_since=req.clarification_requested_at)
    wdr = st["working_days_remaining"]
    if st["paused"]:
        when = "on hold (clock paused)"
    elif wdr < 0:
        when = f"overdue by {abs(wdr)} working day(s)"
    else:
        when = f"due in {wdr} working day(s)"
    return st, when


def send_reminder(db: Session, req: FOIRequest, actor: str = "system") -> dict:
    """Send (or, with the stub provider, record) a deadline reminder to the
    responsible department. Returns a summary of what was sent."""
    s = get_settings()
    dept = responsible_for(req)
    officer = officer_for(dept)
    # Address the named officer's mailbox if we have it; else route per department
    # (digest map), else the central IG list — so a reminder is never dropped.
    recipients = ([officer["email"]] if officer.get("email")
                  else _recipients_for(dept, s, _recipient_map(s)) if dept
                  else [x.strip() for x in s.notify_recipients.split(",") if x.strip()])
    st, when = _timing(req, s)
    scheme = project_label(req.project) if req.project else "—"
    team = dept or "FOI team"
    subject = f"[FOI reminder] {req.reference} — {when}"
    body = (f"Hi {officer['name']},\n\n"
            f"This is a reminder that FOI case {req.reference} is {when}.\n\n"
            f"Subject: {req.subject}\n"
            f"Scheme: {scheme}\n"
            f"Stage: {req.stage}\n"
            f"Responsible: {officer['name']} ({team})\n"
            f"Statutory deadline: {st['deadline']}\n\n"
            f"Please action this case to avoid (or resolve) an SLA breach.")

    sent = get_notifier().send(subject, body, recipients) if recipients else False
    # Plain-English audit line (no recipient addresses / technical keys).
    audit.log(db, req, actor, "reminder_sent",
              detail=f"Reminder to {officer['name']} ({team}) — {when}")
    db.commit()
    return {
        "ok": True, "reference": req.reference,
        "person": officer["name"], "department": team,
        "recipients": recipients, "provider": s.notify_provider,
        "sent": bool(sent), "subject": subject, "body": body,
    }
