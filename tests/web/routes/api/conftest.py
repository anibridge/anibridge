"""Shared helpers for API route tests."""

from collections.abc import Callable
from types import ModuleType, SimpleNamespace
from typing import Protocol

import pytest
from litestar.app import Litestar
from litestar.router import Router as LitestarRouter
from litestar.testing.client.sync_client import TestClient as LitestarTestClient

from anibridge.app.web.routes.api import config as config_api_module


class _RouteModule(Protocol):
    router: LitestarRouter


def _parent_prefix(prefix: str, router_path: str) -> str:
    normalized_prefix = prefix.rstrip("/") or "/"
    normalized_router_path = router_path.rstrip("/") or "/"

    if normalized_router_path != "/" and normalized_prefix.endswith(
        normalized_router_path
    ):
        parent = normalized_prefix[: -len(normalized_router_path)]
        return parent or ""

    return normalized_prefix if normalized_prefix != "/" else ""


@pytest.fixture
def api_client_factory() -> Callable[[LitestarRouter, str], LitestarTestClient]:
    """Build lightweight test clients around individual route groups."""

    def _factory(router: LitestarRouter, prefix: str) -> LitestarTestClient:
        app = Litestar(
            route_handlers=[
                LitestarRouter(
                    path=_parent_prefix(prefix, router.path),
                    route_handlers=[router],
                )
            ]
        )
        return LitestarTestClient(app)

    return _factory


@pytest.fixture
def api_client_for(
    api_client_factory: Callable[[LitestarRouter, str], LitestarTestClient],
) -> Callable[[_RouteModule, str], LitestarTestClient]:
    """Build a router client directly from a route module."""

    def _factory(route_module: _RouteModule, prefix: str) -> LitestarTestClient:
        return api_client_factory(route_module.router, prefix)

    return _factory


@pytest.fixture
def set_config_api_access(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Override config API auth settings for route access tests."""

    def _configure(
        *,
        has_auth: bool = False,
        allow_config_without_auth: bool = False,
    ) -> None:
        monkeypatch.setattr(
            config_api_module,
            "runtime_config",
            SimpleNamespace(
                web=SimpleNamespace(
                    has_auth=has_auth,
                    allows_config_api=has_auth or allow_config_without_auth,
                )
            ),
            raising=False,
        )

    return _configure


class _AppState(SimpleNamespace):
    """Mutable app-state stub for route tests."""

    scheduler: object | None
    started_at: object | None
    restart_requested: bool

    def request_restart(self) -> None:
        self.restart_requested = True


@pytest.fixture
def make_app_state() -> Callable[..., _AppState]:
    """Construct lightweight app-state stubs with sensible defaults."""

    def _factory(**overrides: object) -> _AppState:
        return _AppState(
            scheduler=overrides.pop("scheduler", None),
            started_at=overrides.pop("started_at", None),
            restart_requested=overrides.pop("restart_requested", False),
            **overrides,
        )

    return _factory


@pytest.fixture
def patch_app_state(
    monkeypatch: pytest.MonkeyPatch,
    make_app_state: Callable[..., _AppState],
) -> Callable[..., _AppState]:
    """Patch a route module to use a disposable app-state stub."""

    def _patch(route_module: ModuleType, **overrides: object) -> _AppState:
        state = make_app_state(**overrides)
        monkeypatch.setattr(route_module, "get_app_state", lambda: state)
        return state

    return _patch
