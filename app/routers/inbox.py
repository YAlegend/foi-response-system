"""Dedicated FOI mailbox API — the system's intake source.

Requests arrive in a monitored inbox rather than a public form. Caseworkers
poll the mailbox, then either log a message as an FOI case or dismiss it.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import schemas
from ..auth import Cap, require
from ..config import get_settings
from ..database import get_db
from ..enums import InboxStatus
from ..models import FOIRequest, InboxMessage, User
from ..services import inbox

router = APIRouter(prefix="/inbox", tags=["inbox"])
settings = get_settings()


def _get(db: Session, message_id: int) -> InboxMessage:
    msg = db.get(InboxMessage, message_id)
    if not msg:
        raise HTTPException(404, "Message not found")
    return msg


@router.get("", response_model=list[schemas.InboxOut])
def list_inbox(db: Session = Depends(get_db), user: User = Depends(require(Cap.READ))):
    """All mailbox messages (newest first). For 'new' messages we attach a
    threading hint: which existing case the message looks like a reply to."""
    msgs = inbox.list_messages(db)
    requests = db.execute(select(FOIRequest)).scalars().all()
    out: list[schemas.InboxOut] = []
    for m in msgs:
        item = schemas.InboxOut.model_validate(m)
        if m.status == InboxStatus.NEW.value:
            sug = inbox.suggest_case(m, requests)
            if sug:
                item.suggested_request_id = sug["request_id"]
                item.suggested_reference = sug["reference"]
                item.suggested_reason = sug["reason"]
        out.append(item)
    return out


@router.post("/poll")
def poll_inbox(db: Session = Depends(get_db), user: User = Depends(require(Cap.INTAKE))):
    """Check the dedicated mailbox for newly-arrived FOI emails."""
    try:
        new = inbox.poll(db)
    except NotImplementedError as exc:
        raise HTTPException(501, str(exc))
    return {
        "address": settings.inbox_address,
        "provider": settings.inbox_provider,
        "new": len(new),
        "messages": [schemas.InboxOut.model_validate(m).model_dump(mode="json")
                     for m in new],
    }


@router.post("/{message_id}/import", response_model=schemas.RequestDetail, status_code=201)
def import_message(message_id: int, payload: schemas.InboxImportIn | None = None,
                   db: Session = Depends(get_db), user: User = Depends(require(Cap.INTAKE))):
    """Log a mailbox message as an FOI case (Stage 1 intake)."""
    msg = _get(db, message_id)
    payload = payload or schemas.InboxImportIn()
    try:
        return inbox.import_message(db, msg, requester_type=payload.requester_type,
                                    officer=user.username)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.post("/{message_id}/link", response_model=schemas.RequestDetail)
def link_message(message_id: int, payload: schemas.InboxLinkIn,
                 db: Session = Depends(get_db), user: User = Depends(require(Cap.INTAKE))):
    """File a mailbox message as correspondence on an existing case (threading)."""
    msg = _get(db, message_id)
    try:
        return inbox.link_message(db, msg, request_id=payload.request_id,
                                  officer=user.username)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.post("/{message_id}/dismiss", response_model=schemas.InboxOut)
def dismiss_message(message_id: int, db: Session = Depends(get_db),
                    user: User = Depends(require(Cap.INTAKE))):
    """Mark a message as not an FOI request (misdirected / spam)."""
    return inbox.dismiss(db, _get(db, message_id))
