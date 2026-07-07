# Oxfordshire County Council — FOI Response System

A production-oriented backend that implements the eight-stage FOI workflow from
the architecture document: **intake → triage → retrieval & auto-draft → (human
review) → update → compliance gate → sign-off → dispatch**.

AI assists with classification, retrieval and drafting; **humans authorise every
disclosure** (approval, sign-off and dispatch are explicit human actions). The
system runs fully offline by default — no external calls, no API keys — so you
can explore the whole pipeline immediately. The "train on public data" capability
(Phase 0 ingestion) is built in but switched **off** by default; turn it on later.

> Built as a first slice to continue in Claude Code. It is deliberately small,
> readable and well-separated so each part can be extended independently.

## Quick start

```bash
cd foi-system
python -m venv .venv && source .venv/bin/activate     # optional
pip install -r requirements.txt

# 1) See the whole pipeline run end-to-end with sample data. This also seeds a
#    spread of demo cases across stages and SLA bands (overdue / due-soon /
#    amber / awaiting-clarification / closed) so the dashboard is populated:
python -m app.seed

# 2) Run the app:
uvicorn app.main:app --reload
# then open:
#   http://127.0.0.1:8000/        <- the caseworker web UI (the real application)
#   http://127.0.0.1:8000/docs    <- the API / Swagger test interface

# 3) Run the tests:
pytest
```

### Signing in (authentication & roles)

The app requires login and enforces **role-based access with separation of duties**.
On first run it seeds starter accounts (password = username — **change these in
production**):

| Username | Role | Can do |
|----------|------|--------|
| `caseworker` | Caseworker | intake, triage, draft, SME update, run compliance checks |
| `manager` | Department manager | approve (or request changes) |
| `legal` | Legal & Information Governance | final sign-off |
| `foi` | FOI team | mailbox intake + dispatch |
| `highways` | Subject department (example) | contribute/upload documents to the knowledge base — **no case access** |
| `admin` | Administrator | everything + ingestion / reindex / user administration |

Every audit-trail entry records the **actual signed-in user** — the actor is taken
from the session, never the request body, so the trail is trustworthy for ICO
purposes. Passwords are salted PBKDF2 (stdlib); sessions are server-side with an
HttpOnly cookie. To use council single sign-on (Entra / SAML / OIDC), implement the
clearly-marked seam in `app/auth.py` and keep the same roles/audit. Set
`FOI_SEED_DEFAULT_USERS=false` once real accounts exist.

### Using the web UI

This is an **internal caseworker tool**, not a public-facing portal. FOI requests
arrive in a **dedicated mailbox** (`foi@oxfordshire.gov.uk` by default); the UI
is where the Information Governance Unit triages that mailbox and drives each
request through the workflow.

Open http://127.0.0.1:8000/ after starting the server:

1. **FOI inbox** (top-left) — click **Check inbox** to pull newly-arrived emails.
   Click a message to read it in full, then **Log as FOI case** (this performs
   Stage 1 intake and starts the statutory clock) or **Dismiss** if it is
   misdirected/spam. Imported messages link straight to their case.
2. **Case queue** — click any case to open it on the right and drive it through
   every stage: run triage, generate the draft, handle department review, run the
   compliance checks, approve, sign off and dispatch. The draft letter, an
   eight-step stage tracker, the SLA countdown and the full audit trail update as
   you go. The audit trail records that the case was `intake:from_inbox`.
3. **Log manually** (collapsed) — a fallback for requests that arrive by post,
   phone or in person rather than in the mailbox.

The mailbox runs on an **offline stub** by default (seeded with realistic FOI
emails, no credentials needed). To connect a real mailbox — IMAP, Microsoft 365
or Gmail — implement the clearly-marked seam in `app/services/inbox.py` and set
`FOI_INBOX_PROVIDER` (see `.env.example`).

> Tip: run `python -m app.seed` once first so the knowledge base has content for
> the drafting step to ground on.

## How a request flows (API)

