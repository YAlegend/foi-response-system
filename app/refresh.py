"""CLI to refresh the public-information knowledge base.

Run weekly from cron or a systemd timer (the preferred trigger on council
infrastructure). Forces a refresh regardless of the freshness window:

    FOI_INGEST_ENABLED=true FOI_INGEST_WEBSITE=true \
        python -m app.refresh

Example crontab (06:30 every Monday):

    30 6 * * 1  cd /opt/foi && . .venv/bin/activate && python -m app.refresh

Exit code is non-zero if the refresh ended in error, so the timer surfaces it.
"""
from __future__ import annotations

import sys

from .database import SessionLocal, init_db
from .services import kb_refresh


def main() -> int:
    init_db()
    db = SessionLocal()
    try:
        rec = kb_refresh.refresh(db, trigger="weekly", force=True)
        print(f"[{rec.status}] {rec.detail}")
        print(f"website={rec.website_docs} published={rec.published_docs} "
              f"chunks={rec.chunks_indexed}")
        return 1 if rec.status == "error" else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
