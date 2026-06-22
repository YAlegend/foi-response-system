"""Case lifecycle API — intake through dispatch."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import schemas
from ..auth import Cap, require
from ..config import get_settings
from ..database import get_db
from ..enums import Stage
from ..models import FOIRequest, User
from .. import people
from ..services import casework, reminders
from ..sla import sla_state
from ..workflow import TransitionError

router = APIRouter(prefix="/requests", tags=["requests"])
settings = get_settings()


def _get(db: Session, request_id: int) -> FOIRequest:
    req = db.get(FOIRequest, request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    return req


@router.post("", response_model=schemas.RequestDetail, status_code=201)
def create_request(payload: schemas.RequestCreate, db: Session = Depends(get_db),
                   user: User = Depends(require(Cap.INTAKE))):
    """Stage 1 — register a new FOI request and start the statutory clock."""
    return casework.create_request(
        db, requester_name=payload.requester_name,
        requester_email=payload.requester_email, subject=payload.subject,
        body=payload.body, requester_type=payload.requester_type,
        actor=user.username)


@router.get("", response_model=list[schemas.RequestOut])
def list_requests(db: Session = Depends(get_db), user: User = Depends(require(Cap.READ))):
    reqs = db.execute(select(FOIRequest).order_by(FOIRequest.id.desc())).scalars().all()
    out: list[schemas.RequestOut] = []
    for r in reqs:
        item = schemas.RequestOut.model_validate(r)
        if r.stage != Stage.CLOSED.value:
            st = sla_state(r.received_at, settings.statutory_working_days,
                           settings.sla_amber_day, settings.sla_red_day,
                           paused_days=r.clock_paused_days or 0,
                           paused_since=r.clarification_requested_at)
            item.sla_flag = st["flag"]
            item.working_days_remaining = st["working_days_remaining"]
            item.paused = st["paused"]
        out.append(item)
    return out


@router.get("/{request_id}", response_model=schemas.RequestDetail)
def get_request(request_id: int, db: Session = Depends(get_db),
                user: User = Depends(require(Cap.READ))):
    req = _get(db, request_id)
    dept = reminders.responsible_for(req)
    officer = people.officer_for(dept)
    # Non-mapped attrs read by the response schema (from_attributes) — not stored.
    req.responsible_person = officer["name"]
    req.responsible_department = dept or "FOI team"
    return req


@router.get("/{request_id}/sla")
def get_sla(request_id: int, db: Session = Depends(get_db),
            user: User = Depends(require(Cap.READ))):
    req = _get(db, request_id)
    return sla_state(req.received_at, settings.statutory_working_days,
                     settings.sla_amber_day, settings.sla_red_day,
                     paused_days=req.clock_paused_days or 0,
                     paused_since=req.clarification_requested_at)


@router.post("/{request_id}/remind")
def remind(request_id: int, db: Session = Depends(get_db),
           user: User = Depends(require(Cap.PROCESS))):
    """Send the responsible department a reminder that this case is due/overdue."""
    req = _get(db, request_id)
    return reminders.send_reminder(db, req, actor=user.username)


def _guard(fn):
    try:
        return fn()
    except TransitionError as exc:
        raise HTTPException(409, str(exc))


@router.post("/{request_id}/triage")
def triage_request(request_id: int, db: Session = Depends(get_db),
                   user: User = Depends(require(Cap.PROCESS))):
    """Stage 2 — classify regime, department and risk flags."""
    req = _get(db, request_id)
    return _guard(lambda: casework.run_triage(db, req, actor=user.username))


@router.post("/{request_id}/autodraft")
def autodraft_request(request_id: int, db: Session = Depends(get_db),
                      user: User = Depends(require(Cap.PROCESS))):
    """Stage 3 — retrieve grounding, draft in house style, and route."""
    req = _get(db, request_id)
    return _guard(lambda: casework.run_autodraft(db, req, actor=user.username))


@router.post("/{request_id}/sme-update", response_model=schemas.DraftOut)
def sme_update(request_id: int, payload: schemas.SMEUpdate, db: Session = Depends(get_db),
               user: User = Depends(require(Cap.PROCESS))):
    """Stages 4 & 5 — SME supplies missing info; new draft re-enters the gate."""
    req = _get(db, request_id)
    return _guard(lambda: casework.sme_update(
        db, req, officer=user.username, supplied_text=payload.supplied_text,
        holding_status=payload.holding_status))


@router.post("/{request_id}/compliance")
def compliance_check(request_id: int, db: Session = Depends(get_db),
                     user: User = Depends(require(Cap.PROCESS))):
    """Stage 6 — run the automated compliance / exemptions checks."""
    req = _get(db, request_id)
    return _guard(lambda: casework.run_compliance(db, req, actor=user.username))


@router.post("/{request_id}/approve", response_model=schemas.RequestOut)
def approve(request_id: int, payload: schemas.ApprovalIn, db: Session = Depends(get_db),
            user: User = Depends(require(Cap.APPROVE))):
    """Department manager approval (or request changes)."""
    req = _get(db, request_id)
    return _guard(lambda: casework.approve(
        db, req, manager=user.username, approved=payload.approved, note=payload.note))


@router.post("/{request_id}/sign-off", response_model=schemas.RequestOut)
def sign_off(request_id: int, payload: schemas.SignOffIn, db: Session = Depends(get_db),
             user: User = Depends(require(Cap.SIGN_OFF))):
    """Stage 7 — senior / Legal & IG final sign-off."""
    req = _get(db, request_id)
    return _guard(lambda: casework.sign_off(
        db, req, officer=user.username, authorised=payload.authorised, note=payload.note))


@router.post("/{request_id}/dispatch", response_model=schemas.RequestOut)
def dispatch(request_id: int, db: Session = Depends(get_db),
             user: User = Depends(require(Cap.DISPATCH))):
    """Stage 8 — FOI team issues the response and closes the case."""
    req = _get(db, request_id)
    return _guard(lambda: casework.dispatch(db, req, foi_officer=user.username))


@router.post("/{request_id}/request-clarification", response_model=schemas.RequestOut)
def request_clarification(request_id: int, payload: schemas.ClarificationRequestIn,
                          db: Session = Depends(get_db),
                          user: User = Depends(require(Cap.PROCESS))):
    """Ask the requester to clarify; pause the statutory clock (FOIA s.1(3))."""
    req = _get(db, request_id)
    return _guard(lambda: casework.request_clarification(
        db, req, officer=user.username, question=payload.question))


@router.post("/{request_id}/provide-clarification", response_model=schemas.RequestOut)
def provide_clarification(request_id: int, payload: schemas.ClarificationProvideIn,
                          db: Session = Depends(get_db),
                          user: User = Depends(require(Cap.PROCESS))):
    """Record the requester's clarification and resume the clock."""
    req = _get(db, request_id)
    return _guard(lambda: casework.provide_clarification(
        db, req, officer=user.username, clarification_text=payload.clarification_text))


@router.post("/{request_id}/internal-review", response_model=schemas.RequestOut)
def internal_review(request_id: int, payload: schemas.ReviewRequestIn,
                    db: Session = Depends(get_db),
                    user: User = Depends(require(Cap.INTAKE))):
    """Reopen a closed case for an internal review (requester dissatisfied)."""
    req = _get(db, request_id)
    return _guard(lambda: casework.request_internal_review(
        db, req, officer=user.username, reason=payload.reason))


@router.post("/{request_id}/internal-review/complete", response_model=schemas.RequestOut)
def complete_internal_review(request_id: int, payload: schemas.ReviewCompleteIn,
                             db: Session = Depends(get_db),
                             user: User = Depends(require(Cap.SIGN_OFF))):
    """Conclude an internal review: uphold the original or revise it."""
    req = _get(db, request_id)
    return _guard(lambda: casework.complete_internal_review(
        db, req, officer=user.username, upheld=payload.upheld, note=payload.note))
