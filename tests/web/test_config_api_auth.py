"""Tests for configuration API access policy."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from anibridge.app.web.routes.api import config as config_api_module


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(config_api_module.router, prefix="/api/config")
    return app


def test_config_api_blocked_without_auth_or_override(monkeypatch: MonkeyPatch) -> None:
    """Config API should be blocked by default when web auth is not configured."""
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

    client = TestClient(_build_app())
    response = client.get("/api/config/openapi.json")

    assert response.status_code == 403
    assert "Configuration API is disabled" in response.json()["detail"]


def test_config_api_allowed_with_explicit_unauthenticated_override(
    monkeypatch: MonkeyPatch,
) -> None:
    """Config API should be available with explicit unauthenticated override."""
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

    client = TestClient(_build_app())
    response = client.get("/api/config/openapi.json")

    assert response.status_code == 200


def test_config_api_allowed_when_auth_is_configured(monkeypatch: MonkeyPatch) -> None:
    """Config API should be available when web auth is configured."""
    monkeypatch.setattr(
        config_api_module,
        "runtime_config",
        SimpleNamespace(
            web=SimpleNamespace(
                has_auth=True,
                allow_config_without_auth=False,
            )
        ),
        raising=False,
    )

    client = TestClient(_build_app())
    response = client.get("/api/config/openapi.json")

    assert response.status_code == 200
