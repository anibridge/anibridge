"""Tests for the backup listing and restoration service."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from anibridge.app.web.services.backup_service import (
    BackupService,
    InvalidBackupFilenameError,
    ProfileNotFoundError,
    SchedulerNotInitializedError,
)
from anibridge.app.web.state import get_app_state


class DummyListProvider:
    """Fake provider exposing the subset of behavior the service exercises."""

    NAMESPACE = "alist"

    def __init__(self) -> None:
        """Initialize the provider stub with tracking storage."""
        self._restored: list[dict[str, str]] = []

    def user(self):
        """Return a pseudo user profile object."""
        return SimpleNamespace(title="Tester")

    def deserialize_backup_entries(self, payload):
        """Convert persisted JSON into a restore payload namespace."""
        return SimpleNamespace(entries=list(payload.get("entries", [])), user="tester")

    async def restore_entries(self, entries):
        """Record restored entries for assertions."""
        self._restored.extend(entries)

    async def restore_list(self, backup: str):
        """No-op for list restoration."""
        pass


class DummyBridge(SimpleNamespace):
    """Simple namespace to match scheduler expectations."""

    list_provider: DummyListProvider


class DummyScheduler(SimpleNamespace):
    """Scheduler stub exposing the bridge mapping used by the service."""

    bridge_clients: dict[str, DummyBridge]


@pytest.fixture()
def configured_scheduler(tmp_path: Path):
    """Attach a scheduler with a single bridge to the global app state."""
    provider = DummyListProvider()
    bridge = DummyBridge(
        global_config=SimpleNamespace(data_path=tmp_path),
        list_provider=provider,
    )
    scheduler = DummyScheduler(bridge_clients={"primary": bridge})
    state = get_app_state()
    state.scheduler = cast(Any, scheduler)
    yield tmp_path, provider, scheduler
    state.scheduler = None


def _write_backup(path: Path, name: str, entries: list[dict[str, str]] | None = None):
    payload = {"entries": [{"key": "1"}] if entries is None else entries}
    target = path / "backups" / "primary"
    target.mkdir(parents=True, exist_ok=True)
    file_path = target / name
    file_path.write_text(json.dumps(payload), encoding="utf-8")
    return file_path


def test_list_backups_sorts_newest_first(configured_scheduler):
    """Collect metadata for all backups in reverse chronological order."""
    tmp_path, provider, _ = configured_scheduler
    service = BackupService()
    older = _write_backup(tmp_path, "anibridge_primary_alist_20240101010101.json")
    newer = _write_backup(tmp_path, "anibridge_primary_alist_20240202020202.json")

    items = service.list_backups("primary")
    assert [item.filename for item in items] == [newer.name, older.name]
    assert items[0].user == provider.user().title
    assert items[0].size_bytes == newer.stat().st_size


def test_read_backup_raw_and_invalid_filename(configured_scheduler):
    """Read JSON payloads and reject attempts to escape the profile directory."""
    tmp_path, _, _ = configured_scheduler
    service = BackupService()
    file_path = _write_backup(
        tmp_path,
        "anibridge_primary_alist_20240303030303.json",
        [],
    )

    assert service.read_backup_raw("primary", file_path.name) == {"entries": []}
    with pytest.raises(InvalidBackupFilenameError):
        service.read_backup_raw("primary", "../escape.json")


def test_backup_service_requires_scheduler_and_known_profiles():
    """Missing scheduler or profile are surfaced as expected errors."""
    state = get_app_state()
    state.scheduler = None
    service = BackupService()
    with pytest.raises(SchedulerNotInitializedError):
        service.list_backups("primary")

    scheduler = DummyScheduler(bridge_clients={})
    state.scheduler = cast(Any, scheduler)
    with pytest.raises(ProfileNotFoundError):
        service.list_backups("unknown")

    state.scheduler = None
