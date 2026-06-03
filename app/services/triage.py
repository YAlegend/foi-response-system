"""Stage 2 — triage & classification.

Rule-based classifier that determines the access regime (FOIA vs EIR), the most
likely owning department, and early risk flags (cost limit s.12, vexatious s.14).
It is deliberately transparent and overridable; an LLM classifier can be slotted
in behind the same interface later.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..enums import Regime

# Keyword -> department routing table. Extend as needed.
DEPARTMENT_KEYWORDS: dict[str, list[str]] = {
    "Adult Care Services": ["adult social care", "care home", "safeguarding adult", "domiciliary"],
    "Children's Services": ["children", "fostering", "adoption", "school place", "send", "child protection"],
    "Highways": ["road", "pothole", "highway", "traffic", "pavement", "street light", "gritting"],
    "Environment": ["waste", "recycling", "environmental", "pollution", "flood", "tree", "countryside"],
    "Public Health": ["public health", "smoking", "obesity", "drugs", "alcohol", "vaccination"],
    "Education": ["school", "education", "pupil", "teacher", "admissions", "term dates"],
    "Finance": ["budget", "spend", "expenditure", "procurement", "contract", "invoice", "salary"],
    "HR / Workforce": ["staff", "employee", "headcount", "redundancy", "sickness", "recruitment"],
    "Fire & Rescue": ["fire", "rescue", "incident response", "fire engine"],
}

EIR_KEYWORDS = [
    "environment", "emissions", "pollution", "waste", "water", "air quality",
    "flood", "landfill", "noise", "contaminated land", "tree", "biodiversity",
    "planning", "highway works", "energy",
]

COST_RISK_KEYWORDS = ["all", "every", "since records began", "each", "complete list", "full history"]
VEXATIOUS_RISK_KEYWORDS = ["again", "as before", "repeat", "previously requested"]


@dataclass
class TriageResult:
    regime: str = Regime.FOIA.value
    department: str | None = None
    cost_risk: bool = False
    vexatious_risk: bool = False
    matched_terms: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def classify(subject: str, body: str) -> TriageResult:
    text = f"{subject}\n{body}".lower()
    result = TriageResult()

    # Department
    best_dept, best_hits = None, 0
    for dept, kws in DEPARTMENT_KEYWORDS.items():
        hits = sum(1 for kw in kws if kw in text)
        if hits > best_hits:
            best_dept, best_hits = dept, hits
            result.matched_terms = [kw for kw in kws if kw in text]
    result.department = best_dept

    # Regime
    if any(kw in text for kw in EIR_KEYWORDS):
        result.regime = Regime.EIR.value
        result.notes.append("Environmental keywords detected — handle under EIR 2004.")

    # Risk flags
    if any(kw in text for kw in COST_RISK_KEYWORDS):
        result.cost_risk = True
        result.notes.append("Broad scope — assess against the s.12 cost limit.")
    if any(kw in text for kw in VEXATIOUS_RISK_KEYWORDS):
        result.vexatious_risk = True
        result.notes.append("Possible repeat request — consider s.14.")

    if not best_dept:
        result.notes.append("No clear department match — route to FOI team for manual triage.")
    return result
