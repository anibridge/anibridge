"""API endpoints for mappings (v3 graph)."""

from typing import Annotated

import msgspec
from litestar.exceptions.http_exceptions import HTTPException
from litestar.handlers.http_handlers.decorators import get, post, put
from litestar.params import Body
from litestar.router import Router

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


class MappingEdgeModel(msgspec.Struct):
    target_provider: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Target provider namespace for the edge.",
            examples=["tmdb"],
        ),
    ]
    target_entry_id: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Target provider entry identifier for the edge.",
            examples=["1396"],
        ),
    ]
    source_range: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Source episode or media range represented by the edge.",
            examples=["1-12"],
        ),
    ]
    target_scope: (
        Annotated[
            str,
            msgspec.Meta(
                description="Target scope qualifier for the mapped entry when present.",
                examples=["s1"],
            ),
        ]
        | None
    ) = None
    destination_range: (
        Annotated[
            str,
            msgspec.Meta(
                description="Target episode or media range for the mapped edge.",
                examples=["1-12"],
            ),
        ]
        | None
    ) = None
    sources: Annotated[
        list[str],
        msgspec.Meta(
            description="Source provenance labels for the mapping edge.",
            examples=[["animap", "manual"]],
        ),
    ] = msgspec.field(default_factory=list)


class MappingItemModel(msgspec.Struct):
    descriptor: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Canonical mapping descriptor used as the primary key.",
            examples=["anilist:5114"],
        ),
    ]
    provider: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Source provider namespace for the mapping item.",
            examples=["anilist"],
        ),
    ]
    entry_id: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Source provider entry identifier for the mapping item.",
            examples=["5114"],
        ),
    ]
    edges: Annotated[
        list[MappingEdgeModel],
        msgspec.Meta(
            description="Outgoing mapping edges attached to the descriptor.",
            examples=[
                [
                    {
                        "target_provider": "tmdb",
                        "target_entry_id": "1396",
                        "source_range": "1-12",
                    }
                ]
            ],
        ),
    ]
    scope: (
        Annotated[
            str,
            msgspec.Meta(
                description="Optional scope qualifier for the source descriptor.",
                examples=["s1"],
            ),
        ]
        | None
    ) = None
    custom: Annotated[
        bool,
        msgspec.Meta(
            description="Whether this mapping item is backed by a local override.",
            examples=[False],
        ),
    ] = False
    sources: Annotated[
        list[str],
        msgspec.Meta(
            description="Source provenance labels for the mapping item.",
            examples=[["animap"]],
        ),
    ] = msgspec.field(default_factory=list)
    anilist: (
        Annotated[
            Media,
            msgspec.Meta(
                description=(
                    "Optional AniList media payload attached for UI enrichment."
                ),
                examples=[{"id": 5114}],
            ),
        ]
        | None
    ) = None


class ListMappingsResponse(msgspec.Struct):
    items: Annotated[
        list[MappingItemModel],
        msgspec.Meta(
            description="Paginated mapping items matching the current query.",
            examples=[
                [
                    {
                        "descriptor": "anilist:5114",
                        "provider": "anilist",
                        "entry_id": "5114",
                        "edges": [],
                    }
                ]
            ],
        ),
    ]
    total: Annotated[
        int,
        msgspec.Meta(
            ge=0,
            description="Total number of matching mappings.",
            examples=[128],
        ),
    ]
    page: Annotated[
        int,
        msgspec.Meta(
            ge=1,
            description="Current page number.",
            examples=[1],
        ),
    ]
    per_page: Annotated[
        int,
        msgspec.Meta(
            ge=1,
            description="Maximum number of items requested per page.",
            examples=[25],
        ),
    ]
    pages: Annotated[
        int,
        msgspec.Meta(
            ge=0,
            description="Total number of pages available for the current query.",
            examples=[6],
        ),
    ]
    with_anilist: Annotated[
        bool,
        msgspec.Meta(
            description="Whether AniList enrichment was included in the response.",
            examples=[False],
        ),
    ] = False


