"""Per-scheme SLA aggregation in the analytics endpoint."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register mappers)
from app.database import Base
from app.routers.analytics import analytics
from app.routers.dashboard import dashboard
from app.seed import _drive_to_close
from app.services import casework


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


def _ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _case(db, subject, body, days_ago):
    return casework.create_request(db, requester_name="T", requester_email="t@e.com",
                                   subject=subject, body=body,
                                   received_at=_ago(days_ago), actor="test")


def test_sla_by_scheme(db):
    # Traffic filters: one open & overdue (received well over 20 working days ago).
    casework.run_triage(db, _case(db, "Traffic filter cameras",
                                  "Where are the traffic filter cameras?", 60), actor="test")
    # ZEZ: one closed late (received long ago, closed now).
    z = _case(db, "ZEZ revenue", "Zero Emission Zone revenue last year?", 60)
    casework.run_triage(db, z, actor="test"); casework.run_autodraft(db, z, actor="test")
    _drive_to_close(db, z)
    # LTN: one closed on time (received recently, closed now).
    l = _case(db, "LTN map", "Where are the low traffic neighbourhood filters?", 1)
    casework.run_triage(db, l, actor="test"); casework.run_autodraft(db, l, actor="test")
    _drive_to_close(db, l)

    rows = analytics(db=db, user=None)["sla_by_scheme"]
    by = {r["key"]: r for r in rows}

    assert by["traffic-filters"]["overdue"] == 1
    assert by["traffic-filters"]["breach_rate"] == 100      # 1 overdue of 1
    assert by["traffic-filters"]["on_time_pct"] is None     # nothing closed yet

    assert by["zez"]["closed"] == 1 and by["zez"]["breached"] == 1
    assert by["zez"]["breach_rate"] == 100 and by["zez"]["on_time_pct"] == 0

    assert by["ltn"]["closed"] == 1 and by["ltn"]["breached"] == 0
    assert by["ltn"]["breach_rate"] == 0 and by["ltn"]["on_time_pct"] == 100


def test_overdue_by_scheme_alert(db):
    # An overdue traffic-filters case (open, long past deadline)...
    casework.run_triage(db, _case(db, "Traffic filter cameras",
                                  "Where are the traffic filter cameras?", 60), actor="test")
    # ...and a recent LTN case that is comfortably on track.
    casework.run_triage(db, _case(db, "LTN map",
                                  "Where are the low traffic neighbourhood filters?", 1), actor="test")
    obs = {r["key"]: r["count"] for r in dashboard(db=db, user=None)["overdue_by_scheme"]}
    assert obs.get("traffic-filters") == 1
    assert "ltn" not in obs            # the recent case is not overdue


def test_breach_trend_sparkline(db):
    # A closed-late traffic-filters case: its deadline falls inside the 8-week window.
    z = _case(db, "Traffic filter cost", "What has the traffic filter scheme cost so far?", 45)
    casework.run_triage(db, z, actor="test"); casework.run_autodraft(db, z, actor="test")
    _drive_to_close(db, z)

    res = analytics(db=db, user=None)
    assert len(res["trend_weeks"]) == 8
    tf = next(r for r in res["sla_by_scheme"] if r["key"] == "traffic-filters")
    assert len(tf["trend"]) == 8
    # Every breach in the window is bucketed into exactly one week.
    assert sum(tf["trend"]) == tf["breached"] == 1
