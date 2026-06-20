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

from ..config import get_settings
from ..enums import HoldingStatus
from ..models import FOIRequest
from ..templates.response_template import QA, ResponseContext, render
from ..textutil import split_sentences
from . import llm, retrieval

# Returned when retrieval finds nothing, or the model refuses (cite-or-refuse).
_NOT_LOCATED = ("[Information not located in the knowledge base — refer to "
                "subject-matter expert.]")

# Cite-or-refuse: the model answers ONLY from the retrieved council material and
# refuses rather than guessing — the same governance as the Nagorik assistant.
_LLM_SYSTEM = (
    "You are an information officer at {council} drafting the answer to ONE "
    "question within a Freedom of Information response. Answer ONLY from the "
    "numbered SOURCES provided — council website pages and previously published "
    "FOI responses. Quote any figures exactly as written. If the sources do not "
    "contain the answer, reply with exactly the token NOT_FOUND and nothing else. "
    "Never invent facts. Write two to four sentences of plain British English "
    "suitable for an official letter."
)


def _grounded_prompt(question: str, hits: list, char_cap: int) -> str:
    """Build the user prompt: the question plus numbered source passages, capped
    in total length so a long page cannot blow the model's context budget."""
    blocks: list[str] = []
    used = 0
    for i, h in enumerate(hits, 1):
        snippet = (h.snippet or "").strip()
        if used + len(snippet) > char_cap:
            snippet = snippet[: max(0, char_cap - used)]
        if not snippet:
            break
        used += len(snippet)
        blocks.append(f"[{i}] {h.title}\n{snippet}")
    sources = "\n\n".join(blocks)
    return f"QUESTION:\n{question}\n\nSOURCES:\n{sources}\n\nANSWER:"


def _llm_answer(question: str, hits: list) -> str:
    """Synthesise a grounded answer from the retrieved sources via the configured
    model. Falls back to the top passage on any model error so a draft is never
    hard-blocked by an unreachable LLM."""
    s = get_settings()
    try:
        out = llm.get_llm().complete(
            _LLM_SYSTEM.format(council=s.council_name),
            _grounded_prompt(question, hits, s.llm_source_char_cap),
        ).strip()
    except Exception:
        return hits[0].snippet if hits else _NOT_LOCATED
    if not out or out.upper().startswith("NOT_FOUND"):
        return _NOT_LOCATED
    return out

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

    # With a model configured, pull several sources per question and let it
    # synthesise across them (website + published precedents); otherwise the
    # offline path grounds on the single best passage.
    use_llm = llm.is_enabled()
    k = 4 if use_llm else 1
    for q in questions:
        hits = retrieval.retrieve(db, q, k=k)
        if hits:
            top = hits[0]
            scores.append(top.score)
            answer = _llm_answer(q, hits) if use_llm else top.snippet
            qas.append(QA(question=q, answer=answer, citation=top.title))
            citations.append({"question": q, "title": top.title,
                              "url": top.url, "score": top.score,
                              "sources_considered": len(hits)})
        else:
            scores.append(0.0)
            qas.append(QA(question=q, answer=_NOT_LOCATED))

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
