"""Tests for system API endpoints."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import raises

from anibridge.app.exceptions import SchedulerUnavailableError
from anibridge.app.web.routes.api import config as config_api_module
from anibridge.app.web.routes.api import system as system_api_module


class _DummyScheduler:
    def __init__(self) -> None:
        self.shutdown_requested = False

    def request_shutdown(self) -> None:
        self.shutdown_requested = True


class _DummyAppState:
    def __init__(self, scheduler: _DummyScheduler | None) -> None:
        self.scheduler = scheduler
        self.restart_requested = False

    def request_restart(self) -> None:
        self.restart_requested = True


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(system_api_module.router, prefix="/api/system")
    return app


def test_api_restart_requests_scheduler_shutdown(monkeypatch) -> None:
    """Restart endpoint should mark restart and request scheduler shutdown."""
    scheduler = _DummyScheduler()
    state = _DummyAppState(scheduler=scheduler)
    monkeypatch.setattr(system_api_module, "get_app_state", lambda: state)

    response = system_api_module.api_restart()

    assert response.ok is True
    assert "Restart requested" in response.message
    assert state.restart_requested is True
    assert scheduler.shutdown_requested is True


def test_api_restart_requires_scheduler(monkeypatch) -> None:
    """Restart endpoint should fail when scheduler is unavailable."""
    state = _DummyAppState(scheduler=None)
    monkeypatch.setattr(system_api_module, "get_app_state", lambda: state)

    with raises(SchedulerUnavailableError):
        system_api_module.api_restart()


def test_restart_api_blocked_without_auth_or_override(monkeypatch) -> None:
    """Restart API should be blocked when config API access is blocked."""
    monkeypatch.setattr(
        config_api_module,
        "runtime_config",
        SimpleNamespace(
            web=SimpleNamespace(
                has_auth=False,
                allow_config_without_auth=False,
            )
        ),
        raising=False,
    )

    state = _DummyAppState(scheduler=_DummyScheduler())
    monkeypatch.setattr(system_api_module, "get_app_state", lambda: state)

    client = TestClient(_build_app())
    response = client.post("/api/system/restart")

    assert response.status_code == 403
    assert "Configuration API is disabled" in response.json()["detail"]
    assert state.restart_requested is False


def test_restart_api_allowed_with_unauthenticated_override(monkeypatch) -> None:
    """Restart API should be available when unauthenticated override is enabled."""
    monkeypatch.setattr(
        config_api_module,
        "runtime_config",
        SimpleNamespace(
            web=SimpleNamespace(
                has_auth=False,
                allow_config_without_auth=True,
            )
        ),
        raising=False,
    )

    scheduler = _DummyScheduler()
    state = _DummyAppState(scheduler=scheduler)
    monkeypatch.setattr(system_api_module, "get_app_state", lambda: state)

    client = TestClient(_build_app())
    response = client.post("/api/system/restart")

    assert response.status_code == 202
    assert state.restart_requested is True
    assert scheduler.shutdown_requested is True
