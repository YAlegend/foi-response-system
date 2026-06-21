"""The /healthz readiness probe."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.config import get_settings
from app.database import Base, get_db
from app.main import app as fastapi_app


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    TS = sessionmaker(bind=engine, autoflush=False, future=True)

    def _ov():
        s = TS()
        try:
            yield s
        finally:
            s.close()

    fastapi_app.dependency_overrides[get_db] = _ov
    try:
        yield TestClient(fastapi_app)
    finally:
        fastapi_app.dependency_overrides.clear()


def test_healthz_ok_with_defaults(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["components"]["database"]["status"] == "ok"
    assert body["components"]["retrieval"]["provider"] == "keyword"
    assert body["components"]["llm"]["status"] == "stub"


def test_healthz_reports_ollama_unreachable(client, monkeypatch):
    # Point the LLM at an unreachable Ollama: the probe must degrade, not crash,
    # and the app stays servable (HTTP 200).
    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "ollama")
    monkeypatch.setattr(s, "ollama_base_url", "http://127.0.0.1:1")
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["components"]["llm"]["status"] == "unreachable"
    assert body["status"] == "degraded"
