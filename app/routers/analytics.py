"""Analytics — aggregate FOI performance for the dashboard."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Cap, require
from ..config import get_settings
from ..database import get_db
from ..enums import CaseOutcome, Regime, Stage
from ..models import FOIRequest, User
from ..projects import label as project_label
from ..sla import sla_state, working_days_between

router = APIRouter(prefix="/analytics", tags=["analytics"])
settings = get_settings()

_OUTCOME_LABELS = {
    CaseOutcome.GRANTED_FULL.value: "Granted in full",
    CaseOutcome.GRANTED_PARTIAL.value: "Granted in part",
    CaseOutcome.NOT_HELD.value: "Not held",
    CaseOutcome.REFUSED.value: "Refused",
    CaseOutcome.OPEN.value: "Open",
}


def _close_date(req: FOIRequest):
    """The date a case was closed, from its audit trail (or None)."""
    closed = [e.created_at for e in req.events if e.action.endswith("->closed")]
    return max(closed).date() if closed else None


@router.get("")
def analytics(db: Session = Depends(get_db), user: User = Depends(require(Cap.READ))):
    reqs = db.execute(select(FOIRequest)).scalars().all()

    by_regime = {Regime.FOIA.value: 0, Regime.EIR.value: 0}
    by_outcome: dict[str, int] = {}
    by_department: dict[str, int] = {}
    by_project: dict[str, int] = {}
    # Per-scheme SLA accumulator: open/closed split, on-time vs late closes, and
    # cases currently overdue (open, past the statutory deadline, not paused).
    scheme: dict[str, dict] = defaultdict(
        lambda: {"total": 0, "open": 0, "closed": 0, "on_time": 0,
                 "late": 0, "overdue": 0, "durations": []})
    clarifications = 0
    on_time = breached = 0
    close_durations: list[int] = []

    for r in reqs:
        by_regime[r.regime] = by_regime.get(r.regime, 0) + 1
        if r.owning_department:
            by_department[r.owning_department] = by_department.get(r.owning_department, 0) + 1
        if r.project:
            by_project[r.project] = by_project.get(r.project, 0) + 1
            sc = scheme[r.project]
            sc["total"] += 1
            if r.stage == Stage.CLOSED.value:
                sc["closed"] += 1
                cd = _close_date(r)
                if cd:
                    if r.deadline and cd <= r.deadline.date():
                        sc["on_time"] += 1
                    else:
                        sc["late"] += 1
                    sc["durations"].append(working_days_between(r.received_at.date(), cd))
            else:
                sc["open"] += 1
                st = sla_state(r.received_at, settings.statutory_working_days,
                               settings.sla_amber_day, settings.sla_red_day,
                               paused_days=r.clock_paused_days or 0,
                               paused_since=r.clarification_requested_at)
                if st["flag"] == "breach" and not st["paused"]:
                    sc["overdue"] += 1
        if (r.clock_paused_days or 0) > 0 or r.clarification_requested_at is not None:
            clarifications += 1
        if r.stage == Stage.CLOSED.value:
            by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
            cd = _close_date(r)
            if cd:
                if r.deadline and cd <= r.deadline.date():
                    on_time += 1
                else:
                    breached += 1
                close_durations.append(working_days_between(r.received_at.date(), cd))

    # Requests received per week, last 8 ISO weeks (oldest first).
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    weeks = [monday - timedelta(weeks=i) for i in range(7, -1, -1)]
    wk_counts = {w: 0 for w in weeks}
    for r in reqs:
        d = r.received_at.date()
        wk = d - timedelta(days=d.weekday())
        if wk in wk_counts:
            wk_counts[wk] += 1
    intake = [{"label": w.strftime("%d %b"), "value": wk_counts[w]} for w in weeks]

    # Per-scheme SLA: breach rate counts both closed-late and currently-overdue
    # cases against the scheme's total; on-time % is over resolved cases only.
    sla_by_scheme = []
    for key, sc in scheme.items():
        resolved = sc["on_time"] + sc["late"]
        breaches = sc["late"] + sc["overdue"]
        sla_by_scheme.append({
            "key": key, "label": project_label(key),
            "total": sc["total"], "open": sc["open"], "closed": sc["closed"],
            "breached": sc["late"], "overdue": sc["overdue"],
            "on_time_pct": round(sc["on_time"] / resolved * 100) if resolved else None,
            "breach_rate": round(breaches / sc["total"] * 100) if sc["total"] else 0,
            "avg_working_days_to_close": round(sum(sc["durations"]) / len(sc["durations"]), 1)
                                         if sc["durations"] else None,
        })
    sla_by_scheme.sort(key=lambda x: (-x["breach_rate"], -x["total"]))

    dated = on_time + breached
    return {
        "by_regime": [{"label": k, "value": v} for k, v in by_regime.items()],
        "by_outcome": [{"label": _OUTCOME_LABELS.get(k, k), "key": k, "value": v}
                       for k, v in sorted(by_outcome.items(), key=lambda kv: -kv[1])],
        "by_department": [{"label": k, "value": v} for k, v in
                          sorted(by_department.items(), key=lambda kv: -kv[1])],
        "by_project": [{"label": project_label(k), "key": k, "value": v} for k, v in
                       sorted(by_project.items(), key=lambda kv: -kv[1])],
        "sla_by_scheme": sla_by_scheme,
        "intake_by_week": intake,
        "sla": {
            "closed": sum(1 for r in reqs if r.stage == Stage.CLOSED.value),
            "on_time": on_time,
            "breached": breached,
            "on_time_pct": round(on_time / dated * 100) if dated else None,
            "avg_working_days_to_close": round(sum(close_durations) / len(close_durations), 1)
                                         if close_durations else None,
        },
        "clarifications": clarifications,
    }
