"""Tests for the snippet extractor — the part that turns a matched knowledge
doc into the text that lands in a draft answer."""
from app.services.retrieval import _snippet, _tokens

# A published-response style Q&A document, with letter scaffolding around it.
QA_DOC = (
    "FOI reference: FOI/2025/00231\n"
    "Subject: Pothole reports and repair times\n"
    "Dear Sir/Madam,\n"
    "You asked:\n"
    "1. How many potholes were reported across the county in the last year?\n"
    "Our response: Between March 2024 and February 2025 the council logged "
    "41,300 carriageway defect reports.\n"
    "2. Anything else?\n"
    "Our response: No further information is held.\n"
    "Yours sincerely,\n"
    "Information Governance Unit, Hertfordshire County Council\n"
)


def _snip_for(query: str, doc: str = QA_DOC) -> str:
    terms = _tokens(query) & _tokens(doc)
    return _snippet(doc, terms)


def test_snippet_returns_answer_not_question_or_scaffolding():
    snip = _snip_for("How many potholes were reported across the county last year?")
    # The lexical gap (potholes vs "carriageway defect reports") used to make the
    # sign-off win; question-matching now returns the actual answer.
    assert "41,300" in snip
    assert "?" not in snip                      # not the question
    assert "Hertfordshire County Council" not in snip   # not the sign-off
    assert "Subject:" not in snip and "Our response:" not in snip


def test_snippet_is_word_aligned():
    snip = _snip_for("How many potholes were reported across the county last year?")
    # Never starts or ends mid-word (the old character-window bug, e.g. "END ...").
    assert snip[:1].isupper() or snip[:1].isdigit()
    assert snip.rstrip()[-1] in ".!?"


def test_snippet_falls_back_for_unstructured_text():
    doc = ("Hertfordshire maintains around 3,200 miles of road. "
           "Potholes can be reported online and assessed against criteria.")
    snip = _snip_for("How many miles of road are maintained?", doc)
    assert "3,200 miles" in snip
