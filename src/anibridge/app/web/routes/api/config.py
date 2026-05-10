"""Configuration management API endpoints."""

from typing import Annotated

import msgspec
from litestar.exceptions.http_exceptions import HTTPException
from litestar.handlers.http_handlers.decorators import get, post
from litestar.params import Body
from litestar.router import Router
from pydantic import ValidationError

from anibridge.app.config.settings import AnibridgeConfig, get_config
from anibridge.app.exceptions import SchedulerUnavailableError
from anibridge.app.web.services.configuration_service import get_configuration_service

__all__ = ["router"]


class ConfigDocumentResponse(msgspec.Struct):
    config_path: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Absolute path to the active AniBridge configuration file.",
            examples=["/data/config.yaml"],
        ),
    ]
    file_exists: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the configuration file exists on disk.",
            examples=[True],
        ),
    ]
    content: Annotated[
        str,
        msgspec.Meta(
            description="Raw YAML contents of the configuration file.",
            examples=["profiles:\n  default: {}\n"],
        ),
    ]
    schema: Annotated[
        dict[str, object],
        msgspec.Meta(
            description="JSON Schema document describing the configuration file.",
            examples=[{"title": "AnibridgeConfig", "type": "object"}],
        ),
    ]
    settings: (
        Annotated[
            dict[str, object],
            msgspec.Meta(
                description=(
                    "Parsed configuration mapping for the guided UI. Null when the "
                    "current YAML cannot be parsed into a mapping."
                ),
                examples=[{"global_config": {}, "profiles": {}}],
            ),
        ]
        | None
    ) = None
    settings_error: (
        Annotated[
            str,
            msgspec.Meta(
                description=(
                    "Parsing error for the guided UI when the current YAML cannot be "
                    "represented as structured settings."
                ),
                examples=["Invalid YAML syntax: expected <block end>"],
            ),
        ]
        | None
    ) = None
    mtime: (
        Annotated[
            int,
            msgspec.Meta(
                ge=0,
                description=(
                    "Last modification time of the configuration file "
                    "in epoch milliseconds."
                ),
                examples=[1715179200000],
            ),
        ]
        | None
    ) = None


class ConfigDocumentUpdateRequest(msgspec.Struct):
    content: Annotated[
        str,
        msgspec.Meta(
            description="Full YAML document to persist as the new configuration.",
            examples=["profiles:\n  default: {}\n"],
        ),
    ] = ""
    expected_mtime: (
        Annotated[
            int,
            msgspec.Meta(
                ge=0,
                description=(
                    "Last known file modification time used for "
                    "optimistic concurrency checks."
                ),
                examples=[1715179200000],
            ),
        ]
        | None
    ) = None


class ConfigStructuredUpdateRequest(msgspec.Struct):
    settings: Annotated[
        dict[str, object],
        msgspec.Meta(
            description="Structured AniBridge configuration payload for the guided UI.",
            examples=[{"global_config": {}, "profiles": {}}],
        ),
    ]
    expected_mtime: (
        Annotated[
            int,
            msgspec.Meta(
                ge=0,
                description=(
                    "Last known file modification time used for optimistic "
                    "concurrency checks."
                ),
                examples=[1715179200000],
            ),
        ]
        | None
    ) = None


class ConfigUpdateResponse(msgspec.Struct):
    ok: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the configuration update was accepted.",
            examples=[True],
        ),
    ]
    profiles: Annotated[
        list[str],
        msgspec.Meta(
            description="Sorted profile names present in the saved configuration.",
            examples=[["default", "movies"]],
        ),
    ]
    requires_restart: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the change requires a process restart to fully apply.",
            examples=[True],
        ),
    ] = True
    mtime: (
        Annotated[
            int,
            msgspec.Meta(
                ge=0,
                description=(
                    "New modification time of the saved configuration file "
                    "in epoch milliseconds."
                ),
                examples=[1715179260000],
            ),
        ]
        | None
    ) = None


def require_config_api_access() -> None:
    """Ensure configuration API access is not exposed without explicit opt-in."""
    web_config = getattr(globals().get("runtime_config"), "web", None)
    if web_config is None:
        web_config = get_config().web
    if web_config.has_auth or web_config.allow_config_without_auth:
        return

    raise HTTPException(
        status_code=403,
        detail=(
            "Configuration API is disabled when web authentication is not configured. "
            "Configure web.basic_auth or set "
            "web.allow_config_without_auth=true to override."
        ),
    )


@get(path="", sync_to_thread=True)
def get_configuration() -> ConfigDocumentResponse:
    """Return the current configuration as raw YAML text.

    Returns:
        ConfigDocumentResponse: The configuration document details.
    """
    require_config_api_access()
    try:
        payload = get_configuration_service().load_document_text()
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc.errors()),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    document = dict(payload)
    document["schema"] = AnibridgeConfig.model_json_schema()
    return msgspec.convert(document, type=ConfigDocumentResponse)


@post(path="", status_code=200)
async def update_configuration(
    data: Annotated[ConfigDocumentUpdateRequest, Body()],
) -> ConfigUpdateResponse:
    """Persist the provided configuration document.

    Args:
        data (ConfigDocumentUpdateRequest): The configuration update request.

    Returns:
        ConfigUpdateResponse: The result of the update operation.
    """
    require_config_api_access()
    try:
        (
            config,
            requires_restart,
            mtime,
        ) = await get_configuration_service().save_document_text(
            data.content,
            expected_mtime=data.expected_mtime,
        )
    except FileExistsError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc.errors()),
        ) from exc
    except SchedulerUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    return ConfigUpdateResponse(
        ok=True,
        profiles=sorted(config.profiles.keys()),
        requires_restart=requires_restart,
        mtime=mtime,
    )


@post(path="/structured", status_code=200)
async def update_configuration_structured(
    data: Annotated[ConfigStructuredUpdateRequest, Body()],
) -> ConfigUpdateResponse:
    """Persist the provided structured configuration payload.

    Args:
        data (ConfigStructuredUpdateRequest): The structured configuration update.

    Returns:
        ConfigUpdateResponse: The result of the update operation.
    """
    require_config_api_access()
    try:
        (
            config,
            requires_restart,
            mtime,
        ) = await get_configuration_service().save_settings_payload(
            data.settings,
            expected_mtime=data.expected_mtime,
        )
    except FileExistsError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc.errors()),
        ) from exc
    except SchedulerUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    return ConfigUpdateResponse(
        ok=True,
        profiles=sorted(config.profiles.keys()),
        requires_restart=requires_restart,
        mtime=mtime,
    )


router = Router(
    path="/config",
    route_handlers=[
        get_configuration,
        update_configuration,
        update_configuration_structured,
    ],
)
