"""Tests for HTTP Basic Authentication middleware and integration."""

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import bcrypt
import pytest
from litestar.app import Litestar
from litestar.connection.request import Request
from litestar.connection.websocket import WebSocket
from litestar.exceptions.http_exceptions import NotAuthorizedException
from litestar.handlers.http_handlers.decorators import get
from litestar.handlers.websocket_handlers.route_handler import websocket
from litestar.middleware.base import DefineMiddleware
from litestar.testing.client.sync_client import TestClient
from litestar.types.asgi_types import HeaderScope, Scope, WebSocketReceiveEvent
from litestar.types.internal_types import ControllerRouterHandler
from pydantic import SecretStr
from pytest import MonkeyPatch

from anibridge.app.config.database import db
from anibridge.app.config.settings import AnibridgeConfig, BasicAuthConfig, WebConfig
from anibridge.app.models.db.sync_history import (
    SyncHistoryGroup,
    SyncHistoryOperation,
    SyncHistoryRun,
    SyncOperationAction,
    SyncOutcome,
    SyncResourceKind,
)
from anibridge.app.web import app as app_module
from anibridge.app.web.middlewares.basic_auth import BasicAuthMiddleware
from anibridge.app.web.state import get_app_state


def _basic_auth_header(username: str, password: str) -> dict[str, str]:
    """Build an Authorization header for HTTP Basic credentials."""
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _build_app(
    *, middleware: list[DefineMiddleware], include_probe_routes: bool = False
) -> Litestar:
    @get("/protected")
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    route_handlers: list[ControllerRouterHandler] = [protected]

    if include_probe_routes:

        @get("/livez")
        async def livez() -> dict[str, str]:
            return {"status": "ok"}

        @get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

        @get("/readyz")
        async def readyz() -> dict[str, object]:
            return {"status": "ok", "ready": True}

        route_handlers.extend([livez, healthz, readyz])

    return Litestar(route_handlers=route_handlers, middleware=middleware)


@websocket("/ws-probe")
async def _ws_probe(socket: WebSocket) -> None:
    await socket.accept()


async def _noop_asgi_app(scope, receive, send) -> None:
    return None


def _make_header_scope(headers: list[tuple[bytes, bytes]]) -> HeaderScope:
    return {"headers": headers}


def _make_websocket_scope() -> Scope:
    return cast(
        Scope,
        {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "auth": None,
            "client": ("127.0.0.1", 12345),
            "extensions": None,
            "http_version": "1.1",
            "path": "/ws-probe",
            "path_params": {},
            "path_template": "/ws-probe",
            "query_string": b"",
            "raw_path": b"/ws-probe",
            "root_path": "",
            "route_handler": _ws_probe,
            "scheme": "ws",
            "server": ("testserver", 80),
            "session": None,
            "state": {},
            "subprotocols": [],
            "user": None,
            "headers": [],
        },
    )


def test_basic_auth_middleware_challenges_and_allows_access() -> None:
    """BasicAuthMiddleware challenges invalid credentials and allows valid ones."""
    test_app = _build_app(
        middleware=[
            DefineMiddleware(
                BasicAuthMiddleware,
                username="admin",
                password="secret",
                realm="Realm",
            )
        ]
    )

    client = TestClient(test_app)

    # Missing credentials
    response = client.get("/protected")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Basic realm="Realm"'

    # Wrong credentials
    wrong = client.get("/protected", headers=_basic_auth_header("admin", "wrong"))
    assert wrong.status_code == 401

    # Correct credentials
    success = client.get("/protected", headers=_basic_auth_header("admin", "secret"))
    assert success.status_code == 200
    assert success.json() == {"ok": True}


def test_basic_auth_middleware_sets_request_user_and_auth() -> None:
    """Successful authentication should populate request.user and request.auth."""

    @get("/identity")
    async def identity(request: Request) -> dict[str, str]:
        return {"user": request.user, "auth": request.auth}

    test_app = Litestar(
        route_handlers=[identity],
        middleware=[
            DefineMiddleware(
                BasicAuthMiddleware,
                username="admin",
                password="secret",
                realm="Realm",
            )
        ],
    )

    client = TestClient(test_app)
    response = client.get("/identity", headers=_basic_auth_header("admin", "secret"))

    assert response.status_code == 200
    assert response.json() == {"user": "admin", "auth": "basic"}


