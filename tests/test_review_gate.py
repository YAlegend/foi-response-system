"""The review gate and project scoping at the retrieval layer.

Locks the core requirement: a private department/project upload is invisible to
retrieval until it is approved, and retrieval can be scoped to one scheme.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register mappers)
from app.database import Base
from app.ingestion import knowledge_base
from app.services import retrieval


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, future=True)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def test_pending_upload_is_invisible_until_approved(db):
    # Public website page: auto-approved, immediately retrievable.
    knowledge_base.upsert(db, source="website", title="Traffic filters",
                          content="Six ANPR cameras enforce the Oxford traffic filters.",
                          project="traffic-filters", status="approved")
    # Private department upload: pending review.
    pending = knowledge_base.upsert(
        db, source="department", title="Internal go-live note",
        content="The cameras switch on in a phased sequence starting in March.",
        project="traffic-filters", status="pending_review")
    db.commit()

    def _ids(query: str) -> set[int]:
        return {h.doc_id for h in retrieval.retrieve(db, query, k=10)}

    q = "phased sequence the cameras switch on starting in March"
    # The approved public page is found...
    assert retrieval.retrieve(db, "how many cameras enforce the traffic filters")
    # ...but the pending upload itself never appears in results, even on its own
    # wording (other approved docs may still match shared words like "cameras").
    assert pending.id not in _ids(q)

    # Approve it -> now retrievable.
    pending.status = "approved"
    db.commit()
    assert pending.id in _ids(q)

    # Reject path: a rejected doc is never returned.
    pending.status = "rejected"
    db.commit()
    assert pending.id not in _ids(q)


def test_retrieval_can_be_scoped_to_one_project(db):
    knowledge_base.upsert(db, source="published_response", title="ZEZ PCNs",
                          content="The Zero Emission Zone issued 18,640 penalty notices.",
                          project="zez", status="approved")
    knowledge_base.upsert(db, source="published_response", title="LTN fines",
                          content="The low traffic neighbourhood filters issued penalty notices.",
                          project="ltn", status="approved")
    db.commit()

    # Scoped to zez: only the ZEZ precedent is eligible.
    hits = retrieval.retrieve(db, "how many penalty notices were issued", project="zez")
    assert hits and all(h.title == "ZEZ PCNs" for h in hits)
    # A scheme with no matching docs returns nothing.
    assert retrieval.retrieve(db, "penalty notices", project="traffic-filters") == []
