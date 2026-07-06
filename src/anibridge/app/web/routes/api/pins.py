"""API routes for managing target parent pins."""

from typing import Annotated

import msgspec
from litestar.exceptions.http_exceptions import HTTPException
from litestar.handlers.http_handlers.decorators import delete, get, put
from litestar.params import PathParameter, QueryParameter
from litestar.router import Router

from anibridge.app.web.services.pin_service import (
    PinEntry,
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
                        "target_parent_ref": {"key": "5114", "path": []},
                    }
                ]
            ],
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


class OkResponse(msgspec.Struct):
    """Response model for successful operations."""

    ok: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the pin operation completed successfully.",
            examples=[True],
        ),
    ] = True


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
    """Retrieve pin state for a specific target parent ref."""
    service = get_pin_service()
    entry = await service.get_pin(profile, media_key, with_media=with_media)
    if not entry:
        raise HTTPException(status_code=404, detail="Pin not found")
    return entry


@put(path="/{profile:str}/{media_key:str}")
async def upsert_pin(
    profile: Annotated[str, PathParameter()],
    media_key: Annotated[str, PathParameter()],
    with_media: Annotated[bool, QueryParameter()] = False,
) -> PinEntry:
    """Create or update a target parent pin."""
    try:
        entry = await get_pin_service().upsert_pin(
            profile, media_key, with_media=with_media
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return entry


@delete(path="/{profile:str}/{media_key:str}", status_code=200, sync_to_thread=True)
def delete_pin(
    profile: Annotated[str, PathParameter()],
    media_key: Annotated[str, PathParameter()],
) -> OkResponse:
    """Delete pin state for a target parent ref."""
    get_pin_service().delete_pin(profile, media_key)
    return OkResponse()


router = Router(
    path="/pins",
    route_handlers=[
        list_pins,
        search_pins,
        get_pin,
        upsert_pin,
        delete_pin,
    ],
)
