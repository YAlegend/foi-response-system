"""Admin / Phase 0 ingestion endpoints (feature-flagged)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import auth, schemas
from ..auth import Cap, require
from ..config import get_settings
from ..database import get_db
from ..enums import Role
from ..ingestion import documents, knowledge_base, published_responses, website_crawler
from ..models import KnowledgeChunk, KnowledgeDoc, KnowledgeRefresh, User

router = APIRouter(prefix="/admin", tags=["admin"])
settings = get_settings()


@router.get("/knowledge-base")
def kb_stats(db: Session = Depends(get_db), user: User = Depends(require(Cap.CONTRIBUTE))):
    chunks = db.execute(select(func.count()).select_from(KnowledgeChunk)).scalar_one()
    indexed_docs = db.execute(
        select(func.count(func.distinct(KnowledgeChunk.doc_id)))).scalar_one()
    return {
        "total": knowledge_base.count(db),
        "website": knowledge_base.count(db, "website"),
        "published_responses": knowledge_base.count(db, "published_response"),
        "manual": knowledge_base.count(db, "manual"),
        "department": knowledge_base.count(db, "department"),
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
        content_chars=len(doc.content or ""), chunks=len(doc.chunks),
    )


def _store_doc(db: Session, user: User, *, title: str, content: str,
               url: str | None = None) -> KnowledgeDoc:
    """Upsert a KB document with the contributor's provenance, and index it
    immediately when running semantic retrieval. Shared by paste-add and upload.

    A subject department's contributions are tagged source='department' (and with
    the department + uploader), so the drafter — and the audit — can see where a
    grounding fact came from. Admins curating general material use 'manual'."""
    is_dept = user.role == Role.DEPARTMENT.value
    doc = knowledge_base.upsert(db, source="department" if is_dept else "manual",
                                title=title or "(untitled)", content=content, url=url)
    doc.department = user.department or ""
    doc.uploaded_by = user.username
    db.commit()
    db.refresh(doc)
    if settings.retrieval_provider.lower() == "semantic":
        try:
            from ..reindex import index_doc
            index_doc(db, doc)            # make it searchable immediately
            db.refresh(doc)
        except (RuntimeError, NotImplementedError):
            pass                           # added; a full reindex will pick it up
    return doc


@router.get("/knowledge-base/docs", response_model=list[schemas.KnowledgeDocOut])
def list_docs(db: Session = Depends(get_db), user: User = Depends(require(Cap.CONTRIBUTE))):
    docs = db.execute(select(KnowledgeDoc).order_by(KnowledgeDoc.id.desc())).scalars().all()
    return [_doc_out(d) for d in docs]


@router.post("/knowledge-base/docs", response_model=schemas.KnowledgeDocOut, status_code=201)
def add_doc(payload: schemas.KnowledgeDocIn, db: Session = Depends(get_db),
            user: User = Depends(require(Cap.CONTRIBUTE))):
    """Add a knowledge document (pasted text) the drafter can ground on."""
    return _doc_out(_store_doc(db, user, title=payload.title, content=payload.content,
                               url=payload.url))


# Uploads can be larger than pasted text; cap to keep a single doc sane.
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


@router.post("/knowledge-base/upload", response_model=schemas.KnowledgeDocOut, status_code=201)
async def upload_doc(file: UploadFile = File(...), title: str = Form(""),
                     db: Session = Depends(get_db),
                     user: User = Depends(require(Cap.CONTRIBUTE))):
    """Upload a document (PDF / Word / text / HTML); its extracted text becomes a
    knowledge document. For internal material the department holds that is not
    published anywhere — the drafter grounds on it like any other source."""
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
    return _doc_out(_store_doc(db, user, title=title, content=text))


@router.delete("/knowledge-base/docs/{doc_id}")
def delete_doc(doc_id: int, db: Session = Depends(get_db),
               user: User = Depends(require(Cap.ADMIN))):
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
                user: User = Depends(require(Cap.ADMIN))):
    """Create an account — e.g. a subject department that uploads documents."""
    valid_roles = {r.value for r in Role}
    if payload.role not in valid_roles:
        raise HTTPException(422, f"Unknown role '{payload.role}'. "
                                 f"Valid: {', '.join(sorted(valid_roles))}.")
    if db.execute(select(User).where(User.username == payload.username)).scalar_one_or_none():
        raise HTTPException(409, f"Username '{payload.username}' already exists.")
    return auth.create_user(db, username=payload.username, password=payload.password,
                            role=payload.role, full_name=payload.full_name,
                            department=payload.department)


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
def refresh_now(db: Session = Depends(get_db), user: User = Depends(require(Cap.ADMIN))):
    """Refresh the public-information knowledge base now (re-crawl + re-ingest +
    reindex). Forced, so it runs even if the auto-refresh flag is off; individual
    sources still require their FOI_INGEST_* flags and stay best-effort."""
    from ..services import kb_refresh
    return kb_refresh.refresh(db, trigger="manual", force=True)


@router.post("/reindex")
def reindex(db: Session = Depends(get_db), user: User = Depends(require(Cap.ADMIN))):
    """Build the semantic chunk index (needs FOI_RETRIEVAL_PROVIDER=semantic)."""
    from ..reindex import reindex as _reindex
    try:
        return _reindex(db)
    except (NotImplementedError, RuntimeError) as exc:
        raise HTTPException(501, str(exc))


@router.post("/ingest/website")
def ingest_website(max_pages: int | None = None, db: Session = Depends(get_db),
                   user: User = Depends(require(Cap.ADMIN))):
    try:
        n = website_crawler.crawl(db, max_pages=max_pages)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return {"ingested": n, "source": "website"}


@router.post("/ingest/published-responses")
def ingest_published(feed_dir: str | None = None, db: Session = Depends(get_db),
                     user: User = Depends(require(Cap.ADMIN))):
    try:
        n = published_responses.ingest(db, feed_dir=feed_dir)
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        raise HTTPException(409, str(exc))
    return {"ingested": n, "source": "published_response"}
