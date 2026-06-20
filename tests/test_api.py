"""End-to-end API test driving the full lifecycle through HTTP."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register mappers; import before shadowing `app` name)
from app import auth
from app.database import Base, get_db
from app.ingestion import knowledge_base
from app.main import app as fastapi_app
from app.models import User


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _override():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    fastapi_app.dependency_overrides[get_db] = _override
    # Drive the whole lifecycle as one admin (admin holds every capability).
    admin = User(username="tester", full_name="Test Admin", role="admin", password_hash="x")
    fastapi_app.dependency_overrides[auth.current_user] = lambda: admin

    # seed KB through one session
    s = TestingSession()
    knowledge_base.upsert(s, source="website", title="Waste",
                          content="The council collected 520,000 tonnes of waste, "
                                  "about 50 per cent recycled.")
    s.commit(); s.close()

    yield TestClient(fastapi_app)
    fastapi_app.dependency_overrides.clear()
    engine.dispose()


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_dashboard_shape(client):
    rid = client.post("/requests", json={
        "requester_name": "Alex", "requester_email": "alex@example.com",
        "subject": "Waste", "body": "1. A question?"}).json()["id"]
    client.post(f"/requests/{rid}/triage")
    d = client.get("/dashboard").json()
    assert {"stage_counts", "sla", "totals", "deadlines"} <= d.keys()
    assert d["totals"]["all"] >= 1
    assert "overdue" in d["deadlines"] and "due_soon" in d["deadlines"]
    a = client.get("/analytics").json()
    assert {"by_regime", "by_outcome", "by_department", "intake_by_week", "sla"} <= a.keys()
    assert len(a["intake_by_week"]) == 8


def test_full_lifecycle(client):
    r = client.post("/requests", json={
        "requester_name": "Alex", "requester_email": "alex@example.com",
        "subject": "Waste", "body": "1. How much waste was recycled last year?"})
    assert r.status_code == 201
    rid = r.json()["id"]
    assert r.json()["reference"].startswith("FOI/")

    assert client.post(f"/requests/{rid}/triage").status_code == 200
    draft = client.post(f"/requests/{rid}/autodraft").json()
    assert "routed_to" in draft

    # SLA endpoint
    sla = client.get(f"/requests/{rid}/sla").json()
    assert sla["working_days_remaining"] <= 20

    # If routed to human review, push it through the SME step.
    detail = client.get(f"/requests/{rid}").json()
    if detail["stage"] == "4_department_review":
        client.post(f"/requests/{rid}/sme-update", json={
            "supplied_text": "Figures confirmed.", "holding_status": "held"})

    checks = client.post(f"/requests/{rid}/compliance").json()
    assert "items" in checks

    client.post(f"/requests/{rid}/approve", json={"approved": True})
    client.post(f"/requests/{rid}/sign-off", json={"authorised": True})
    final = client.post(f"/requests/{rid}/dispatch", json={}).json()
    assert final["stage"] == "closed"

    # The audit trail records the authenticated user, not a client-supplied string.
    events = client.get(f"/requests/{rid}").json()["events"]
    assert any(e["actor"] == "tester" for e in events)


def test_kb_admin_add_list_cite_delete(client):
    # Admin curates a manual knowledge document.
    add = client.post("/admin/knowledge-base/docs", json={
        "title": "Looked-after children",
        "content": "The council supported 2,100 looked-after children last year.",
        "url": "https://example.gov.uk/lac"})
    assert add.status_code == 201
    doc = add.json()
    # New: a private upload lands in the review queue, not straight into the corpus.
    assert doc["source"] == "manual" and doc["content_chars"] > 0
    assert doc["status"] == "pending_review"
    doc_id = doc["id"]

    # It appears in the admin listing and in the pending-review queue.
    listed = client.get("/admin/knowledge-base/docs").json()
    assert any(d["id"] == doc_id and d["source"] == "manual" for d in listed)
    assert any(d["id"] == doc_id for d in client.get("/admin/knowledge-base/pending").json())

    # While pending, it is INVISIBLE to retrieval: a matching request is not
    # grounded on the unreviewed figure.
    def _draft_for(body: str) -> dict:
        rid = client.post("/requests", json={
            "requester_name": "Sam", "requester_email": "sam@example.com",
            "subject": "LAC", "body": body}).json()["id"]
        client.post(f"/requests/{rid}/triage")
        client.post(f"/requests/{rid}/autodraft")
        return client.get(f"/requests/{rid}").json()["drafts"][0]

    q = "1. How many looked-after children did the council support?"
    assert "2,100" not in _draft_for(q)["body"]

    # The reviewer approves it; now it grounds a draft and cites the figure.
    appr = client.post(f"/admin/knowledge-base/docs/{doc_id}/approve")
    assert appr.status_code == 200 and appr.json()["status"] == "approved"
    draft = _draft_for(q)
    assert "2,100" in draft["body"]
    assert any(c["title"] == "Looked-after children" for c in draft["citations"])

    # Deleting removes it; a second delete 404s.
    assert client.delete(f"/admin/knowledge-base/docs/{doc_id}").status_code == 200
    assert all(d["id"] != doc_id for d in client.get("/admin/knowledge-base/docs").json())
    assert client.delete(f"/admin/knowledge-base/docs/{doc_id}").status_code == 404


def test_kb_refresh_status_and_manual_trigger(client):
    # Fresh DB: auto-refresh off, never refreshed, therefore stale.
    st = client.get("/admin/knowledge-base/refresh").json()
    assert st["enabled"] is False and st["stale"] is True and st["last"] is None

    # Manual refresh runs but, with ingestion flags off, is recorded as skipped
    # (no network touched) — so the KB stays stale and history grows.
    r = client.post("/admin/knowledge-base/refresh").json()
    assert r["status"] == "skipped" and "skipped" in r["detail"]

    st2 = client.get("/admin/knowledge-base/refresh").json()
    assert st2["stale"] is True
    assert st2["history"] and st2["history"][0]["trigger"] == "manual"


def test_kb_refresh_staleness_window():
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database import Base
    from app.models import KnowledgeRefresh
    from app.services import kb_refresh

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool, future=True)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, future=True)()

    assert kb_refresh.is_stale(db) is True                 # never refreshed
    # Auto-refresh disabled by default -> refresh_if_stale is a no-op.
    assert kb_refresh.refresh_if_stale(db, trigger="pre_draft") is None

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(KnowledgeRefresh(trigger="weekly", status="ok",
                            started_at=now, finished_at=now, website_docs=3))
    db.commit()
    assert kb_refresh.is_stale(db) is False                # just refreshed
    assert kb_refresh.is_stale(db, max_age_days=0) is True  # zero-day window

    old = now - timedelta(days=8)
    db.add(KnowledgeRefresh(trigger="weekly", status="ok",
                            started_at=old, finished_at=old))
    db.commit()
    # last_successful is the most recent (now), so still fresh.
    assert kb_refresh.is_stale(db) is False


def test_kb_upload_and_user_admin(client):
    # Admin uploads a text document; it becomes a citable KB doc.
    files = {"file": ("widgets.txt", b"The council owns 8,800 street widgets in 2025.", "text/plain")}
    r = client.post("/admin/knowledge-base/upload", files=files, data={"title": "Street widgets 2025"})
    assert r.status_code == 201 and r.json()["content_chars"] > 0

    upload_id = r.json()["id"]
    assert r.json()["status"] == "pending_review"

    # Unsupported file types are rejected.
    assert client.post("/admin/knowledge-base/upload",
                       files={"file": ("x.exe", b"MZ\x00", "application/octet-stream")}).status_code == 415

    def _widget_draft() -> str:
        rid = client.post("/requests", json={"requester_name": "Q", "requester_email": "q@e.com",
            "subject": "Widgets", "body": "1. How many street widgets does the council own?"}).json()["id"]
        client.post(f"/requests/{rid}/triage")
        client.post(f"/requests/{rid}/autodraft")
        return client.get(f"/requests/{rid}").json()["drafts"][0]["body"]

    # Pending: not grounded. After review approval: grounds the draft.
    assert "8,800" not in _widget_draft()
    assert client.post(f"/admin/knowledge-base/docs/{upload_id}/approve").status_code == 200
    assert "8,800" in _widget_draft()

    # Admin creates a subject-department account; it appears in the user list.
    r = client.post("/admin/users", json={"username": "childrens", "password": "secret1",
        "role": "department", "full_name": "Children's Services", "department": "Children's Services"})
    assert r.status_code == 201 and r.json()["role"] == "department"
    # Duplicate username and unknown role are rejected.
    assert client.post("/admin/users", json={"username": "childrens", "password": "secret1",
        "role": "department"}).status_code == 409
    assert client.post("/admin/users", json={"username": "bad", "password": "secret1",
        "role": "wizard"}).status_code == 422
    assert any(u["username"] == "childrens" and u["department"] == "Children's Services"
               for u in client.get("/admin/users").json())


def test_department_role_contributes_but_is_not_admin():
    # A department user holds READ + CONTRIBUTE only — can upload, but not
    # administer users, delete docs, or do casework intake.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, future=True)

    def _override():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    fastapi_app.dependency_overrides[get_db] = _override
    dept = User(username="highways", full_name="Highways", role="department",
                department="Highways", password_hash="x")
    fastapi_app.dependency_overrides[auth.current_user] = lambda: dept
    try:
        c = TestClient(fastapi_app)
        up = c.post("/admin/knowledge-base/upload",
                    files={"file": ("note.txt", b"Highways maintains 412 km of cycleway.", "text/plain")})
        assert up.status_code == 201 and up.json()["source"] == "department"
        did = up.json()["id"]
        # ADMIN-only actions are forbidden for a department user.
        assert c.post("/admin/users", json={"username": "z", "password": "secret1",
                                            "role": "department"}).status_code == 403
        assert c.delete(f"/admin/knowledge-base/docs/{did}").status_code == 403
        # No INTAKE capability -> cannot register a case.
        assert c.post("/requests", json={"requester_name": "a", "requester_email": "a@e.com",
            "subject": "s", "body": "1. q?"}).status_code == 403
    finally:
        fastapi_app.dependency_overrides.clear()
        engine.dispose()
