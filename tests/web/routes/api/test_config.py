"""Tests for configuration API access policy."""

from typing import Any, cast

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from anibridge.app.exceptions import SchedulerUnavailableError
from anibridge.app.web.routes.api import config as config_api_module


@pytest.mark.parametrize(
    ("has_auth", "allow_without_auth", "expected_status"),
    [
        pytest.param(False, False, 403, id="blocked-by-default"),
        pytest.param(False, True, 200, id="explicit-override"),
        pytest.param(True, False, 200, id="configured-auth"),
    ],
)
def test_config_api_access_policy(
    api_client_factory,
    set_config_api_access,
    has_auth: bool,
    allow_without_auth: bool,
    expected_status: int,
) -> None:
    """Config API access policy should match web auth configuration."""
    set_config_api_access(
        has_auth=has_auth,
        allow_config_without_auth=allow_without_auth,
    )

    response = api_client_factory(config_api_module.router, "/api/config").get(
        "/api/config/openapi.json"
    )

    assert response.status_code == expected_status
    if expected_status == 403:
        assert "Configuration API is disabled" in response.json()["detail"]


def _validation_error() -> ValidationError:
    class _Model(BaseModel):
        x: int

    try:
        _Model(x=cast(Any, "bad"))
    except ValidationError as exc:
        return exc
    raise AssertionError("expected validation error")


def test_require_config_api_access_can_fall_back_to_get_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(config_api_module, "runtime_config", raising=False)
    monkeypatch.setattr(
        config_api_module,
        "get_config",
        lambda: type(
            "Cfg",
            (),
            {
                "web": type(
                    "Web",
                    (),
                    {"has_auth": True, "allow_config_without_auth": False},
                )()
            },
        )(),
    )

    config_api_module.require_config_api_access()


def test_get_configuration_success_and_error_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_api_module,
        "get_configuration_service",
        lambda: type(
            "Svc",
            (),
            {
                "load_document_text": lambda self: {
                    "config_path": "/tmp/config.yaml",
                    "file_exists": True,
                    "content": "profiles: {}",
                    "mtime": 123,
                }
            },
        )(),
    )

    response = config_api_module.get_configuration()
    assert response.file_exists is True
    assert "title" in response.schema_

    monkeypatch.setattr(
        config_api_module,
        "get_configuration_service",
        lambda: type(
            "Svc",
            (),
            {
                "load_document_text": lambda self: (_ for _ in ()).throw(
                    ValueError("bad config")
                )
            },
        )(),
    )
    with pytest.raises(HTTPException, match="bad config"):
        config_api_module.get_configuration()

    monkeypatch.setattr(
        config_api_module,
        "get_configuration_service",
        lambda: type(
            "Svc",
            (),
            {
                "load_document_text": lambda self: (_ for _ in ()).throw(
                    _validation_error()
                )
            },
        )(),
    )
    with pytest.raises(HTTPException) as excinfo:
        config_api_module.get_configuration()
    assert excinfo.value.status_code == 422


@pytest.mark.asyncio
async def test_update_configuration_success_and_error_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = config_api_module.ConfigDocumentUpdateRequest(
        content="profiles: {}", expected_mtime=123
    )

    class _Service:
        async def save_document_text(self, content: str, expected_mtime: int | None):
            assert content == "profiles: {}"
            assert expected_mtime == 123
            return (
                type("Cfg", (), {"profiles": {"b": object(), "a": object()}})(),
                False,
                456,
            )

    monkeypatch.setattr(
        config_api_module, "get_configuration_service", lambda: _Service()
    )
    response = await config_api_module.update_configuration(request)
    assert response.profiles == ["a", "b"]
    assert response.requires_restart is False
    assert response.mtime == 456

    for exc, status_code in [
        (FileExistsError("stale"), 409),
        (ValueError("bad"), 400),
        (_validation_error(), 422),
        (SchedulerUnavailableError("busy"), 503),
    ]:

        class _ErrorService:
            async def save_document_text(
                self,
                content: str,
                expected_mtime: int | None,
                *,
                error=exc,
            ):
                raise error

        monkeypatch.setattr(
            config_api_module, "get_configuration_service", lambda: _ErrorService()
        )
        with pytest.raises(HTTPException) as excinfo:
            await config_api_module.update_configuration(request)
        assert excinfo.value.status_code == status_code
