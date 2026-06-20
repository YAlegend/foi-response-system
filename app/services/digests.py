"""Per-department SLA summary digest.

A periodic (weekly) summary emailed to each owning department: their open /
overdue / due-soon counts, breach rate, on-time %, the worst overdue cases, and
any deteriorating schemes among their cases. Unlike the breach-trend *alert*
(event-driven), this is a scheduled roll-up — run from cron via
`python -m app.digest`, or on demand from the admin UI.

Reuses the notification provider (stub records the message, no egress, by
default; smtp sends). Recipients are routed per department from
FOI_DIGEST_RECIPIENTS ("Dept=email, ..."), falling back to the central
FOI_NOTIFY_RECIPIENTS list. One digest per (department, ISO week) — a forced run
re-sends.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..enums import Stage
from ..models import DepartmentDigest, FOIRequest
from ..projects import label as project_label
from ..sla import sla_state
from . import notifications


def _close_date(req: FOIRequest):
    closed = [e.created_at for e in req.events if e.action.endswith("->closed")]
    return max(closed).date() if closed else None


def current_period(today: date | None = None) -> str:
    """ISO year-week key, e.g. '2026-W25' — the digest's dedup period."""
    iso = (today or date.today()).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _recipient_map(s) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in s.digest_recipients.split(","):
        if "=" in pair:
            dept, email = pair.split("=", 1)
            if dept.strip() and email.strip():
                out[dept.strip()] = email.strip()
    return out


def _recipients_for(dept: str, s, dmap: dict[str, str]) -> list[str]:
    if dept in dmap:
        return [dmap[dept]]
    # Fall back to the central IG list so a digest is never silently dropped.
    return [x.strip() for x in s.notify_recipients.split(",") if x.strip()]


def build_department_digests(db: Session, reqs=None) -> list[dict]:
    """One SLA summary per owning department (departments with at least one case),
    sorted worst-first (overdue, then breach rate)."""
    s = get_settings()
    if reqs is None:
        reqs = db.execute(select(FOIRequest)).scalars().all()

    # Schemes currently deteriorating (reuse the dashboard computation).
    from ..routers.analytics import analytics
    det_schemes = {r["key"] for r in analytics(db=db, user=None)["sla_by_scheme"] if r["deteriorating"]}

    dept = defaultdict(lambda: {"total": 0, "open": 0, "closed": 0, "on_time": 0,
                                "late": 0, "overdue": 0, "due_soon": 0,
                                "overdue_cases": [], "projects": set()})
    for r in reqs:
        g = dept[r.owning_department or "Unassigned"]
        g["total"] += 1
        if r.project:
            g["projects"].add(r.project)
        if r.stage == Stage.CLOSED.value:
            g["closed"] += 1
            cd = _close_date(r)
            if cd:
                if r.deadline and cd <= r.deadline.date():
                    g["on_time"] += 1
                else:
                    g["late"] += 1
        else:
            g["open"] += 1
            st = sla_state(r.received_at, s.statutory_working_days, s.sla_amber_day,
                           s.sla_red_day, paused_days=r.clock_paused_days or 0,
                           paused_since=r.clarification_requested_at)
            if st["paused"]:
                continue
            if st["flag"] == "breach":
                g["overdue"] += 1
                g["overdue_cases"].append({"reference": r.reference, "subject": r.subject,
                                           "deadline": st["deadline"]})
            elif st["flag"] in ("amber", "red"):
                g["due_soon"] += 1

    rows = []
    for name, g in dept.items():
        resolved = g["on_time"] + g["late"]
        breaches = g["late"] + g["overdue"]
        g["overdue_cases"].sort(key=lambda c: c["deadline"])
        rows.append({
            "department": name,
            "total": g["total"], "open": g["open"], "closed": g["closed"],
            "overdue": g["overdue"], "due_soon": g["due_soon"],
            "breach_rate": round(breaches / g["total"] * 100) if g["total"] else 0,
            "on_time_pct": round(g["on_time"] / resolved * 100) if resolved else None,
            "overdue_cases": g["overdue_cases"],
            "deteriorating_schemes": sorted(project_label(p) for p in g["projects"] if p in det_schemes),
        })
    rows.sort(key=lambda x: (-x["overdue"], -x["breach_rate"], -x["open"]))
    return rows


def _render(dg: dict, s, period: str) -> tuple[str, str]:
    subject = f"[FOI SLA digest {period}] {dg['department']} — {dg['open']} open, {dg['overdue']} overdue"
    lines = [
        f"FOI SLA summary for {dg['department']} — week {period}.",
        "",
        f"  Open cases     : {dg['open']}",
        f"  Overdue        : {dg['overdue']}",
        f"  Due soon       : {dg['due_soon']}",
        f"  Closed         : {dg['closed']}",
        f"  Breach rate    : {dg['breach_rate']}%",
        f"  On-time (closed): {'n/a' if dg['on_time_pct'] is None else str(dg['on_time_pct']) + '%'}",
    ]
    if dg["deteriorating_schemes"]:
        lines += ["", "  ⚠ Deteriorating schemes: " + ", ".join(dg["deteriorating_schemes"])]
    if dg["overdue_cases"]:
        lines += ["", "  Overdue cases (oldest deadline first):"]
        for c in dg["overdue_cases"][:10]:
            lines.append(f"    - {c['reference']} (due {c['deadline']}): {c['subject']}")
        if len(dg["overdue_cases"]) > 10:
            lines.append(f"    … and {len(dg['overdue_cases']) - 10} more")
    lines += ["", f"Open the FOI dashboard and filter to {dg['department']} to action these.",
              f"— {s.council_name} FOI response system"]
    return subject, "\n".join(lines)


def send_department_digests(db: Session, force: bool = False) -> dict:
    """Send (or stub-record) one SLA digest per department for the current ISO
    week. Skips departments already sent this week unless ``force``."""
    s = get_settings()
    if not (s.digest_enabled or force):
        return {"status": "disabled", "sent": [], "skipped": [],
                "detail": "Set FOI_DIGEST_ENABLED=true (or trigger manually)."}

    period = current_period()
    digests = build_department_digests(db)
    notifier = notifications.get_notifier()
    dmap = _recipient_map(s)
    already = {row.department for row in db.execute(
        select(DepartmentDigest).where(DepartmentDigest.period == period)).scalars().all()}

    sent, skipped = [], []
    for dg in digests:
        dept = dg["department"]
        if dept in already and not force:
            skipped.append(dept)
            continue
        recipients = _recipients_for(dept, s, dmap)
        subject, body = _render(dg, s, period)
        notifier.send(subject, body, recipients)
        db.add(DepartmentDigest(
            department=dept, period=period, open_cases=dg["open"], overdue=dg["overdue"],
            breach_rate=dg["breach_rate"], channel=s.notify_provider,
            recipients=",".join(recipients), subject=subject,
            detail=body if s.notify_provider == "stub" else "sent"))
        sent.append(dept)

    db.commit()
    return {"status": "ok", "period": period, "provider": s.notify_provider,
            "sent": sent, "skipped": skipped}
