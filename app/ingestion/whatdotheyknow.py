"""Source C — WhatDoTheyKnow (mySociety public FOI archive).

A third source of already-published FOI Q&A, alongside the council's own
disclosure log (published_responses.py). WhatDoTheyKnow runs mySociety's
Alaveteli platform and hosts every FOI request made to the authority through the
site — the requester's question *and* the authority's published response. For
high-volume Oxford topics (traffic filters, the Zero Emission Zone) it is by far
the richest precedent corpus.

We read it considerately: robots-aware, rate limited, an identified User-Agent,
and capped per run. Two entry points:

  - discover : read the authority's request index (/body/<slug>) and follow each
               linked request page, extracting the correspondence text.
  - manifest : a caller-supplied list of specific request URLs (precise, ideal
               for a curated demo set).

Ingested rows are source="published_response" — the same type as the disclosure
log — so retrieval and drafting treat both archives identically. They are public
by definition, so they are auto-"approved" (not gated by the review queue).

Disabled unless FOI_INGEST_ENABLED and FOI_INGEST_WHATDOTHEYKNOW are true.
"""
from __future__ import annotations

import time
from urllib import robotparser
from urllib.parse import urljoin, urlparse

from sqlalchemy.orm import Session

from ..config import get_settings
from ..projects import detect_project
from . import knowledge_base


def ingest(db: Session, request_urls: list[str] | None = None) -> int:
    """Ingest published FOI Q&A from WhatDoTheyKnow. Returns the number stored.

    If ``request_urls`` is given, those exact request pages are ingested
    (manifest mode). Otherwise the authority's request index is read and its
    requests are followed up to ``whatdotheyknow_max_requests`` (discover mode).
    """
    s = get_settings()
    if not (s.ingest_enabled and s.ingest_whatdotheyknow):
        raise RuntimeError(
            "WhatDoTheyKnow ingestion is disabled. Set FOI_INGEST_ENABLED=true "
            "and FOI_INGEST_WHATDOTHEYKNOW=true to enable."
        )

    import httpx  # lazy optional deps
    from bs4 import BeautifulSoup

    base = s.whatdotheyknow_base_url.rstrip("/")
    headers = {"User-Agent": s.ingest_user_agent}
    ingested = 0

    with httpx.Client(headers=headers, timeout=20, follow_redirects=True) as client:
        robots = _load_robots(client, base, s.ingest_user_agent)
        delay = max(1.0, robots.crawl_delay(s.ingest_user_agent) or 0.0)

        urls = request_urls or _discover_requests(
            client, BeautifulSoup, base, s.whatdotheyknow_authority,
            s.whatdotheyknow_max_requests, s.ingest_user_agent, robots, delay)

        for url in urls[: s.whatdotheyknow_max_requests]:
            if not robots.can_fetch(s.ingest_user_agent, url):
                continue
            title, text = _fetch_request(client, BeautifulSoup, url)
            if text and len(text) >= 200:
                knowledge_base.upsert(
                    db, source="published_response", title=title, content=text,
                    url=url, project=detect_project(f"{title} {text}"),
                    status="approved")
                ingested += 1
                db.commit()
            time.sleep(delay)

    return ingested


def _discover_requests(client, BeautifulSoup, base: str, authority: str,
                       limit: int, user_agent: str, robots, delay: float) -> list[str]:
    """Collect request-page URLs from the authority's index, following the
    paginated list until we have ``limit`` of them."""
    found: list[str] = []
    seen: set[str] = set()
    page = 1
    while len(found) < limit and page <= 10:
        index_url = f"{base}/body/{authority}?page={page}"
        if not robots.can_fetch(user_agent, index_url):
            break
        try:
            resp = client.get(index_url)
        except Exception:
            break
        if resp.status_code != 200:
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        new = 0
        for a in soup.find_all("a", href=True):
            href = urljoin(base, a["href"])
            # Request pages look like /request/<url_title>; skip anchors/sub-paths.
            p = urlparse(href)
            if p.netloc != urlparse(base).netloc:
                continue
            parts = [seg for seg in p.path.split("/") if seg]
            if len(parts) == 2 and parts[0] == "request" and href not in seen:
                seen.add(href)
                found.append(href)
                new += 1
        if not new:
            break
        page += 1
        time.sleep(delay)
    return found


def _fetch_request(client, BeautifulSoup, url: str) -> tuple[str, str]:
    """Return (title, readable text) for a single request page.

    Extracts the request title and the full correspondence thread (request +
    the authority's response), stripping site chrome. Returns ('', '') on any
    failure so the caller skips it rather than aborting the run."""
    try:
        resp = client.get(url)
    except Exception:
        return "", ""
    if resp.status_code != 200:
        return "", ""
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "form"]):
        tag.decompose()

    h1 = soup.find("h1")
    title = (h1.get_text(" ").strip() if h1 else "") or url

    # Prefer the correspondence container Alaveteli renders; fall back to body.
    container = soup.select_one("#correspondence") or soup.body or soup
    text = " ".join(container.get_text(" ").split())
    return title[:200], text


def _load_robots(client, base: str, user_agent: str) -> robotparser.RobotFileParser:
    rp = robotparser.RobotFileParser()
    try:
        resp = client.get(urljoin(base, "/robots.txt"))
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
            return rp
    except Exception:
        pass
    rp.allow_all = True
    return rp
