"""Knowledge-base store operations used by all ingestion sources."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import KnowledgeDoc


def upsert(db: Session, *, source: str, title: str, content: str,
           url: str | None = None, project: str = "",
           status: str = "approved") -> KnowledgeDoc:
    """Insert or update a knowledge doc keyed by (source, url) when url is given,
    otherwise by (source, title).

    ``status`` defaults to "approved": public sources (the council website and
    already-published FOI responses) are public by definition, so they go
    straight into the retrievable corpus. Private department/project uploads are
    inserted as "pending_review" by their caller (see admin._store_doc) and stay
    out of retrieval until a reviewer approves them. ``project`` optionally tags
    the doc to a scheme (e.g. traffic-filters, zez)."""
    stmt = select(KnowledgeDoc).where(KnowledgeDoc.source == source)
    stmt = stmt.where(KnowledgeDoc.url == url) if url else stmt.where(KnowledgeDoc.title == title)
    existing = db.execute(stmt).scalars().first()
    if existing:
        if existing.content != content:
            existing.chunks.clear()   # stale embeddings; `reindex` rebuilds them
        existing.title, existing.content = title, content
        if project:
            existing.project = project
        db.add(existing)
        return existing
    doc = KnowledgeDoc(source=source, title=title, content=content, url=url,
                       project=project, status=status)
    db.add(doc)
    return doc


def count(db: Session, source: str | None = None, status: str | None = None) -> int:
    stmt = select(KnowledgeDoc)
    if source:
        stmt = stmt.where(KnowledgeDoc.source == source)
    if status:
        stmt = stmt.where(KnowledgeDoc.status == status)
    return len(db.execute(stmt).scalars().all())
