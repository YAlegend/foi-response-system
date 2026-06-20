"""Source A — council website crawler (feature-flagged).

A polite, rate-limited, same-domain crawler that extracts readable text from the
council website into the knowledge base — both **HTML pages** and **linked PDFs**
(reports, datasets), so figures published only in a document are searchable too.
It honours the site's robots.txt (including any crawl-delay) and identifies itself
with a contactable User-Agent. Disabled unless both FOI_INGEST_ENABLED and
FOI_INGEST_WEBSITE are true (PDFs additionally gated by FOI_INGEST_PDFS). Network
and parsing libraries (httpx, bs4, pypdf) are imported lazily so the app has no
hard dependency on them when ingestion is off.
"""
from __future__ import annotations

import re
import time
from collections import deque
from urllib import robotparser
from urllib.parse import (parse_qsl, unquote, urldefrag, urlencode, urljoin,
                          urlparse, urlunparse)

from sqlalchemy.orm import Session

from ..config import get_settings
from . import knowledge_base

# Query parameters that identify a marketing/tracking variant of the same page.
# Stripping them collapses near-duplicates (e.g. ?utm_source=homepage) onto one
# canonical URL so the knowledge base holds each page once.
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {"gclid", "fbclid", "mc_cid", "mc_eid", "igshid", "ref", "ref_src"}


def _normalise(url: str) -> str:
    """Canonicalise a URL: drop the fragment, lowercase scheme/host, and remove
    tracking query parameters. Two links that differ only by tracking params
    normalise to the same string, so dedupe (the ``seen`` set and the KB ``url``
    key) treats them as one page."""
    url = urldefrag(url)[0]
    p = urlparse(url)
    kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if not k.lower().startswith(_TRACKING_PREFIXES) and k.lower() not in _TRACKING_KEYS]
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, p.params,
                       urlencode(kept), ""))


_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


def _collect_sitemap(client, sitemap_url: str, limit: int, _depth: int = 0) -> list[str]:
    """Return page URLs listed in a sitemap, following one level of sitemap
    index nesting. Returns [] on any failure so the caller can fall back to a
    breadth-first crawl from the root."""
    if _depth > 2:
        return []
    try:
        resp = client.get(sitemap_url)
        if resp.status_code != 200:
            return []
    except Exception:
        return []

    locs = _LOC.findall(resp.text)
    pages: list[str] = []
    for loc in locs:
        # A <loc> pointing at another .xml is a child sitemap (sitemap index).
        if loc.lower().rstrip("/").endswith(".xml") or "sitemap" in loc.lower():
            pages.extend(_collect_sitemap(client, loc, limit, _depth + 1))
        else:
            pages.append(loc)
        if len(pages) >= limit:
            break
    return pages


def _load_robots(client, root: str, user_agent: str) -> robotparser.RobotFileParser:
    """Fetch and parse robots.txt for the crawl root.

    On a missing or unreadable robots.txt we default to allow-all, which is the
    standard convention (no rules published == no restrictions).
    """
    rp = robotparser.RobotFileParser()
    robots_url = urljoin(root, "/robots.txt")
    try:
        resp = client.get(robots_url)
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
            return rp
    except Exception:
        pass
    rp.allow_all = True
    return rp


def _is_pdf_response(content_type: str, data: bytes) -> bool:
    """True only if the fetched body is actually a PDF — by content-type or the
    ``%PDF-`` magic bytes. A URL ending in .pdf is NOT enough: some CMSs serve
    .pdf links as HTML viewer pages, and treating those as PDFs would drop the
    page instead of ingesting its HTML."""
    if "application/pdf" in content_type.lower():
        return True
    return data[:5] == b"%PDF-"


def _extract_pdf_text(data: bytes) -> str:
    """Extract text from PDF bytes. Returns '' if pypdf is unavailable or the file
    can't be parsed. Scanned/image-only PDFs yield little or no text (no OCR), so
    they fall below the content floor and are skipped — that is intended."""
    try:
        from pypdf import PdfReader  # lazy optional dep
    except ImportError:
        return ""
    import io
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""


def _project_for(url: str, project_seeds: list[tuple[str, str]]) -> str:
    """Tag a crawled page with the scheme it belongs to: the project whose seed
    URL is a prefix of this page's URL. Empty when the page is general council
    content rather than part of a tracked scheme (traffic filters, ZEZ, ...)."""
    for project, seed in project_seeds:
        base = seed.rstrip("/")
        if url == seed or url.startswith(base + "/"):
            return project
    return ""