| Step | Endpoint | Stage |
|------|----------|-------|
| Check the FOI mailbox | `POST /inbox/poll` | (intake source) |
| List mailbox messages | `GET /inbox` | (intake source) |
| Log a message as a case | `POST /inbox/{id}/import` | → 1 Intake |
| Dismiss a message | `POST /inbox/{id}/dismiss` | (intake source) |
| Register a request (manual) | `POST /requests` | 1 Intake |
| Classify it | `POST /requests/{id}/triage` | 2 Triage |
| Retrieve + draft | `POST /requests/{id}/autodraft` | 3 Draft (auto-routes) |
| SME supplies missing info | `POST /requests/{id}/sme-update` | 4 & 5 Review/Update |
| Compliance checks | `POST /requests/{id}/compliance` | 6 Gate |
| Manager approves | `POST /requests/{id}/approve` | 6 → 7 |
| Final sign-off | `POST /requests/{id}/sign-off` | 7 |
| FOI team dispatches | `POST /requests/{id}/dispatch` | 8 → Closed |
| Ask requester to clarify | `POST /requests/{id}/request-clarification` | → Awaiting clarification (clock paused) |
| Record clarification | `POST /requests/{id}/provide-clarification` | → 2 Triage (clock resumes) |
| Requester requests review | `POST /requests/{id}/internal-review` | Closed → Internal review |
| Conclude review | `POST /requests/{id}/internal-review/complete` | uphold → Closed · revise → 4 Review |
| SLA status | `GET /requests/{id}/sla` | (any) |

At Stage 3 the system decides automatically: if the draft is confidently grounded
it proceeds to the compliance gate; otherwise it routes to **department human
review**, exactly as in the architecture diagram.

**Clarification (clock pause).** Before drafting, if a request is unclear or too
broad, a caseworker can ask the requester to clarify (FOIA s.1(3)). This moves the
case to *awaiting clarification* and **stops the 20-working-day clock**; when the
clarification is recorded the clock resumes and the deadline is pushed out by the
working days spent waiting. `GET /sla` reports `paused` and `working_days_paused`.

**Internal review.** A closed case can be reopened for an internal review if the
requester is dissatisfied. The review either **upholds** the original (re-closes)
or sends it back to be **revised** (re-enters department review → the normal
sign-off and dispatch path).

## Project layout

```
app/
  config.py                 settings + feature flags (statutory params, house-style facts)
  enums.py                  stages, regimes, statuses
  database.py  models.py    single case record + drafts, exemptions, audit, knowledge docs
  sla.py                    20-working-day clock (England & Wales bank holidays)
  workflow.py               the eight-stage state machine (legal transitions only)
  audit.py                  audit-trail helper
  templates/
    response_template.py    the council house-style letter (the format to follow)
  services/
    triage.py               regime + department + risk classification (s.12 / s.14)
    drafting.py             assembles a grounded draft + confidence score
    compliance.py           Stage 6 pre-approval checks (statute, confirm/deny, PII, rights)
    casework.py             orchestrates the stages (called by the API)
    inbox.py                dedicated FOI mailbox: pluggable provider (offline "stub") + poll/import/dismiss
    retrieval.py            keyword ranker + semantic (embeddings) retrieval — same interface
    embedding.py            pluggable local embedder (offline fastembed/ONNX; no data egress)
    chunking.py             split docs into passages (Q&A pairs / paragraph windows)
    kb_refresh.py           keep the KB current: re-ingest + reindex, staleness + best-effort
    llm.py                  pluggable LLM provider (offline "stub" by default)
  ingestion/                Phase 0 — OFF by default
    website_crawler.py      Source A: crawl the council website (HTML pages + linked PDFs)
    published_responses.py  Source B: feed export (preferred) or browser render of the log
    documents.py            extract text from uploaded files (PDF/Word/text/HTML)
    knowledge_base.py       store operations
  reindex.py                build the semantic chunk index (FOI_RETRIEVAL_PROVIDER=semantic)
  fetch_model.py            pre-download/vendor the embedding model for air-gap
  refresh.py                CLI for the weekly public-info refresh (run from cron)
  auth.py                   login, sessions, roles & capabilities (offline; SSO seam)
  routers/                  FastAPI endpoints (auth.py, requests.py, inbox.py, admin.py)
  main.py                   app entry point
tests/                      pytest: sla, workflow, drafting, full API lifecycle
```

## Phase 0 — training on public data (optional, later)

Off by default. When you are ready to seed the knowledge base from public sources,
set the flags in `.env` (see `.env.example`):

```bash
FOI_INGEST_ENABLED=true
FOI_INGEST_WEBSITE=true                  # crawl the council website
FOI_INGEST_PUBLISHED_RESPONSES=true      # ingest published FOI responses
FOI_PUBLISHED_RESPONSES_FETCH_MODE=feed  # 'feed' (preferred) or 'browser'
```

Then trigger ingestion:

