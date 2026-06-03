"""The eight-stage workflow state machine.

Encodes the legal transitions between stages, including the branch from Stage 3
to human review when confidence is low, and the loop-backs when approval or
sign-off request changes. Side-effectful service calls live in the services
package; this module only governs *which* transitions are allowed and updates
the case stage + audit trail.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import audit
from .enums import Stage
from .models import FOIRequest

# Allowed forward/branch transitions. Loop-backs are listed explicitly.
ALLOWED: dict[Stage, set[Stage]] = {
    # Before drafting, an unclear/too-broad request can be paused for clarification.
    Stage.INTAKE: {Stage.TRIAGE, Stage.AWAITING_CLARIFICATION},
    Stage.TRIAGE: {Stage.RETRIEVAL_DRAFT, Stage.AWAITING_CLARIFICATION},
    # Clarification received -> re-triage; or treated as withdrawn if no reply.
    Stage.AWAITING_CLARIFICATION: {Stage.TRIAGE, Stage.CLOSED},
    # Stage 3 branches: high confidence -> compliance gate; else -> human review.
    Stage.RETRIEVAL_DRAFT: {Stage.COMPLIANCE_GATE, Stage.DEPARTMENT_REVIEW},
    Stage.DEPARTMENT_REVIEW: {Stage.UPDATE_RESPONSE},
    Stage.UPDATE_RESPONSE: {Stage.COMPLIANCE_GATE},
    # Compliance gate -> sign-off, or back to the department for rework.
    Stage.COMPLIANCE_GATE: {Stage.SIGN_OFF, Stage.DEPARTMENT_REVIEW},
    # Sign-off -> dispatch, or back to the department if changes are required.
    Stage.SIGN_OFF: {Stage.DISPATCH, Stage.DEPARTMENT_REVIEW},
    Stage.DISPATCH: {Stage.CLOSED},
    # A closed case can be reopened for an internal review.
    Stage.CLOSED: {Stage.INTERNAL_REVIEW},
    # Review either upholds the original (re-close) or sends it back to be revised.
    Stage.INTERNAL_REVIEW: {Stage.CLOSED, Stage.DEPARTMENT_REVIEW},
}


class TransitionError(RuntimeError):
    """Raised when an illegal stage transition is attempted."""


def can_transition(current: Stage, target: Stage) -> bool:
    return target in ALLOWED.get(current, set())


def transition(db: Session, request: FOIRequest, target: Stage, actor: str,
               detail: str = "") -> FOIRequest:
    current = Stage(request.stage)
    if not can_transition(current, target):
        raise TransitionError(
            f"Illegal transition {current.value} -> {target.value}"
        )
    request.stage = target.value
    audit.log(db, request, actor=actor,
              action=f"stage:{current.value}->{target.value}", detail=detail)
    db.add(request)
    return request
