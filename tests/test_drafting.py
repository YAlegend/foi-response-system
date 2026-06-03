from datetime import date

from app.enums import HoldingStatus, Regime
from app.services.drafting import split_questions
from app.templates.response_template import QA, ResponseContext, render


def test_split_questions_numbered():
    body = "1. First question?\n2. Second question?\n"
    qs = split_questions(body)
    assert len(qs) == 2
    assert qs[0] == "First question?"


def test_split_questions_prose_strips_boilerplate():
    body = (
        "Dear FOI team,\n\n"
        "Please treat this as a request under the FOIA. We would like the total "
        "annual spend on home-to-school transport for SEND pupils in 2024/25.\n\n"
        "Best,\nNewsdesk, The Hertford Mercury"
    )
    qs = split_questions(body)
    # The greeting and sign-off are dropped; the request sentence is kept.
    assert any("we would like the total annual spend" in q.lower() for q in qs)
    joined = " ".join(qs).lower()
    assert "dear" not in joined and "newsdesk" not in joined


def test_split_questions_prose_keeps_only_request_sentences():
    body = "Hello. I run a local blog. Could you tell me how many libraries there are?"
    qs = split_questions(body)
    assert len(qs) == 1
    assert qs[0].startswith("Could you tell me")


def test_rendered_letter_contains_house_style_elements():
    ctx = ResponseContext(
        reference="FOI/2026/00001",
        requester_name="Alex Taylor",
        received=date(2026, 6, 1),
        regime=Regime.FOIA.value,
        holding_status=HoldingStatus.HELD.value,
        qas=[QA(question="How many roads?", answer="3,200 miles.", citation="Highways")],
    )
    letter = render(ctx)
    # Statutory and house-style anchors that compliance checks for.
    assert "Freedom of Information Act 2000" in letter
    assert "does hold the information" in letter
    assert "Re-use of information" in letter
    assert "internal review" in letter
    assert "Information Commissioner" in letter
    assert "County Hall, Pegs Lane, Hertford, SG13 8DQ" in letter
    assert "FOI/2026/00001" in letter


def test_not_held_letter():
    ctx = ResponseContext(
        reference="FOI/2026/00002", requester_name="", received=date(2026, 6, 1),
        regime=Regime.FOIA.value, holding_status=HoldingStatus.NOT_HELD.value)
    letter = render(ctx)
    assert "does not hold" in letter
    assert "Dear Sir/Madam," in letter
