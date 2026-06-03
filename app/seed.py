"""Seed the database with sample knowledge and a worked example case.

Run with:  python -m app.seed
Lets you exercise the whole pipeline offline without enabling ingestion.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from .database import SessionLocal, init_db
from .models import FOIRequest
from .ingestion import knowledge_base
from .services import casework, inbox
from .sla import is_working_day

# A few public-style knowledge snippets so retrieval has something to ground on.
SAMPLE_KB = [
    ("website", "Recycling and waste collection",
     "Hertfordshire County Council provides household waste recycling centres "
     "across the county. In the last year the council collected approximately "
     "520,000 tonnes of waste, of which around 50 per cent was recycled or composted."),
    ("website", "Highways and pothole repairs",
     "The council is responsible for maintaining around 3,200 miles of road. "
     "Potholes can be reported online and are assessed against intervention "
     "criteria; category 1 defects are made safe within 24 hours."),
    ("published_response", "FOI school admissions appeals 2025",
     "You asked how many school admission appeals were lodged in 2025. We can "
     "confirm that the council received 1,420 admission appeals in the 2025 cycle, "
     "of which 360 were upheld."),
    ("published_response", "FOI staff headcount",
     "You asked for the council's staff headcount. As at 31 March 2025 the council "
     "employed the equivalent of 8,100 full-time staff (excluding schools)."),
]

SAMPLE_REQUEST = {
    "requester_name": "Alex Taylor",
    "requester_email": "alex.taylor@example.com",
    "requester_type": "resident",
    "subject": "Recycling tonnage and pothole repairs",
    "body": (
        "Please could you tell me:\n"
        "1. How much household waste was collected last year and what percentage "
        "was recycled?\n"
        "2. How many miles of road does the council maintain?\n"
    ),
}


def _working_days_ago(n: int) -> datetime:
    """A timestamp `n` working days before today (for back-dated demo cases)."""
    d = date.today()
    counted = 0
    while counted < n:
        d -= timedelta(days=1)
        if is_working_day(d):
            counted += 1
    return datetime.combine(d, time(9, 0), tzinfo=timezone.utc)


def seed_demo_cases(db) -> int:
    """Add a spread of cases across stages and SLA bands so the dashboard shows
    overdue / due-soon / paused / closed out of the box. Receipt dates are
    relative to today, so the SLA flags are correct whenever this is run.
    Idempotent: skips if the demo set is already present."""
    if db.execute(select(FOIRequest).where(FOIRequest.subject.like("[demo]%"))).first():
        return 0

    def mk(subject: str, body: str, wd_ago: int) -> FOIRequest:
        return casework.create_request(
            db, requester_name="Demo Requester", requester_email="demo@example.com",
            subject=f"[demo] {subject}", body=body,
            received_at=_working_days_ago(wd_ago), actor="demo")

    # Overdue (breach) — received 28 working days ago, still in progress.
    r = mk("Overdue parking PCN stats", "1. How many PCNs were issued last year?", 28)
    casework.run_triage(db, r, actor="demo"); casework.run_autodraft(db, r, actor="demo")

    # Due soon (red) — ~18 working days elapsed.
    r = mk("Due-soon school places", "1. How many reception places were offered?", 18)
    casework.run_triage(db, r, actor="demo"); casework.run_autodraft(db, r, actor="demo")

    # Amber.
    mk("Amber library opening hours", "1. What are the library opening hours?", 13)

    # Paused — awaiting clarification (clock stopped).
    r = mk("Vague spending request", "Please send all the spending data you have.", 2)
    casework.run_triage(db, r, actor="demo")
    casework.request_clarification(db, r, officer="demo",
                                   question="Which financial year and which service area?")

    # Closed (submitted) — driven all the way through.
    r = mk("Closed recycling tonnage", "1. How much waste was recycled last year?", 10)
    casework.run_triage(db, r, actor="demo"); casework.run_autodraft(db, r, actor="demo")
    db.refresh(r)
    if r.stage == "4_department_review":
        casework.sme_update(db, r, officer="demo",
                            supplied_text="Confirmed 520,000 tonnes.", holding_status="held")
    casework.run_compliance(db, r, actor="demo")
    casework.approve(db, r, manager="demo", approved=True)
    casework.sign_off(db, r, officer="demo", authorised=True)
    casework.dispatch(db, r, foi_officer="demo")
    return 5


def run() -> None:
    init_db()
    db = SessionLocal()
    try:
        for source, title, content in SAMPLE_KB:
            knowledge_base.upsert(db, source=source, title=title, content=content)
        db.commit()
        print(f"Knowledge base seeded: {knowledge_base.count(db)} docs.")

        # Populate the dedicated FOI mailbox so the inbox-driven intake has
        # something to triage in the UI from the first page load.
        new_mail = inbox.poll(db)
        print(f"FOI inbox seeded: {len(new_mail)} message(s) waiting.")

        req = casework.create_request(db, **SAMPLE_REQUEST)
        print(f"Created {req.reference} (deadline {req.deadline.date()}).")

        print("Triage:", casework.run_triage(db, req))
        print("Auto-draft:", casework.run_autodraft(db, req))

        n_demo = seed_demo_cases(db)
        print(f"Demo cases seeded: {n_demo} (overdue / due-soon / amber / paused / closed).")

        db.refresh(req)
        print(f"\nCase {req.reference} is now at stage: {req.stage}")
        if req.latest_draft:
            print("\n--- Draft v{} (confidence {}) ---\n".format(
                req.latest_draft.version, req.latest_draft.confidence))
            print(req.latest_draft.body)
    finally:
        db.close()


if __name__ == "__main__":
    run()
