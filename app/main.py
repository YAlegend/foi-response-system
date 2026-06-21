"""FastAPI application entry point."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .database import SessionLocal, init_db
from .enums import STAGE_LABELS
from .routers import admin, analytics, auth, dashboard, health, inbox, requests

settings = get_settings()
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from .auth import ensure_seed_users
    db = SessionLocal()
    try:
        ensure_seed_users(db)   # create starter accounts on first run
    finally:
        db.close()
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="AI-assisted FOI response workflow: intake -> triage -> draft -> "
                "human review -> compliance -> sign-off -> dispatch.",
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(requests.router)
app.include_router(inbox.router)
app.include_router(dashboard.router)
app.include_router(analytics.router)
app.include_router(admin.router)
app.include_router(health.router)

# Serve the caseworker web UI at the root. The API docs remain at /docs.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def ui() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "version": "0.1.0"}


@app.get("/stages", tags=["meta"])
def stages() -> dict:
    return {"stages": STAGE_LABELS}
