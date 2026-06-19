"""Tests for system API endpoints."""

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from pytest import raises

from anibridge.app.exceptions import SchedulerUnavailableError
from anibridge.app.web.routes.api import system as system_api_module


class _DummyGlobalConfig:
    def __init__(self) -> None:
        self.profiles = {
            "primary": SimpleNamespace(
                model_dump=lambda mode="json": {
                    "source_namespace": "plex",
                    "target_namespace": "anilist",
                    "scan_modes": ["poll", "periodic"],
                }
            )
        }

    def model_dump(self, mode="json", exclude=None):
        return {"web": {"host": "127.0.0.1"}}


@pytest.fixture
def system_client(api_client_for):
    return api_client_for(system_api_module, "/api/system")


def test_api_restart_requests_scheduler_shutdown(
    patch_app_state,
    scheduler_stub_cls,
) -> None:
    """Restart endpoint should mark restart and request scheduler shutdown."""
    api_restart = cast(
        Callable[[], system_api_module.RestartResponse],
        system_api_module.api_restart.fn,
    )
    scheduler = scheduler_stub_cls()
    state = patch_app_state(system_api_module, scheduler=scheduler)

    response = api_restart()

    assert response.ok is True
    assert "Restart requested" in response.message
    assert state.restart_requested is True
    assert scheduler.shutdown_requested is True


def test_api_restart_requires_scheduler(patch_app_state) -> None:
    """Restart endpoint should fail when scheduler is unavailable."""
    api_restart = cast(
        Callable[[], system_api_module.RestartResponse],
        system_api_module.api_restart.fn,
    )
    patch_app_state(system_api_module, scheduler=None)

    with raises(SchedulerUnavailableError):
        api_restart()


@pytest.mark.parametrize(
    ("allow_without_auth", "expected_status", "restart_requested"),
    [
        pytest.param(False, 403, False, id="blocked-without-override"),
        pytest.param(True, 202, True, id="allowed-with-override"),
    ],
)
def test_restart_api_access_policy(
    patch_app_state,
    system_client,
    set_config_api_access,
    scheduler_stub_cls,
    allow_without_auth: bool,
    expected_status: int,
    restart_requested: bool,
) -> None:
    """Restart API should follow the shared config API access policy."""
    set_config_api_access(allow_config_without_auth=allow_without_auth)
    scheduler = scheduler_stub_cls()
    state = patch_app_state(system_api_module, scheduler=scheduler)

    response = system_client.post("/api/system/restart")

    assert response.status_code == expected_status
    assert state.restart_requested is restart_requested
    assert scheduler.shutdown_requested is restart_requested
    if expected_status == 403:
        assert "Configuration API is disabled" in response.json()["detail"]


@pytest.mark.parametrize(
    ("with_scheduler", "expected_global_config", "expected_profile_count"),
    [
        pytest.param(
            True,
            {"web": {"host": "127.0.0.1"}},
            1,
            id="with-scheduler",
        ),
        pytest.param(None, {}, 0, id="without-scheduler"),
    ],
)
def test_api_settings_serializes_scheduler_state(
    patch_app_state,
    scheduler_stub_cls,
    with_scheduler: bool,
    expected_global_config: dict[str, object],
    expected_profile_count: int,
) -> None:
    api_settings = cast(
        Callable[[], system_api_module.SettingsResponse],
        system_api_module.api_settings.fn,
    )
    scheduler = None
    if with_scheduler:
        scheduler = scheduler_stub_cls(global_config=_DummyGlobalConfig())
    patch_app_state(system_api_module, scheduler=scheduler)

    response = api_settings()

    assert response.global_config == expected_global_config
    assert len(response.profiles) == expected_profile_count
    if expected_profile_count:
        assert response.profiles[0].name == "primary"
        assert response.profiles[0].settings["source_namespace"] == "plex"


