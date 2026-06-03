"""Semantic retrieval tests.

A deterministic fake embedder (no model download) verifies the plumbing:
chunking, cosine ranking across the lexical gap, the relevance floor, per-doc
aggregation, and the Hit contract. numpy is required by the semantic path, so
the whole module skips if it is absent.
"""
import pytest

pytest.importorskip("numpy")

from app.config import get_settings
from app.ingestion import knowledge_base
from app.reindex import reindex
from app.services import embedding, retrieval

# Concept buckets — words that mean the same thing map to the same axis, so the
# fake embeddings bridge the lexical gap (potholes <-> carriageway defects) the
# way a real model would, without any shared tokens.
_CONCEPTS = {
    "road": ["pothole", "potholes", "carriageway", "defect", "defects",
             "highway", "road", "roads", "reported", "reports"],
    "recycle": ["recycling", "recycled", "waste", "tonnes", "household", "compost"],
    "school": ["school", "schools", "admission", "admissions", "pupils", "appeals"],
}


class _FakeEmbedder:
    dim = len(_CONCEPTS)

    def embed(self, texts):
        import numpy as np
        keys = list(_CONCEPTS)
        out = []
        for t in texts:
            tl = t.lower()
            v = np.array([sum(tl.count(w) for w in _CONCEPTS[k]) for k in keys],
                         dtype=float)
            n = np.linalg.norm(v)
            out.append((v / n if n else v).tolist())
        return out

    def embed_one(self, text):
        return self.embed([text])[0]


@pytest.fixture()
def semantic(db, monkeypatch):
    monkeypatch.setattr(embedding, "get_embedder", lambda: _FakeEmbedder())
    monkeypatch.setenv("FOI_RETRIEVAL_PROVIDER", "semantic")
    get_settings.cache_clear()
    yield db
    get_settings.cache_clear()


def _seed(db):
    knowledge_base.upsert(
        db, source="published_response", title="Pothole reports and repair times",
        content=("You asked:\n1. How many defects were logged last year?\n"
                 "Our response: The council logged 41,300 carriageway defect "
                 "reports last year.\n"))
    knowledge_base.upsert(
        db, source="website", title="Recycling and waste",
        content="The council collected 520,000 tonnes of household waste; half was recycled.")
    db.commit()


def test_reindex_builds_chunks(semantic):
    _seed(semantic)
    result = reindex(semantic)
    assert result["docs"] == 2
    assert result["chunks"] >= 2


def test_semantic_matches_by_meaning_not_words(semantic):
    _seed(semantic)
    reindex(semantic)
    # Query shares NO words with the answer ("carriageway defect reports").
    hits = retrieval.retrieve(semantic, "how many potholes were reported", k=5)
    assert hits, "expected a semantic hit despite the lexical gap"
    assert hits[0].title == "Pothole reports and repair times"
    assert "41,300" in hits[0].snippet            # answer extracted, not the question
    assert "?" not in hits[0].snippet
    assert 0.0 <= hits[0].score <= 1.0


def test_irrelevant_doc_dropped_by_floor(semantic):
    _seed(semantic)
    reindex(semantic)
    hits = retrieval.retrieve(semantic, "how many potholes were reported", k=5)
    # The recycling doc is orthogonal -> below the relevance floor -> not returned.
    assert all(h.title != "Recycling and waste" for h in hits)


def test_no_match_returns_no_hits(semantic):
    _seed(semantic)
    reindex(semantic)
    hits = retrieval.retrieve(semantic, "planning permission for a conservatory", k=5)
    assert hits == []        # nothing relevant -> routes to a human, no fabrication
