"""Knowledge-base store operations used by all ingestion sources."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import KnowledgeDoc


def upsert(db: Session, *, source: str, title: str, content: str,
           url: str | None = None) -> KnowledgeDoc:
    """Insert or update a knowledge doc keyed by (source, url) when url is given,
    otherwise by (source, title)."""
    stmt = select(KnowledgeDoc).where(KnowledgeDoc.source == source)
    stmt = stmt.where(KnowledgeDoc.url == url) if url else stmt.where(KnowledgeDoc.title == title)
    existing = db.execute(stmt).scalars().first()
    if existing:
        if existing.content != content:
            existing.chunks.clear()   # stale embeddings; `reindex` rebuilds them
        existing.title, existing.content = title, content
        db.add(existing)
        return existing
    doc = KnowledgeDoc(source=source, title=title, content=content, url=url)
    db.add(doc)
    return doc


def count(db: Session, source: str | None = None) -> int:
    stmt = select(KnowledgeDoc)
    if source:
        stmt = stmt.where(KnowledgeDoc.source == source)
    return len(db.execute(stmt).scalars().all())