@pytest.mark.asyncio
async def test_api_about_returns_runtime_summary(
    monkeypatch,
    patch_app_state,
    scheduler_stub_cls,
) -> None:
    scheduler = scheduler_stub_cls(
        running=True,
        global_config=_DummyGlobalConfig(),
        status_payload={
            "primary": {
                "config": {
                    "source_namespace": "plex",
                    "target_namespace": "anilist",
                    "scan_modes": ["poll", "periodic"],
                },
                "status": {
                    "running": True,
                    "last_synced": "2026-01-01T00:00:00+00:00",
                    "current_sync": {"state": "running"},
                    "initialization_error": None,
                },
            }
        },
        runtime_metrics={"coordinator": {"queued": 1}},
        next_database_sync_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    patch_app_state(
        system_api_module,
        scheduler=scheduler,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(system_api_module.platform, "python_version", lambda: "3.14.0")
    monkeypatch.setattr(system_api_module.platform, "platform", lambda: "Linux")
    monkeypatch.setattr(system_api_module.os, "getpid", lambda: 123)
    monkeypatch.setattr(system_api_module.psutil, "cpu_count", lambda logical=True: 8)
    monkeypatch.setattr(
        system_api_module.psutil,
        "Process",
        lambda pid: SimpleNamespace(
            memory_info=lambda: SimpleNamespace(rss=2048 * 1024)
        ),
    )

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 1, 0, tzinfo=UTC)

    monkeypatch.setattr(system_api_module, "datetime", FrozenDateTime)

    response = await system_api_module.api_about.fn()

    assert response.info.python == "3.14.0"
    assert response.process.pid == 123
    assert response.scheduler.running is True
    assert response.scheduler.syncing_profiles == 1
    assert response.scheduler.most_recent_sync_profile == "primary"
    assert response.scheduler.next_database_sync_at == "2026-01-02T00:00:00+00:00"


@pytest.mark.asyncio
async def test_api_about_ignores_non_running_sync_payloads(
    monkeypatch,
    patch_app_state,
    scheduler_stub_cls,
) -> None:
    scheduler = scheduler_stub_cls(
        running=True,
        global_config=_DummyGlobalConfig(),
        status_payload={
            "primary": {
                "config": {
                    "source_namespace": "plex",
                    "target_namespace": "anilist",
                    "scan_modes": ["poll", "periodic"],
                },
                "status": {
                    "running": True,
                    "last_synced": "2026-01-01T00:00:00+00:00",
                    "current_sync": {"state": "idle", "stage": "completed"},
                    "initialization_error": None,
                },
            }
        },
        runtime_metrics={"coordinator": {"queued": 1}},
        next_database_sync_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    patch_app_state(
        system_api_module,
        scheduler=scheduler,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(system_api_module.platform, "python_version", lambda: "3.14.0")
    monkeypatch.setattr(system_api_module.platform, "platform", lambda: "Linux")
    monkeypatch.setattr(system_api_module.os, "getpid", lambda: 123)
    monkeypatch.setattr(system_api_module.psutil, "cpu_count", lambda logical=True: 8)
    monkeypatch.setattr(
        system_api_module.psutil,
        "Process",
        lambda pid: SimpleNamespace(
            memory_info=lambda: SimpleNamespace(rss=2048 * 1024)
        ),
    )

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 1, 0, tzinfo=UTC)

    monkeypatch.setattr(system_api_module, "datetime", FrozenDateTime)

    response = await system_api_module.api_about.fn()

    assert response.scheduler.syncing_profiles == 0


@pytest.mark.asyncio
async def test_api_about_wraps_scheduler_errors(
    patch_app_state,
    scheduler_stub_cls,
) -> None:
    class _BrokenScheduler(scheduler_stub_cls):
        async def get_status(self) -> dict[str, dict[str, dict[str, object]]]:
            raise RuntimeError("boom")

    patch_app_state(system_api_module, scheduler=_BrokenScheduler())

    with pytest.raises(SchedulerUnavailableError, match="Unable to fetch scheduler"):
        await system_api_module.api_about.fn()


def test_meta_returns_version_and_git_hash() -> None:
    meta = cast(
        Callable[[], system_api_module.MetaResponse],
        system_api_module.meta.fn,
    )
    response = meta()

    assert response.version
    assert response.git_hash
