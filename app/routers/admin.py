"""Admin / Phase 0 ingestion endpoints (feature-flagged)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import schemas
from ..auth import Cap, require
from ..config import get_settings
from ..database import get_db
from ..ingestion import knowledge_base, published_responses, website_crawler
from ..models import KnowledgeChunk, KnowledgeDoc, KnowledgeRefresh, User

router = APIRouter(prefix="/admin", tags=["admin"])
settings = get_settings()


@router.get("/knowledge-base")
def kb_stats(db: Session = Depends(get_db), user: User = Depends(require(Cap.READ))):
    chunks = db.execute(select(func.count()).select_from(KnowledgeChunk)).scalar_one()
    indexed_docs = db.execute(
        select(func.count(func.distinct(KnowledgeChunk.doc_id)))).scalar_one()
    return {
        "total": knowledge_base.count(db),
        "website": knowledge_base.count(db, "website"),
        "published_responses": knowledge_base.count(db, "published_response"),
        "manual": knowledge_base.count(db, "manual"),
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
        content_chars=len(doc.content or ""), chunks=len(doc.chunks),
    )


@router.get("/knowledge-base/docs", response_model=list[schemas.KnowledgeDocOut])
def list_docs(db: Session = Depends(get_db), user: User = Depends(require(Cap.READ))):
    docs = db.execute(select(KnowledgeDoc).order_by(KnowledgeDoc.id.desc())).scalars().all()
    return [_doc_out(d) for d in docs]


@router.post("/knowledge-base/docs", response_model=schemas.KnowledgeDocOut, status_code=201)
def add_doc(payload: schemas.KnowledgeDocIn, db: Session = Depends(get_db),
            user: User = Depends(require(Cap.ADMIN))):
    """Add a manually-curated knowledge document the drafter can ground on."""
    doc = knowledge_base.upsert(db, source="manual", title=payload.title or "(untitled)",
                                content=payload.content, url=payload.url)
    db.commit()
    db.refresh(doc)
    if settings.retrieval_provider.lower() == "semantic":
        try:
            from ..reindex import index_doc
            index_doc(db, doc)        # make it searchable immediately
            db.refresh(doc)
        except (RuntimeError, NotImplementedError):
            pass                       # added; will be picked up by a full reindex
    return _doc_out(doc)


@router.delete("/knowledge-base/docs/{doc_id}")
def delete_doc(doc_id: int, db: Session = Depends(get_db),
               user: User = Depends(require(Cap.ADMIN))):
    doc = db.get(KnowledgeDoc, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    db.delete(doc)        # cascade removes its chunks
    db.commit()
    return {"deleted": doc_id}


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
