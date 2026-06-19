"""Tests for readiness probe routes."""

from types import SimpleNamespace

import pytest
from litestar.app import Litestar
from litestar.testing.client.sync_client import TestClient

from anibridge.app.web.routes.z import readyz as readyz_module


@pytest.fixture
def readyz_client() -> TestClient:
    app = Litestar(route_handlers=[readyz_module.router])
    return TestClient(app)


def test_readyz_reports_scheduler_failures(
    monkeypatch: pytest.MonkeyPatch,
    readyz_client: TestClient,
    scheduler_stub_cls,
) -> None:
    scheduler = scheduler_stub_cls(
        running=True,
        profiles={"one": object(), "two": object()},
        bridge_clients={"one": object()},
        failed_profile_errors={"two": "Provider auth failed"},
    )
    monkeypatch.setattr(
        readyz_module,
        "get_app_state",
        lambda: SimpleNamespace(scheduler=scheduler),
    )

    ready = readyz_client.get("/readyz")

    assert ready.status_code == 503
    assert ready.json() == {
        "status": "degraded",
        "ready": False,
        "scheduler_running": True,
        "profiles": {
            "configured": 2,
            "initialized": 1,
            "failed": 1,
        },
    }


def test_readyz_is_unavailable_without_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    readyz_client: TestClient,
) -> None:
    monkeypatch.setattr(
        readyz_module,
        "get_app_state",
        lambda: SimpleNamespace(scheduler=None),
    )

    ready = readyz_client.get("/readyz")

    assert ready.status_code == 503
    assert ready.json() == {
        "status": "unavailable",
        "ready": False,
        "scheduler_running": False,
        "profiles": {
            "configured": 0,
            "initialized": 0,
            "failed": 0,
        },
    }


def test_readyz_is_ok_when_scheduler_is_running(
    monkeypatch: pytest.MonkeyPatch,
    readyz_client: TestClient,
    scheduler_stub_cls,
) -> None:
    scheduler = scheduler_stub_cls(
        running=True,
        profiles={"one": object()},
        bridge_clients={"one": object()},
    )
    monkeypatch.setattr(
        readyz_module,
        "get_app_state",
        lambda: SimpleNamespace(scheduler=scheduler),
    )

    ready = readyz_client.get("/readyz")

    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ok",
        "ready": True,
        "scheduler_running": True,
        "profiles": {
            "configured": 1,
            "initialized": 1,
            "failed": 0,
        },
    }
