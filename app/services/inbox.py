"""The dedicated FOI mailbox — intake source for the whole system.

FOI requests are not submitted through a public web form; they arrive in a
monitored inbox (e.g. ``foi@hertfordshire.gov.uk``). This module abstracts that
mailbox behind a small provider interface so the connector can be swapped
without touching the rest of the app:

* ``stub``        — an offline simulated mailbox seeded with realistic FOI
                    emails. Default, needs no credentials, runs immediately.
* ``imap`` / ``microsoft365`` / ``gmail`` — real connectors. A clearly-marked
  seam is provided below; implement ``fetch`` and flip ``FOI_INBOX_PROVIDER``.

The caseworker workflow is: **poll** the mailbox -> review **new** messages ->
**import** the genuine FOI requests as cases (Stage 1 intake) or **dismiss**
the misdirected ones. Polling is idempotent: messages are deduped by provider
``uid``, so re-checking the inbox never creates duplicates.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..enums import InboxStatus
from ..models import FOIRequest, InboxMessage
from . import casework

settings = get_settings()

# Our outbound letters carry a reference like "FOI/2026/00001" or "EIR/2025/42";
# spotting it in a reply is the most reliable way to thread it to its case.
_CASE_REF = re.compile(r"\b((?:FOI|EIR)/\d{4}/\d{3,6})\b", re.IGNORECASE)
_REPLY_PREFIX = re.compile(r"^\s*(re|fwd|fw)\s*:", re.IGNORECASE)


@dataclass
class IncomingEmail:
    """A message as the mailbox provider hands it over, before it is stored."""
    uid: str
    from_name: str
    from_email: str
    subject: str
    body: str
    received_at: datetime


# --- Providers ----------------------------------------------------------------

# A small pool of realistic inbound FOI/EIR emails for the offline stub. Each
# poll reveals the next few unseen ones, so "Check inbox" feels like a live
# mailbox filling up rather than a static list.
_STUB_POOL: list[dict] = [
    {
        "uid": "stub-0001",
        "from_name": "Jordan Mills",
        "from_email": "jordan.mills@example.com",
        "subject": "FOI request - household recycling rates",
        "body": (
            "Dear Information Governance Unit,\n\n"
            "Under the Freedom of Information Act 2000 I would like to request:\n"
            "1. How much household waste was collected last year and what "
            "percentage of it was recycled or composted?\n"
            "2. How many miles of road does the council maintain?\n\n"
            "Please send the response by email.\n\nRegards,\nJordan Mills"
        ),
    },
    {
        "uid": "stub-0002",
        "from_name": "Priya Sharma",
        "from_email": "p.sharma@example.org",
        "subject": "Environmental information request - air quality monitoring",
        "body": (
            "Hello,\n\nUnder the Environmental Information Regulations 2004 please "
            "provide the locations of all air quality monitoring stations operated "
            "by the council and the most recent annual readings for each.\n\n"
            "Thank you,\nPriya Sharma"
        ),
    },
    {
        "uid": "stub-0003",
        "from_name": "The Hertford Mercury",
        "from_email": "newsdesk@example-press.co.uk",
        "subject": "FOI: SEND home-to-school transport spend 2023-2025",
        "body": (
            "To the FOI team,\n\nPlease treat this as a request under the FOIA. "
            "We would like the total annual spend on home-to-school transport for "
            "children with special educational needs and disabilities (SEND) for "
            "the financial years 2023/24 and 2024/25, broken down by in-house vs "
            "external provision.\n\nBest,\nNewsdesk, The Hertford Mercury"
        ),
    },
    {
        "uid": "stub-0004",
        "from_name": "Sam O'Connor",
        "from_email": "sam.oconnor@example.com",
        "subject": "Request for my own social care records",
        "body": (
            "Hi,\n\nI would like to obtain copies of all the information the "
            "council holds about me personally relating to my adult social care "
            "case.\n\nThanks,\nSam O'Connor"
        ),
    },
    {
        "uid": "stub-0005",
        "from_name": "Local Cycling Campaign",
        "from_email": "info@example-cycle.org",
        "subject": "FOIA request - pothole reports and repair times",
        "body": (
            "Dear Sir/Madam,\n\nUnder the Freedom of Information Act please tell "
            "us:\n1. How many potholes were reported across the county in the last "
            "12 months?\n2. What was the average time taken to repair a category 1 "
            "defect?\n\nKind regards,\nLocal Cycling Campaign"
        ),
    },
    {
        "uid": "stub-0006",
        "from_name": "Pat Reynolds",
        "from_email": "pat.reynolds@example.com",
        "subject": "Re: your newsletter - unsubscribe me",
        "body": (
            "Please take me off your mailing list, I keep getting the council "
            "newsletter and never signed up for it.\n\nPat"
        ),
    },
    # Follow-up correspondence (arrives on a later poll). These thread onto an
    # existing case rather than starting a new one — see suggest_case().
    {
        "uid": "stub-0007",
        "from_name": "Alex Taylor",
        "from_email": "alex.taylor@example.com",
        "subject": "Re: your FOI request FOI/2026/00001",
        "body": (
            "Thank you for the acknowledgement. To clarify my request "
            "FOI/2026/00001: for the roads question I am only interested in "
            "A-roads, not all maintained roads. Please scope the response to "
            "those.\n\nRegards,\nAlex Taylor"
        ),
    },
    {
        "uid": "stub-0008",
        "from_name": "Jordan Mills",
        "from_email": "jordan.mills@example.com",
        "subject": "Re: FOI request - household recycling rates",
        "body": (
            "Following up on my request — please could you confirm the recycling "
            "figure is for the 2024/25 year specifically? Thank you.\n\nJordan Mills"
        ),
    },
]


class StubInbox:
    """Offline mailbox. Returns the next unseen messages from the pool, stamped
    as if they had just arrived, so polling progressively reveals new mail."""

    batch_size = 3

    def fetch(self, seen_uids: set[str]) -> list[IncomingEmail]:
        now = datetime.now(timezone.utc)
        fresh = [m for m in _STUB_POOL if m["uid"] not in seen_uids][: self.batch_size]
        out: list[IncomingEmail] = []
        for i, m in enumerate(fresh):
            # Spread arrival times over the last little while for a realistic feel.
            received = now - timedelta(minutes=7 * (len(fresh) - i))
            out.append(IncomingEmail(received_at=received, **m))
        return out


def _provider():
    name = settings.inbox_provider.lower()
    if name == "stub":
        return StubInbox()
    # --- Real-connector seam ---------------------------------------------------
    # Implement a class with `fetch(seen_uids: set[str]) -> list[IncomingEmail]`
    # and return it here. Sketches:
    #   imap         -> imaplib: SELECT the FOI folder, SEARCH UNSEEN, parse with
    #                   the `email` stdlib module; use the IMAP UID as `uid`.
    #   microsoft365 -> Microsoft Graph /me/mailFolders/inbox/messages (delta).
    #   gmail        -> Gmail API users.messages.list (label:FOI is:unread).
    # Whatever the source, map each message to an IncomingEmail and keep `uid`
    # stable so dedupe works. Credentials belong in settings / env, never here.
    raise NotImplementedError(
        f"Inbox provider '{settings.inbox_provider}' is not wired yet. "
        "Implement it in app/services/inbox.py (see the seam) or set "
        "FOI_INBOX_PROVIDER=stub to use the offline mailbox."
    )


# --- Operations ---------------------------------------------------------------

def poll(db: Session) -> list[InboxMessage]:
    """Check the mailbox and store any messages we haven't seen before.

    Idempotent: existing ``uid``s are skipped, so this is safe to call on every
    page load or on a timer. Returns only the newly-stored messages.
    """
    seen = set(db.execute(select(InboxMessage.uid)).scalars().all())
    new_rows: list[InboxMessage] = []
    for email in _provider().fetch(seen):
        if email.uid in seen:
            continue
        row = InboxMessage(
            uid=email.uid, from_name=email.from_name, from_email=email.from_email,
            subject=email.subject, body=email.body, received_at=email.received_at,
            status=InboxStatus.NEW.value,
        )
        db.add(row)
        new_rows.append(row)
        seen.add(email.uid)
    db.commit()
    for row in new_rows:
        db.refresh(row)
    return new_rows


def list_messages(db: Session) -> list[InboxMessage]:
    """All mailbox messages, newest arrivals first."""
    return db.execute(
        select(InboxMessage).order_by(InboxMessage.received_at.desc(),
                                      InboxMessage.id.desc())
    ).scalars().all()


def import_message(db: Session, msg: InboxMessage, *, requester_type: str = "resident",
                   officer: str = "foi.team") -> FOIRequest:
    """Log a mailbox message as an FOI case (Stage 1 intake) and link it back."""
    if msg.status == InboxStatus.IMPORTED.value and msg.request_id:
        raise ValueError(f"Message already logged as case #{msg.request_id}.")

    req = casework.create_request(
        db, requester_name=msg.from_name or msg.from_email,
        requester_email=msg.from_email,
        subject=msg.subject or "(no subject)",
        body=msg.body, requester_type=requester_type)

    # Record where the case came from — provenance for the audit trail.
    audit.log(db, req, actor=officer, action="intake:from_inbox",
              detail=f"Logged from {settings.inbox_address}; "
                     f"sender={msg.from_email}; inbox-uid={msg.uid}")

    msg.status = InboxStatus.IMPORTED.value
    msg.request_id = req.id
    db.add(msg)
    db.commit()
    db.refresh(req)
    return req


def dismiss(db: Session, msg: InboxMessage) -> InboxMessage:
    """Mark a message as not an FOI request (misdirected / spam)."""
    msg.status = InboxStatus.DISMISSED.value
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


# --- Threading --------------------------------------------------------------

def suggest_case(msg: InboxMessage, requests: list[FOIRequest]) -> dict | None:
    """Guess which existing case a message belongs to, so a caseworker can file
    it as correspondence rather than logging a duplicate request.

    Pure function over a preloaded list of cases (no DB) to avoid N+1 in the
    inbox listing. Two signals, strongest first:
      1. the message quotes a case reference (FOI/2026/00001) we issued, or
      2. it is a reply (Re:/Fwd:) from an address that matches a case's requester.
    Returns ``{request_id, reference, reason}`` or ``None``.
    """
    ref_match = _CASE_REF.search(f"{msg.subject}\n{msg.body}")
    if ref_match:
        ref = ref_match.group(1).upper()
        for r in requests:
            if r.reference.upper() == ref:
                return {"request_id": r.id, "reference": r.reference,
                        "reason": f"Message quotes our reference {r.reference}"}

    if _REPLY_PREFIX.match(msg.subject or ""):
        sender = (msg.from_email or "").lower()
        # Most recent matching case wins.
        for r in sorted(requests, key=lambda x: x.id, reverse=True):
            if (r.requester_email or "").lower() == sender:
                return {"request_id": r.id, "reference": r.reference,
                        "reason": f"Reply from {msg.from_email} on case {r.reference}"}
    return None


def link_message(db: Session, msg: InboxMessage, *, request_id: int,
                 officer: str = "foi.team") -> FOIRequest:
    """File a mailbox message as correspondence on an existing case."""
    req = db.get(FOIRequest, request_id)
    if not req:
        raise ValueError(f"Case #{request_id} not found.")
    if msg.status == InboxStatus.LINKED.value and msg.request_id == request_id:
        raise ValueError(f"Message already linked to {req.reference}.")

    body = (msg.body or "").strip()
    excerpt = body if len(body) <= 1500 else body[:1500] + " […]"
    audit.log(db, req, actor=officer, action="correspondence:received",
              detail=f"Inbound from {msg.from_email} — \"{msg.subject}\"\n{excerpt}")

    msg.status = InboxStatus.LINKED.value
    msg.request_id = request_id
    db.add(msg)
    db.commit()
    db.refresh(req)
    return req
