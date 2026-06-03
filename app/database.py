"""Database engine, session factory and Base."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

settings = get_settings()

# check_same_thread is a SQLite-only concern; harmless to pass for other DBs via connect_args guard.
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables. Import models first so they register on Base.metadata."""
    from . import models  # noqa: F401  (registers mappers)
    Base.metadata.create_all(bind=engine)
    _ensure_columns()


# New columns added to *existing* tables over time. create_all() only creates
# missing tables, not missing columns, and there is no migration tool here — so
# this lightweight, idempotent backfill keeps a dev SQLite DB usable across
# upgrades without dropping data. Production should use Alembic.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "foi_requests": [
        ("clock_paused_days", "INTEGER NOT NULL DEFAULT 0"),
        ("clarification_requested_at", "DATETIME"),
    ],
}


def _ensure_columns() -> None:
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in columns:
                if name not in existing:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
