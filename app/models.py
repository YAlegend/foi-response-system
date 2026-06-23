"""SQLAlchemy ORM models — the single case record and its related entities."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer,
                        LargeBinary, String, Text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base
from .enums import (CaseOutcome, HoldingStatus, InboxStatus, Regime,
                    RequesterType, Role, Stage)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FOIRequest(Base):
    """A single FOI/EIR case — the source of truth for the whole lifecycle."""
    __tablename__ = "foi_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reference: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    requester_name: Mapped[str] = mapped_column(String(200))
    requester_email: Mapped[str] = mapped_column(String(320))
    requester_type: Mapped[str] = mapped_column(String(20), default=RequesterType.RESIDENT.value)

    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)

    regime: Mapped[str] = mapped_column(String(8), default=Regime.FOIA.value)
    owning_department: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Scheme/initiative the request is about (traffic-filters, zez, ltn, ...),
    # set at triage. Empty for general requests. Drives grouping and analytics.
    project: Mapped[str] = mapped_column(String(120), default="", index=True)
    holding_status: Mapped[str] = mapped_column(String(16), default=HoldingStatus.UNKNOWN.value)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    stage: Mapped[str] = mapped_column(String(32), default=Stage.INTAKE.value, index=True)
    outcome: Mapped[str] = mapped_column(String(20), default=CaseOutcome.OPEN.value)

    received_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pit_extension_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    # Clarification clock-pause (FOIA s.1(3)/s.10(6)): working days the statutory
    # clock has been stopped, and when the current pause began (None if running).
    clock_paused_days: Mapped[int] = mapped_column(Integer, default=0)
    clarification_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    drafts: Mapped[list["Draft"]] = relationship(back_populates="request",
                                                cascade="all, delete-orphan",
                                                order_by="Draft.version")
    events: Mapped[list["AuditEvent"]] = relationship(back_populates="request",
                                                      cascade="all, delete-orphan",
                                                      order_by="AuditEvent.created_at")
    exemptions: Mapped[list["Exemption"]] = relationship(back_populates="request",
                                                        cascade="all, delete-orphan")

    @property
    def latest_draft(self) -> "Draft | None":
        return self.drafts[-1] if self.drafts else None


class Draft(Base):
    """A versioned response draft. Version 1 is the AI draft; later versions
    incorporate SME input and approvals."""
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("foi_requests.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    body: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(80), default="system")
    citations: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    request: Mapped[FOIRequest] = relationship(back_populates="drafts")


class Exemption(Base):
    """An exemption considered or applied to a case."""
    __tablename__ = "exemptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("foi_requests.id"), index=True)
    section: Mapped[str] = mapped_column(String(40))           # e.g. "s.40(2)"
    title: Mapped[str] = mapped_column(String(200), default="")
    is_qualified: Mapped[bool] = mapped_column(Boolean, default=True)
    public_interest_decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reasoning: Mapped[str] = mapped_column(Text, default="")

    request: Mapped[FOIRequest] = relationship(back_populates="exemptions")


class AuditEvent(Base):
    """Immutable-by-convention audit trail entry."""
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("foi_requests.id"), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(120))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    request: Mapped[FOIRequest] = relationship(back_populates="events")


class InboxMessage(Base):
    """A message received in the dedicated FOI mailbox, before it becomes a case.

    The mailbox is polled by a pluggable provider (offline ``stub`` by default;
    see ``app/services/inbox.py``). A caseworker reviews each message and either
    logs it as an FOI case (status -> ``imported``, linked via ``request_id``)
    or dismisses it (misdirected / spam). This is the system's intake source —
    the council does not expose a public request form.
    """
    __tablename__ = "inbox_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Provider-side identifier (IMAP UID, Graph/Gmail message id, ...) used to
    # dedupe so polling the same mailbox twice never creates duplicate rows.
    uid: Mapped[str] = mapped_column(String(200), unique=True, index=True)

    from_name: Mapped[str] = mapped_column(String(200), default="")
    from_email: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(400), default="")
    body: Mapped[str] = mapped_column(Text, default="")

    received_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    status: Mapped[str] = mapped_column(String(16), default=InboxStatus.NEW.value, index=True)
    request_id: Mapped[int | None] = mapped_column(ForeignKey("foi_requests.id"), nullable=True)


class User(Base):
    """A caseworker/officer who signs in. Passwords are salted PBKDF2 hashes
    (see app/auth.py). For production this is replaced/fronted by council SSO."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(320), default="")
    password_hash: Mapped[str] = mapped_column(String(255))   # algo$iter$salt$hash
    role: Mapped[str] = mapped_column(String(20), default=Role.CASEWORKER.value)
    department: Mapped[str] = mapped_column(String(120), default="")  # for role=department
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Session(Base):
    """A server-side login session. The opaque token lives in an HttpOnly cookie."""
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class KnowledgeDoc(Base):
    """A document in the Phase 0 knowledge base. `source` distinguishes the
    council website, published FOI responses, and manually added material."""
    __tablename__ = "knowledge_docs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)   # website|published_response|manual|department
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    title: Mapped[str] = mapped_column(String(400), default="")
    content: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    # Provenance for documents contributed by a subject department (role=department).
    department: Mapped[str] = mapped_column(String(120), default="")
    uploaded_by: Mapped[str] = mapped_column(String(80), default="")
    # Scheme/initiative a document belongs to (e.g. "traffic-filters", "zez").
    # Lets the crawl tag project pages and a reviewer scope an upload to a project.
    project: Mapped[str] = mapped_column(String(120), default="", index=True)
    # Review gate. Public sources (council website, published FOI responses) are
    # auto-"approved" — they are already public. Private department/project
    # uploads land as "pending_review" and are INVISIBLE to retrieval until a
    # reviewer approves them, so unvetted internal material can never ground a
    # draft. "rejected" docs are kept for the audit trail but never retrieved.
    status: Mapped[str] = mapped_column(String(20), default="approved", index=True)
    reviewed_by: Mapped[str] = mapped_column(String(80), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        back_populates="doc", cascade="all, delete-orphan", order_by="KnowledgeChunk.ordinal")


