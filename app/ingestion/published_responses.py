"""Source B — published FOI responses (feature-flagged).

These are the council's already-disclosed answers, ingested both as factual
precedent and as the basis for the house style. Two fetch modes:

  - "feed"    : read a directory of exported response files (.txt/.md/.html).
                Preferred: agree an export with the Information Governance Unit;
                no scraping, no rendering, fully reliable.
  - "browser" : render the JavaScript disclosure-log portal (iCasework) with
                Playwright. The portal is client-rendered, so a plain HTTP fetch
                returns an empty shell — a headless browser is required.

Disabled unless FOI_INGEST_ENABLED and FOI_INGEST_PUBLISHED_RESPONSES are true.
"""
from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import get_settings
from . import knowledge_base

_TEXT_SUFFIXES = {".txt", ".md", ".html", ".htm"}
_TITLE_LINE = re.compile(r"(?i)^\s*(?:subject|title|re)\s*[:\-]\s*(.+)$")


def _derive_title(content: str, fallback: str) -> str:
    """Pick a meaningful title for a published response: a Subject/Title/Re line
    or a Markdown heading near the top, else the filename. Better titles make
    these precedents far easier to retrieve and cite."""
    head = content.splitlines()[:15]
    for line in head:
        m = _TITLE_LINE.match(line)
        if m and m.group(1).strip():
            return m.group(1).strip()[:200]
    for line in head[:5]:
        if line.lstrip().startswith("#"):
            return line.strip("# ").strip()[:200] or fallback
    return fallback


def ingest(db: Session, feed_dir: str | None = None) -> int:
    s = get_settings()
    if not (s.ingest_enabled and s.ingest_published_responses):
        raise RuntimeError(
            "Published-response ingestion is disabled. Set FOI_INGEST_ENABLED=true "
            "and FOI_INGEST_PUBLISHED_RESPONSES=true to enable."
        )
    if s.published_responses_fetch_mode == "browser":
        return _ingest_via_browser(db)
    return _ingest_via_feed(db, feed_dir)


def _ingest_via_feed(db: Session, feed_dir: str | None) -> int:
    """Read exported response letters from a local directory."""
    if not feed_dir:
        raise ValueError("feed_dir is required for 'feed' mode (path to exported responses).")
    base = Path(feed_dir)
    if not base.exists():
        raise FileNotFoundError(f"Feed directory not found: {feed_dir}")

    ingested = 0
    for path in sorted(base.rglob("*")):
        if path.suffix.lower() not in _TEXT_SUFFIXES or not path.is_file():
            continue
        raw = path.read_text(encoding="utf-8", errors="ignore")
        content = _strip_html(raw) if path.suffix.lower() in {".html", ".htm"} else raw
        content = content.strip()
        if len(content) < 100:
            continue
        title = _derive_title(content, fallback=path.stem.replace("-", " "))
        knowledge_base.upsert(db, source="published_response",
                             title=title, content=content, url=None)
        ingested += 1
    db.commit()
    return ingested


def _ingest_via_browser(db: Session) -> int:  # pragma: no cover - needs Playwright + network
    """Render the iCasework disclosure log and extract published responses.

    Requires Playwright (`pip install playwright && playwright install chromium`).
    Selectors below are placeholders — confirm them against the live portal, or
    prefer the 'feed' mode with an IGU-provided export.
    """
    s = get_settings()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Browser mode needs Playwright. Install with:\n"
            "  pip install playwright && playwright install chromium\n"
            "Or use FOI_PUBLISHED_RESPONSES_FETCH_MODE=feed with an IGU export."
        ) from exc

    ingested = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=s.ingest_user_agent)
        page.goto(s.disclosure_log_url, wait_until="networkidle")
        # TODO: confirm these selectors against the live iCasework portal.
        cards = page.query_selector_all("article, .case, .result")
        for card in cards:
            text = card.inner_text().strip()
            if len(text) < 100:
                continue
            title = text.splitlines()[0][:200]
            knowledge_base.upsert(db, source="published_response",
                                 title=title, content=text, url=s.disclosure_log_url)
            ingested += 1
        browser.close()
    db.commit()
    return ingested


def _strip_html(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return html
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())