```bash
# Website (Source A) — seeded from the site's sitemap.xml (content pages, not
# nav), honouring robots.txt + crawl-delay, with a content-quality floor that
# skips thin/stub pages. Tracking params (utm_*, gclid, ...) are stripped so each
# page is stored once.
curl -X POST "http://127.0.0.1:8000/admin/ingest/website?max_pages=50"

# Published responses (Source B) — 'feed' mode reads a folder of exported letters.
# A set of realistic sample exports ships in sample_data/published_responses/:
curl -X POST "http://127.0.0.1:8000/admin/ingest/published-responses?feed_dir=sample_data/published_responses"

# Knowledge-base stats
curl "http://127.0.0.1:8000/admin/knowledge-base"
```

Two fetch modes for published responses:

- **feed (preferred):** read a folder of exported response letters (`.txt/.md/.html`).
  The council disclosure log is a JavaScript portal (iCasework), so the most
  reliable, lowest-risk route is a data export agreed with the Information
  Governance Unit — no scraping required.
- **browser:** render the live portal with Playwright
  (`pip install playwright && playwright install chromium`). The selectors in
  `published_responses.py` are placeholders to confirm against the live site.

Only public, already-disclosed information is ingested at this stage. Internal
systems (SharePoint/EDRMS, case and finance) are a later phase.

## Semantic retrieval (offline, optional)

By default, retrieval uses a dependency-free keyword ranker. It matches on shared
words, so it misses paraphrases — a request about "potholes reported" won't match
a precedent answer that says "carriageway defect reports". Semantic retrieval
fixes that by matching on **meaning**, using a **local embedding model**.

It runs **fully on the council's own infrastructure — no data egress.** The model
is `fastembed` (an ONNX model on CPU, no PyTorch); there are no API calls at
inference. The model files are fetched once and can be **vendored for air-gapped
deployments** (copy the model cache directory onto the target machine).

```bash
# 1) Install the optional deps (only needed for semantic mode):
pip install fastembed numpy

# 2) Turn it on and build the vector index from the knowledge base:
export FOI_RETRIEVAL_PROVIDER=semantic
python -m app.reindex          # chunks every doc into passages and embeds them
#   (or POST /admin/reindex while the server is running)

# 3) Check coverage:
curl "http://127.0.0.1:8000/admin/knowledge-base"   # shows indexed_chunks
```

Docs are embedded as **passages** (Q&A pairs for published responses, paragraph
windows for web pages), so the matched passage both ranks the document and becomes
the drafted answer. Re-run `python -m app.reindex` after ingesting new content.

> **Similarity is not answer-presence — calibrate conservatively.** Cosine
> similarity measures *topical* relevance, not whether the requested figure is
> actually in the document. On the sample KB, a request for a number that isn't
> published ("safeguarding referrals last month") still matched a topical page at
> ~0.75 — as high as a genuinely answerable request. A single score can't separate
> the two, so semantic mode uses a deliberately high Stage-3 threshold
> (`FOI_SEMANTIC_CONFIDENCE_THRESHOLD`, default 0.80) to auto-proceed only the
> strongest matches and route everything else to **department review**, plus a
> relevance floor (`FOI_SEMANTIC_RELEVANCE_FLOOR`, default 0.35) below which there
> is no grounding at all. The human review + compliance gate remain the real
> safeguards. Re-measure both on your own knowledge base.

## Department document contributions

Much of what answers an FOI request is **not published on the website** — it sits
with the responsible service as a report, spreadsheet export or letter. So each
subject department gets its **own login** and can **upload those documents** into
the knowledge base, where the drafter grounds on them like any other source.

- **Accounts.** An admin creates a department account in the **Accounts** panel
  (or `POST /admin/users` with `role=department` and a `department` name). The
  example `highways` starter account shows the experience.
- **Scope (data minimisation).** A department user holds the `contribute`
  capability **only** — deliberately *not* `read`. They see a knowledge-base
  contribution page and nothing else: no FOI cases, no requester personal data.
  The backend enforces this (case endpoints return 403 for them), not just the UI.
- **Upload.** Paste text, or upload a **PDF / Word (.docx) / text / HTML** file;
  its text is extracted (`app/ingestion/documents.py`) and stored as a
  `department` source, tagged with the uploader and department for provenance.
  Image-only/scanned PDFs are rejected (no OCR). In semantic mode the upload is
  indexed immediately.

