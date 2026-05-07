"""Configuration management API endpoints."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ValidationError

from anibridge.app.config.settings import AnibridgeConfig, get_config
from anibridge.app.exceptions import SchedulerUnavailableError
from anibridge.app.web.services.configuration_service import get_configuration_service

__all__ = ["router"]


class ConfigDocumentResponse(BaseModel):
    config_path: str
    file_exists: bool
    content: str
    mtime: int | None = None
    schema_: dict[str, Any] = Field(alias="schema")


class ConfigDocumentUpdateRequest(BaseModel):
    content: str = Field(min_length=0)
    expected_mtime: int | None = None


class ConfigUpdateResponse(BaseModel):
    ok: bool
    profiles: list[str]
    requires_restart: bool = True
    mtime: int | None = None


def require_config_api_access() -> None:
    """Ensure configuration API access is not exposed without explicit opt-in."""
    # Use runtime_config if present (for test overrides), else get_config()
    from anibridge.app.web.routes.api import config as config_api_module

    web_config = None
    if hasattr(config_api_module, "runtime_config"):
        web_config = getattr(config_api_module.runtime_config, "web", None)
    if web_config is None:
        web_config = get_config().web
    if web_config.has_auth or web_config.allow_config_without_auth:
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "Configuration API is disabled when web authentication is not configured. "
            "Configure web.basic_auth or set "
            "web.allow_config_without_auth=true to override."
        ),
    )


router = APIRouter(dependencies=[Depends(require_config_api_access)])


@router.get("", response_model=ConfigDocumentResponse)
def get_configuration() -> ConfigDocumentResponse:
    """Return the current configuration as raw YAML text.

    Returns:
        ConfigDocumentResponse: The configuration document details.
    """
    try:
        payload = get_configuration_service().load_document_text()
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.errors(),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    payload["schema"] = AnibridgeConfig.model_json_schema()
    return ConfigDocumentResponse(**payload)


@router.post("", response_model=ConfigUpdateResponse)
async def update_configuration(
    request: ConfigDocumentUpdateRequest,
) -> ConfigUpdateResponse:
    """Persist the provided configuration document.

    Args:
        request (ConfigDocumentUpdateRequest): The configuration update request.

    Returns:
        ConfigUpdateResponse: The result of the update operation.
    """
    try:
        (
            config,
            requires_restart,
            mtime,
        ) = await get_configuration_service().save_document_text(
            request.content,
            expected_mtime=request.expected_mtime,
        )
    except FileExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.errors(),
        ) from exc
    except SchedulerUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return ConfigUpdateResponse(
        ok=True,
        profiles=sorted(config.profiles.keys()),
        requires_restart=requires_restart,
        mtime=mtime,
    )


@router.get("/openapi.json")
def get_openapi_schema() -> dict:
    """Return the OpenAPI schema for the configuration API.

    Returns:
        dict: OpenAPI schema
    """
    return AnibridgeConfig.model_json_schema()
