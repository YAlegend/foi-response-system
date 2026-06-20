"""Stage 3 (part 1) — retrieval over the Phase 0 knowledge base.

A simple, dependency-free keyword/overlap ranker stands in for a vector index so
the system runs offline. The interface (`retrieve`) is what a production RAG
service would expose, so swapping in embeddings later is a drop-in change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import KnowledgeChunk, KnowledgeDoc
from ..textutil import LABEL, is_boilerplate, is_prompt, split_sentences

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "is", "are",
    "please", "provide", "information", "request", "would", "like", "all", "any",
    "how", "many", "what", "which", "your", "you", "we", "council", "data",
}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}


@dataclass
class Hit:
    doc_id: int
    title: str
    source: str
    url: str | None
    score: float
    snippet: str


def retrieve(db: Session, query: str, k: int = 5,
             sources: list[str] | None = None,
             project: str | None = None) -> list[Hit]:
    """Return the top-k knowledge docs for a query.

    Dispatches on the configured provider: the offline keyword ranker (default)
    or local semantic search. Both return the same ``Hit`` contract, so callers
    (drafting) are unchanged.

    Only documents with status "approved" are ever returned: a private
    department/project upload that has not been reviewed is invisible here, so it
    can never ground a draft until a reviewer approves it. ``project`` optionally
    restricts retrieval to one scheme's corpus.
    """
    if get_settings().retrieval_provider.lower() == "semantic":
        return _retrieve_semantic(db, query, k, sources, project)
    return _retrieve_keyword(db, query, k, sources, project)


def _approved(stmt, sources: list[str] | None, project: str | None):
    """Apply the retrieval-time filters shared by both rankers: approved-only,
    optional source set, optional project scope."""
    stmt = stmt.where(KnowledgeDoc.status == "approved")
    if sources:
        stmt = stmt.where(KnowledgeDoc.source.in_(sources))
    if project:
        stmt = stmt.where(KnowledgeDoc.project == project)
    return stmt


def _retrieve_keyword(db: Session, query: str, k: int,
                      sources: list[str] | None,
                      project: str | None = None) -> list[Hit]:
    """Rank docs by token overlap with the query (dependency-free, offline)."""
    q_tokens = _tokens(query)
    if not q_tokens:
        return []

    stmt = _approved(select(KnowledgeDoc), sources, project)
    docs = db.execute(stmt).scalars().all()

    scored: list[Hit] = []
    for d in docs:
        d_tokens = _tokens(f"{d.title} {d.content}")
        if not d_tokens:
            continue
        overlap = q_tokens & d_tokens
        if not overlap:
            continue
        score = len(overlap) / len(q_tokens)
        scored.append(Hit(
            doc_id=d.id, title=d.title, source=d.source, url=d.url,
            score=round(score, 3), snippet=_snippet(d.content, overlap),
        ))
    scored.sort(key=lambda h: h.score, reverse=True)
    return scored[:k]


def _passage_answer(text: str, max_chars: int = 400) -> str:
    """Reduce a retrieved passage to its answer: drop question/prompt and
    scaffolding lines, strip answer labels. Used for semantic hits, where the
    matched passage is already the relevant unit (so no keyword scoring needed)."""
    kept = [LABEL.sub("", s) for s in split_sentences(text)
            if not is_prompt(s) and not is_boilerplate(s)]
    out = " ".join(kept).strip() or LABEL.sub("", text).strip()
    return out[:max_chars].strip()


def _retrieve_semantic(db: Session, query: str, k: int,
                       sources: list[str] | None,
                       project: str | None = None) -> list[Hit]:
    """Rank docs by embedding cosine similarity over their passages.

    Bridges the lexical gap (e.g. "potholes reported" vs "carriageway defect
    reports"). Returns the best passage per doc; hits below the relevance floor
    are dropped, so "no good match" yields no hit and the case routes to a human.
    """
    import numpy as np  # lazy — only needed for semantic mode

    from . import embedding

    settings = get_settings()
    qvec = np.asarray(embedding.get_embedder().embed_one(query), dtype=np.float32)
    q_norm = float(np.linalg.norm(qvec)) or 1.0

    stmt = (select(KnowledgeChunk, KnowledgeDoc)
            .join(KnowledgeDoc, KnowledgeChunk.doc_id == KnowledgeDoc.id))
    stmt = _approved(stmt, sources, project)

    best: dict[int, tuple[float, KnowledgeChunk, KnowledgeDoc]] = {}
    for chunk, doc in db.execute(stmt).all():
        vec = embedding.from_bytes(chunk.embedding)
        if vec.size == 0:
            continue
        sim = float(np.dot(qvec, vec) / (q_norm * (float(np.linalg.norm(vec)) or 1.0)))
        if doc.id not in best or sim > best[doc.id][0]:
            best[doc.id] = (sim, chunk, doc)

    hits: list[Hit] = []
    for sim, chunk, doc in best.values():
        if sim < settings.semantic_relevance_floor:
            continue
        hits.append(Hit(
            doc_id=doc.id, title=doc.title, source=doc.source, url=doc.url,
            score=round(max(0.0, min(1.0, sim)), 3),
            snippet=_passage_answer(chunk.text),
        ))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:k]


def _is_skippable(s: str) -> bool:
    return is_prompt(s) or is_boilerplate(s)


def _gather(sents: list[str], start: int, max_chars: int) -> str:
    """Join answer statements from ``start`` until the next prompt/boilerplate."""
    out = ""
    for s in sents[start:]:
        if _is_skippable(s):
            if out:
                break          # next Q&A block / closing — stop here
            continue
        if out and len(out) + len(s) + 1 > max_chars:
            break
        out = f"{out} {LABEL.sub('', s)}".strip()
    return LABEL.sub("", out)[:max_chars].strip()


def _snippet(content: str, terms: set[str], max_chars: int = 400) -> str:
    """Return a coherent, sentence-aligned extract that answers the query.

    Published responses are Q&A documents, so we first match the query to the
    precedent's *question* (questions share the requester's vocabulary even when
    the answer does not — "potholes reported" vs "carriageway defect reports")
    and return the answer that follows it. For unstructured pages we fall back
    to the best-matching statement, skipping letter scaffolding. Either way the
    extract is sentence-aligned, so it never starts or ends mid-word.
    """
    sents = split_sentences(content)
    if not sents:
        return content[:max_chars].strip()

    # 1) Q&A: best-matching prompt -> the answer beneath it.
    best_i, best_score = None, 0
    for i, s in enumerate(sents):
        if not is_prompt(s):
            continue
        score = sum(1 for t in terms if t in s.lower())
        if score > best_score:
            best_score, best_i = score, i
    if best_i is not None:
        answer = _gather(sents, best_i + 1, max_chars)
        if answer:
            return answer

    # 2) Fallback: best substantive statement + following statements.
    best_i, best_score = None, 0
    for i, s in enumerate(sents):
        if _is_skippable(s):
            continue
        score = sum(1 for t in terms if t in s.lower())
        if score > best_score:
            best_score, best_i = score, i
    if best_i is None:
        return LABEL.sub("", sents[0])[:max_chars].strip()
    return _gather(sents, best_i, max_chars)
