# Oxfordshire demo — runbook

This configures the FOI system for an **Oxfordshire County Council** demo: it
crawls the council website (branching out to the ZEZ / traffic-filter / LTN
scheme sites), ingests published FOI Q&A from two sources, holds private
department/project uploads behind a **review gate**, and drafts responses with a
**local on-prem model** (Ollama).

## 1. Quick start (offline, no network or model needed)

```bash
python -m app.seed          # loads the curated Oxfordshire corpus + a worked case
uvicorn app.main:app --reload
```

The seed ships:
- **6 council website pages** (traffic filters, ZEZ, LTNs, potholes, waste, schools), scheme pages tagged with a `project`.
- **4 published FOI precedents** (`sample_data/oxfordshire/published_foi/`) — traffic-filter exemptions, ZEZ PCNs/revenue, LTN fines, consultation responses.
- **2 pending department uploads** sitting in the review queue (a Highways draft note and a Parking Services PII extract) — present but **not** retrievable.

A worked case (`FOI/2026/00001`, an Oxford traffic-filters request) is auto-drafted from the website + published FOI precedents so the pipeline is demonstrable from first load.

## 2. The review gate (the new capability)

Public sources (website, published FOI) are auto-`approved`. **Private uploads
are `pending_review` and invisible to retrieval until a reviewer approves them**,
so unvetted internal material can never leak into a published response.

**In the web UI** (Knowledge base panel): uploads now carry an optional
**Project / scheme** field and land marked `pending review`. An admin reviewer
sees an **"⏳ Awaiting review"** queue (grouped by contributing department) with
**Approve** / **Reject** on each item; approving makes it searchable (and indexes
it in semantic mode). Every document row shows source + status + project pills.
The two seeded department uploads appear here on first load.

A **"Knowledge by department & scheme"** breakdown sits above the queue: schemes
are grouped under their **owning department** (from `FOI_PROJECT_CATALOG`), each
with doc and pending counts — e.g. *Highways & Transport → Traffic filters, Low
Traffic Neighbourhoods* and *Environment & Climate → Zero Emission Zone*.
Clicking a scheme filters the document list to that scheme; untagged council
pages collect under *General council information*.

| Action | Endpoint |
|---|---|
| Upload (paste) — lands pending | `POST /admin/knowledge-base/docs` (`project` optional) |
| Upload (file) — lands pending | `POST /admin/knowledge-base/upload` (`project` form field) |
| See the review queue | `GET /admin/knowledge-base/pending` |
| Approve (makes it retrievable) | `POST /admin/knowledge-base/docs/{id}/approve` |
| Reject (kept for audit, never retrieved) | `POST /admin/knowledge-base/docs/{id}/reject` |

KB stats (`GET /admin/knowledge-base`) now report `approved` / `pending_review` / `rejected` counts.

## 3. Live website crawl (branches out to scheme sites)

Enable ingestion for the run, then trigger the crawl:

```bash
FOI_INGEST_ENABLED=true FOI_INGEST_WEBSITE=true python - <<'PY'
from app.database import SessionLocal, init_db
from app.ingestion import website_crawler
init_db(); db = SessionLocal()
print("ingested", website_crawler.crawl(db))
PY
```

- Roots at `www.oxfordshire.gov.uk`, queues the **priority scheme seeds** first (`FOI_CRAWL_PROJECT_SEEDS`), and may follow links into the **allow-listed related domains** (`FOI_CRAWL_RELATED_DOMAINS` — the city council's ZEZ pages, the consultation hubs) when `FOI_CRAWL_FOLLOW_RELATED=true`. It will **not** wander the open web.
- Pages under a scheme seed are tagged with that `project`.
- robots.txt + crawl-delay are honoured; cap is `FOI_INGEST_CRAWL_MAX_PAGES`.

## 4. Published FOI Q&A — both sources

- **Disclosure log** (the council's own): `POST /admin/ingest/published-responses` (feed export, or `FOI_PUBLISHED_RESPONSES_FETCH_MODE=browser` for the JS portal).
- **WhatDoTheyKnow** (mySociety archive — richest for Oxford traffic filters/ZEZ):
  ```bash
  FOI_INGEST_ENABLED=true FOI_INGEST_WHATDOTHEYKNOW=true \
    curl -X POST localhost:8000/admin/ingest/whatdotheyknow
  ```
  Pulls request+response pairs for `FOI_WHATDOTHEYKNOW_AUTHORITY` (`oxfordshire_county_council`), capped by `FOI_WHATDOTHEYKNOW_MAX_REQUESTS`, robots-aware. Both sources store as `published_response`, so drafting treats them identically.

## 5. Local model drafting (Ollama — data stays on-prem)

```bash
ollama serve &
ollama pull qwen2.5:7b-instruct
export FOI_LLM_PROVIDER=ollama FOI_LLM_MODEL=qwen2.5:7b-instruct
```

With a model configured, drafting pulls the top sources per question and
**synthesises a grounded, cite-or-refuse answer** across the website and published
FOI precedents (refuses rather than guessing when the corpus lacks the answer).
Nothing leaves the box. With no model set (`stub`), drafting falls back to the
offline template assembly shown in the quick start. An unreachable model also
degrades gracefully to the template, so a demo never hard-fails.

## 6. Schemes on the cases themselves

Triage now **tags each incoming FOI request with its scheme** (traffic-filters /
zez / ltn) using the same catalogue (`FOI_PROJECT_CATALOG`). Detection prefers the
longest matching keyword, so an LTN request that mentions "traffic filters" is
still tagged `ltn` (see `app/projects.py` — the one detector shared by triage,
ingestion and seeding).

In the UI this surfaces as a **scheme pill on each case row**, a **Scheme line in
the case detail**, and a **"By scheme" analytics chart** that — like By
department — **cross-filters the case queue** when you click a bar.

When any scheme-tagged case is past its statutory deadline, a red
**overdue-by-scheme alert banner** appears at the top of the dashboard — e.g.
*"⚠ 1 overdue case is past the statutory deadline by scheme: [Traffic filters 1]"*
— and each scheme chip clicks through to that scheme's cases in the queue.

A **"Scheme SLA performance"** table sits under the charts: per scheme it shows
cases, open, **overdue** (open and past the statutory deadline), closed,
**breach rate** (closed-late + currently-overdue ÷ total), **on-time %** (of
resolved cases) and **average working days to close**. Rows are clickable to
filter the queue. The seed spreads demo cases across SLA states so it reads, for
example, *ZEZ 50% breach / 0% on-time / 29d* and *Traffic filters 33% breach,
1 overdue / 100% on-time / 8d*.

## 7. Key settings (all `FOI_`-prefixed, see `app/config.py`)

`COUNCIL_NAME`, `COUNCIL_WEBSITE_ROOT`, `DISCLOSURE_LOG_URL`,
`CRAWL_FOLLOW_RELATED`, `CRAWL_RELATED_DOMAINS`, `CRAWL_PROJECT_SEEDS`,
`INGEST_WHATDOTHEYKNOW`, `WHATDOTHEYKNOW_AUTHORITY`, `LLM_PROVIDER`, `LLM_MODEL`,
`OLLAMA_BASE_URL`, `RETRIEVAL_PROVIDER` (`semantic` for local embeddings).
