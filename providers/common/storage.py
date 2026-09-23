"""Per-organization storage.

Each simulated organization gets its *own* SQLAlchemy Base, its own engine and
its own database URL. They never share a metadata object with ServiceMesh core
or with each other. This is not decoration: it is the property that makes the
orchestration problem real. If all five organizations shared one schema, a
single SQL transaction could span them and the entire Saga/compensation design
would be unnecessary.

Default: one SQLite file per organization under ./data/.
docker-compose: one PostgreSQL database per organization.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class ProviderStore:
    """Encapsulates one organization's isolated persistence."""

    def __init__(self, service_name: str, env_var: str | None = None) -> None:
        self.service_name = service_name
        self.env_var = env_var or f"{service_name.upper()}_DB_URL"

        class _Base(DeclarativeBase):
            pass

        self.Base: type[DeclarativeBase] = _Base
        self._engine = None
        self._factory: sessionmaker[Session] | None = None

    # -- lazy so that tests can repoint the env var before first use --------
    def _url(self) -> str:
        url = os.getenv(self.env_var)
        if url:
            return url
        data_dir = Path(os.getenv("PROVIDER_DATA_DIR", "./data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite+pysqlite:///{data_dir / (self.service_name + '.db')}"

    @property
    def engine(self):
        if self._engine is None:
            url = self._url()
            kwargs: dict = {"future": True}
            if url.startswith("sqlite"):
                kwargs["connect_args"] = {"check_same_thread": False}
            else:
                kwargs["pool_pre_ping"] = True
            self._engine = create_engine(url, **kwargs)
        return self._engine

    @property
    def factory(self) -> sessionmaker[Session]:
        if self._factory is None:
            self._factory = sessionmaker(
                bind=self.engine, autoflush=False, autocommit=False, future=True
            )
        return self._factory

    def create_all(self) -> None:
        self.Base.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        self.Base.metadata.drop_all(self.engine)

    def reset(self) -> None:
        """Used by tests to rebuild a clean organization database."""
        if self._engine is not None:
            self._engine.dispose()
        self._engine = None
        self._factory = None

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def dependency(self) -> Iterator[Session]:
        """FastAPI dependency."""
        s = self.factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()