def _pdf_title(raw_text: str, url: str) -> str:
    """A readable title for a PDF: its first substantial line, else a tidied
    filename derived from the URL."""
    for line in raw_text.splitlines():
        s = line.strip()
        if len(s) >= 8:
            return s[:200]
    name = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
    name = name.rsplit(".", 1)[0].replace("-", " ").replace("_", " ").strip()
    return name[:200] or url


def crawl(db: Session, root: str | None = None, max_pages: int | None = None,
          delay_seconds: float = 1.0) -> int:
    """Crawl the council website and upsert pages into the knowledge base.

    Returns the number of pages ingested. Raises RuntimeError if the feature is
    disabled, so callers fail loudly rather than silently doing nothing.

    robots.txt is respected: disallowed URLs are skipped, and if the site
    publishes a crawl-delay we wait at least that long between requests.
    """
    s = get_settings()
    if not (s.ingest_enabled and s.ingest_website):
        raise RuntimeError(
            "Website ingestion is disabled. Set FOI_INGEST_ENABLED=true and "
            "FOI_INGEST_WEBSITE=true to enable."
        )

    import httpx  # lazy
    from bs4 import BeautifulSoup  # lazy

    root = root or s.council_website_root
    max_pages = max_pages or s.ingest_crawl_max_pages
    min_chars = s.ingest_min_content_chars
    domain = urlparse(root).netloc
    user_agent = s.ingest_user_agent

    # Domains the crawl may follow into. The council site always; allow-listed
    # related scheme/consultation sites only when crawl_follow_related is on — so
    # a ZEZ link to the city council site is followed, but the open web is not.
    allowed = {domain}
    if s.crawl_follow_related:
        allowed |= {d.lower() for d in s.crawl_related_domains}
    # (project, normalised seed URL) pairs: queued first and used to tag pages.
    project_seeds = [(seed["project"], _normalise(seed["url"]))
                     for seed in s.crawl_project_seeds]

    seen: set[str] = set()
    ingested = 0
    headers = {"User-Agent": user_agent}

    with httpx.Client(headers=headers, timeout=20, follow_redirects=True) as client:
        robots = _load_robots(client, root, user_agent)
        # Respect a published crawl-delay, but never go faster than our own floor.
        crawl_delay = robots.crawl_delay(user_agent)
        effective_delay = max(delay_seconds, crawl_delay or 0.0)

        # Prefer the sitemap (real content pages); fall back to BFS from the root.
        seeds: list[str] = []
        if s.ingest_use_sitemap:
            seeds = _collect_sitemap(client, urljoin(root, "/sitemap.xml"), max_pages * 5)
        seeds = seeds or [root]
        # Priority scheme pages first, then the sitemap/root, so demo-relevant
        # content (traffic filters, ZEZ) is ingested even if the cap is reached.
        ordered = [u for _p, u in project_seeds] + [_normalise(u) for u in seeds]
        queue: deque[str] = deque(ordered)

        while queue and ingested < max_pages:
            url = _normalise(queue.popleft())
            if url in seen or urlparse(url).netloc not in allowed:
                continue
            seen.add(url)
            if not robots.can_fetch(user_agent, url):
                continue  # disallowed by robots.txt
            try:
                resp = client.get(url)
            except Exception:
                continue
            if resp.status_code != 200:
                continue
            ctype = resp.headers.get("content-type", "")

            # Documents (PDFs): extract their text so figures published only in a
            # report/dataset are searchable too, not just HTML pages.
            if s.ingest_pdfs and _is_pdf_response(ctype, resp.content):
                raw = _extract_pdf_text(resp.content)
                text = " ".join(raw.split())
                if len(text) >= min_chars:
                    knowledge_base.upsert(db, source="website",
                                          title=_pdf_title(raw, url), content=text, url=url,
                                          project=_project_for(url, project_seeds))
                    ingested += 1
                db.commit()
                time.sleep(effective_delay)
                continue

            if "text/html" not in ctype:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            title = (soup.title.string or url).strip() if soup.title else url
            text = " ".join(soup.get_text(" ").split())
            if len(text) >= min_chars:
                knowledge_base.upsert(db, source="website", title=title,
                                     content=text, url=url,
                                     project=_project_for(url, project_seeds))
                ingested += 1

            for a in soup.find_all("a", href=True):
                nxt = _normalise(urljoin(url, a["href"]))
                if urlparse(nxt).netloc in allowed and nxt not in seen:
                    queue.append(nxt)

            db.commit()
            time.sleep(effective_delay)  # politeness (>= robots crawl-delay)

    return ingested