class DeleteMappingResponse(msgspec.Struct):
    ok: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the delete operation completed successfully.",
            examples=[True],
        ),
    ]


class RangeInputModel(msgspec.Struct):
    source_range: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Source episode or media range for the override target.",
            examples=["1-12"],
        ),
    ]
    destination_range: (
        Annotated[
            str,
            msgspec.Meta(
                description=(
                    "Destination range on the target provider when remapping episodes."
                ),
                examples=["1-12"],
            ),
        ]
        | None
    ) = None


class TargetInputModel(msgspec.Struct):
    provider: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Target provider namespace for the override.",
            examples=["tmdb"],
        ),
    ]
    entry_id: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Target provider entry identifier for the override.",
            examples=["1396"],
        ),
    ]
    scope: (
        Annotated[
            str,
            msgspec.Meta(
                description="Optional target scope qualifier.",
                examples=["s1"],
            ),
        ]
        | None
    ) = None
    ranges: Annotated[
        list[RangeInputModel],
        msgspec.Meta(
            description="Optional explicit range mappings for this target.",
            examples=[[{"source_range": "1-12", "destination_range": "1-12"}]],
        ),
    ] = msgspec.field(default_factory=list)
    deleted: Annotated[
        bool,
        msgspec.Meta(
            description=(
                "Whether this target should be marked deleted "
                "relative to upstream data."
            ),
            examples=[False],
        ),
    ] = False


class MappingOverridePayload(msgspec.Struct):
    """Payload for creating or updating a mapping override."""

    descriptor: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description=(
                "Canonical descriptor whose override is being created or updated."
            ),
            examples=["anilist:5114"],
        ),
    ]
    targets: Annotated[
        list[TargetInputModel],
        msgspec.Meta(
            description="Replacement target set for the override.",
            examples=[
                [
                    {
                        "provider": "tmdb",
                        "entry_id": "1396",
                        "ranges": [
                            {"source_range": "1-12", "destination_range": "1-12"}
                        ],
                    }
                ]
            ],
        ),
    ] = msgspec.field(default_factory=list)


class MappingConfigPayload(msgspec.Struct):
    """Payload for updating top-level mappings configuration."""

    includes: Annotated[
        list[str],
        msgspec.Meta(
            description="Top-level mapping sources loaded via the $includes field.",
            examples=[
                ["/example/path/to/mappings.json", "https://example.com/mappings.json"]
            ],
        ),
    ] = msgspec.field(default_factory=list)


class MappingConfigModel(msgspec.Struct):
    """Editable top-level mappings configuration."""

    path: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Filesystem path of the custom mappings file being edited.",
            examples=["/data/mappings.json"],
        ),
    ]
    format: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Serialization format of the custom mappings file.",
            examples=["json"],
        ),
    ]
    includes: Annotated[
        list[str],
        msgspec.Meta(
            description="Resolved include entries from the custom mappings file.",
            examples=[["./mappings.yaml", "https://example.com/mappings.json"]],
        ),
    ] = msgspec.field(default_factory=list)
    mappings_url: Annotated[
        str,
        msgspec.Meta(
            description="Upstream mappings source configured for the app.",
            examples=["https://example.com/mappings.json"],
        ),
    ] = ""


class MappingRangeViewModel(msgspec.Struct):
    source_range: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Source range represented in the layered mapping view.",
            examples=["1-12"],
        ),
    ]
    origin: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Origin layer that supplied the effective range value.",
            examples=["custom"],
        ),
    ]
    upstream: (
        Annotated[
            str,
            msgspec.Meta(
                description="Upstream range value before local overrides are applied.",
                examples=["1-12"],
            ),
        ]
        | None
    ) = None
    custom: (
        Annotated[
            str,
            msgspec.Meta(
                description="Locally configured custom range value when present.",
                examples=["1-12"],
            ),
        ]
        | None
    ) = None
    effective: (
        Annotated[
            str,
            msgspec.Meta(
                description="Final effective range after applying layered overrides.",
                examples=["1-12"],
            ),
        ]
        | None
    ) = None
    inherited: Annotated[
        bool,
        msgspec.Meta(
            description=(
                "Whether the effective range is inherited unchanged from upstream."
            ),
            examples=[False],
        ),
    ] = False


