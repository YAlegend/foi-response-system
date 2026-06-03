"""Shared test fixtures — an isolated in-memory database per test."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    import app.models  # noqa: F401  (register mappers)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def seeded_kb(db):
    from app.ingestion import knowledge_base
    knowledge_base.upsert(db, source="website", title="Recycling and waste",
                          content="The council collected 520,000 tonnes of household "
                                  "waste last year, about 50 per cent recycled.")
    knowledge_base.upsert(db, source="website", title="Highways maintenance",
                          content="The council maintains around 3,200 miles of road "
                                  "and repairs potholes against intervention criteria.")
    db.commit()
    # Semantic retrieval reads the chunk index, not the doc text — build it so the
    # fixture grounds drafts the same way under either retrieval provider.
    from app.config import get_settings
    if get_settings().retrieval_provider.lower() == "semantic":
        from app.reindex import reindex
        reindex(db)
    return db
