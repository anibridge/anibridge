"""API routes for managing field pins across list providers."""

from fastapi.exceptions import HTTPException
from fastapi.param_functions import Path, Query
from fastapi.routing import APIRouter
from pydantic import BaseModel, Field

from anibridge.app.config.settings import SyncField
from anibridge.app.models.schemas.provider import ProviderMediaMetadata
from anibridge.app.web.services.pin_service import (
    PinEntry,
    PinFieldOption,
    UpdatePinPayload,
    get_pin_service,
)

router = APIRouter()


class PinListResponse(BaseModel):
    """Response model for listing pins."""

    pins: list[PinEntry]


class PinOptionsResponse(BaseModel):
    """Response model for available pin field options."""

    options: list[PinFieldOption]


class PinSearchItem(BaseModel):
    """Search result item combining provider metadata with existing pin state."""

    media: ProviderMediaMetadata
    pin: PinEntry | None = None


class PinSearchResponse(BaseModel):
    """Response model for provider search results within the pin manager."""

    results: list[PinSearchItem]


class UpdatePinRequest(BaseModel):
    """Request body for updating pin fields."""

    fields: list[SyncField | str] = Field(default_factory=list)

    def to_payload(self) -> UpdatePinPayload:
        """Convert request into payload for the service layer."""
        payload = UpdatePinPayload(fields=[])
        normalized: list[str] = []
        for field in self.fields:
            value = field.value if isinstance(field, SyncField) else str(field)
            normalized.append(value)
        payload.fields = normalized
        return payload


class OkResponse(BaseModel):
    """Response model for successful operations."""

    ok: bool = True


@router.get("/fields", response_model=PinOptionsResponse)
def get_pin_fields() -> PinOptionsResponse:
    """Return selectable pin field metadata."""
    service = get_pin_service()
    return PinOptionsResponse(options=service.list_options())


@router.get("/{profile}", response_model=PinListResponse)
async def list_pins(
    profile: str = Path(..., min_length=1),
    with_media: bool = Query(False),
) -> PinListResponse:
    """Return all pins for a profile.

    Args:
        profile (str): Profile name.
        with_media (bool): When True, include provider metadata.
    """
    service = get_pin_service()
    pins = await service.list_pins(profile, with_media=with_media)
    return PinListResponse(pins=pins)


@router.get("/{profile}/{media_key}", response_model=PinEntry)
async def get_pin(
    profile: str = Path(..., min_length=1),
    media_key: str = Path(..., min_length=1),
    with_media: bool = Query(False),
) -> PinEntry:
    """Retrieve pin configuration for a specific list entry.

    Args:
        profile (str): Profile name.
        media_key (str): Media key.
        with_media (bool): When True, include provider metadata.

    Returns:
        PinEntry: Pin configuration.
    """
    service = get_pin_service()
    entry = await service.get_pin(profile, media_key, with_media=with_media)
    if not entry:
        raise HTTPException(status_code=404, detail="Pin not found")
    return entry


@router.put("/{profile}/{media_key}", response_model=PinEntry)
async def upsert_pin(
    request: UpdatePinRequest,
    profile: str = Path(..., min_length=1),
    media_key: str = Path(..., min_length=1),
    with_media: bool = Query(False),
) -> PinEntry:
    """Create or update pin fields for a media item.

    Args:
        request (UpdatePinRequest): Pin update request payload.
        profile (str): Profile name.
        media_key (str): Media key.
        with_media (bool): When True, include provider metadata.
    """
    payload = request.to_payload()
    try:
        entry = await get_pin_service().upsert_pin(
            profile, media_key, payload.normalized(), with_media=with_media
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return entry


@router.delete("/{profile}/{media_key}", response_model=OkResponse)
def delete_pin(
    profile: str = Path(..., min_length=1),
    media_key: str = Path(..., min_length=1),
) -> OkResponse:
    """Delete pin configuration for an list entry.

    Args:
        profile (str): Profile name.
        media_key (str): Media key.

    Returns:
        OkResponse: Confirmation of successful deletion.
    """
    get_pin_service().delete_pin(profile, media_key)
    return OkResponse()
