"""Stage 6 — compliance & exemptions gate.

Runs the automated pre-checks from the house-style guide's pre-approval
checklist and surfaces anything a human must confirm. It never releases
information; it produces a checklist result that gates the transition to
sign-off.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import FOIRequest

# Lightweight personal-data signals (illustrative — replace with a proper DLP /
# NER service in production). Presence means "human must confirm redaction".
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\b(?:0\d{9,10}|\+44\d{9,10})\b")
_NI = re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2}\d{6}[A-D]\b", re.I)


@dataclass
class CheckItem:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ComplianceResult:
    items: list[CheckItem] = field(default_factory=list)
    requires_human: bool = False

    @property
    def passed(self) -> bool:
        return all(i.passed for i in self.items)


def run_checks(request: FOIRequest, draft_text: str) -> ComplianceResult:
    res = ComplianceResult()

    # Statute cited
    statute_ok = ("Freedom of Information Act 2000" in draft_text
                  or "Environmental Information Regulations 2004" in draft_text)
    res.items.append(CheckItem("statute_cited", statute_ok,
                              "Correct access regime named in the letter."))

    # Confirm-or-deny present
    confirm_ok = "does hold" in draft_text or "does not hold" in draft_text or "holds some" in draft_text
    res.items.append(CheckItem("confirm_or_deny", confirm_ok,
                              "Holding position stated."))

    # Re-use clause
    res.items.append(CheckItem("reuse_clause", "Re-use of information" in draft_text,
                              "Re-use clause included."))

    # Appeal rights
    review_ok = "internal review" in draft_text and "Information Commissioner" in draft_text
    res.items.append(CheckItem("appeal_rights", review_ok,
                              "Internal review + ICO wording included."))

    # Personal data scan
    found = []
    if _EMAIL.search(draft_text):
        found.append("email address")
    if _PHONE.search(draft_text):
        found.append("phone number")
    if _NI.search(draft_text):
        found.append("national insurance number")
    pii_clean = not found
    res.items.append(CheckItem(
        "no_unredacted_personal_data", pii_clean,
        "No obvious third-party personal data." if pii_clean
        else f"Possible personal data to redact: {', '.join(found)}.",
    ))

    # A failing personal-data check, or any qualified exemption, needs a human.
    has_qualified_exemption = any(e.is_qualified for e in request.exemptions)
    res.requires_human = (not pii_clean) or has_qualified_exemption
    return res