class KnowledgeChunk(Base):
    """A passage of a KnowledgeDoc with its embedding, used for semantic search.

    Populated by `python -m app.reindex` when FOI_RETRIEVAL_PROVIDER=semantic.
    The embedding is a float32 vector stored as raw bytes (see app/services/
    retrieval.py for how it is packed/unpacked). Lives in its own table so it
    is created by create_all() on existing databases without a migration."""
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("knowledge_docs.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)   # float32 vector, packed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    doc: Mapped["KnowledgeDoc"] = relationship(back_populates="chunks")


class KnowledgeRefresh(Base):
    """A run of the public-information refresh (re-crawl + re-ingest + reindex).

    One row per refresh attempt — this is both the audit trail of when the
    knowledge base was last brought up to date and the source of truth for the
    staleness check that drives weekly / pre-draft auto-refreshes. Created by
    create_all() on existing databases without a migration."""
    __tablename__ = "knowledge_refreshes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual|weekly|pre_draft|startup
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)  # running|ok|error|skipped
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    website_docs: Mapped[int] = mapped_column(Integer, default=0)
    published_docs: Mapped[int] = mapped_column(Integer, default=0)
    chunks_indexed: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str] = mapped_column(Text, default="")


class SchemeNotification(Base):
    """A breach-trend notification event for a scheme.

    Both the audit trail of what was sent and the dedup state: a scheme is
    considered "currently alerted" when its most recent event is "alerted", so it
    is not emailed again until a later "resolved" event clears it (the scheme
    recovered). Created by create_all() on existing databases without a migration."""
    __tablename__ = "scheme_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scheme: Mapped[str] = mapped_column(String(120), index=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    event: Mapped[str] = mapped_column(String(16), default="alerted")  # alerted|resolved
    recent: Mapped[int] = mapped_column(Integer, default=0)
    prior: Mapped[int] = mapped_column(Integer, default=0)
    breach_rate: Mapped[int] = mapped_column(Integer, default=0)
    channel: Mapped[str] = mapped_column(String(16), default="stub")
    recipients: Mapped[str] = mapped_column(String(500), default="")
    subject: Mapped[str] = mapped_column(String(300), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class DepartmentDigest(Base):
    """A per-department SLA digest send. The (department, period) pair is the
    dedup key — a department gets one digest per ISO week unless a run is forced.
    Created by create_all() on existing databases without a migration."""
    __tablename__ = "department_digests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    department: Mapped[str] = mapped_column(String(120), index=True)
    period: Mapped[str] = mapped_column(String(12), index=True)   # ISO year-week, e.g. 2026-W25
    open_cases: Mapped[int] = mapped_column(Integer, default=0)
    overdue: Mapped[int] = mapped_column(Integer, default=0)
    breach_rate: Mapped[int] = mapped_column(Integer, default=0)
    channel: Mapped[str] = mapped_column(String(16), default="stub")
    recipients: Mapped[str] = mapped_column(String(500), default="")
    subject: Mapped[str] = mapped_column(String(300), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
