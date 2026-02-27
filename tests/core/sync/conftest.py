"""Shared fixtures for sync client test suites."""

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import sessionmaker

from anibridge.app.models.db.base import Base


@pytest.fixture
def sync_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """Patch `anibridge.app.core.sync.base.db` with an in-memory SQLite database."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, future=True, autoflush=False)

    class _DB:
        def __init__(self) -> None:
            self._session = None

        def __enter__(self):
            self._session = session_factory()
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            if self._session is not None:
                self._session.close()
                self._session = None

        @property
        def session(self):
            if self._session is None:
                self._session = session_factory()
            return self._session

        def close(self) -> None:
            if self._session is not None:
                self._session.close()
                self._session = None

    db_instance = _DB()

    import anibridge.app.core.sync.base as base_module

    monkeypatch.setattr(base_module, "db", lambda: db_instance)

    try:
        yield db_instance
    finally:
        db_instance.close()
        engine.dispose()
