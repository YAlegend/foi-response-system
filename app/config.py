"""Application configuration and feature flags.

All behaviour that touches external systems or AI providers is controlled by
flags here so the system can run fully offline by default. The data-training /
ingestion capability (Phase 0) is OFF by default and can be switched on later
without code changes — exactly the "build production first, train later" plan.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FOI_", env_file=".env", extra="ignore")

    # --- Core ---
    app_name: str = "Oxfordshire County Council FOI Response System"
    environment: str = "local"
    database_url: str = "sqlite:///./foi.db"

    # --- Statutory parameters (FOIA 2000) ---
    statutory_working_days: int = 20          # s.10 deadline
    public_interest_extension_days: int = 20  # permitted PIT extension
    sla_amber_day: int = 12                   # dashboard amber alert
    sla_red_day: int = 17                     # dashboard red alert / escalation

    # --- Council house-style facts (used by the drafting templates) ---
    responding_team: str = "Information Management Team"
    council_name: str = "Oxfordshire County Council"
    council_address: str = (
        "Information Management Team, Oxfordshire County Council, "
        "County Hall, New Road, Oxford, OX1 1ND"
    )
    ico_address: str = (
        "Information Commissioner's Office, Wycliffe House, Water Lane, "
        "Wilmslow, Cheshire, SK9 5AF"
    )

    # --- Breach-trend notifications (default OFF; no egress until configured) ---
    # When a scheme starts deteriorating (breaches trending up — see
    # app/services/notifications.py), notify a distribution list. Triggered
    # deliberately via `python -m app.notify` (cron) or POST /admin/notifications/
    # run — never on a page load. Idempotent: a scheme already in an alerted state
    # is not emailed again until it recovers and deteriorates anew.
    #   "stub" -> record the message only, no send (offline default, safe in tests)
    #   "smtp" -> send via SMTP (wire host/credentials below)
    notify_enabled: bool = False
    notify_provider: str = "stub"
    notify_recipients: str = ""        # comma-separated, e.g. "ig@oxfordshire.gov.uk"
    notify_from: str = "foi-no-reply@oxfordshire.gov.uk"
    # Named responsible officers. Built-in demo names are used when this is blank;
    # override as "Department=Name <email>; Department=Name <email>".
    responsible_officers: str = ""
    foi_officer_name: str = "Jordan Ellis"     # central FOI/IG officer (the fallback owner)
    foi_officer_email: str = ""                # blank -> falls back to notify_recipients
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True

    # --- Per-department SLA digest (default OFF) ---
    # A periodic summary emailed to each owning department: open / overdue /
    # due-soon counts, breach rate, on-time %, and any deteriorating schemes among
    # their cases. Periodic (once per ISO week per department), not event-driven —
    # run from cron via `python -m app.digest`. Uses the same provider/SMTP as the
    # alerts above. Recipients are routed per department; departments with no
    # mapping fall back to FOI_NOTIFY_RECIPIENTS (the central IG list).
    digest_enabled: bool = False
    # "Department=email" pairs, comma-separated, e.g.
    #   "Highways=highways@oxfordshire.gov.uk, Environment=env@oxfordshire.gov.uk"
    digest_recipients: str = ""

    # --- AI provider (pluggable) ---
    # "stub"    -> deterministic template drafting, no external calls (default)
    # "ollama"  -> local on-prem model; nothing leaves the council network
    #              (data-sovereignty posture for the gov client). Needs Ollama
    #              running and the model pulled: `ollama pull <llm_model>`.
    # "anthropic"/"openai" -> wire a real cloud client in app/services/llm.py
    #              (acceptable for the all-public demo corpus; NOT for real case
    #              data without an IG ruling).
    llm_provider: str = "stub"
    llm_model: str = "qwen2.5:7b-instruct"     # used by the ollama provider
    llm_api_key: str = ""
    # Where the local Ollama daemon listens (default install). On the demo box
    # this is the only "model endpoint" and it stays inside the deployment.
    ollama_base_url: str = "http://localhost:11434"
    # Cap the grounded context handed to the model per question so a long page
    # can't blow the prompt budget (mirrors the onprem-rag source-char cap).
    llm_max_tokens: int = 1200
    llm_source_char_cap: int = 2400

    # --- Dedicated FOI inbox (pluggable) ---
    # Requests arrive in a monitored mailbox, not a public form. The provider is
    # swappable; it defaults to an offline stub so the system runs with no auth.
    #   "stub"          -> offline simulated mailbox seeded with sample FOI emails
    #   "imap" / "microsoft365" / "gmail" -> wire a real connector in
    #                      app/services/inbox.py (clearly-marked seam provided)
    inbox_provider: str = "stub"
    inbox_address: str = "foi@oxfordshire.gov.uk"

    # --- Confidence threshold for routing to human review (Stage 3 decision) ---
    # NB: keyword and semantic scores distribute differently. Keep 0.6 for the
    # keyword ranker; the semantic default below is calibrated separately.
    auto_draft_confidence_threshold: float = 0.6

    # --- Authentication / sessions ---
    # Built-in username/password by default (offline). Replace/front with council
    # SSO (Entra/SAML) at the seam in app/auth.py. Default starter accounts are
    # seeded on first run and MUST be changed — see README.
    session_cookie_name: str = "foi_session"
    session_ttl_hours: int = 12
    session_cookie_secure: bool = False        # set true behind HTTPS in production
    seed_default_users: bool = True            # create starter accounts if none exist

    # --- Demo mode (no login) ---
    # When true, unauthenticated visitors are transparently treated as
    # `demo_username` so the public demo opens straight on the dashboard with no
    # sign-in. NEVER enable for a real deployment — it grants that user's role to
    # anyone who can reach the URL. Off by default.
    demo_mode: bool = False
    demo_username: str = "admin"

    # --- Retrieval (pluggable) ---
    # "keyword"  -> offline token-overlap ranker, no dependencies (default)
    # "semantic" -> local embeddings + cosine search (offline, no data egress);
    #               needs the optional deps (fastembed, numpy) and a built index
    #               (run `python -m app.reindex`). See app/services/embedding.py.
    retrieval_provider: str = "keyword"
    embedding_model: str = "BAAI/bge-small-en-v1.5"   # fastembed model id (ONNX)
    embedding_dim: int = 384                           # bge-small-en-v1.5 dimension
    # Where fastembed caches/loads the ONNX model. Project-local by default so the
    # model can be vendored and shipped with the deployment artifact for air-gapped
    # council infrastructure — no runtime HuggingFace download. Pre-fetch while
    # online with `python -m app.reindex` (or `python -m app.fetch_model`); the
    # files left under this dir are all semantic mode needs offline.
    embedding_cache_dir: str = "./models/fastembed"
    # Hard air-gap switch: when true, forbid ANY network fetch at model load
    # (sets HF_HUB_OFFLINE). The model MUST already be in embedding_cache_dir or
    # load fails fast instead of silently hanging on a download.
    embedding_offline: bool = False
    # Drop semantic hits whose cosine similarity is below this — "no good match"
    # then yields no hit, so the case routes to a human instead of grounding on
    # something irrelevant.
    semantic_relevance_floor: float = 0.35
    # Stage-3 routing threshold when retrieval_provider == "semantic".
    # Calibrated on the sample KB: strong answer-bearing matches score ~0.86-0.89,
    # while a merely topical match (relevant page, but the specific figure isn't
    # there) can score ~0.75. Cosine similarity measures topical relevance, NOT
    # whether the answer exists — so this is set high to auto-proceed only the
    # strongest matches and route everything else to department review. Bias is
    # deliberately toward a human; the gate + review remain the real safeguards.
    semantic_confidence_threshold: float = 0.80

    # --- Phase 0 ingestion feature flags (default OFF) ---
    ingest_enabled: bool = False
    ingest_website: bool = False
    ingest_published_responses: bool = False
    # How published responses are fetched once enabled:
    #   "browser" -> render the JS disclosure-log portal (needs Playwright)
    #   "feed"    -> a data feed/export agreed with the IGU (preferred)
    published_responses_fetch_mode: str = "feed"
    council_website_root: str = "https://www.oxfordshire.gov.uk/"
    disclosure_log_url: str = (
        "https://public.oxfordshire.gov.uk/freedom-of-information-disclosure-log"
    )
    # Local directory of exported published responses for "feed" mode, so the
    # automated refresh can re-ingest them without a path passed by hand.
    # Ships with a curated Oxfordshire FOI set so the demo works offline.
    published_responses_feed_dir: str | None = "./sample_data/oxfordshire/published_foi"
    # Full-site crawl, capped so a single run stays bounded. Oxfordshire's site is
    # large; the priority seeds below are queued first so demo-relevant scheme
    # pages (traffic filters, ZEZ) are ingested even if the cap is hit.
    ingest_crawl_max_pages: int = 1500

    # --- Cross-site crawl: follow a scheme out to its own project sites ---------
    # The main council site links out to dedicated scheme / consultation domains
    # (the ZEZ is run jointly with the city council; consultations live on a
    # separate hub). With crawl_follow_related on, the crawler may follow links
    # into these allow-listed domains too — but ONLY these, so it can never wander
    # the open web. Each entry is an exact host. Subdomains must be listed explicitly.
    crawl_follow_related: bool = True
    crawl_related_domains: list[str] = [
        "www.oxford.gov.uk",            # Oxford City Council (joint ZEZ operator)
        "oxford.gov.uk",
        "letstalk.oxfordshire.gov.uk",  # Oxfordshire consultation hub
        "yourvoice.oxford.gov.uk",      # Oxford City consultation hub
        "consultations.oxfordshire.gov.uk",
    ]
    # Priority seed pages, queued before the sitemap so the schemes the demo is
    # about land first, and tagged with their project so retrieval can scope to
    # one scheme. URL prefixes are also used to label any crawled page beneath
    # them with that project. Best-effort: a 404 seed is skipped, not fatal.
    crawl_project_seeds: list[dict] = [
        {"project": "traffic-filters",
         "url": "https://www.oxfordshire.gov.uk/residents/roads-and-transport/connecting-oxfordshire/traffic-filters"},
        {"project": "zez",
         "url": "https://www.oxford.gov.uk/zez"},
        {"project": "zez",
         "url": "https://www.oxfordshire.gov.uk/residents/roads-and-transport/connecting-oxfordshire/zero-emission-zone"},
        {"project": "ltn",
         "url": "https://www.oxfordshire.gov.uk/residents/roads-and-transport/connecting-oxfordshire/low-traffic-neighbourhoods"},
    ]

    # Scheme/project catalogue: which department owns each scheme. Used to group
    # the knowledge base by department in the UI — a website page or upload tagged
    # with a project key is shown under that project's owning department. Keys
    # match crawl_project_seeds / the project tags written at ingest time.
    # `keywords` drive scheme detection (triage, ingestion tagging). Detection
    # prefers the longest matching keyword, so an LTN item that also says
    # "traffic filters" is still tagged ltn (see app/projects.py).
    project_catalog: list[dict] = [
        {"key": "traffic-filters", "label": "Traffic filters",
         "department": "Highways & Transport",
         "keywords": ["traffic filter", "bus gate"]},
        {"key": "zez", "label": "Zero Emission Zone",
         "department": "Environment & Climate",
         "keywords": ["zero emission zone", "zez"]},
        {"key": "ltn", "label": "Low Traffic Neighbourhoods",
         "department": "Highways & Transport",
         "keywords": ["low traffic neighbourhood", "low-traffic neighbourhood", "ltn"]},
    ]

    # --- WhatDoTheyKnow (mySociety public FOI archive) ingestion ---------------
    # Source for already-published FOI Q&A, in addition to the council's own
    # disclosure log. Pulls request+response pairs for the authority via the
    # per-request JSON the site exposes. Gated by ingest_whatdotheyknow.
    ingest_whatdotheyknow: bool = False
    # The authority's WDTK slug; its request index lives at /body/<slug>.
    whatdotheyknow_authority: str = "oxfordshire_county_council"
    whatdotheyknow_base_url: str = "https://www.whatdotheyknow.com"
    # Cap how many successful requests to pull per run (politeness + demo size).
    whatdotheyknow_max_requests: int = 60

    # --- Public-information auto-refresh (default OFF) ---
    # Keeps the knowledge base current by re-running ingestion (+ reindex). It is
    # network-bound, so it stays off until a deployment opts in; it also no-ops
    # safely unless the ingestion flags above are on. See app/services/kb_refresh.py.
    kb_refresh_enabled: bool = False
    # Treat the KB as stale once the last successful refresh is older than this.
    kb_refresh_max_age_days: int = 7                  # "weekly"
    # Refresh (when stale) at the start of drafting a response, best-effort and
    # never blocking the draft if a source is unreachable.
    kb_refresh_on_draft: bool = False
    # The weekly trigger is an external cron / systemd timer running
    # `python -m app.refresh` (see app/refresh.py) — not an in-process scheduler.
    ingest_user_agent: str = "OCC-FOI-Bot/0.1 (+foi@oxfordshire.gov.uk)"
    # Seed the crawl from the site's sitemap.xml (preferred — content pages, not
    # nav). Falls back to a breadth-first crawl from the root if absent.
    ingest_use_sitemap: bool = True
    # Skip pages whose extracted text is shorter than this — drops thin index /
    # listing / stub pages that add noise rather than groundable facts.
    ingest_min_content_chars: int = 400
    # Also extract text from linked PDFs (reports/datasets) during the website
    # crawl, so figures published only in a document are searchable. Needs pypdf.
    ingest_pdfs: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
