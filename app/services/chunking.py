"""Split a knowledge doc into passages for embedding and semantic retrieval.

Passage-level chunks both rank the doc and become the snippet. Two strategies by
source type:
  - published_response: one passage per Q&A pair (question + its answer), so a
    precedent's answer is retrievable as a unit.
  - website / manual: sentence windows of roughly ``target_chars``, with a small
    overlap so facts spanning a boundary are not lost.
The doc title is prepended to every passage for topical context.
"""
from __future__ import annotations

from ..textutil import is_boilerplate, is_prompt, split_sentences


def _chunk_qa(content: str) -> list[str]:
    """Group sentences into question+answer passages; skip letter scaffolding.

    A new passage begins only when a question follows an answer — so a number
    marker split off as its own segment ("1." then "How many...?") and any other
    consecutive question fragments stay in the same passage.
    """
    passages: list[str] = []
    current: list[str] = []
    started = False
    have_answer = False
    for s in split_sentences(content):
        if is_boilerplate(s):
            continue
        if is_prompt(s):
            if have_answer:                # answer -> new question: close passage
                passages.append(" ".join(current))
                current, have_answer = [], False
            current.append(s)
            started = True
        elif started:
            current.append(s)
            have_answer = True
    if current:
        passages.append(" ".join(current))
    return passages


def _chunk_windows(content: str, target_chars: int = 500, overlap: int = 1) -> list[str]:
    """Group sentences into ~target_chars windows with a small sentence overlap."""
    sents = [s for s in split_sentences(content) if not is_boilerplate(s)]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for s in sents:
        current.append(s)
        size += len(s) + 1
        if size >= target_chars:
            chunks.append(" ".join(current))
            current = current[-overlap:] if overlap else []
            size = sum(len(x) + 1 for x in current)
    tail = " ".join(current).strip()
    if tail and (not chunks or tail != chunks[-1]):
        chunks.append(tail)
    return chunks


def chunk_document(source: str, content: str) -> list[str]:
    """Return the passages for a doc (clean text — no title prefix).

    The caller embeds ``title + passage`` for topical context but stores the bare
    passage, so the passage can be used directly as a drafted answer."""
    passages = _chunk_qa(content) if source == "published_response" else []
    if not passages:                       # no Q&A structure -> sentence windows
        passages = _chunk_windows(content)
    return [p.strip() for p in passages if p.strip()]
