"""Dashboard metrics — per-stage counts and deadline/SLA tracking for the UI."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Cap, require
from ..config import get_settings
from ..database import get_db
from ..enums import Stage
from ..models import FOIRequest, User
from ..people import officer_for
from ..projects import label as project_label
from ..projects import owning_department as scheme_department
from ..sla import sla_state

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
settings = get_settings()


def _card(req: FOIRequest, st: dict) -> dict:
    dept = (req.owning_department or scheme_department(req.project) or "").strip()
    officer = officer_for(dept)
    return {
        "id": req.id, "reference": req.reference, "subject": req.subject,
        "stage": req.stage, "project": req.project or "", "deadline": st["deadline"],
        "working_days_remaining": st["working_days_remaining"],
        "flag": st["flag"], "paused": st["paused"],
        "owner": dept or "FOI team",
        "owner_person": officer["name"],
    }


@router.get("")
def dashboard(db: Session = Depends(get_db), user: User = Depends(require(Cap.READ))):
    s = settings
    reqs = db.execute(select(FOIRequest)).scalars().all()

    stage_counts: dict[str, int] = {}
    sla = {"breach": 0, "red": 0, "amber": 0, "green": 0, "paused": 0}
    overdue: list[dict] = []
    due_soon: list[dict] = []
    overdue_by_scheme: dict[str, int] = {}
    open_count = 0

    for r in reqs:
        stage_counts[r.stage] = stage_counts.get(r.stage, 0) + 1
        if r.stage == Stage.CLOSED.value:
            continue
        open_count += 1
        st = sla_state(r.received_at, s.statutory_working_days, s.sla_amber_day,
                       s.sla_red_day, paused_days=r.clock_paused_days or 0,
                       paused_since=r.clarification_requested_at)
        if st["paused"]:
            sla["paused"] += 1
        else:
            sla[st["flag"]] = sla.get(st["flag"], 0) + 1
        if st["flag"] == "breach" and not st["paused"]:
            overdue.append(_card(r, st))
            if r.project:
                overdue_by_scheme[r.project] = overdue_by_scheme.get(r.project, 0) + 1
        elif st["flag"] in ("amber", "red") and not st["paused"]:
            due_soon.append(_card(r, st))

    overdue.sort(key=lambda c: c["deadline"])
    due_soon.sort(key=lambda c: c["working_days_remaining"])
    closed = stage_counts.get(Stage.CLOSED.value, 0)

    return {
        "stage_counts": stage_counts,
        "totals": {
            "all": len(reqs),
            "open": open_count,
            "closed": closed,
            "awaiting_clarification": stage_counts.get(Stage.AWAITING_CLARIFICATION.value, 0),
            "internal_review": stage_counts.get(Stage.INTERNAL_REVIEW.value, 0),
        },
        "sla": sla,
        "deadlines": {
            "overdue": overdue[:12],
            "overdue_total": len(overdue),
            "due_soon": due_soon[:12],
            "due_soon_total": len(due_soon),
        },
        "overdue_by_scheme": [
            {"key": k, "label": project_label(k), "count": v}
            for k, v in sorted(overdue_by_scheme.items(), key=lambda kv: -kv[1])
        ],
    }
