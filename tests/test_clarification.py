"""Clarification clock-pause and internal-review loop, over HTTP."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
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
    TS = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _override():
        s = TS()
        try:
            yield s
        finally:
            s.close()

    fastapi_app.dependency_overrides[get_db] = _override
    admin = User(username="tester", full_name="Test Admin", role="admin", password_hash="x")
    fastapi_app.dependency_overrides[auth.current_user] = lambda: admin

    s = TS()
    knowledge_base.upsert(s, source="website", title="Waste",
                          content="The council collected 520,000 tonnes of waste, "
                                  "about 50 per cent recycled.")
    s.commit(); s.close()
    yield TestClient(fastapi_app)
    fastapi_app.dependency_overrides.clear()
    engine.dispose()


def _new_case(client, body="1. How much waste was recycled last year?"):
    return client.post("/requests", json={
        "requester_name": "Alex", "requester_email": "alex@example.com",
        "subject": "Waste", "body": body}).json()["id"]


def _drive_to_closed(client, rid):
    client.post(f"/requests/{rid}/triage")
    client.post(f"/requests/{rid}/autodraft")
    if client.get(f"/requests/{rid}").json()["stage"] == "4_department_review":
        client.post(f"/requests/{rid}/sme-update",
                    json={"supplied_text": "Confirmed.", "holding_status": "held"})
    client.post(f"/requests/{rid}/compliance")
    client.post(f"/requests/{rid}/approve", json={"approved": True})
    client.post(f"/requests/{rid}/sign-off", json={"authorised": True})
    client.post(f"/requests/{rid}/dispatch", json={})


# --- Clarification ------------------------------------------------------------

def test_clarification_pauses_and_resumes_clock(client):
    rid = _new_case(client)
    client.post(f"/requests/{rid}/triage")

    r = client.post(f"/requests/{rid}/request-clarification",
                    json={"question": "Which financial year do you mean?"})
    assert r.status_code == 200 and r.json()["stage"] == "awaiting_clarification"
    assert client.get(f"/requests/{rid}/sla").json()["paused"] is True

    r = client.post(f"/requests/{rid}/provide-clarification",
                    json={"clarification_text": "The 2024/25 year, please."})
    assert r.status_code == 200 and r.json()["stage"] == "2_triage"
    assert client.get(f"/requests/{rid}/sla").json()["paused"] is False
    # The clarification is folded into the request body for re-drafting.
    assert "2024/25" in client.get(f"/requests/{rid}").json()["body"]


def test_clarification_only_before_drafting(client):
    rid = _new_case(client)
    client.post(f"/requests/{rid}/triage")
    client.post(f"/requests/{rid}/autodraft")     # now past triage
    r = client.post(f"/requests/{rid}/request-clarification", json={"question": "?"})
    assert r.status_code == 409


# --- Internal review ----------------------------------------------------------

def test_internal_review_uphold_recloses(client):
    rid = _new_case(client)
    _drive_to_closed(client, rid)
    assert client.get(f"/requests/{rid}").json()["stage"] == "closed"

    r = client.post(f"/requests/{rid}/internal-review", json={"reason": "Too little disclosed."})
    assert r.status_code == 200 and r.json()["stage"] == "internal_review"

    r = client.post(f"/requests/{rid}/internal-review/complete",
                    json={"upheld": True, "note": "Original response was correct."})
    assert r.status_code == 200 and r.json()["stage"] == "closed"


def test_internal_review_revise_reopens_for_rework(client):
    rid = _new_case(client)
    _drive_to_closed(client, rid)
    client.post(f"/requests/{rid}/internal-review", json={"reason": "Missing data."})
    r = client.post(f"/requests/{rid}/internal-review/complete", json={"upheld": False})
    assert r.status_code == 200 and r.json()["stage"] == "4_department_review"


def test_internal_review_only_on_closed(client):
    rid = _new_case(client)
    assert client.post(f"/requests/{rid}/internal-review", json={"reason": "x"}).status_code == 409
