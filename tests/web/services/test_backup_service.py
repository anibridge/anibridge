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
    SchedulerUnavailableError,
    get_backup_service,
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
    global_config: Any
    failed_profile_errors: dict[str, str]

    async def run_maintenance(self, work, *, source: str, timeout_=None):
        """Execute maintenance immediately while recording its source."""
        del timeout_
        self.maintenance_sources.append(source)
        return await work()


@pytest.fixture()
def configured_scheduler(tmp_path: Path):
    """Attach a scheduler with a single bridge to the global app state."""
    provider = DummyListProvider()
    bridge = DummyBridge(
        global_config=SimpleNamespace(data_path=tmp_path),
        list_provider=provider,
    )
    scheduler = DummyScheduler(
        bridge_clients={"primary": bridge},
        global_config=SimpleNamespace(
            data_path=tmp_path,
            profiles={
                "primary": SimpleNamespace(list_provider="alist"),
                "errored": SimpleNamespace(list_provider="alist"),
            },
        ),
        failed_profile_errors={},
        maintenance_sources=[],
    )
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

    list_file = (
        tmp_path / "backups" / "primary" / "anibridge_primary_mal_20240303030304.json"
    )
    list_file.write_text(
        json.dumps([{"id": 1, "status": "watching"}]), encoding="utf-8"
    )
    assert service.read_backup_raw("primary", list_file.name) == [
        {"id": 1, "status": "watching"}
    ]

    with pytest.raises(InvalidBackupFilenameError):
        service.read_backup_raw("primary", "../escape.json")


def test_backup_service_requires_scheduler_and_known_profiles():
    """Missing scheduler or profile are surfaced as expected errors."""
    state = get_app_state()
    state.scheduler = None
    service = BackupService()
    with pytest.raises(SchedulerNotInitializedError):
        service.list_backups("primary")

    scheduler = DummyScheduler(
        bridge_clients={},
        global_config=SimpleNamespace(data_path=Path("."), profiles={}),
        failed_profile_errors={},
    )
    state.scheduler = cast(Any, scheduler)
    with pytest.raises(ProfileNotFoundError):
        service.list_backups("unknown")

    state.scheduler = None


def test_list_backups_allows_errored_profiles(configured_scheduler):
    """Errored profiles should still list backups from disk."""
    tmp_path, _, scheduler = configured_scheduler
    service = BackupService()

    file_path = tmp_path / "backups" / "errored"
    file_path.mkdir(parents=True, exist_ok=True)
    backup_name = "anibridge_errored_alist_20240303030303.json"
    (file_path / backup_name).write_text(json.dumps({"entries": []}), encoding="utf-8")

    scheduler.failed_profile_errors["errored"] = "Provider auth failed"

    items = service.list_backups("errored")
    assert [item.filename for item in items] == [backup_name]


def test_list_backups_returns_empty_when_directory_missing(configured_scheduler):
    """Profiles with no backup directory should return an empty list."""
    _tmp_path, _provider, _scheduler = configured_scheduler
    assert BackupService().list_backups("primary") == []


def test_list_backups_without_bridge_uses_profile_provider_and_mtime_fallback(
    configured_scheduler,
):
    """Missing bridges should still list backups using the configured provider name."""
    tmp_path, _provider, scheduler = configured_scheduler
    scheduler.bridge_clients.pop("primary")
    service = BackupService()
    file_path = _write_backup(tmp_path, "anibridge_primary_alist_snapshot.json", [])

    items = service.list_backups("primary")
    assert [item.filename for item in items] == [file_path.name]


def test_errored_profile_raw_preview_is_allowed(configured_scheduler):
    """Raw preview should still work for errored profiles."""
    tmp_path, _, scheduler = configured_scheduler
    service = BackupService()

    file_path = tmp_path / "backups" / "errored"
    file_path.mkdir(parents=True, exist_ok=True)
    backup_name = "anibridge_errored_alist_20240303030303.json"
    (file_path / backup_name).write_text(json.dumps({"entries": []}), encoding="utf-8")

    scheduler.failed_profile_errors["errored"] = "Provider auth failed"

    assert service.read_backup_raw("errored", backup_name) == {"entries": []}


def test_read_backup_raw_requires_scheduler_and_known_profile(configured_scheduler):
    """Raw backup reads should validate scheduler state and profile existence."""
    _tmp_path, _provider, scheduler = configured_scheduler
    service = BackupService()

    get_app_state().scheduler = None
    with pytest.raises(SchedulerNotInitializedError):
        service.read_backup_raw("primary", "backup.json")

    get_app_state().scheduler = cast(Any, scheduler)
    with pytest.raises(ProfileNotFoundError):
        service.read_backup_raw("unknown", "backup.json")


@pytest.mark.asyncio
async def test_errored_profile_restore_is_blocked(configured_scheduler):
    """Restore endpoint logic should reject errored profiles."""
    tmp_path, _, scheduler = configured_scheduler
    service = BackupService()

    file_path = tmp_path / "backups" / "errored"
    file_path.mkdir(parents=True, exist_ok=True)
    backup_name = "anibridge_errored_alist_20240303030303.json"
    (file_path / backup_name).write_text(json.dumps({"entries": []}), encoding="utf-8")

    scheduler.failed_profile_errors["errored"] = "Provider auth failed"

    with pytest.raises(SchedulerUnavailableError):
        await service.restore_backup("errored", backup_name)


@pytest.mark.asyncio
async def test_restore_backup_success(configured_scheduler):
    """Restoring a valid backup should delegate raw JSON to the list provider."""
    tmp_path, provider, scheduler = configured_scheduler
    service = BackupService()
    backup_name = "anibridge_primary_alist_20240303030303.json"
    _write_backup(tmp_path, backup_name, [{"key": "1"}])

    await service.restore_backup("primary", backup_name)

    assert provider._restored == []
    assert scheduler.maintenance_sources == ["backup_restore:primary"]


@pytest.mark.asyncio
async def test_restore_backup_requires_bridge_client(configured_scheduler):
    """Profiles without an active bridge client should be treated as unavailable."""
    tmp_path, _provider, scheduler = configured_scheduler
    service = BackupService()
    backup_name = "anibridge_primary_alist_20240303030303.json"
    _write_backup(tmp_path, backup_name, [{"key": "1"}])
    scheduler.bridge_clients.pop("primary")

    with pytest.raises(SchedulerUnavailableError, match="unavailable for restore"):
        await service.restore_backup("primary", backup_name)


@pytest.mark.asyncio
async def test_restore_backup_wraps_provider_errors(configured_scheduler, monkeypatch):
    """Provider restore failures should be converted into backup parse errors."""
    tmp_path, provider, _scheduler = configured_scheduler
    service = BackupService()
    backup_name = "anibridge_primary_alist_20240303030303.json"
    _write_backup(tmp_path, backup_name, [{"key": "1"}])

    async def _not_implemented(_payload: str) -> None:
        raise NotImplementedError

    monkeypatch.setattr(provider, "restore_list", _not_implemented)
    with pytest.raises(Exception, match="does not support backup restoration"):
        await service.restore_backup("primary", backup_name)

    async def _boom(_payload: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(provider, "restore_list", _boom)
    with pytest.raises(Exception, match="Error during backup restoration: boom"):
        await service.restore_backup("primary", backup_name)


def test_get_backup_service_is_cached() -> None:
    assert get_backup_service() is get_backup_service()
