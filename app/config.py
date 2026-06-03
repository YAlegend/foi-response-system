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
    app_name: str = "Hertfordshire County Council FOI Response System"
    environment: str = "local"
    database_url: str = "sqlite:///./foi.db"

    # --- Statutory parameters (FOIA 2000) ---
    statutory_working_days: int = 20          # s.10 deadline
    public_interest_extension_days: int = 20  # permitted PIT extension
    sla_amber_day: int = 12                   # dashboard amber alert
    sla_red_day: int = 17                     # dashboard red alert / escalation

    # --- Council house-style facts (used by the drafting templates) ---
    responding_team: str = "Information Governance Unit"
    council_name: str = "Hertfordshire County Council"
    council_address: str = (
        "Information Governance Unit, Hertfordshire County Council, "
        "County Hall, Pegs Lane, Hertford, SG13 8DQ"
    )
    ico_address: str = (
        "Information Commissioner's Office, Wycliffe House, Water Lane, "
        "Wilmslow, Cheshire, SK9 5AF"
    )

    # --- AI provider (pluggable) ---
    # "stub"  -> deterministic template drafting, no external calls (default)
    # "anthropic"/"openai" -> wire a real client in app/services/llm.py
    llm_provider: str = "stub"
    llm_model: str = ""
    llm_api_key: str = ""

    # --- Dedicated FOI inbox (pluggable) ---
    # Requests arrive in a monitored mailbox, not a public form. The provider is
    # swappable; it defaults to an offline stub so the system runs with no auth.
    #   "stub"          -> offline simulated mailbox seeded with sample FOI emails
    #   "imap" / "microsoft365" / "gmail" -> wire a real connector in
    #                      app/services/inbox.py (clearly-marked seam provided)
    inbox_provider: str = "stub"
    inbox_address: str = "foi@hertfordshire.gov.uk"

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
    council_website_root: str = "https://www.hertfordshire.gov.uk/"
    disclosure_log_url: str = (
        "https://hertfordshireportal.icasework.com/resource?id=855014&db=hertfordshire"
    )
    # Local directory of exported published responses for "feed" mode, so the
    # automated refresh can re-ingest them without a path passed by hand.
    published_responses_feed_dir: str | None = None
    ingest_crawl_max_pages: int = 200

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
    ingest_user_agent: str = "HCC-FOI-Bot/0.1 (+governance@hertfordshire.gov.uk)"
    # Seed the crawl from the site's sitemap.xml (preferred — content pages, not
    # nav). Falls back to a breadth-first crawl from the root if absent.
    ingest_use_sitemap: bool = True
    # Skip pages whose extracted text is shorter than this — drops thin index /
    # listing / stub pages that add noise rather than groundable facts.
    ingest_min_content_chars: int = 400


@lru_cache
def get_settings() -> Settings:
    return Settings()
