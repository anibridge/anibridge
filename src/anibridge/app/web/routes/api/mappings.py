"""API endpoints for mappings (v3 graph)."""

import asyncio
from typing import Any

from fastapi import Request
from fastapi.exceptions import HTTPException
from fastapi.param_functions import Query
from fastapi.routing import APIRouter
from pydantic import BaseModel, Field

from anibridge.app.exceptions import (
    AniListFilterError,
    AniListSearchError,
    BooruQueryEvaluationError,
    BooruQuerySyntaxError,
    MappingIdMismatchError,
)
from anibridge.app.models.schemas.anilist import Media
from anibridge.app.web.services.mapping_overrides_service import (
    get_mapping_overrides_service,
)
from anibridge.app.web.services.mappings_query_spec import get_query_field_specs
from anibridge.app.web.services.mappings_service import get_mappings_service

__all__ = ["router"]


class MappingEdgeModel(BaseModel):
    target_provider: str
    target_entry_id: str
    target_scope: str | None = None
    source_range: str
    destination_range: str | None = None
    sources: list[str] = Field(default_factory=list)


class MappingItemModel(BaseModel):
    descriptor: str
    provider: str
    entry_id: str
    scope: str | None = None
    edges: list[MappingEdgeModel]
    custom: bool = False
    sources: list[str] = Field(default_factory=list)
    anilist: Media | None = None


class ListMappingsResponse(BaseModel):
    items: list[MappingItemModel]
    total: int
    page: int
    per_page: int
    pages: int
    with_anilist: bool = False


class DeleteMappingResponse(BaseModel):
    ok: bool


class RangeInputModel(BaseModel):
    source_range: str
    destination_range: str | None = None


class TargetInputModel(BaseModel):
    provider: str
    entry_id: str
    scope: str | None = None
    ranges: list[RangeInputModel] = Field(default_factory=list)
    deleted: bool = False


class MappingOverridePayload(BaseModel):
    """Payload for creating or updating a mapping override."""

    descriptor: str
    targets: list[TargetInputModel] = Field(default_factory=list)

    def to_service_kwargs(self) -> dict[str, Any]:
        """Return kwargs expected by MappingOverridesService."""
        return {
            "descriptor": self.descriptor,
            "targets": [entry.model_dump() for entry in self.targets],
        }


class MappingRangeViewModel(BaseModel):
    source_range: str
    upstream: str | None = None
    custom: str | None = None
    effective: str | None = None
    origin: str
    inherited: bool = False


class MappingTargetViewModel(BaseModel):
    descriptor: str
    provider: str
    entry_id: str
    scope: str | None = None
    origin: str
    deleted: bool = False
    ranges: list[MappingRangeViewModel] = Field(default_factory=list)


class MappingLayersModel(BaseModel):
    upstream: dict[str, dict[str, str | None] | None] = Field(default_factory=dict)
    custom: dict[str, dict[str, str | None] | None] = Field(default_factory=dict)
    effective: dict[str, dict[str, str | None] | None] = Field(default_factory=dict)


class MappingDetailModel(BaseModel):
    descriptor: str
    provider: str
    entry_id: str
    scope: str | None = None
    layers: MappingLayersModel = Field(default_factory=MappingLayersModel)
    targets: list[MappingTargetViewModel] = Field(default_factory=list)


class FieldCapabilityModel(BaseModel):
    key: str
    aliases: list[str] = Field(default_factory=list)
    type: str
    operators: list[str]
    values: list[str] | None = None
    desc: str | None = None


class QueryCapabilitiesResponse(BaseModel):
    fields: list[FieldCapabilityModel]


router = APIRouter()


@router.get("", response_model=ListMappingsResponse)
async def list_mappings(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=250),
    q: str | None = None,
    custom_only: bool = False,
    with_anilist: bool = False,
) -> ListMappingsResponse:
    svc = get_mappings_service()

    async def cancel_check() -> bool:
        return await request.is_disconnected()

    try:
        raw_items, total = await svc.list_mappings(
            page=page,
            per_page=per_page,
            q=q,
            custom_only=custom_only,
            with_anilist=with_anilist,
            cancel_check=cancel_check,
        )
    except (
        BooruQuerySyntaxError,
        BooruQueryEvaluationError,
        AniListFilterError,
    ) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AniListSearchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except asyncio.CancelledError as exc:
        raise HTTPException(status_code=499, detail="Client Closed Request") from exc

    items = [MappingItemModel(**it) for it in raw_items]
    pages = (total + per_page - 1) // per_page if per_page else 1
    return ListMappingsResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
        with_anilist=with_anilist,
    )


@router.get("/query-capabilities", response_model=QueryCapabilitiesResponse)
def query_capabilities() -> QueryCapabilitiesResponse:
    specs = get_query_field_specs()
    fields = [
        FieldCapabilityModel(
            key=spec.key,
            aliases=list(spec.aliases),
            type=str(spec.type.value),
            operators=[op.value for op in spec.operators],
            values=list(spec.values) if spec.values is not None else None,
            desc=spec.desc,
        )
        for spec in specs
    ]
    return QueryCapabilitiesResponse(fields=fields)


@router.get("/{descriptor}", response_model=MappingDetailModel)
async def get_mapping(descriptor: str) -> MappingDetailModel:
    svc = get_mapping_overrides_service()
    data = await svc.get_mapping_detail(descriptor)
    return MappingDetailModel(**data)


@router.post("", response_model=MappingDetailModel)
async def create_mapping(mapping: MappingOverridePayload) -> MappingDetailModel:
    svc = get_mapping_overrides_service()
    payload = mapping.to_service_kwargs()
    data = await svc.save_override(**payload)
    return MappingDetailModel(**data)


@router.put("/{descriptor}", response_model=MappingDetailModel)
async def update_mapping(
    descriptor: str, mapping: MappingOverridePayload
) -> MappingDetailModel:
    if mapping.descriptor != descriptor:
        raise MappingIdMismatchError("descriptor in path and body must match")

    svc = get_mapping_overrides_service()
    payload = mapping.to_service_kwargs()
    data = await svc.save_override(**payload)
    return MappingDetailModel(**data)
