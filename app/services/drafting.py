"""Stage 3 (part 2) — auto-draft assembly.

Splits the request into individual questions, retrieves grounding for each from
the knowledge base, and renders a response in the council's house style via the
template. Returns the draft text plus a confidence score that drives the Stage 3
routing decision (auto-proceed vs. route to a human).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..enums import HoldingStatus
from ..models import FOIRequest
from ..templates.response_template import QA, ResponseContext, render
from ..textutil import split_sentences
from . import retrieval

_NUMBERED = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+")
# Lines that are email scaffolding, not part of the request itself.
_GREETING = re.compile(
    r"(?i)^(dear\b|hi\b|hello\b|to whom\b|to the\b|good (morning|afternoon|evening))")
_SIGNOFF = re.compile(
    r"(?i)^(regards|kind regards|best wishes|best regards|best|many thanks|thanks|"
    r"thank you|yours (sincerely|faithfully)|sincerely|cheers)\b")
# Phrases that mark a sentence as an actual information request.
_REQUEST_CUE = re.compile(
    r"(?i)\b(please (provide|send|supply|tell|confirm|advise|let me know)|"
    r"i would like|we would like|i am requesting|we are requesting|i request|we request|"
    r"could you|can you|how (many|much|long|often)|what (is|are|was|were)|"
    r"details of|a copy of|copies of|information (on|about|regarding|relating))\b")


def _strip_boilerplate(sentences: list[str]) -> list[str]:
    """Drop greeting sentences and everything from the sign-off onwards.

    Operates on sentences (not lines) so it also handles emails where the
    greeting and request share a line ("Hello. Could you tell me ...?")."""
    kept: list[str] = []
    for s in sentences:
        if _SIGNOFF.match(s):
            break                       # signature block starts here
        if _GREETING.match(s):
            continue
        kept.append(s)
    return kept


def split_questions(body: str) -> list[str]:
    """Extract the discrete information requests from a request body.

    Prefers an explicit numbered/bulleted list; otherwise reads the prose,
    stripping email greetings/sign-offs and keeping only sentences that are
    questions or carry a request cue ("please provide", "we would like", ...).
    """
    numbered = [_NUMBERED.sub("", l.strip()).strip()
                for l in body.splitlines() if _NUMBERED.match(l.strip())]
    if numbered:
        return [q for q in numbered if q]

    kept = _strip_boilerplate(split_sentences(body))
    picked = [s for s in kept
              if (s.endswith("?") or _REQUEST_CUE.search(s)) and len(s.split()) >= 3]
    if picked:
        return picked
    return [" ".join(kept).strip() or body.strip()]


@dataclass
class DraftResult:
    body: str
    confidence: float
    holding_status: str
    citations: list[dict]


def build_draft(db: Session, request: FOIRequest) -> DraftResult:
    questions = split_questions(request.body)
    qas: list[QA] = []
    citations: list[dict] = []
    scores: list[float] = []

    for q in questions:
        hits = retrieval.retrieve(db, q, k=1)
        if hits:
            top = hits[0]
            scores.append(top.score)
            qas.append(QA(question=q, answer=top.snippet, citation=top.title))
            citations.append({"question": q, "title": top.title,
                              "url": top.url, "score": top.score})
        else:
            scores.append(0.0)
            qas.append(QA(
                question=q,
                answer="[Information not located in the knowledge base — refer to "
                       "subject-matter expert.]",
            ))

    confidence = round(sum(scores) / len(scores), 3) if scores else 0.0
    if confidence == 0:
        holding = HoldingStatus.UNKNOWN.value
    elif all(s > 0 for s in scores):
        holding = HoldingStatus.HELD.value
    else:
        holding = HoldingStatus.PARTIAL.value

    ctx = ResponseContext(
        reference=request.reference,
        requester_name=request.requester_name,
        received=request.received_at.date(),
        regime=request.regime,
        holding_status=holding,
        qas=qas,
    )
    return DraftResult(body=render(ctx), confidence=confidence,
                      holding_status=holding, citations=citations)