class MappingTargetViewModel(msgspec.Struct):
    descriptor: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Descriptor of the source mapping item.",
            examples=["anilist:5114"],
        ),
    ]
    provider: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Target provider namespace.",
            examples=["tmdb"],
        ),
    ]
    entry_id: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Target provider entry identifier.",
            examples=["1396"],
        ),
    ]
    origin: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Origin layer that supplied the target mapping.",
            examples=["upstream"],
        ),
    ]
    scope: (
        Annotated[
            str,
            msgspec.Meta(
                description="Optional target scope qualifier.",
                examples=["s1"],
            ),
        ]
        | None
    ) = None
    deleted: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the target is effectively deleted after overrides.",
            examples=[False],
        ),
    ] = False
    ranges: Annotated[
        list[MappingRangeViewModel],
        msgspec.Meta(
            description="Layered range view for the target mapping.",
            examples=[
                [{"source_range": "1-12", "origin": "upstream", "effective": "1-12"}]
            ],
        ),
    ] = msgspec.field(default_factory=list)


class MappingLayersModel(msgspec.Struct):
    upstream: Annotated[
        dict[str, dict[str, str | None] | None],
        msgspec.Meta(
            description="Raw upstream mapping layers keyed by target descriptor.",
            examples=[{"tmdb:1396": {"1-12": "1-12"}}],
        ),
    ] = msgspec.field(default_factory=dict)
    custom: Annotated[
        dict[str, dict[str, str | None] | None],
        msgspec.Meta(
            description=(
                "Locally configured override layers keyed by target descriptor."
            ),
            examples=[{"tmdb:1396": {"1-12": "1-12"}}],
        ),
    ] = msgspec.field(default_factory=dict)
    effective: Annotated[
        dict[str, dict[str, str | None] | None],
        msgspec.Meta(
            description=(
                "Final effective mapping layers after merging upstream and custom data."
            ),
            examples=[{"tmdb:1396": {"1-12": "1-12"}}],
        ),
    ] = msgspec.field(default_factory=dict)


class MappingDetailModel(msgspec.Struct):
    descriptor: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Canonical descriptor for the mapping detail payload.",
            examples=["anilist:5114"],
        ),
    ]
    provider: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Source provider namespace for the mapping detail payload.",
            examples=["anilist"],
        ),
    ]
    entry_id: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description=(
                "Source provider entry identifier for the mapping detail payload."
            ),
            examples=["5114"],
        ),
    ]
    scope: (
        Annotated[
            str,
            msgspec.Meta(
                description="Optional scope qualifier for the mapping detail payload.",
                examples=["s1"],
            ),
        ]
        | None
    ) = None
    layers: Annotated[
        MappingLayersModel,
        msgspec.Meta(
            description=(
                "Layered raw mapping representation for upstream, "
                "custom, and effective data."
            ),
            examples=[{"upstream": {}, "custom": {}, "effective": {}}],
        ),
    ] = msgspec.field(default_factory=MappingLayersModel)
    targets: Annotated[
        list[MappingTargetViewModel],
        msgspec.Meta(
            description="Expanded target mappings associated with the descriptor.",
            examples=[
                [
                    {
                        "descriptor": "anilist:5114",
                        "provider": "tmdb",
                        "entry_id": "1396",
                        "origin": "upstream",
                    }
                ]
            ],
        ),
    ] = msgspec.field(default_factory=list)


