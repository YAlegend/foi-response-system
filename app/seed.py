"""Seed the database with sample knowledge and a worked example case.

Run with:  python -m app.seed
Lets you exercise the whole pipeline offline without enabling ingestion.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from .database import SessionLocal, init_db
from .models import FOIRequest
from .projects import detect_project
from .ingestion import knowledge_base
from .services import casework, inbox
from .sla import is_working_day

# Curated Oxfordshire website pages so retrieval has something to ground on
# offline, before any live crawl. (source, title, content, project) — `project`
# tags the page to a scheme so retrieval can be scoped to e.g. traffic-filters.
SAMPLE_KB = [
    ("website", "Oxford traffic filters",
     "Oxfordshire County Council has proposed six traffic filters on key roads in "
     "Oxford to reduce through-traffic and improve bus journeys, walking and cycling. "
     "The filters are enforced by automatic number plate recognition (ANPR) cameras, "
     "not physical barriers, and operate at certain times of day. Residents of Oxford "
     "and named surrounding areas can apply for a permit to drive through the filters "
     "on up to 100 days a year. Buses, taxis, blue badge holders, emergency services "
     "and carers are among the exempt groups.", "traffic-filters"),
    ("website", "Zero Emission Zone",
     "The Oxford Zero Emission Zone (ZEZ) pilot, run jointly by Oxford City Council "
     "and Oxfordshire County Council, charges the most polluting vehicles to enter a "
     "small number of streets in the city centre. Zero emission vehicles pay nothing. "
     "Charges apply from 7am to 7pm every day and are enforced by ANPR cameras. The "
     "pilot is intended to test the approach ahead of a possible larger zone.", "zez"),
    ("website", "Low Traffic Neighbourhoods in east Oxford",
     "Low Traffic Neighbourhoods (LTNs) in Cowley, Church Cowley and Temple Cowley use "
     "traffic filters — planters, bollards and ANPR cameras — to stop through-traffic "
     "on residential streets while keeping access for residents, deliveries and "
     "emergency services. The east Oxford LTNs were made permanent following an "
     "experimental traffic regulation order.", "ltn"),
    ("website", "Report a pothole",
     "Oxfordshire County Council is the highway authority for around 3,000 miles of "
     "road. Potholes and other highway defects can be reported online through "
     "FixMyStreet or the council's website and are inspected against intervention "
     "criteria. The most urgent (category 1) defects are made safe within 24 hours.", ""),
    ("website", "Household waste recycling centres",
     "The county council runs household waste recycling centres across Oxfordshire and "
     "is responsible for the disposal of waste collected by the district councils. "
     "Around half of household waste in the county is reused, recycled or composted.", ""),
    ("website", "School admissions",
     "Oxfordshire County Council coordinates admissions to community and voluntary "
     "controlled schools. Parents apply through the council and may appeal to an "
     "independent panel if their child is not offered a place at a preferred school.", ""),
]

# The incoming FOI used by the worked example: a typical Oxford traffic-filters
# request whose answers live across the website pages and published FOI precedents.
SAMPLE_REQUEST = {
    "requester_name": "Alex Taylor",
    "requester_email": "alex.taylor@example.com",
    "requester_type": "resident",
    "subject": "Oxford traffic filters — cameras and exemptions",
    "body": (
        "Please could you tell me:\n"
        "1. How many ANPR cameras will enforce the Oxford traffic filters?\n"
        "2. Which vehicles are exempt from the traffic filters?\n"
        "3. How many responses did the public consultation on the traffic filters "
        "receive?\n"
    ),
}

# Two private department uploads that land in the review queue, so the demo shows
# the gate: these are NOT retrievable until a reviewer approves them.
PENDING_UPLOADS = [
    {"department": "Highways & Transport", "project": "traffic-filters",
     "title": "DRAFT internal note — traffic filter go-live sequencing",
     "content": (
         "INTERNAL DRAFT, not for publication. Officer working note on the order in "
         "which the six traffic filter cameras would be switched on once Botley Road "
         "reopens. Contains provisional dates and an unconfirmed contractor name. To "
         "be checked by Legal before any release.")},
    {"department": "Environment & Climate", "project": "zez",
     "title": "ZEZ exemption applications — internal caseload spreadsheet extract",
     "content": (
         "Internal extract listing ZEZ exemption and discount applications under "
         "review, including some applicant names and vehicle registrations. Personal "
         "data — must be reviewed and redacted before any of these figures are used "
         "in a published response.")},
]


def _working_days_ago(n: int) -> datetime:
    """A timestamp `n` working days before today (for back-dated demo cases)."""
    d = date.today()
    counted = 0
    while counted < n:
        d -= timedelta(days=1)
        if is_working_day(d):
            counted += 1
    return datetime.combine(d, time(9, 0), tzinfo=timezone.utc)


def _drive_to_close(db, r) -> None:
    """Drive an already triaged+drafted case all the way to dispatch (closed)."""
    db.refresh(r)
    if r.stage == "4_department_review":
        casework.sme_update(db, r, officer="demo",
                            supplied_text="Confirmed.", holding_status="held")
    casework.run_compliance(db, r, actor="demo")
    casework.approve(db, r, manager="demo", approved=True)
    casework.sign_off(db, r, officer="demo", authorised=True)
    casework.dispatch(db, r, foi_officer="demo")


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

    # Scheme-themed cases spanning SLA states, so the per-scheme SLA table shows
    # a real breach rate, on-time % and average days (triage tags each scheme).
    def open_case(subject, body, wd):
        rr = mk(subject, body, wd)
        casework.run_triage(db, rr, actor="demo")
        casework.run_autodraft(db, rr, actor="demo")
        return rr

    # Traffic filters: one overdue (open, breached) + one closed on time.
    open_case("Traffic filter ANPR camera locations",
              "1. Where will the traffic filter cameras be sited?", 28)
    _drive_to_close(db, open_case("Traffic filter permit eligibility",
              "1. How many residents are eligible for a traffic filter permit?", 9))
    # ZEZ: one closed late (breach) + one open in the amber band.
    _drive_to_close(db, open_case("ZEZ penalty charge revenue",
              "1. How much net revenue did the Zero Emission Zone raise last year?", 30))
    r = mk("ZEZ exemption application process",
           "1. How do I apply for a Zero Emission Zone exemption?", 13)
    casework.run_triage(db, r, actor="demo")   # open, amber band
    # LTN: one open, comfortably on track.
    open_case("LTN filter map",
              "1. Where are the low traffic neighbourhood filters in east Oxford?", 4)
    return 10


def _seed_published_foi(db) -> int:
    """Load the curated Oxfordshire published-FOI corpus directly (offline, no
    ingestion flags), so the precedent archive is present for the demo."""
    from pathlib import Path
    base = Path(__file__).resolve().parent.parent / "sample_data" / "oxfordshire" / "published_foi"
    if not base.exists():
        return 0
    n = 0
    for path in sorted(base.glob("*.txt")):
        content = path.read_text(encoding="utf-8", errors="ignore").strip()
        if len(content) < 100:
            continue
        # Title from the Subject: line; project tagged by simple keyword.
        title = next((ln.split(":", 1)[1].strip() for ln in content.splitlines()[:6]
                      if ln.lower().startswith("subject:")), path.stem)
        project = detect_project(content)
        knowledge_base.upsert(db, source="published_response", title=title,
                              content=content, project=project, status="approved")
        n += 1
    db.commit()
    return n


def _seed_pending_uploads(db) -> int:
    """Create the demo's pending department uploads (status=pending_review), so the
    review queue is non-empty and the gate is visible from first load."""
    n = 0
    for up in PENDING_UPLOADS:
        doc = knowledge_base.upsert(db, source="department", title=up["title"],
                                    content=up["content"], project=up["project"],
                                    status="pending_review")
        doc.status = "pending_review"
        doc.department = up["department"]
        doc.uploaded_by = f"{up['department'].lower().split()[0]}.officer"
        n += 1
    db.commit()
    return n


def run() -> None:
    init_db()
    db = SessionLocal()
    try:
        for source, title, content, project in SAMPLE_KB:
            knowledge_base.upsert(db, source=source, title=title, content=content,
                                  project=project, status="approved")
        db.commit()
        n_foi = _seed_published_foi(db)
        n_pending = _seed_pending_uploads(db)
        print(f"Knowledge base seeded: {knowledge_base.count(db)} docs "
              f"({n_foi} published FOI precedents, {n_pending} pending review).")

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
