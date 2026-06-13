"""Shared fixtures for sync test suites."""

from collections.abc import Iterator

import pytest


@pytest.fixture
def sync_db(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_db_factory,
) -> Iterator[object]:
    """Patch sync base DB access with an in-memory SQLite database."""
    db_instance = sqlite_db_factory()

    import anibridge.app.core.sync.base as base_module

    monkeypatch.setattr(base_module, "db", lambda: db_instance)
    yield db_instance
