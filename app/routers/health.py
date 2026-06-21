"""Readiness/health probe — verify the deployed stack end to end.

`/healthz` reports the live status of the database, retrieval, and the LLM
(including whether the local Ollama model is actually pulled and reachable), so
you can confirm a deployment — especially the local LLM — right after launch.

Unauthenticated and fast: every probe is wrapped and time-bounded so the endpoint
never hangs or 500s. HTTP is 200 while the app can serve (database ok) even if a
component is degraded; it is 503 only when the database is unreachable. The JSON
`status` is "degraded" whenever any component is not fully healthy.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..models import KnowledgeChunk

router = APIRouter(tags=["meta"])


def _llm_health(s) -> dict:
    """Probe the configured drafting model. For Ollama, check the daemon is up
    and the model is pulled — the two things that decide whether drafting uses
    the local LLM or falls back to the template."""
    provider = (s.llm_provider or "stub").lower()
    if provider in ("", "stub"):
        return {"provider": "stub", "status": "stub", "detail": "template drafting (no model)"}
    if provider == "ollama":
        try:
            import httpx
            resp = httpx.get(f"{s.ollama_base_url.rstrip('/')}/api/tags", timeout=4)
            resp.raise_for_status()
            models = [m.get("name", "") for m in resp.json().get("models", [])]
            base = s.llm_model.split(":")[0]
            present = any(m == s.llm_model or m.split(":")[0] == base for m in models)
            return {
                "provider": "ollama", "model": s.llm_model,
                "status": "ok" if present else "model_missing",
                "models": models[:10],
                "detail": None if present else "model not pulled yet — drafting falls back to template",
            }
        except Exception as exc:
            return {"provider": "ollama", "model": s.llm_model, "status": "unreachable",
                    "detail": str(exc)[:200]}
    if provider == "anthropic":
        return {"provider": "anthropic", "status": "ok" if s.llm_api_key else "no_api_key"}
    return {"provider": provider, "status": "unknown"}


@router.get("/healthz")
def healthz(response: Response, db: Session = Depends(get_db)) -> dict:
    s = get_settings()
    components: dict[str, dict] = {}

    # Database — the only component whose failure makes the app unservable (503).
    try:
        db.execute(text("SELECT 1"))
        components["database"] = {"status": "ok"}
    except Exception as exc:
        components["database"] = {"status": "error", "detail": str(exc)[:200]}

    # Retrieval — for semantic mode, surface whether the chunk index is populated.
    provider = (s.retrieval_provider or "keyword").lower()
    retrieval = {"provider": provider, "status": "ok"}
    if provider == "semantic":
        try:
            n = db.execute(select(func.count()).select_from(KnowledgeChunk)).scalar_one()
            retrieval["indexed_chunks"] = n
            if n == 0:
                retrieval["status"] = "empty"
                retrieval["detail"] = "no embeddings yet — run reindex"
        except Exception as exc:
            retrieval["status"] = "error"
            retrieval["detail"] = str(exc)[:200]
    components["retrieval"] = retrieval

    # LLM (local model reachability + presence).
    components["llm"] = _llm_health(s)

    db_ok = components["database"]["status"] == "ok"
    healthy = (db_ok
               and components["retrieval"]["status"] == "ok"
               and components["llm"]["status"] in ("ok", "stub"))
    if not db_ok:
        response.status_code = 503
    return {"status": "ok" if healthy else "degraded",
            "council": s.council_name, "components": components}
