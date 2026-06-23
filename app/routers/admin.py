"""Admin / Phase 0 ingestion endpoints (feature-flagged)."""
from __future__ import annotations

from collections import Counter, defaultdict

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import auth, schemas
from ..auth import Cap, require, require_live
from ..config import get_settings
from ..database import get_db
from ..enums import Role
from ..ingestion import (documents, knowledge_base, published_responses,
                         website_crawler, whatdotheyknow)
from ..models import (DepartmentDigest, KnowledgeChunk, KnowledgeDoc,
                      KnowledgeRefresh, SchemeNotification, User)

router = APIRouter(prefix="/admin", tags=["admin"])
settings = get_settings()


def _kb_breakdown(db: Session) -> list[dict]:
    """Group the knowledge base by **owning department -> scheme**, with counts.

    A doc's scheme is its `project` tag; the scheme's owning department comes from
    the project catalogue (config), falling back to the most common uploader
    department for that scheme, then "Other". Untagged docs (public council
    material with no scheme) collect under "General council information"."""
    catalog = {c["key"]: c for c in get_settings().project_catalog}
    rows = db.execute(
        select(KnowledgeDoc.project, KnowledgeDoc.status, KnowledgeDoc.department,
               func.count()).group_by(KnowledgeDoc.project, KnowledgeDoc.status,
                                      KnowledgeDoc.department)).all()

    per_project: dict[str, dict] = defaultdict(
        lambda: {"total": 0, "approved": 0, "pending_review": 0, "depts": Counter()})
    for project, status, dept, n in rows:
        p = per_project[project or ""]
        p["total"] += n
        if status in ("approved", "pending_review"):
            p[status] += n
        if dept:
            p["depts"][dept] += n

    GENERAL = "General council information"
    by_dept: dict[str, list] = defaultdict(list)
    for key, c in per_project.items():
        if not key:
            owner, label = GENERAL, "(no scheme)"
        else:
            cat = catalog.get(key)
            label = cat["label"] if cat else key
            owner = (cat["department"] if cat
                     else (c["depts"].most_common(1)[0][0] if c["depts"] else "Other"))
        by_dept[owner].append({"key": key, "label": label, "total": c["total"],
                               "approved": c["approved"], "pending_review": c["pending_review"]})

    out = []
    for owner in sorted(by_dept, key=lambda d: (d == GENERAL, d.lower())):
        projects = sorted(by_dept[owner], key=lambda x: (x["key"] == "", x["label"].lower()))
        out.append({"department": owner,
                    "total": sum(p["total"] for p in projects),
                    "pending_review": sum(p["pending_review"] for p in projects),
                    "projects": projects})
    return out


@router.get("/knowledge-base")
def kb_stats(db: Session = Depends(get_db), user: User = Depends(require(Cap.CONTRIBUTE))):
    chunks = db.execute(select(func.count()).select_from(KnowledgeChunk)).scalar_one()
    indexed_docs = db.execute(
        select(func.count(func.distinct(KnowledgeChunk.doc_id)))).scalar_one()
    return {
        "breakdown": _kb_breakdown(db),
        "total": knowledge_base.count(db),
        "website": knowledge_base.count(db, "website"),
        "published_responses": knowledge_base.count(db, "published_response"),
        "manual": knowledge_base.count(db, "manual"),
        "department": knowledge_base.count(db, "department"),
        "approved": knowledge_base.count(db, status="approved"),
        "pending_review": knowledge_base.count(db, status="pending_review"),
        "rejected": knowledge_base.count(db, status="rejected"),
        "ingestion_enabled": settings.ingest_enabled,
        "retrieval_provider": settings.retrieval_provider,
        "indexed_docs": indexed_docs,
        "indexed_chunks": chunks,
    }


def _doc_out(doc: KnowledgeDoc) -> schemas.KnowledgeDocOut:
    # Build explicitly: `chunks` on the model is a relationship list, not the
    # int count the schema exposes, so from-attributes validation can't be used.
    return schemas.KnowledgeDocOut(
        id=doc.id, source=doc.source, title=doc.title, url=doc.url,
        ingested_at=doc.ingested_at,
        department=doc.department or "", uploaded_by=doc.uploaded_by or "",
        project=doc.project or "", status=doc.status or "approved",
        reviewed_by=doc.reviewed_by or "",
        content_chars=len(doc.content or ""), chunks=len(doc.chunks),
    )


