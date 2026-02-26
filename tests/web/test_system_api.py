"""Tests for system API endpoints."""

from pytest import raises

from src.exceptions import SchedulerUnavailableError
from src.web.routes.api import system as system_api_module


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