class FieldCapabilityModel(msgspec.Struct):
    key: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description=(
                "Query field identifier accepted by the mappings search parser."
            ),
            examples=["source.provider"],
        ),
    ]
    type: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Primitive type category for the query field.",
            examples=["string"],
        ),
    ]
    operators: Annotated[
        list[str],
        msgspec.Meta(
            min_length=1,
            description="Supported operators for the query field.",
            examples=[["eq", "contains"]],
        ),
    ]
    aliases: Annotated[
        list[str],
        msgspec.Meta(
            description="Alternate field names accepted by the query parser.",
            examples=[["provider"]],
        ),
    ] = msgspec.field(default_factory=list)
    values: (
        Annotated[
            list[str],
            msgspec.Meta(
                description="Suggested discrete values for the query field when known.",
                examples=[["anilist", "tmdb"]],
            ),
        ]
        | None
    ) = None
    desc: (
        Annotated[
            str,
            msgspec.Meta(
                description="Human-readable description of the query field.",
                examples=["Filter by source provider namespace."],
            ),
        ]
        | None
    ) = None


class QueryCapabilitiesResponse(msgspec.Struct):
    fields: Annotated[
        list[FieldCapabilityModel],
        msgspec.Meta(
            description="Field capability metadata for building mappings search UIs.",
            examples=[
                [{"key": "source.provider", "type": "string", "operators": ["eq"]}]
            ],
        ),
    ]


@get(path="")
async def list_mappings(
    page: int = 1,
    per_page: int = 25,
    q: str | None = None,
    custom_only: bool = False,
    with_anilist: bool = False,
) -> ListMappingsResponse:
    svc = get_mappings_service()

    async def cancel_check() -> bool:
        return False

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

    items = msgspec.convert(raw_items, type=list[MappingItemModel])
    pages = (total + per_page - 1) // per_page if per_page else 1
    return ListMappingsResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
        with_anilist=with_anilist,
    )


@get(path="/query-capabilities", sync_to_thread=True)
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


@get(path="/{descriptor:str}")
async def get_mapping(descriptor: str) -> MappingDetailModel:
    svc = get_mapping_overrides_service()
    data = await svc.get_mapping_detail(descriptor)
    return msgspec.convert(data, type=MappingDetailModel)


@get(path="/config")
async def get_mapping_config() -> MappingConfigModel:
    svc = get_mapping_overrides_service()
    data = await svc.get_mapping_config()
    return msgspec.convert(data, type=MappingConfigModel)


@post(path="", status_code=200)
async def create_mapping(
    data: Annotated[MappingOverridePayload, Body()],
) -> MappingDetailModel:
    svc = get_mapping_overrides_service()
    saved = await svc.save_override(
        descriptor=data.descriptor,
        targets=msgspec.to_builtins(data.targets),
    )
    return msgspec.convert(saved, type=MappingDetailModel)


@put(path="/{descriptor:str}")
async def update_mapping(
    descriptor: str,
    data: Annotated[MappingOverridePayload, Body()],
) -> MappingDetailModel:
    if data.descriptor != descriptor:
        raise MappingIdMismatchError("descriptor in path and body must match")

    svc = get_mapping_overrides_service()
    saved = await svc.save_override(
        descriptor=data.descriptor,
        targets=msgspec.to_builtins(data.targets),
    )
    return msgspec.convert(saved, type=MappingDetailModel)


@put(path="/config")
async def update_mapping_config(
    data: Annotated[MappingConfigPayload, Body()],
) -> MappingConfigModel:
    svc = get_mapping_overrides_service()
    saved = await svc.save_mapping_config(includes=list(data.includes))
    return msgspec.convert(saved, type=MappingConfigModel)


router = Router(
    path="/mappings",
    route_handlers=[
        list_mappings,
        query_capabilities,
        get_mapping_config,
        get_mapping,
        create_mapping,
        update_mapping,
        update_mapping_config,
    ],
)
