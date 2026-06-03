"""(Re)build the semantic chunk index for the knowledge base.

Run with:  FOI_RETRIEVAL_PROVIDER=semantic python -m app.reindex

Chunks every knowledge doc into passages, embeds them with the local model, and
stores the vectors in `knowledge_chunks`. Ingestion (crawl/feed) stays text-only;
this is the one batched pass that builds the vectors. Safe to re-run — it rebuilds
the index from scratch each time. Needs the optional deps (fastembed, numpy).
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .database import SessionLocal, init_db
from .models import KnowledgeChunk, KnowledgeDoc
from .services import chunking, embedding


def reindex(db: Session) -> dict:
    """Rebuild all chunk embeddings. Returns counts of docs and chunks indexed."""
    docs = db.execute(select(KnowledgeDoc)).scalars().all()

    passages: list[str] = []               # stored text (clean passage)
    embed_inputs: list[str] = []           # title + passage, for topical context
    where: list[tuple[int, int]] = []      # (doc_id, ordinal) parallel to passages
    for doc in docs:
        prefix = f"{doc.title.strip()} — " if doc.title and doc.title.strip() else ""
        for ordinal, text in enumerate(chunking.chunk_document(doc.source, doc.content)):
            passages.append(text)
            embed_inputs.append(f"{prefix}{text}")
            where.append((doc.id, ordinal))

    db.execute(delete(KnowledgeChunk))     # rebuild from scratch
    if passages:
        vectors = embedding.get_embedder().embed(embed_inputs)
        for (doc_id, ordinal), text, vec in zip(where, passages, vectors):
            db.add(KnowledgeChunk(doc_id=doc_id, ordinal=ordinal, text=text,
                                  embedding=embedding.to_bytes(vec)))
    db.commit()
    return {"docs": len(docs), "chunks": len(passages)}


def index_doc(db: Session, doc) -> int:
    """(Re)build the chunk embeddings for a single doc. Used when a manual doc is
    added in semantic mode so it is searchable immediately. Returns chunk count."""
    db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc.id))
    prefix = f"{doc.title.strip()} — " if doc.title and doc.title.strip() else ""
    passages = chunking.chunk_document(doc.source, doc.content)
    if passages:
        vectors = embedding.get_embedder().embed([prefix + p for p in passages])
        for ordinal, (text, vec) in enumerate(zip(passages, vectors)):
            db.add(KnowledgeChunk(doc_id=doc.id, ordinal=ordinal, text=text,
                                  embedding=embedding.to_bytes(vec)))
    db.commit()
    return len(passages)


def run() -> None:
    init_db()
    db = SessionLocal()
    try:
        result = reindex(db)
        print(f"Reindexed {result['chunks']} chunks across {result['docs']} docs.")
    finally:
        db.close()


if __name__ == "__main__":
    run()
