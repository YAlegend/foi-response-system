"""Authentication and role-based access control, exercised over real HTTP
(login -> session cookie -> gated endpoints). No dependency overrides for auth."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app import auth
from app.database import Base, get_db
from app.enums import Role
from app.main import app as fastapi_app


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

    s = TestingSession()
    auth.create_user(s, username="cw", password="pw", role=Role.CASEWORKER.value)
    auth.create_user(s, username="mgr", password="pw", role=Role.MANAGER.value)
    s.close()

    yield TestClient(fastapi_app)
    fastapi_app.dependency_overrides.clear()
    engine.dispose()


def _login(client, username, password="pw"):
    return client.post("/auth/login", json={"username": username, "password": password})


def test_unauthenticated_is_rejected(client):
    assert client.get("/requests").status_code == 401


def test_demo_mode_resolves_anonymous_to_demo_user(client, monkeypatch):
    """With demo_mode on, requests without a session are treated as the demo user
    so the public demo opens with no login."""
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "demo_mode", True)
    monkeypatch.setattr(s, "demo_username", "cw")
    me = client.get("/auth/me")          # no cookie / never logged in
    assert me.status_code == 200
    body = me.json()
    assert body["username"] == "cw" and body["demo"] is True
    # A gated endpoint is now reachable anonymously (not a 401).
    assert client.get("/requests").status_code != 401


def test_bad_credentials(client):
    assert _login(client, "cw", "wrong").status_code == 401


def test_login_sets_session_and_me(client):
    r = _login(client, "cw")
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "caseworker" and "intake" in body["capabilities"]
    assert client.get("/auth/me").json()["username"] == "cw"


def test_caseworker_cannot_approve(client):
    _login(client, "cw")
    rid = client.post("/requests", json={
        "requester_name": "A", "requester_email": "a@example.com",
        "subject": "X", "body": "1. A question?"}).json()["id"]
    # Caseworker lacks the approve capability -> 403 (gate runs before stage checks).
    assert client.post(f"/requests/{rid}/approve", json={"approved": True}).status_code == 403


def test_manager_passes_role_gate(client):
    # Manager has approve; the action fails only on workflow state (409), not on role (403).
    _login(client, "cw")
    rid = client.post("/requests", json={
        "requester_name": "A", "requester_email": "a@example.com",
        "subject": "X", "body": "1. A question?"}).json()["id"]
    _login(client, "mgr")
    assert client.post(f"/requests/{rid}/approve", json={"approved": True}).status_code == 409


def test_audit_records_logged_in_user(client):
    _login(client, "cw")
    rid = client.post("/requests", json={
        "requester_name": "A", "requester_email": "a@example.com",
        "subject": "X", "body": "1. A question?"}).json()["id"]
    events = client.get(f"/requests/{rid}").json()["events"]
    assert events[0]["actor"] == "cw"


def test_logout_clears_session(client):
    _login(client, "cw")
    assert client.get("/auth/me").status_code == 200
    client.post("/auth/logout")
    assert client.get("/auth/me").status_code == 401
