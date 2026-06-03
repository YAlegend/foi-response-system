"""Keep the public-information knowledge base current.

One function — :func:`refresh` — re-runs the public-data ingestion (council
website crawl + published-response feed) and rebuilds the semantic index, then
records a :class:`KnowledgeRefresh` row. Two triggers drive it:

  * **weekly**      — an external cron job / systemd timer runs
    ``python -m app.refresh`` (see :mod:`app.refresh`), which forces a refresh.
  * **pre-draft**   — :func:`refresh_if_stale` is called at the start of drafting
    a new FOI response, so a draft is grounded on up-to-date public information.

Everything is **best-effort and flag-gated**. Ingestion only touches *public*
data and stays OFF by default (``FOI_KB_REFRESH_ENABLED`` plus the per-source
``FOI_INGEST_*`` flags). If a source is unreachable or disabled the refresh is
recorded and the caller carries on — a refresh must never block intake, drafting
or dispatch, and no council case data ever leaves the machine.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..ingestion import published_responses, website_crawler
from ..models import KnowledgeRefresh
from ..reindex import reindex


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime | None) -> datetime | None:
    """DB datetimes come back naive (UTC). Make them comparable to _now()."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def last_successful(db: Session) -> KnowledgeRefresh | None:
    """The most recent refresh that completed successfully (status 'ok')."""
    return db.execute(
        select(KnowledgeRefresh)
        .where(KnowledgeRefresh.status == "ok")
        .order_by(KnowledgeRefresh.finished_at.desc())
    ).scalars().first()


def latest(db: Session) -> KnowledgeRefresh | None:
    """The most recent refresh of any outcome (for status display)."""
    return db.execute(
        select(KnowledgeRefresh).order_by(KnowledgeRefresh.started_at.desc())
    ).scalars().first()


def is_stale(db: Session, max_age_days: int | None = None) -> bool:
    """True if the KB has never been refreshed, or not within the freshness
    window (default ``kb_refresh_max_age_days`` — weekly)."""
    max_age = max_age_days if max_age_days is not None else get_settings().kb_refresh_max_age_days
    last = last_successful(db)
    if last is None or last.finished_at is None:
        return True
    return _now() - _as_aware(last.finished_at) >= timedelta(days=max_age)


def refresh(db: Session, *, trigger: str = "manual", force: bool = False) -> KnowledgeRefresh:
    """Re-ingest public data and rebuild the index. Records and returns the run.

    ``force`` runs even when ``kb_refresh_enabled`` is off (used by the manual
    admin button / CLI). Per-source failures are caught so one unreachable
    source never aborts the others; the row's ``status`` is 'ok' (something was
    ingested), 'skipped' (nothing was enabled to run) or 'error' (a source that
    was enabled failed)."""
    s = get_settings()
    rec = KnowledgeRefresh(trigger=trigger, status="running", started_at=_now())
    db.add(rec)
    db.commit()
    db.refresh(rec)

    if not (s.kb_refresh_enabled or force):
        return _finish(db, rec, "skipped",
                       "Auto-refresh disabled (set FOI_KB_REFRESH_ENABLED=true).")

    notes: list[str] = []
    attempted = errored = False

    if s.ingest_enabled and s.ingest_website:
        attempted = True
        try:
            rec.website_docs = website_crawler.crawl(db)
            notes.append(f"website: {rec.website_docs} pages")
        except Exception as exc:               # network/parse failure — keep going
            errored = True
            notes.append(f"website failed: {exc}")
    else:
        notes.append("website: skipped (FOI_INGEST_WEBSITE off)")

    if s.ingest_enabled and s.ingest_published_responses:
        attempted = True
        try:
            rec.published_docs = published_responses.ingest(
                db, feed_dir=s.published_responses_feed_dir)
            notes.append(f"published: {rec.published_docs} responses")
        except Exception as exc:
            errored = True
            notes.append(f"published failed: {exc}")
    else:
        notes.append("published: skipped (FOI_INGEST_PUBLISHED_RESPONSES off)")

    if s.retrieval_provider.lower() == "semantic":
        try:
            rec.chunks_indexed = reindex(db)["chunks"]
            notes.append(f"reindex: {rec.chunks_indexed} chunks")
        except Exception as exc:
            errored = True
            notes.append(f"reindex failed: {exc}")

    status = "error" if errored else ("ok" if attempted else "skipped")
    return _finish(db, rec, status, "; ".join(notes))


def _finish(db: Session, rec: KnowledgeRefresh, status: str, detail: str) -> KnowledgeRefresh:
    rec.status = status
    rec.detail = detail[:2000]
    rec.finished_at = _now()
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def refresh_if_stale(db: Session, *, trigger: str,
                     max_age_days: int | None = None) -> KnowledgeRefresh | None:
    """Refresh only when enabled AND the KB is stale. Returns the run, or None
    if auto-refresh is off or the KB is already fresh."""
    if not get_settings().kb_refresh_enabled:
        return None
    if not is_stale(db, max_age_days):
        return None
    return refresh(db, trigger=trigger)
