"""Per-case deadline reminders: who is responsible, and recording/sending."""
from __future__ import annotations

from sqlalchemy import select

from app.models import AuditEvent, FOIRequest
from app.projects import owning_department
from app.services import reminders


def _req(**kw):
    base = dict(reference="FOI/T/1", requester_name="A", requester_email="a@example.com",
                subject="ANPR cameras", body="1. A question?", project="")
    base.update(kw)
    return FOIRequest(**base)


def test_responsible_prefers_explicit_owner_then_scheme(db):
    r = _req(project="zez")
    assert reminders.responsible_for(r) == owning_department("zez")   # from the scheme
    r.owning_department = "Legal & IG"
    assert reminders.responsible_for(r) == "Legal & IG"               # explicit owner wins
    assert reminders.responsible_for(_req(project="")) == ""          # unassigned


def test_send_reminder_records_message_and_audit(db, monkeypatch):
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "digest_recipients", "")               # no per-dept routing
    monkeypatch.setattr(s, "notify_recipients", "ig@oxfordshire.gov.uk")  # central fallback

    req = _req(reference="FOI/T/3", project="traffic-filters")
    db.add(req); db.commit(); db.refresh(req)

    out = reminders.send_reminder(db, req, actor="tester")
    assert out["ok"] and out["reference"] == "FOI/T/3"
    assert out["department"] == owning_department("traffic-filters")
    assert out["recipients"] == ["ig@oxfordshire.gov.uk"]         # fell back to central list
    assert "FOI/T/3" in out["subject"]

    events = db.execute(
        select(AuditEvent).where(AuditEvent.action == "reminder_sent")).scalars().all()
    assert len(events) == 1
    assert owning_department("traffic-filters") in events[0].detail