def _store_doc(db: Session, user: User, *, title: str, content: str,
               url: str | None = None, project: str = "") -> KnowledgeDoc:
    """Upsert a private KB upload with the contributor's provenance.

    A subject department's contributions are tagged source='department' (and with
    the department + uploader); admins curating general material use 'manual'.
    Either way the upload lands as **pending_review** and is NOT indexed or
    retrievable yet — it only grounds drafts once a reviewer approves it (see
    `approve_doc`). This is the project/department review gate: unvetted internal
    material can never leak into a published FOI response."""
    is_dept = user.role == Role.DEPARTMENT.value
    doc = knowledge_base.upsert(db, source="department" if is_dept else "manual",
                                title=title or "(untitled)", content=content, url=url,
                                project=project, status="pending_review")
    doc.department = user.department or ""
    doc.uploaded_by = user.username
    doc.status = "pending_review"   # force-pending even on re-upsert of an existing doc
    db.commit()
    db.refresh(doc)
    return doc


@router.get("/knowledge-base/docs", response_model=list[schemas.KnowledgeDocOut])
def list_docs(db: Session = Depends(get_db), user: User = Depends(require(Cap.CONTRIBUTE))):
    docs = db.execute(select(KnowledgeDoc).order_by(KnowledgeDoc.id.desc())).scalars().all()
    return [_doc_out(d) for d in docs]


@router.post("/knowledge-base/docs", response_model=schemas.KnowledgeDocOut, status_code=201)
def add_doc(payload: schemas.KnowledgeDocIn, db: Session = Depends(get_db),
            user: User = Depends(require_live(Cap.CONTRIBUTE))):
    """Add a private knowledge document (pasted text). Lands in the review queue
    (pending_review); a reviewer must approve it before it can ground a draft."""
    return _doc_out(_store_doc(db, user, title=payload.title, content=payload.content,
                               url=payload.url, project=payload.project))


# Uploads can be larger than pasted text; cap to keep a single doc sane.
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


@router.post("/knowledge-base/upload", response_model=schemas.KnowledgeDocOut, status_code=201)
async def upload_doc(file: UploadFile = File(...), title: str = Form(""),
                     project: str = Form(""),
                     db: Session = Depends(get_db),
                     user: User = Depends(require_live(Cap.CONTRIBUTE))):
    """Upload a document (PDF / Word / text / HTML) for internal material the
    department holds that is not published anywhere. Its extracted text becomes a
    knowledge document in the review queue (pending_review) — optionally scoped to
    a ``project`` — and only grounds drafts once a reviewer approves it."""
    suffix = documents._suffix(file.filename or "")
    if suffix not in documents.SUPPORTED:
        raise HTTPException(415, f"Unsupported file type '{suffix or file.filename}'. "
                                 f"Allowed: {', '.join(sorted(documents.SUPPORTED))}.")
    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large (limit 25 MB).")
    text = documents.extract_text(file.filename or "", data)
    if len(text) < 20:
        raise HTTPException(422, "No readable text found in the file. Scanned/image-only "
                                 "PDFs aren't supported (no OCR); upload a text-based file.")
    title = title.strip() or (file.filename or "Uploaded document").rsplit(".", 1)[0]
    return _doc_out(_store_doc(db, user, title=title, content=text, project=project.strip()))


# --- Review gate: approve / reject pending department & project uploads --------

@router.get("/knowledge-base/pending", response_model=list[schemas.KnowledgeDocOut])
def list_pending(db: Session = Depends(get_db), user: User = Depends(require(Cap.CONTRIBUTE))):
    """The review queue: private uploads awaiting approval before they can ground
    a draft. Ordered oldest-first so the queue is worked in arrival order."""
    docs = db.execute(
        select(KnowledgeDoc).where(KnowledgeDoc.status == "pending_review")
        .order_by(KnowledgeDoc.id)
    ).scalars().all()
    return [_doc_out(d) for d in docs]


