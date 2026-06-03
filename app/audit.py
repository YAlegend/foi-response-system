"""Audit helper — every state change and decision is logged against the case."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import AuditEvent, FOIRequest


def log(db: Session, request: FOIRequest, actor: str, action: str,
        detail: str = "") -> AuditEvent:
    event = AuditEvent(
        request_id=request.id,
        stage=request.stage,
        actor=actor,
        action=action,
        detail=detail,
    )
    db.add(event)
    return event
