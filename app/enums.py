"""Workflow stages, statuses and FOI domain enums."""
from __future__ import annotations

import enum


class Stage(str, enum.Enum):
    """The eight stages from the architecture, plus terminal and hold states."""
    INTAKE = "1_intake"
    TRIAGE = "2_triage"
    RETRIEVAL_DRAFT = "3_retrieval_draft"
    DEPARTMENT_REVIEW = "4_department_review"
    UPDATE_RESPONSE = "5_update_response"
    COMPLIANCE_GATE = "6_compliance_gate"
    SIGN_OFF = "7_sign_off"
    DISPATCH = "8_dispatch"
    CLOSED = "closed"
    # Off-pipeline states.
    AWAITING_CLARIFICATION = "awaiting_clarification"  # clock paused (FOIA s.1(3))
    INTERNAL_REVIEW = "internal_review"                # reopened after dispatch


# Human-friendly labels for UI / audit.
STAGE_LABELS: dict[str, str] = {
    Stage.INTAKE: "Intake & registration",
    Stage.TRIAGE: "AI triage & classification",
    Stage.RETRIEVAL_DRAFT: "Knowledge retrieval & auto-draft",
    Stage.DEPARTMENT_REVIEW: "Department human review",
    Stage.UPDATE_RESPONSE: "Update FOI response",
    Stage.COMPLIANCE_GATE: "Compliance & exemptions gate",
    Stage.SIGN_OFF: "Final sign-off",
    Stage.DISPATCH: "FOI team dispatch",
    Stage.CLOSED: "Closed",
    Stage.AWAITING_CLARIFICATION: "Awaiting clarification (clock paused)",
    Stage.INTERNAL_REVIEW: "Internal review",
}


class Regime(str, enum.Enum):
    """Which access regime governs the request."""
    FOIA = "FOIA"   # Freedom of Information Act 2000
    EIR = "EIR"     # Environmental Information Regulations 2004


class HoldingStatus(str, enum.Enum):
    HELD = "held"
    PARTIAL = "partial"
    NOT_HELD = "not_held"
    UNKNOWN = "unknown"


class RequesterType(str, enum.Enum):
    RESIDENT = "resident"
    BUSINESS = "business"
    STAKEHOLDER = "stakeholder"
    JOURNALIST = "journalist"
    OTHER = "other"


class CaseOutcome(str, enum.Enum):
    OPEN = "open"
    GRANTED_FULL = "granted_full"
    GRANTED_PARTIAL = "granted_partial"
    REFUSED = "refused"
    NOT_HELD = "not_held"


class Role(str, enum.Enum):
    """Who a user is, and therefore which stage actions they may perform.
    Separation of duties: the person who approves is not the person who signs
    off, who is not the person who dispatches."""
    CASEWORKER = "caseworker"   # intake + triage/draft/SME (processing)
    MANAGER = "manager"         # department approval
    LEGAL_IG = "legal_ig"       # final sign-off (Legal & Information Governance)
    FOI_TEAM = "foi_team"       # mailbox intake + dispatch
    ADMIN = "admin"             # everything + ingestion/index/user admin


ROLE_LABELS: dict[str, str] = {
    Role.CASEWORKER: "Caseworker",
    Role.MANAGER: "Department manager",
    Role.LEGAL_IG: "Legal & Information Governance",
    Role.FOI_TEAM: "FOI team",
    Role.ADMIN: "Administrator",
}


class InboxStatus(str, enum.Enum):
    """Lifecycle of a message sitting in the dedicated FOI mailbox."""
    NEW = "new"              # arrived; awaiting a caseworker's triage
    IMPORTED = "imported"    # logged as a new FOI case (linked via request_id)
    LINKED = "linked"        # filed as correspondence on an existing case
    DISMISSED = "dismissed"  # not an FOI request (spam / misdirected)