@router.post("/knowledge-base/docs/{doc_id}/approve", response_model=schemas.KnowledgeDocOut)
def approve_doc(doc_id: int, payload: schemas.KnowledgeReviewIn | None = None,
                db: Session = Depends(get_db), user: User = Depends(require_live(Cap.ADMIN))):
    """Approve a pending upload: mark it reviewed, make it retrievable, and (in
    semantic mode) index it now so it is searchable immediately."""
    from datetime import datetime, timezone
    doc = db.get(KnowledgeDoc, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    if doc.status != "pending_review":
        raise HTTPException(409, f"Document is '{doc.status}', not pending review.")
    doc.status = "approved"
    doc.reviewed_by = user.username
    doc.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(doc)
    if settings.retrieval_provider.lower() == "semantic":
        try:
            from ..reindex import index_doc
            index_doc(db, doc)            # now searchable
            db.refresh(doc)
        except (RuntimeError, NotImplementedError):
            pass                           # approved; a full reindex will pick it up
    return _doc_out(doc)


@router.post("/knowledge-base/docs/{doc_id}/reject", response_model=schemas.KnowledgeDocOut)
def reject_doc(doc_id: int, payload: schemas.KnowledgeReviewIn | None = None,
               db: Session = Depends(get_db), user: User = Depends(require_live(Cap.ADMIN))):
    """Reject a pending upload: it stays on record (audit) but is never retrieved.
    Its chunks, if any, are cleared so it cannot ground a draft."""
    from datetime import datetime, timezone
    doc = db.get(KnowledgeDoc, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    doc.status = "rejected"
    doc.reviewed_by = user.username
    doc.reviewed_at = datetime.now(timezone.utc)
    doc.chunks.clear()
    db.commit()
    db.refresh(doc)
    return _doc_out(doc)


@router.delete("/knowledge-base/docs/{doc_id}")
def delete_doc(doc_id: int, db: Session = Depends(get_db),
               user: User = Depends(require_live(Cap.ADMIN))):
    doc = db.get(KnowledgeDoc, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    db.delete(doc)        # cascade removes its chunks
    db.commit()
    return {"deleted": doc_id}


# --- User administration: create department (and other) accounts --------------

@router.get("/users", response_model=list[schemas.UserSummary])
def list_users(db: Session = Depends(get_db), user: User = Depends(require(Cap.ADMIN))):
    return db.execute(select(User).order_by(User.id)).scalars().all()


@router.post("/users", response_model=schemas.UserSummary, status_code=201)
def create_user(payload: schemas.UserCreateIn, db: Session = Depends(get_db),
                user: User = Depends(require_live(Cap.ADMIN))):
    """Create an account — e.g. a subject department that uploads documents."""
    valid_roles = {r.value for r in Role}
    if payload.role not in valid_roles:
        raise HTTPException(422, f"Unknown role '{payload.role}'. "
                                 f"Valid: {', '.join(sorted(valid_roles))}.")
    if db.execute(select(User).where(User.username == payload.username)).scalar_one_or_none():
        raise HTTPException(409, f"Username '{payload.username}' already exists.")
    return auth.create_user(db, username=payload.username, password=payload.password,
                            role=payload.role, full_name=payload.full_name,
                            department=payload.department, email=payload.email)


@router.get("/knowledge-base/refresh", response_model=schemas.KnowledgeRefreshStatus)
def refresh_status(db: Session = Depends(get_db), user: User = Depends(require(Cap.READ))):
    """When the public-information KB was last refreshed, whether it is stale,
    and the recent refresh history."""
    from ..services import kb_refresh
    recent = db.execute(
        select(KnowledgeRefresh).order_by(KnowledgeRefresh.started_at.desc()).limit(10)
    ).scalars().all()
    return schemas.KnowledgeRefreshStatus(
        enabled=settings.kb_refresh_enabled,
        on_draft=settings.kb_refresh_on_draft,
        max_age_days=settings.kb_refresh_max_age_days,
        stale=kb_refresh.is_stale(db),
        last=kb_refresh.last_successful(db),
        history=recent,
    )


@router.post("/knowledge-base/refresh", response_model=schemas.KnowledgeRefreshOut)
def refresh_now(db: Session = Depends(get_db), user: User = Depends(require_live(Cap.ADMIN))):
    """Refresh the public-information knowledge base now (re-crawl + re-ingest +
    reindex). Forced, so it runs even if the auto-refresh flag is off; individual
    sources still require their FOI_INGEST_* flags and stay best-effort."""
    from ..services import kb_refresh
    return kb_refresh.refresh(db, trigger="manual", force=True)


@router.post("/reindex")
def reindex(db: Session = Depends(get_db), user: User = Depends(require_live(Cap.ADMIN))):
    """Build the semantic chunk index (needs FOI_RETRIEVAL_PROVIDER=semantic)."""
    from ..reindex import reindex as _reindex
    try:
        return _reindex(db)
    except (NotImplementedError, RuntimeError) as exc:
        raise HTTPException(501, str(exc))


@router.post("/ingest/website")
def ingest_website(max_pages: int | None = None, db: Session = Depends(get_db),
                   user: User = Depends(require_live(Cap.ADMIN))):
    try:
        n = website_crawler.crawl(db, max_pages=max_pages)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return {"ingested": n, "source": "website"}


@router.post("/ingest/published-responses")
def ingest_published(feed_dir: str | None = None, db: Session = Depends(get_db),
                     user: User = Depends(require_live(Cap.ADMIN))):
    try:
        n = published_responses.ingest(db, feed_dir=feed_dir)
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        raise HTTPException(409, str(exc))
    return {"ingested": n, "source": "published_response"}


@router.post("/ingest/whatdotheyknow")
def ingest_wdtk(db: Session = Depends(get_db), user: User = Depends(require_live(Cap.ADMIN))):
    """Ingest already-published FOI Q&A from the WhatDoTheyKnow archive for the
    configured authority (FOI_WHATDOTHEYKNOW_AUTHORITY)."""
    try:
        n = whatdotheyknow.ingest(db)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return {"ingested": n, "source": "published_response", "via": "whatdotheyknow"}


# --- Breach-trend notifications ------------------------------------------------

@router.get("/notifications")
def notifications_history(db: Session = Depends(get_db), user: User = Depends(require(Cap.READ))):
    """Recent breach-trend notification events (sent alerts + recoveries).
    With the stub provider, each alert row's `detail` is the message that would
    have been emailed."""
    rows = db.execute(select(SchemeNotification)
                      .order_by(SchemeNotification.created_at.desc(), SchemeNotification.id.desc())
                      .limit(50)).scalars().all()
    return {
        "provider": settings.notify_provider,
        "enabled": settings.notify_enabled,
        "recipients": [x.strip() for x in settings.notify_recipients.split(",") if x.strip()],
        "events": [{
            "scheme": r.scheme, "label": r.label, "event": r.event,
            "recent": r.recent, "prior": r.prior, "breach_rate": r.breach_rate,
            "channel": r.channel, "recipients": r.recipients, "subject": r.subject,
            "detail": r.detail, "created_at": r.created_at,
        } for r in rows],
    }


@router.post("/notifications/run")
def notifications_run(db: Session = Depends(get_db), user: User = Depends(require_live(Cap.ADMIN))):
    """Run the breach-trend notification check now (forced, even if the scheduled
    job is disabled). Sends only on schemes that have newly started deteriorating."""
    from ..services import notifications
    return notifications.check_and_notify(db, force=True)


# --- Per-department SLA digest -------------------------------------------------

@router.get("/digests")
def digests_history(db: Session = Depends(get_db), user: User = Depends(require(Cap.READ))):
    """Recent per-department SLA digests. With the stub provider each row's
    `detail` is the digest body that would have been emailed."""
    rows = db.execute(select(DepartmentDigest)
                      .order_by(DepartmentDigest.created_at.desc(), DepartmentDigest.id.desc())
                      .limit(50)).scalars().all()
    return {
        "provider": settings.notify_provider,
        "enabled": settings.digest_enabled,
        "events": [{
            "department": r.department, "period": r.period, "open": r.open_cases,
            "overdue": r.overdue, "breach_rate": r.breach_rate, "channel": r.channel,
            "recipients": r.recipients, "subject": r.subject, "detail": r.detail,
            "created_at": r.created_at,
        } for r in rows],
    }


@router.post("/digests/run")
def digests_run(force: bool = False, db: Session = Depends(get_db),
                user: User = Depends(require_live(Cap.ADMIN))):
    """Send the per-department SLA digests now (forced). Without ``force`` a
    department already sent this ISO week is skipped."""
    from ..services import digests
    return digests.send_department_digests(db, force=True)
