"""Tests for HTTP Basic Authentication middleware and integration."""

import base64
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pytest import MonkeyPatch

from anibridge.app.config.settings import AniBridgeConfig, BasicAuthConfig, WebConfig
from anibridge.app.web import app as app_module
from anibridge.app.web.middlewares.basic_auth import BasicAuthMiddleware


def _basic_auth_header(username: str, password: str) -> dict[str, str]:
    """Build an Authorization header for HTTP Basic credentials."""
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_basic_auth_middleware_challenges_and_allows_access() -> None:
    """BasicAuthMiddleware challenges invalid credentials and allows valid ones."""
    test_app = FastAPI()
    test_app.add_middleware(
        BasicAuthMiddleware,  # type: ignore[arg-type]
        username="admin",
        password="secret",
        realm="Realm",
    )

    @test_app.get("/protected")
    async def protected() -> dict[str, bool]:
        return {"ok": True}

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


def test_basic_auth_middleware_allows_access_with_htpasswd(
    tmp_path: Path,
) -> None:
    """BasicAuthMiddleware allows access with valid htpasswd credentials."""
    htpasswd_file = tmp_path / "htpasswd"
    htpasswd_file.write_text(
        "test:$2y$10$AVmi7rydBM1wRpzyrv2V5eGmBdYiHLIq07V.xOGza.tBTkTa1eZ1S",
        encoding="utf-8",
    )  # bcrypt hash for "test"

    test_app = FastAPI()
    test_app.add_middleware(
        BasicAuthMiddleware,  # type: ignore[arg-type]
        htpasswd_path=htpasswd_file,
        realm="Realm",
    )

    @test_app.get("/protected")
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(test_app)

    # Wrong credentials
    wrong = client.get("/protected", headers=_basic_auth_header("test", "wrong"))
    assert wrong.status_code == 401

    # Correct credentials
    success = client.get("/protected", headers=_basic_auth_header("test", "test"))
    assert success.status_code == 200
    assert success.json() == {"ok": True}


def test_basic_auth_middleware_plain_and_htpasswd(
    tmp_path: Path,
) -> None:
    """BasicAuthMiddleware allows access with both plain and htpasswd credentials."""
    htpasswd_file = tmp_path / "htpasswd"
    htpasswd_file.write_text(
        "htuser:$2y$10$AVmi7rydBM1wRpzyrv2V5eGmBdYiHLIq07V.xOGza.tBTkTa1eZ1S",
        encoding="utf-8",
    )  # bcrypt hash for "test"

    test_app = FastAPI()
    test_app.add_middleware(
        BasicAuthMiddleware,  # type: ignore[arg-type]
        username="plainuser",
        password="plainpass",
        htpasswd_path=htpasswd_file,
        realm="Realm",
    )

    @test_app.get("/protected")
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(test_app)

    # Correct plain credentials
    success_plain = client.get(
        "/protected", headers=_basic_auth_header("plainuser", "plainpass")
    )
    assert success_plain.status_code == 200
    assert success_plain.json() == {"ok": True}

    # Correct htpasswd credentials
    success_htpasswd = client.get(
        "/protected", headers=_basic_auth_header("htuser", "test")
    )
    assert success_htpasswd.status_code == 200
    assert success_htpasswd.json() == {"ok": True}


def test_create_app_registers_basic_auth_middleware_when_configured(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """create_app should attach BasicAuthMiddleware when credentials are configured."""
    web_config = WebConfig(
        basic_auth=BasicAuthConfig(
            username="admin",
            password=SecretStr("secret"),
            htpasswd_path=None,
            realm="Realm",
        )
    )
    test_config = AniBridgeConfig(web=web_config)
    monkeypatch.setattr(app_module, "config", test_config, raising=False)

    # Ensure the SPA assets check passes
    index_file = tmp_path / "index.html"
    index_file.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(app_module, "FRONTEND_BUILD_DIR", tmp_path, raising=False)

    app = app_module.create_app()

    middleware_classes = {middleware.cls for middleware in app.user_middleware}
    assert BasicAuthMiddleware in middleware_classes


def test_create_app_skips_basic_auth_without_complete_credentials(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """create_app should skip BasicAuthMiddleware if either credential is missing."""
    web_config = WebConfig(
        basic_auth=BasicAuthConfig(
            username="admin",
            password=None,
            htpasswd_path=None,
            realm="Realm",
        )
    )
    incomplete_config = AniBridgeConfig(web=web_config)
    monkeypatch.setattr(app_module, "config", incomplete_config, raising=False)

    index_file = tmp_path / "index.html"
    index_file.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(app_module, "FRONTEND_BUILD_DIR", tmp_path, raising=False)

    app = app_module.create_app()

    middleware_classes = {middleware.cls for middleware in app.user_middleware}
    assert BasicAuthMiddleware not in middleware_classes


def test_create_app_registers_basic_auth_middleware_with_htpasswd(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """create_app should attach BasicAuthMiddleware when htpasswd path is configured."""
    htpasswd_file = tmp_path / "htpasswd"
    htpasswd_file.write_text(
        "test:$2y$10$AVmi7rydBM1wRpzyrv2V5eGmBdYiHLIq07V.xOGza.tBTkTa1eZ1S",
        encoding="utf-8",
    )  # bcrypt hash for "test"

    web_config = WebConfig(
        basic_auth=BasicAuthConfig(
            username=None,
            password=None,
            htpasswd_path=htpasswd_file,
            realm="Realm",
        )
    )
    test_config = AniBridgeConfig(web=web_config)
    monkeypatch.setattr(app_module, "config", test_config, raising=False)

    # Ensure the SPA assets check passes
    index_file = tmp_path / "index.html"
    index_file.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(app_module, "FRONTEND_BUILD_DIR", tmp_path, raising=False)

    app = app_module.create_app()

    middleware_classes = {middleware.cls for middleware in app.user_middleware}
    assert BasicAuthMiddleware in middleware_classes
