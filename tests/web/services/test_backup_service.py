"""Tests for the backup listing and restoration service."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from anibridge.provider.base import Account, SupportsBackupImports

from anibridge.app.web.services.backup_service import (
    BackupFileNotFoundError,
    BackupParseError,
    BackupService,
    InvalidBackupFilenameError,
    ProfileNotFoundError,
    SchedulerNotInitializedError,
    SchedulerUnavailableError,
    get_backup_service,
)
from anibridge.app.web.state import get_app_state


class DummyTargetProvider(SupportsBackupImports):
    """Fake provider exposing the subset of behavior the service exercises."""

    NAMESPACE = "alist"

    def __init__(self) -> None:
        """Initialize the provider stub with tracking storage."""
        self._restored_payloads: list[bytes] = []

    def account(self):
        """Return a pseudo user profile object."""
        return Account(key="tester", title="Tester")

    async def import_backup(self, payload: bytes) -> None:
        """Record restored backup bytes for assertions."""
        self._restored_payloads.append(payload)


class DummyBridge(SimpleNamespace):
    """Simple namespace to match scheduler expectations."""

    target_provider: DummyTargetProvider


class DummyScheduler(SimpleNamespace):
    """Scheduler stub exposing the bridge mapping used by the service."""

    bridge_clients: dict[str, DummyBridge]
    global_config: Any
    failed_profile_errors: dict[str, str]


@pytest.fixture()
def configured_scheduler(tmp_path: Path):
    """Attach a scheduler with a single bridge to the global app state."""
    provider = DummyTargetProvider()
    bridge = DummyBridge(
        global_config=SimpleNamespace(data_path=tmp_path),
        target_provider=provider,
    )
    scheduler = DummyScheduler(
        bridge_clients={"primary": bridge},
        global_config=SimpleNamespace(
            data_path=tmp_path,
            profiles={
                "primary": SimpleNamespace(target_provider="alist"),
                "errored": SimpleNamespace(target_provider="alist"),
            },
        ),
        failed_profile_errors={},
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
    assert items[0].user == provider.account().title
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


def test_list_backups_uses_mtime_when_timestamp_is_invalid(configured_scheduler):
    """Numeric but invalid timestamps should fall back to file mtime."""
    tmp_path, _provider, _scheduler = configured_scheduler
    service = BackupService()
    file_path = _write_backup(
        tmp_path,
        "anibridge_primary_alist_99999999999999.json",
        [],
    )

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


def test_read_backup_raw_returns_binary_metadata(configured_scheduler):
    """Non-JSON backup previews should return a small binary metadata payload."""
    tmp_path, _, _scheduler = configured_scheduler
    service = BackupService()
    target = tmp_path / "backups" / "primary"
    target.mkdir(parents=True, exist_ok=True)
    backup = target / "anibridge_primary_alist_binary.bin"
    backup.write_bytes(b"\x80\x81")

    assert service.read_backup_raw("primary", backup.name) == {
        "binary": True,
        "size_bytes": 2,
        "filename": backup.name,
    }


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


def test_read_backup_raw_rejects_missing_file(configured_scheduler):
    """Missing backup files should surface as file-not-found errors."""
    _tmp_path, _provider, _scheduler = configured_scheduler

    with pytest.raises(BackupFileNotFoundError):
        BackupService().read_backup_raw("primary", "missing.json")


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
async def test_restore_backup_requires_scheduler_and_known_profile(
    configured_scheduler,
):
    """Restore validation should reject unavailable scheduler/profile states."""
    _tmp_path, _provider, scheduler = configured_scheduler
    service = BackupService()

    get_app_state().scheduler = None
    with pytest.raises(SchedulerNotInitializedError):
        await service.restore_backup("primary", "backup.json")

    get_app_state().scheduler = cast(Any, scheduler)
    with pytest.raises(ProfileNotFoundError):
        await service.restore_backup("unknown", "backup.json")


@pytest.mark.asyncio
async def test_restore_backup_success(configured_scheduler):
    """Restoring a valid backup should delegate raw bytes to the target provider."""
    tmp_path, provider, _scheduler = configured_scheduler
    service = BackupService()
    backup_name = "anibridge_primary_alist_20240303030303.json"
    _write_backup(tmp_path, backup_name, [{"key": "1"}])

    await service.restore_backup("primary", backup_name)

    assert len(provider._restored_payloads) == 1
    assert json.loads(provider._restored_payloads[0]) == {"entries": [{"key": "1"}]}


@pytest.mark.asyncio
async def test_restore_backup_requires_import_capability(configured_scheduler):
    """Target providers must implement backup imports."""
    tmp_path, _provider, scheduler = configured_scheduler
    service = BackupService()
    backup_name = "anibridge_primary_alist_20240303030303.json"
    _write_backup(tmp_path, backup_name, [{"key": "1"}])
    scheduler.bridge_clients["primary"].target_provider = object()

    with pytest.raises(BackupParseError, match="does not support backup restoration"):
        await service.restore_backup("primary", backup_name)


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

    async def _not_implemented(_payload: bytes) -> None:
        raise NotImplementedError

    monkeypatch.setattr(provider, "import_backup", _not_implemented)
    with pytest.raises(Exception, match="does not support backup restoration"):
        await service.restore_backup("primary", backup_name)

    async def _boom(_payload: bytes) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(provider, "import_backup", _boom)
    with pytest.raises(Exception, match="Error during backup restoration: boom"):
        await service.restore_backup("primary", backup_name)


def test_get_backup_service_is_cached() -> None:
    assert get_backup_service() is get_backup_service()
