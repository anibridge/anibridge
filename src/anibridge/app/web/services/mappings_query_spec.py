"""Query field specifications for mapping graph search."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from anibridge.utils.cache import cache

from anibridge.app.models.db.animap import AnimapEntry
from anibridge.app.models.schemas.anilist import MediaFormat, MediaStatus

__all__ = [
    "QueryFieldKind",
    "QueryFieldOperator",
    "QueryFieldSpec",
    "QueryFieldType",
    "get_query_field_map",
    "get_query_field_specs",
]


class QueryFieldType(StrEnum):
    """Supported value shapes for booru-like query fields."""

    INT = "int"
    STRING = "string"
    ENUM = "enum"


class QueryFieldOperator(StrEnum):
    """Supported operator tokens for query fields."""

    EQ = "="
    IN = "in"
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    STAR_WILDCARD = "*"
    QMARK_WILDCARD = "?"
    RANGE = "range"


class QueryFieldKind(StrEnum):
    """Categorisation for query field resolution backends."""

    DB_SCALAR = "db_scalar"
    DB_EDGE_TARGET = "db_edge_target"
    DB_EDGE_RANGE = "db_edge_range"
    ANILIST_STRING = "anilist_string"
    ANILIST_NUMERIC = "anilist_numeric"
    ANILIST_ENUM = "anilist_enum"


@dataclass(frozen=True)
class QueryFieldSpec:
    """Describes a single query-capable field."""

    key: str
    kind: QueryFieldKind
    type: QueryFieldType
    operators: Iterable[QueryFieldOperator]
    desc: str | None = None
    aliases: Iterable[str] = ()
    values: Iterable[str] | None = None
    column: Any | None = None
    edge_field: str | None = None
    anilist_field: str | None = None
    anilist_value_type: str | None = None
    anilist_multi_field: str | None = None


_ENUM_OPS = (QueryFieldOperator.EQ, QueryFieldOperator.IN)
_INT_OPS = (
    QueryFieldOperator.EQ,
    QueryFieldOperator.GT,
    QueryFieldOperator.GTE,
    QueryFieldOperator.LT,
    QueryFieldOperator.LTE,
    QueryFieldOperator.RANGE,
    QueryFieldOperator.IN,
)
_STRING_OPS = (
    QueryFieldOperator.EQ,
    QueryFieldOperator.STAR_WILDCARD,
    QueryFieldOperator.QMARK_WILDCARD,
    QueryFieldOperator.IN,
)

_DB_FIELDS: tuple[QueryFieldSpec, ...] = (
    QueryFieldSpec(
        key="id",
        desc="AniBridge mapping entry ID",
        kind=QueryFieldKind.DB_SCALAR,
        type=QueryFieldType.INT,
        operators=_INT_OPS,
        column=AnimapEntry.id,
    ),
    QueryFieldSpec(
        key="source.provider",
        desc="Source provider",
        kind=QueryFieldKind.DB_SCALAR,
        type=QueryFieldType.STRING,
        operators=_STRING_OPS,
        column=AnimapEntry.provider,
    ),
    QueryFieldSpec(
        key="source.id",
        desc="Source entry identifier",
        kind=QueryFieldKind.DB_SCALAR,
        type=QueryFieldType.STRING,
        operators=_STRING_OPS,
        column=AnimapEntry.entry_id,
    ),
    QueryFieldSpec(
        key="source.scope",
        desc="Source entry scope (optional, e.g. s1)",
        kind=QueryFieldKind.DB_SCALAR,
        type=QueryFieldType.STRING,
        operators=(QueryFieldOperator.EQ,),
        column=AnimapEntry.entry_scope,
    ),
    QueryFieldSpec(
        key="target.provider",
        desc="Destination provider",
        kind=QueryFieldKind.DB_EDGE_TARGET,
        type=QueryFieldType.STRING,
        operators=_STRING_OPS,
        edge_field="provider",
    ),
    QueryFieldSpec(
        key="target.id",
        desc="Destination entry identifier",
        kind=QueryFieldKind.DB_EDGE_TARGET,
        type=QueryFieldType.STRING,
        operators=_STRING_OPS,
        edge_field="entry_id",
    ),
    QueryFieldSpec(
        key="target.scope",
        desc="Destination entry scope (optional, e.g. s1)",
        kind=QueryFieldKind.DB_EDGE_TARGET,
        type=QueryFieldType.STRING,
        operators=(QueryFieldOperator.EQ,),
        edge_field="entry_scope",
    ),
    QueryFieldSpec(
        key="edge.source_range",
        desc="Source episode range",
        kind=QueryFieldKind.DB_EDGE_RANGE,
        type=QueryFieldType.STRING,
        operators=_STRING_OPS,
        edge_field="source_range",
    ),
    QueryFieldSpec(
        key="edge.target_range",
        desc="Destination episode range",
        kind=QueryFieldKind.DB_EDGE_RANGE,
        type=QueryFieldType.STRING,
        operators=_STRING_OPS,
        edge_field="destination_range",
    ),
)

_ANILIST_FIELDS: tuple[QueryFieldSpec, ...] = (
    QueryFieldSpec(
        key="anilist.title",
        desc="AniList title search",
        kind=QueryFieldKind.ANILIST_STRING,
        type=QueryFieldType.STRING,
        operators=(QueryFieldOperator.EQ,),
        anilist_field="search",
        anilist_value_type="string",
    ),
    QueryFieldSpec(
        key="anilist.id",
        desc="AniList ID",
        kind=QueryFieldKind.ANILIST_NUMERIC,
        type=QueryFieldType.INT,
        operators=_INT_OPS,
        anilist_field="id",
        anilist_value_type="int",
    ),
    QueryFieldSpec(
        key="anilist.duration",
        desc="Episode duration (minutes)",
        kind=QueryFieldKind.ANILIST_NUMERIC,
        type=QueryFieldType.INT,
        operators=_INT_OPS,
        anilist_field="duration",
        anilist_value_type="int",
    ),
    QueryFieldSpec(
        key="anilist.episodes",
        desc="Episode count",
        kind=QueryFieldKind.ANILIST_NUMERIC,
        type=QueryFieldType.INT,
        operators=_INT_OPS,
        anilist_field="episodes",
        anilist_value_type="int",
    ),
    QueryFieldSpec(
        key="anilist.start_date",
        desc="Start date (YYYYMMDD)",
        kind=QueryFieldKind.ANILIST_NUMERIC,
        type=QueryFieldType.INT,
        operators=_INT_OPS,
        anilist_field="startDate",
        anilist_value_type="fuzzy_date",
    ),
    QueryFieldSpec(
        key="anilist.end_date",
        desc="End date (YYYYMMDD)",
        kind=QueryFieldKind.ANILIST_NUMERIC,
        type=QueryFieldType.INT,
        operators=_INT_OPS,
        anilist_field="endDate",
        anilist_value_type="fuzzy_date",
    ),
    QueryFieldSpec(
        key="anilist.format",
        desc="AniList format",
        kind=QueryFieldKind.ANILIST_ENUM,
        type=QueryFieldType.ENUM,
        operators=_ENUM_OPS,
        values=tuple(
            fmt.value
            for fmt in MediaFormat
            if fmt not in {MediaFormat.MANGA, MediaFormat.NOVEL, MediaFormat.ONE_SHOT}
        ),
        anilist_field="format",
        anilist_value_type="enum",
        anilist_multi_field="format_in",
    ),
    QueryFieldSpec(
        key="anilist.status",
        desc="AniList status",
        kind=QueryFieldKind.ANILIST_ENUM,
        type=QueryFieldType.ENUM,
        operators=_ENUM_OPS,
        values=tuple(
            status.value for status in MediaStatus if status not in {MediaStatus.HIATUS}
        ),
        anilist_field="status",
        anilist_value_type="enum",
        anilist_multi_field="status_in",
    ),
)

_QUERY_FIELDS: tuple[QueryFieldSpec, ...] = _DB_FIELDS + _ANILIST_FIELDS

_FIELD_MAP: dict[str, QueryFieldSpec] = {}
for spec in _QUERY_FIELDS:
    _FIELD_MAP[spec.key.lower()] = spec
    for alias in spec.aliases:
        _FIELD_MAP[alias.lower()] = spec


@cache
def get_query_field_specs() -> list[QueryFieldSpec]:
    """Return the query field specifications.

    Returns:
        list[QueryFieldSpec]: All available query field specifications.
    """
    return list(_QUERY_FIELDS)


def get_query_field_map() -> Mapping[str, QueryFieldSpec]:
    """Return a mapping of lowercase key/aliases to field specs.

    Returns:
        Mapping[str, QueryFieldSpec]: Mapping of field keys and aliases to specs.
    """
    return _FIELD_MAP
