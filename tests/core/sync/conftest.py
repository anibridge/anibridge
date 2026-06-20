"""Shared fixtures for sync test suites."""

from collections.abc import Iterator

import pytest

import anibridge.app.core.sync.base as base_module


@pytest.fixture
def sync_db(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_db_factory,
) -> Iterator[object]:
    """Patch sync base DB access with an in-memory SQLite database."""
    db_instance = sqlite_db_factory()

    monkeypatch.setattr(base_module, "db", lambda: db_instance)
    yield db_instance
