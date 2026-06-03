"""High-level case operations that orchestrate the workflow + services.

This is the layer the API routers call. Each function performs one meaningful
step, records audit events, and (where relevant) drives a stage transition.
Human authorisation is required for every disclosure: drafting and checks are
automated, but approval, sign-off and dispatch are explicit human actions.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit, workflow
from ..config import get_settings
from ..enums import CaseOutcome, HoldingStatus, Stage
from ..models import Draft, FOIRequest
from ..sla import add_working_days, deadline_for, working_days_between
from . import compliance, drafting, triage

settings = get_settings()


# --- Intake (Stage 1) ---------------------------------------------------------

def _next_reference(db: Session) -> str:
    year = datetime.now(timezone.utc).year
    count = db.execute(select(func.count()).select_from(FOIRequest)).scalar_one()
    return f"FOI/{year}/{count + 1:05d}"


def create_request(db: Session, *, requester_name: str, requester_email: str,
                   subject: str, body: str, requester_type: str = "resident",
                   received_at: datetime | None = None,
                   actor: str = "system") -> FOIRequest:
    received = received_at or datetime.now(timezone.utc)
    req = FOIRequest(
        reference=_next_reference(db),
        requester_name=requester_name,
        requester_email=requester_email,
        requester_type=requester_type,
        subject=subject,
        body=body,
        received_at=received,
        deadline=datetime.combine(deadline_for(received, settings.statutory_working_days),
                                  datetime.min.time(), tzinfo=timezone.utc),
        stage=Stage.INTAKE.value,
    )
    db.add(req)
    db.flush()  # assign id
    audit.log(db, req, actor=actor, action="intake:registered",
              detail=f"Registered {req.reference}; deadline {req.deadline.date()}.")
    db.commit()
    db.refresh(req)
    return req


# --- Triage (Stage 2) ---------------------------------------------------------

def run_triage(db: Session, req: FOIRequest, actor: str = "system") -> dict:
    workflow.transition(db, req, Stage.TRIAGE, actor=actor, detail="Auto-triage")
    result = triage.classify(req.subject, req.body)
    req.regime = result.regime
    req.owning_department = result.department
    audit.log(db, req, actor=actor, action="triage:classified",
              detail=f"regime={result.regime}; dept={result.department}; "
                     f"cost_risk={result.cost_risk}; vexatious_risk={result.vexatious_risk}")
    db.add(req)
    db.commit()
    db.refresh(req)
    return {
        "regime": result.regime,
        "department": result.department,
        "cost_risk": result.cost_risk,
        "vexatious_risk": result.vexatious_risk,
        "notes": result.notes,
    }


# --- Retrieval & auto-draft (Stage 3) ----------------------------------------

def _refresh_public_info_before_drafting(db: Session, req: FOIRequest, actor: str) -> None:
    """Bring the public-information knowledge base up to date before drafting,
    when enabled and stale. Best-effort: any failure is swallowed so a refresh
    can never block a draft (the existing KB is still used)."""
    if not (settings.kb_refresh_enabled and settings.kb_refresh_on_draft):
        return
    try:
        from . import kb_refresh
        rec = kb_refresh.refresh_if_stale(db, trigger="pre_draft")
        if rec is not None:
            audit.log(db, req, actor=actor, action="kb:refreshed",
                      detail=f"[{rec.status}] {rec.detail}")
    except Exception:
        pass


def run_autodraft(db: Session, req: FOIRequest, actor: str = "system") -> dict:
    _refresh_public_info_before_drafting(db, req, actor)
    workflow.transition(db, req, Stage.RETRIEVAL_DRAFT, actor=actor,
                        detail="RAG retrieval + draft")
    result = drafting.build_draft(db, req)

    version = (req.latest_draft.version + 1) if req.latest_draft else 1
    draft = Draft(request_id=req.id, version=version, body=result.body,
                  created_by="system", citations=result.citations,
                  confidence=result.confidence)
    db.add(draft)
    req.confidence = result.confidence
    req.holding_status = result.holding_status

    threshold = (settings.semantic_confidence_threshold
                 if settings.retrieval_provider.lower() == "semantic"
                 else settings.auto_draft_confidence_threshold)
    if result.confidence >= threshold and result.holding_status == HoldingStatus.HELD.value:
        route = Stage.COMPLIANCE_GATE
        decision = "auto-proceed to compliance gate"
    else:
        route = Stage.DEPARTMENT_REVIEW
        decision = "route to department human review (low confidence / missing info)"

    audit.log(db, req, actor=actor, action="autodraft:created",
              detail=f"confidence={result.confidence}; {decision}")
    workflow.transition(db, req, route, actor=actor, detail=decision)
    db.commit()
    db.refresh(req)
    return {"confidence": result.confidence, "holding_status": result.holding_status,
            "routed_to": route.value, "decision": decision, "draft_version": version}


# --- Department review + update (Stages 4 & 5) -------------------------------

def sme_update(db: Session, req: FOIRequest, *, officer: str, supplied_text: str,
               holding_status: str | None = None) -> Draft:
    """The subject-matter expert supplies missing information; a new draft
    version is produced and the case re-enters the compliance gate."""
    if req.stage != Stage.DEPARTMENT_REVIEW.value:
        raise workflow.TransitionError("Case is not awaiting department review.")
    workflow.transition(db, req, Stage.UPDATE_RESPONSE, actor=officer,
                        detail="SME supplied information")
    if holding_status:
        req.holding_status = holding_status

    base = req.latest_draft.body if req.latest_draft else ""
    merged = (base + "\n\n[Subject-matter expert addition]\n" + supplied_text).strip()
    version = (req.latest_draft.version + 1) if req.latest_draft else 1
    draft = Draft(request_id=req.id, version=version, body=merged,
                  created_by=officer, confidence=req.confidence)
    db.add(draft)
    audit.log(db, req, actor=officer, action="sme:updated",
              detail=f"draft v{version} produced")
    workflow.transition(db, req, Stage.COMPLIANCE_GATE, actor=officer,
                        detail="Revised draft re-enters compliance gate")
    db.commit()
    db.refresh(req)
    return draft


# --- Compliance gate (Stage 6) -----------------------------------------------

def run_compliance(db: Session, req: FOIRequest, actor: str = "system") -> dict:
    if req.stage != Stage.COMPLIANCE_GATE.value:
        raise workflow.TransitionError("Case is not at the compliance gate.")
    draft = req.latest_draft
    if not draft:
        raise workflow.TransitionError("No draft to check.")
    result = compliance.run_checks(req, draft.body)
    audit.log(db, req, actor=actor, action="compliance:checked",
              detail=f"passed={result.passed}; requires_human={result.requires_human}; "
                     + "; ".join(f"{i.name}={i.passed}" for i in result.items))
    db.commit()
    return {
        "passed": result.passed,
        "requires_human": result.requires_human,
        "items": [{"name": i.name, "passed": i.passed, "detail": i.detail}
                  for i in result.items],
    }


# --- Approval, sign-off, dispatch (Stages 6->7->8) ---------------------------

def approve(db: Session, req: FOIRequest, *, manager: str, approved: bool,
            note: str = "") -> FOIRequest:
    """Department manager approval. If not approved, loop back to review."""
    if req.stage != Stage.COMPLIANCE_GATE.value:
        raise workflow.TransitionError("Case is not awaiting approval.")
    if approved:
        workflow.transition(db, req, Stage.SIGN_OFF, actor=manager,
                            detail=f"Approved. {note}".strip())
    else:
        workflow.transition(db, req, Stage.DEPARTMENT_REVIEW, actor=manager,
                            detail=f"Changes requested. {note}".strip())
    db.commit()
    db.refresh(req)
    return req


def sign_off(db: Session, req: FOIRequest, *, officer: str, authorised: bool,
             note: str = "") -> FOIRequest:
    """Senior / Legal & IG final sign-off."""
    if req.stage != Stage.SIGN_OFF.value:
        raise workflow.TransitionError("Case is not awaiting sign-off.")
    if authorised:
        audit.log(db, req, actor=officer, action="signoff:authorised", detail=note)
        # Stage remains SIGN_OFF until the FOI team dispatches.
    else:
        workflow.transition(db, req, Stage.DEPARTMENT_REVIEW, actor=officer,
                            detail=f"Sign-off rejected. {note}".strip())
    db.commit()
    db.refresh(req)
    return req


def dispatch(db: Session, req: FOIRequest, *, foi_officer: str) -> FOIRequest:
    """FOI team issues the response to the requester and closes the case."""
    if req.stage != Stage.SIGN_OFF.value:
        raise workflow.TransitionError("Case must be signed off before dispatch.")
    workflow.transition(db, req, Stage.DISPATCH, actor=foi_officer,
                        detail="Response issued to requester")
    # Map holding status to a case outcome.
    req.outcome = {
        HoldingStatus.HELD.value: CaseOutcome.GRANTED_FULL.value,
        HoldingStatus.PARTIAL.value: CaseOutcome.GRANTED_PARTIAL.value,
        HoldingStatus.NOT_HELD.value: CaseOutcome.NOT_HELD.value,
    }.get(req.holding_status, CaseOutcome.OPEN.value)
    workflow.transition(db, req, Stage.CLOSED, actor=foi_officer,
                        detail=f"Case closed; outcome={req.outcome}")
    db.commit()
    db.refresh(req)
    return req


# --- Clarification (FOIA s.1(3)) — pause/resume the statutory clock ----------

def request_clarification(db: Session, req: FOIRequest, *, officer: str,
                          question: str) -> FOIRequest:
    """Ask the requester to clarify an unclear/too-broad request, stopping the
    20-working-day clock until they reply."""
    if req.stage not in (Stage.INTAKE.value, Stage.TRIAGE.value):
        raise workflow.TransitionError(
            "Clarification can only be requested before drafting (intake or triage).")
    req.clarification_requested_at = datetime.now(timezone.utc)
    audit.log(db, req, actor=officer, action="clarification:requested",
              detail=f"Sent to {req.requester_email}: {question}".strip())
    workflow.transition(db, req, Stage.AWAITING_CLARIFICATION, actor=officer,
                        detail="Awaiting clarification from requester; clock paused")
    db.commit()
    db.refresh(req)
    return req


def provide_clarification(db: Session, req: FOIRequest, *, officer: str,
                          clarification_text: str) -> FOIRequest:
    """Record the requester's clarification, resume the clock (extending the
    deadline by the working days spent waiting) and return to triage."""
    if req.stage != Stage.AWAITING_CLARIFICATION.value:
        raise workflow.TransitionError("Case is not awaiting clarification.")

    paused = 0
    if req.clarification_requested_at:
        paused = working_days_between(req.clarification_requested_at.date(), date.today())
    req.clock_paused_days = (req.clock_paused_days or 0) + paused
    req.clarification_requested_at = None
    if req.deadline:                       # push the stored deadline out by the pause
        req.deadline = datetime.combine(
            add_working_days(req.deadline.date(), paused), time.min, tzinfo=timezone.utc)
    # Fold the clarification into the request so re-triage / drafting use it.
    req.body = f"{req.body}\n\n[Clarification from requester]\n{clarification_text}".strip()

    audit.log(db, req, actor=officer, action="clarification:received",
              detail=f"Clock resumed after {paused} working day(s) paused.")
    workflow.transition(db, req, Stage.TRIAGE, actor=officer,
                        detail="Clarification received; clock resumed")
    db.commit()
    db.refresh(req)
    return req


# --- Internal review (reopen a closed case) ----------------------------------

def request_internal_review(db: Session, req: FOIRequest, *, officer: str,
                            reason: str) -> FOIRequest:
    """Reopen a dispatched case because the requester is dissatisfied."""
    if req.stage != Stage.CLOSED.value:
        raise workflow.TransitionError("Internal review applies only to closed cases.")
    audit.log(db, req, actor=officer, action="review:requested", detail=reason)
    workflow.transition(db, req, Stage.INTERNAL_REVIEW, actor=officer,
                        detail="Internal review requested by requester")
    db.commit()
    db.refresh(req)
    return req


def complete_internal_review(db: Session, req: FOIRequest, *, officer: str,
                             upheld: bool, note: str = "") -> FOIRequest:
    """Conclude a review: uphold the original (re-close) or send it back to the
    department to be revised."""
    if req.stage != Stage.INTERNAL_REVIEW.value:
        raise workflow.TransitionError("Case is not under internal review.")
    if upheld:
        audit.log(db, req, actor=officer, action="review:upheld", detail=note)
        workflow.transition(db, req, Stage.CLOSED, actor=officer,
                            detail=f"Internal review: original response upheld. {note}".strip())
    else:
        audit.log(db, req, actor=officer, action="review:revise", detail=note)
        workflow.transition(db, req, Stage.DEPARTMENT_REVIEW, actor=officer,
                            detail=f"Internal review: response to be revised. {note}".strip())
    db.commit()
    db.refresh(req)
    return req
