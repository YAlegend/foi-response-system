"""Pydantic request/response schemas for the API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginIn(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    username: str
    full_name: str
    role: str
    department: str = ""
    capabilities: list[str] = []
    demo: bool = False


class UserCreateIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=80)
    password: str = Field(..., min_length=6)
    role: str
    full_name: str = ""
    email: str = ""
    department: str = ""


class UserSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    full_name: str
    email: str = ""
    role: str
    department: str = ""
    is_active: bool = True


class RequestCreate(BaseModel):
    requester_name: str = Field(..., max_length=200)
    requester_email: EmailStr
    requester_type: str = "resident"
    subject: str = Field(..., max_length=300)
    body: str = Field(..., min_length=1)


class DraftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    version: int
    body: str
    created_by: str
    confidence: float
    citations: list = []
    created_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    stage: str
    actor: str
    action: str
    detail: str
    created_at: datetime


class RequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    reference: str
    requester_name: str
    requester_email: str
    requester_type: str
    subject: str
    body: str
    regime: str
    owning_department: str | None
    project: str = ""
    holding_status: str
    confidence: float
    stage: str
    outcome: str
    received_at: datetime
    deadline: datetime | None
    # SLA snapshot (computed for open cases by the list endpoint; lets the queue
    # show flags and the dashboard cards filter by SLA band).
    sla_flag: str | None = None
    working_days_remaining: int | None = None
    paused: bool = False


class RequestDetail(RequestOut):
    drafts: list[DraftOut] = []
    events: list[EventOut] = []
    responsible_person: str = ""        # named officer on the hook for this case
    responsible_department: str = ""    # their team (or "FOI team" when unassigned)


# The acting officer on these actions is taken from the authenticated session,
# never the request body — that is what makes the audit trail trustworthy.
class SMEUpdate(BaseModel):
    supplied_text: str
    holding_status: str | None = None


class ApprovalIn(BaseModel):
    approved: bool
    note: str = ""


class SignOffIn(BaseModel):
    authorised: bool
    note: str = ""


class ClarificationRequestIn(BaseModel):
    question: str


class ClarificationProvideIn(BaseModel):
    clarification_text: str


class ReviewRequestIn(BaseModel):
    reason: str = ""


class ReviewCompleteIn(BaseModel):
    upheld: bool
    note: str = ""


class InboxOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    from_name: str
    from_email: str
    subject: str
    body: str
    received_at: datetime
    status: str
    request_id: int | None
    # Threading hint for "new" messages (computed, not stored).
    suggested_request_id: int | None = None
    suggested_reference: str | None = None
    suggested_reason: str | None = None


class InboxImportIn(BaseModel):
    requester_type: str = "resident"


class InboxLinkIn(BaseModel):
    request_id: int


class KnowledgeDocIn(BaseModel):
    title: str = Field("", max_length=400)
    content: str = Field(..., min_length=1)
    url: str | None = None
    project: str = Field("", max_length=120)


class KnowledgeDocOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source: str
    title: str
    url: str | None
    ingested_at: datetime
    department: str = ""
    uploaded_by: str = ""
    project: str = ""
    status: str = "approved"
    reviewed_by: str = ""
    content_chars: int = 0
    chunks: int = 0


class KnowledgeReviewIn(BaseModel):
    """A reviewer's decision on a pending department/project upload."""
    note: str = Field("", max_length=500)


class KnowledgeRefreshOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    website_docs: int
    published_docs: int
    chunks_indexed: int
    detail: str


class KnowledgeRefreshStatus(BaseModel):
    enabled: bool
    on_draft: bool
    max_age_days: int
    stale: bool
    last: KnowledgeRefreshOut | None = None
    history: list[KnowledgeRefreshOut] = []
