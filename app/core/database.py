"""SQLAlchemy engine + session factory shared by API and workers."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings
from app.core.logging import logger


def _build_engine():
    """QueuePool's `pool_size`/`max_overflow` aren't valid kwargs for the
    SQLite dialect (`create_engine` raises `TypeError` if you pass them) —
    this only matters for CI/tests and a from-scratch local dev mode without
    Postgres, but SQLite is a real, supported `DATABASE_URL` for both, so we
    special-case it rather than hard-failing anyone who tries it."""
    is_sqlite = settings.database_url.startswith("sqlite")
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=1800,  # recycle before a firewall/LB drops connections silently
        )
    return create_engine(settings.database_url, **kwargs)


engine = _build_engine()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def session_scope() -> Session:
    """Manual session for Celery tasks (commit/rollback handled by caller).

    A plain SQLAlchemy `Session` already implements the context-manager
    protocol (`__exit__` calls `.close()`), so `with session_scope() as db:`
    at every Celery task call site works without a custom wrapper.
    """
    return SessionLocal()


def check_db_connection() -> bool:
    """Cheap connectivity probe for the readiness endpoint. Never raises —
    a broken DB should make readiness report False, not crash the health
    check itself."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError as e:
        logger.warning("Database readiness check failed: {}", e)
        return False
