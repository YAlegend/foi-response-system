"""Small, dependency-free text helpers shared by retrieval and drafting."""
from __future__ import annotations

import re

# Split on sentence terminators, but only when the next sentence plausibly
# starts (capital letter, digit, opening bracket, or a £/$ amount). This avoids
# breaking on decimals ("£38.6 million") and most abbreviations.
_SENT_BOUNDARY = re.compile(r"(?<=[.?!])\s+(?=[A-Z(£$0-9])")


def split_sentences(text: str) -> list[str]:
    """Return the sentences in *text*, whitespace-normalised.

    Line breaks are treated as boundaries too, so structured documents (letters
    with one-line headers like "Our ref:" / "Subject:") segment cleanly instead
    of collapsing a whole header block into one giant pseudo-sentence.
    """
    out: list[str] = []
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        out.extend(s.strip() for s in _SENT_BOUNDARY.split(line) if s.strip())
    return out


# --- Q&A / letter structure detectors -----------------------------------------
# Shared by the snippet extractor (retrieval) and the chunker (chunking) so both
# segment published-response letters the same way.

# A leading answer label to strip from extracted text.
LABEL = re.compile(r"(?i)^\s*(?:our response:|you asked:?|response:)\s*")
# A numbered/bulleted request item.
_NUM_Q = re.compile(r"^\s*\d+[.)]")
# Letter scaffolding that is term-dense but never the answer (header/footer).
BOILERPLATE = re.compile(
    r"(?i)^(subject|title|re|foi reference|our ref|date|dear|sincerely|"
    r"request for information under|thank you for your request|i can confirm|"
    r"having made appropriate enquiries|re-use of information|"
    r"information released under|if you are dissatisfied|yours (sincerely|faithfully)|"
    r"information governance unit|you asked|please see our response)\b")


def is_prompt(s: str) -> bool:
    """True if the segment is a question or a numbered/bulleted request item."""
    return s.endswith("?") or bool(_NUM_Q.match(s))


def is_boilerplate(s: str) -> bool:
    """True if the segment is letter scaffolding (header/footer/salutation)."""
    return bool(BOILERPLATE.match(s))