```bash
# Admin creates a department account, then that user uploads a document:
curl -X POST .../admin/users -H 'Content-Type: application/json' \
  -d '{"username":"childrens","password":"…","role":"department","department":"Children'\''s Services"}'
curl -X POST .../admin/knowledge-base/upload -F "file=@report.pdf" -F "title=Children in care 2025"
```

## Keeping public information current (auto-refresh)

Ingestion (above) is a one-off pull. To keep the knowledge base current, the
system can **refresh public information on a schedule and/or before drafting** —
re-running the website crawl and published-response feed and rebuilding the
semantic index, in one step.

It is **off by default and best-effort**: it only ever touches *public* data,
no case data leaves the machine, and a slow or unreachable source is recorded
and skipped — a refresh never blocks intake, drafting or dispatch. Each run is
recorded (a `KnowledgeRefresh` row) so the UI can show when the KB was last
updated and whether it is stale.

```bash
# Enable, and pick the triggers you want (see .env.example):
FOI_KB_REFRESH_ENABLED=true
FOI_KB_REFRESH_MAX_AGE_DAYS=7      # treat the KB as "stale" after this many days
FOI_KB_REFRESH_ON_DRAFT=true      # refresh (only if stale) before drafting a response

# The refresh re-runs whichever sources are enabled, so turn those on too:
FOI_INGEST_ENABLED=true
FOI_INGEST_WEBSITE=true
FOI_INGEST_PUBLISHED_RESPONSES=true
FOI_PUBLISHED_RESPONSES_FEED_DIR=sample_data/published_responses   # for feed mode
```

**Weekly trigger — cron / systemd timer (recommended).** A scheduled job runs
the CLI, which forces a refresh and exits non-zero on error so the timer surfaces
failures:

```bash
python -m app.refresh
```

Example crontab (06:30 every Monday):

```cron
30 6 * * 1  cd /opt/foi && . .venv/bin/activate && python -m app.refresh
```

**Before-drafting trigger.** With `FOI_KB_REFRESH_ON_DRAFT=true`, Stage 3 checks
freshness at the start of `autodraft` and refreshes first **only if the KB is
stale** — so a new response is grounded on up-to-date information without slowing
down every draft. The refresh is logged against the case (`kb:refreshed`).

**Manual / status (admin).** The admin **Knowledge base** panel shows a
"last updated · current/stale" line with a **Refresh now** button, backed by:

```bash
curl "http://127.0.0.1:8000/admin/knowledge-base/refresh"          # status + recent history
curl -X POST "http://127.0.0.1:8000/admin/knowledge-base/refresh"  # force a refresh now
```

> Refresh is network-bound, so it stays off until a deployment opts in, and it
> no-ops safely (status `skipped`) if the `FOI_INGEST_*` flags are off. The
> weekly trigger is an **external** cron/timer, not an in-process scheduler, so
> it behaves correctly behind multiple workers.

## Plugging in a real LLM

Drafting is template-driven and deterministic by default. To use a model for
richer drafting or summaries, set `FOI_LLM_PROVIDER=anthropic` and implement the
call in `app/services/llm.py` (a clearly marked stub is ready for it). The house
style and compliance checks remain in force regardless of the model used.

## Deployment

For running the heavy public-information crawl on a server (and optionally
hosting the app), see **[docs/deploy-digitalocean.md](docs/deploy-digitalocean.md)**
— droplet setup, vendoring the model, systemd + nginx/TLS, the weekly crawl cron,
and the information-governance split between a public-data **build box** and
hosting live casework (which needs IG sign-off).

## Compliance notes

- Statutory deadline: 20 working days from receipt (`FOIA s.10`); SLA flags at
  day 12 (amber) and day 17 (red); a public-interest extension field is modelled.
- The compliance gate checks statute cited, confirm-or-deny, re-use clause,
  internal-review + ICO wording, and scans for obvious personal data.
- This is a first implementation, not legal advice; exemption and public-interest
  decisions remain human judgements recorded against the case.
```

## License

This project is **source-available, not open source**. It is licensed under the
**[PolyForm Noncommercial License 1.0.0](LICENSE.md)** — free to use, modify and
share for **noncommercial purposes** (personal, research, education, and use by
government/charitable/nonprofit organisations).

**Commercial use requires a separate paid licence.** If you want to use this in a
commercial product or service, or a for-profit company's operations, contact
**arafatta.8583@gmail.com** to arrange a commercial licence. See [LICENSE.md](LICENSE.md).
