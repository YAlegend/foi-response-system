import pytest

from app.enums import Stage
from app.services import casework
from app.workflow import TransitionError, can_transition


def test_legal_and_illegal_transitions():
    assert can_transition(Stage.INTAKE, Stage.TRIAGE)
    assert can_transition(Stage.RETRIEVAL_DRAFT, Stage.DEPARTMENT_REVIEW)
    assert not can_transition(Stage.INTAKE, Stage.DISPATCH)
    assert not can_transition(Stage.CLOSED, Stage.INTAKE)


def test_low_confidence_routes_to_human_review(db):
    # No knowledge base -> draft cannot be grounded -> route to department review.
    req = casework.create_request(
        db, requester_name="Jo", requester_email="jo@example.com",
        subject="Obscure topic", body="1. Please provide the unknowable widget count?")
    casework.run_triage(db, req)
    result = casework.run_autodraft(db, req)
    assert result["routed_to"] == Stage.DEPARTMENT_REVIEW.value
    assert req.stage == Stage.DEPARTMENT_REVIEW.value


def test_full_happy_path_with_grounding(seeded_kb):
    db = seeded_kb
    req = casework.create_request(
        db, requester_name="Sam", requester_email="sam@example.com",
        subject="Waste and roads",
        body=("1. How much household waste was collected and what percentage was recycled?\n"
              "2. How many miles of road does the council maintain?"))
    casework.run_triage(db, req)
    draft_res = casework.run_autodraft(db, req)
    assert draft_res["confidence"] > 0

    # If grounded enough it goes to the gate; if not, simulate the SME path.
    if req.stage == Stage.DEPARTMENT_REVIEW.value:
        casework.sme_update(db, req, officer="dept.expert",
                            supplied_text="Confirmed figures attached.",
                            holding_status="held")
    assert req.stage == Stage.COMPLIANCE_GATE.value

    checks = casework.run_compliance(db, req)
    assert "items" in checks

    casework.approve(db, req, manager="dept.manager", approved=True)
    assert req.stage == Stage.SIGN_OFF.value

    casework.sign_off(db, req, officer="legal.ig", authorised=True)
    casework.dispatch(db, req, foi_officer="foi.team")
    assert req.stage == Stage.CLOSED.value
    assert req.outcome != "open"


def test_cannot_dispatch_before_signoff(seeded_kb):
    db = seeded_kb
    req = casework.create_request(
        db, requester_name="Lee", requester_email="lee@example.com",
        subject="Waste", body="1. How much waste was recycled?")
    casework.run_triage(db, req)
    casework.run_autodraft(db, req)
    with pytest.raises(TransitionError):
        casework.dispatch(db, req, foi_officer="foi.team")