def test_basic_auth_middleware_allows_htpasswd_credentials(tmp_path: Path) -> None:
    """Bcrypt htpasswd entries should authenticate Basic credentials."""
    password_hash = bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode()
    htpasswd_path = tmp_path / "users.htpasswd"
    htpasswd_path.write_text(f"admin:{password_hash}\n", encoding="utf-8")
    test_app = _build_app(
        middleware=[DefineMiddleware(BasicAuthMiddleware, htpasswd_path=htpasswd_path)]
    )

    client = TestClient(test_app)

    wrong_response = client.get(
        "/protected", headers=_basic_auth_header("admin", "wrong")
    )
    assert wrong_response.status_code == 401
    assert (
        client.get(
            "/protected", headers=_basic_auth_header("admin", "secret")
        ).status_code
        == 200
    )


def test_basic_auth_middleware_bypasses_probe_endpoints() -> None:
    """BasicAuthMiddleware should not challenge unauthenticated probe endpoints."""
    test_app = _build_app(
        middleware=[
            DefineMiddleware(
                BasicAuthMiddleware,
                username="admin",
                password="secret",
                realm="Realm",
            )
        ],
        include_probe_routes=True,
    )

    client = TestClient(test_app)

    health = client.get("/livez")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    health_alias = client.get("/healthz")
    assert health_alias.status_code == 200
    assert health_alias.json() == {"status": "ok"}

    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok", "ready": True}

    assert client.get("/protected").status_code == 401


def test_basic_auth_extract_credentials_handles_invalid_headers() -> None:
    """Malformed Authorization headers should be ignored safely."""

    middleware = BasicAuthMiddleware(app=_noop_asgi_app)

    scope = _make_header_scope([])
    assert middleware._extract_credentials(scope) is None

    bad_scheme = _make_header_scope([(b"authorization", b"Bearer token")])
    assert middleware._extract_credentials(bad_scheme) is None

    invalid_base64 = _make_header_scope([(b"authorization", b"Basic !!!")])
    assert middleware._extract_credentials(invalid_base64) is None

    missing_separator = _make_header_scope([(b"authorization", b"Basic dXNlcg==")])
    assert middleware._extract_credentials(missing_separator) is None


@pytest.mark.asyncio
async def test_basic_auth_middleware_authenticates_websocket() -> None:
    """WebSocket scopes should require the same Basic credentials as HTTP."""
    calls: list[Scope] = []

    async def app(scope, receive, send) -> None:
        calls.append(scope)

    async def _middleware_receive() -> WebSocketReceiveEvent:
        return {"type": "websocket.receive", "bytes": None, "text": "hello"}

    async def _middleware_send(message) -> None:
        pass

    middleware = BasicAuthMiddleware(app, username="admin", password="secret")
    with pytest.raises(NotAuthorizedException):
        await middleware(_make_websocket_scope(), _middleware_receive, _middleware_send)

    scope = _make_websocket_scope()
    scope["headers"] = [
        (key.lower().encode(), value.encode())
        for key, value in _basic_auth_header("admin", "secret").items()
    ]
    await middleware(scope, _middleware_receive, _middleware_send)

    assert calls == [scope]


