"""Database engine, session factory and declarative base.

ServiceMesh uses a synchronous SQLAlchemy 2.0 session. Synchronous was a
deliberate choice: the orchestrator performs a lot of read-modify-write cycles
on transaction state and the mental model for "this row is locked while I
decide the next step" is far clearer synchronously. FastAPI runs sync endpoints
in a threadpool, so throughput is still adequate for the target workload.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from servicemesh.core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for every ServiceMesh ORM model."""


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _build_engine() -> Engine:
    settings = get_settings()
    kwargs: dict = {"echo": settings.db_echo, "future": True}
    if settings.database_url.startswith("sqlite"):
        # check_same_thread=False because FastAPI serves sync endpoints from a
        # threadpool; each request still gets its own Session.
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = True
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
    return create_engine(settings.database_url, **kwargs)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(), autoflush=False, autocommit=False, future=True
        )
    return _SessionLocal


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """Foreign keys are OFF by default in SQLite; ServiceMesh relies on them."""
    module = type(dbapi_connection).__module__
    if "sqlite" in module:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background work (orchestrator, scripts, tests)."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency. Commit/rollback is handled per request."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Drop cached engine/session factory (used when tests repoint the DSN)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
