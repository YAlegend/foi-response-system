"""Per-department SLA digest: build, recipient routing, send, period dedup."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.config import get_settings
from app.database import Base
from app.models import DepartmentDigest
from app.services import casework
from app.services.digests import (_recipient_map, _recipients_for,
                                   build_department_digests, send_department_digests)


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


class _S:
    digest_recipients = "Highways=highways@x.gov, Environment=env@x.gov"
    notify_recipients = "ig@x.gov"


def test_recipient_routing_with_fallback():
    s, dmap = _S(), _recipient_map(_S())
    assert dmap == {"Highways": "highways@x.gov", "Environment": "env@x.gov"}
    assert _recipients_for("Highways", s, dmap) == ["highways@x.gov"]       # mapped
    assert _recipients_for("Finance", s, dmap) == ["ig@x.gov"]              # central fallback


def _overdue(db, subject, body):
    r = casework.create_request(db, requester_name="T", requester_email="t@e.com",
                                subject=subject, body=body,
                                received_at=datetime.now(timezone.utc) - timedelta(days=60),
                                actor="test")
    casework.run_triage(db, r, actor="test")   # tags owning_department; left open -> overdue
    return r


def test_build_groups_by_owning_department(db):
    _overdue(db, "Pothole on the road", "How many potholes on the highway?")   # -> Highways
    rows = {d["department"]: d for d in build_department_digests(db)}
    assert "Highways" in rows
    hw = rows["Highways"]
    assert hw["open"] == 1 and hw["overdue"] == 1 and hw["breach_rate"] == 100
    assert hw["overdue_cases"] and hw["overdue_cases"][0]["reference"].startswith("FOI/")


def test_disabled_without_force(db):
    res = send_department_digests(db)
    assert res["status"] == "disabled"
    assert db.execute(select(DepartmentDigest)).first() is None


def test_forced_send_records_digest(db):
    _overdue(db, "Pothole on the road", "How many potholes on the highway?")
    res = send_department_digests(db, force=True)
    assert res["status"] == "ok" and "Highways" in res["sent"]
    row = db.execute(select(DepartmentDigest)
                     .where(DepartmentDigest.department == "Highways")).scalars().one()
    assert row.period == res["period"] and row.overdue == 1
    assert "FOI SLA summary for Highways" in row.detail   # stub records the body


def test_dedups_per_period_when_enabled(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "digest_enabled", True)
    _overdue(db, "Pothole on the road", "How many potholes on the highway?")

    first = send_department_digests(db)            # enabled, not forced
    assert "Highways" in first["sent"]
    second = send_department_digests(db)           # same ISO week
    assert "Highways" in second["skipped"] and "Highways" not in second["sent"]