def test_create_app_registers_basic_auth_middleware_when_configured(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """create_app should attach BasicAuthMiddleware when credentials are configured."""
    web_config = WebConfig(
        basic_auth=BasicAuthConfig(
            username="admin",
            password=SecretStr("secret"),
            realm="Realm",
        )
    )
    test_config = AnibridgeConfig(web=web_config)
    monkeypatch.setattr(app_module, "get_config", lambda: test_config)

    # Ensure the SPA assets check passes
    index_file = tmp_path / "index.html"
    index_file.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(app_module, "FRONTEND_BUILD_DIR", tmp_path, raising=False)

    app = app_module.create_app()

    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/status", auth=("admin", "secret")).status_code == 200


def test_create_app_exempts_prefixed_probe_routes(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """Prefixed health probes should bypass Basic Auth."""
    web_config = WebConfig(
        path_prefix="/anibridge",
        basic_auth=BasicAuthConfig(username="admin", password=SecretStr("secret")),
    )
    monkeypatch.setattr(
        app_module, "get_config", lambda: AnibridgeConfig(web=web_config)
    )

    index_file = tmp_path / "index.html"
    index_file.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(app_module, "FRONTEND_BUILD_DIR", tmp_path, raising=False)

    app = app_module.create_app()

    with TestClient(app) as client:
        assert client.get("/anibridge/healthz").status_code == 200
        assert client.get("/anibridge/api/status").status_code == 401


def test_create_app_registers_basic_auth_middleware_with_htpasswd(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """create_app should treat an htpasswd file as configured auth."""
    password_hash = bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode()
    htpasswd_path = tmp_path / "users.htpasswd"
    htpasswd_path.write_text(f"admin:{password_hash}\n", encoding="utf-8")
    test_config = AnibridgeConfig(
        web=WebConfig(basic_auth=BasicAuthConfig(htpasswd_path=htpasswd_path))
    )
    monkeypatch.setattr(app_module, "get_config", lambda: test_config)

    index_file = tmp_path / "index.html"
    index_file.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(app_module, "FRONTEND_BUILD_DIR", tmp_path, raising=False)

    app = app_module.create_app()

    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/status", auth=("admin", "secret")).status_code == 200


def test_create_app_skips_basic_auth_without_complete_credentials(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """create_app should skip BasicAuthMiddleware if either credential is missing."""
    web_config = WebConfig(
        basic_auth=BasicAuthConfig(
            username="admin",
            password=None,
            realm="Realm",
        )
    )
    incomplete_config = AnibridgeConfig(web=web_config)
    monkeypatch.setattr(app_module, "get_config", lambda: incomplete_config)

    index_file = tmp_path / "index.html"
    index_file.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(app_module, "FRONTEND_BUILD_DIR", tmp_path, raising=False)

    app = app_module.create_app()

    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 200


def test_create_app_lifespan_purges_ephemeral_history_on_startup(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """Starting the app should delete ephemeral history rows."""
    with db() as ctx:
        ctx.session.query(SyncHistoryOperation).delete()
        ctx.session.query(SyncHistoryGroup).delete()
        ctx.session.query(SyncHistoryRun).delete()
        for index, (key, ephemeral) in enumerate(
            (("persisted", False), ("ephemeral", True)),
            start=1,
        ):
            timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index)
            run = SyncHistoryRun(
                profile_name="profile",
                source_namespace="lib",
                target_namespace="alist",
                outcome=SyncOutcome.SYNCED,
                ephemeral=ephemeral,
                started_at=timestamp,
                completed_at=timestamp,
            )
            ctx.session.add(run)
            ctx.session.flush()
            group = SyncHistoryGroup(
                run_id=run.id,
                profile_name="profile",
                source_namespace="lib",
                source_parent_ref={"key": key, "path": []},
                target_namespace="alist",
                target_parent_ref={"key": key, "path": []},
                outcome=SyncOutcome.SYNCED,
                operation_count=1,
                record_count=1,
                event_count=0,
                node_count=0,
                error_count=0,
                ephemeral=ephemeral,
                timestamp=timestamp,
            )
            ctx.session.add(group)
            ctx.session.flush()
            ctx.session.add(
                SyncHistoryOperation(
                    group_id=group.id,
                    profile_name="profile",
                    resource_kind=SyncResourceKind.RECORD,
                    action=SyncOperationAction.UPSERT,
                    source_namespace="lib",
                    source_ref={"key": key, "path": []},
                    target_namespace="alist",
                    target_ref={"key": key, "path": []},
                    outcome=SyncOutcome.SYNCED,
                    ephemeral=ephemeral,
                    timestamp=timestamp,
                )
            )
        ctx.session.commit()

    index_file = tmp_path / "index.html"
    index_file.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(app_module, "FRONTEND_BUILD_DIR", tmp_path, raising=False)

    async def _ensure_public_anilist():
        return SimpleNamespace()

    monkeypatch.setattr(
        get_app_state(),
        "ensure_public_anilist",
        _ensure_public_anilist,
        raising=True,
    )

    app = app_module.create_app()
    with TestClient(app):
        pass

    with db() as ctx:
        rows = (
            ctx.session.query(SyncHistoryOperation)
            .order_by(SyncHistoryOperation.source_ref.asc())
            .all()
        )
        assert len(rows) == 1
        assert rows[0].source_ref["key"] == "persisted"
        assert rows[0].ephemeral is False
