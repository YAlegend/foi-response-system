"""Source A — council website crawler (feature-flagged).

A polite, rate-limited, same-domain crawler that extracts readable text from the
council website into the knowledge base. It honours the site's robots.txt
(including any crawl-delay) and identifies itself with a contactable User-Agent.
Disabled unless both FOI_INGEST_ENABLED and FOI_INGEST_WEBSITE are true. Network
libraries are imported lazily so the app has no hard dependency on them when
ingestion is off.
"""
from __future__ import annotations

import re
import time
from collections import deque
from urllib import robotparser
from urllib.parse import (parse_qsl, urldefrag, urlencode, urljoin, urlparse,
                          urlunparse)

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
        queue: deque[str] = deque(_normalise(u) for u in seeds)

        while queue and ingested < max_pages:
            url = _normalise(queue.popleft())
            if url in seen or urlparse(url).netloc != domain:
                continue
            seen.add(url)
            if not robots.can_fetch(user_agent, url):
                continue  # disallowed by robots.txt
            try:
                resp = client.get(url)
            except Exception:
                continue
            if resp.status_code != 200 or "text/html" not in resp.headers.get("content-type", ""):
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            title = (soup.title.string or url).strip() if soup.title else url
            text = " ".join(soup.get_text(" ").split())
            if len(text) >= min_chars:
                knowledge_base.upsert(db, source="website", title=title,
                                     content=text, url=url)
                ingested += 1

            for a in soup.find_all("a", href=True):
                nxt = _normalise(urljoin(url, a["href"]))
                if urlparse(nxt).netloc == domain and nxt not in seen:
                    queue.append(nxt)

            db.commit()
            time.sleep(effective_delay)  # politeness (>= robots crawl-delay)

    return ingested
