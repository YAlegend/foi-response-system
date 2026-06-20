"""Breach-trend deterioration notifications: alert, dedup, disable, recover."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.database import Base
from app.models import SchemeNotification
from app.seed import _drive_to_close
from app.services import casework
from app.services.notifications import check_and_notify


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


def _closed_late(db, subject, body, days_ago):
    r = casework.create_request(db, requester_name="T", requester_email="t@e.com",
                                subject=subject, body=body,
                                received_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
                                actor="test")
    casework.run_triage(db, r, actor="test")
    casework.run_autodraft(db, r, actor="test")
    _drive_to_close(db, r)        # closed today; deadline already passed -> late
    return r


def _make_traffic_filters_deteriorate(db):
    # Two recent closed-late traffic-filter breaches -> recent half >= 2, prior 0.
    _closed_late(db, "Traffic filter cameras", "Where are the traffic filter cameras?", 35)
    _closed_late(db, "Traffic filter signage", "Which contractor supplied traffic filter signage?", 40)


def test_disabled_without_force(db):
    # Notifications are off by default; a normal run is a no-op.
    res = check_and_notify(db)
    assert res["status"] == "disabled"
    assert db.execute(select(SchemeNotification)).first() is None


def test_alerts_then_dedups(db):
    _make_traffic_filters_deteriorate(db)

    first = check_and_notify(db, force=True)
    assert first["status"] == "ok"
    assert first["alerted"] == ["traffic-filters"]

    row = db.execute(select(SchemeNotification)
                     .where(SchemeNotification.scheme == "traffic-filters")).scalars().one()
    assert row.event == "alerted" and row.recent >= 2 and row.prior == 0
    # Stub provider stores the would-be email body for inspection.
    assert "Traffic filters" in row.detail and "Recent 4 weeks" in row.detail

    # Second run: still deteriorating, but already alerted -> no duplicate email.
    second = check_and_notify(db, force=True)
    assert second["alerted"] == []
    assert len(db.execute(select(SchemeNotification)
                          .where(SchemeNotification.event == "alerted")).scalars().all()) == 1


def test_resolves_when_recovered(db):
    # An active alert exists for a scheme that is not currently deteriorating.
    db.add(SchemeNotification(scheme="zez", label="Zero Emission Zone", event="alerted",
                              recent=3, prior=0))
    db.commit()
    res = check_and_notify(db, force=True)
    assert "zez" in res["resolved"]
    latest = db.execute(select(SchemeNotification).where(SchemeNotification.scheme == "zez")
                        .order_by(SchemeNotification.id.desc())).scalars().first()
    assert latest.event == "resolved"
