"""API routes for managing target record-field pins."""

from typing import Annotated

import msgspec
from anibridge.provider.base import RecordField
from litestar.exceptions.http_exceptions import HTTPException
from litestar.handlers.http_handlers.decorators import delete, get, put
from litestar.params import Body, PathParameter, QueryParameter
from litestar.router import Router

from anibridge.app.web.services.pin_service import (
    PinEntry,
    PinFieldOption,
    PinSearchResult,
    get_pin_service,
)

__all__ = ["router"]


class PinListResponse(msgspec.Struct):
    """Response model for listing pins."""

    pins: Annotated[
        list[PinEntry],
        msgspec.Meta(
            description="Pinned entries for the requested profile.",
            examples=[
                [
                    {
                        "profile_name": "default",
                        "target_namespace": "anilist",
                        "target_ref": {"key": "5114", "path": []},
                        "fields": ["status"],
                    }
                ]
            ],
        ),
    ]


class PinOptionsResponse(msgspec.Struct):
    """Response model for available pin field options."""

    options: Annotated[
        list[PinFieldOption],
        msgspec.Meta(
            description="Selectable sync fields that can be pinned.",
            examples=[[{"value": "status", "label": "Status"}]],
        ),
    ]


class PinSearchResponse(msgspec.Struct):
    """Response model for provider search results within the pin manager."""

    results: Annotated[
        list[PinSearchResult],
        msgspec.Meta(
            description="Provider search results enriched with current pin state.",
            examples=[[{"media": {"namespace": "anilist", "key": "5114"}}]],
        ),
    ]


class UpdatePinRequest(msgspec.Struct):
    """Request body for updating pin fields."""

    fields: Annotated[
        list[str],
        msgspec.Meta(
            min_length=1,
            description="Requested sync fields to pin for the target media item.",
            examples=[["status", "progress"]],
        ),
    ] = msgspec.field(default_factory=list)


class OkResponse(msgspec.Struct):
    """Response model for successful operations."""

    ok: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the pin operation completed successfully.",
            examples=[True],
        ),
    ] = True


@get(path="/fields", sync_to_thread=True)
def get_pin_fields() -> PinOptionsResponse:
    """Return selectable pin field metadata."""
    service = get_pin_service()
    return PinOptionsResponse(options=service.list_options())


@get(path="/{profile:str}")
async def list_pins(
    profile: Annotated[str, PathParameter()],
    with_media: Annotated[bool, QueryParameter()] = False,
) -> PinListResponse:
    """Return all pins for a profile."""
    service = get_pin_service()
    pins = await service.list_pins(profile, with_media=with_media)
    return PinListResponse(pins=pins)


@get(path="/{profile:str}/search")
async def search_pins(
    profile: Annotated[str, PathParameter()],
    q: Annotated[str, QueryParameter(min_length=1)],
    limit: Annotated[int, QueryParameter(ge=1, le=50)] = 10,
) -> PinSearchResponse:
    """Search the target provider for entries that can be pinned."""
    try:
        results = await get_pin_service().search_pins(profile, q, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PinSearchResponse(results=results)


@get(path="/{profile:str}/{media_key:str}")
async def get_pin(
    profile: Annotated[str, PathParameter()],
    media_key: Annotated[str, PathParameter()],
    with_media: Annotated[bool, QueryParameter()] = False,
) -> PinEntry:
    """Retrieve pin configuration for a specific target anchor ref."""
    service = get_pin_service()
    entry = await service.get_pin(profile, media_key, with_media=with_media)
    if not entry:
        raise HTTPException(status_code=404, detail="Pin not found")
    return entry


@put(path="/{profile:str}/{media_key:str}")
async def upsert_pin(
    data: Annotated[UpdatePinRequest, Body()],
    profile: Annotated[str, PathParameter()],
    media_key: Annotated[str, PathParameter()],
    with_media: Annotated[bool, QueryParameter()] = False,
) -> PinEntry:
    """Create or update pin fields for a media item."""
    allowed_fields = {field.value for field in RecordField}
    normalized_fields: list[str] = []
    for raw_field in data.fields:
        value = str(raw_field).strip().lower()
        if not value:
            continue
        if value not in allowed_fields:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported field '{raw_field}'",
            )
        if value not in normalized_fields:
            normalized_fields.append(value)

    try:
        entry = await get_pin_service().upsert_pin(
            profile, media_key, normalized_fields, with_media=with_media
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return entry


@delete(path="/{profile:str}/{media_key:str}", status_code=200, sync_to_thread=True)
def delete_pin(
    profile: Annotated[str, PathParameter()],
    media_key: Annotated[str, PathParameter()],
) -> OkResponse:
    """Delete pin configuration for a target anchor ref."""
    get_pin_service().delete_pin(profile, media_key)
    return OkResponse()


router = Router(
    path="/pins",
    route_handlers=[
        get_pin_fields,
        list_pins,
        search_pins,
        get_pin,
        upsert_pin,
        delete_pin,
    ],
)
