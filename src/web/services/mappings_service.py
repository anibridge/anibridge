"""Mappings service for provider-range mapping graph (v3)."""

import asyncio
import calendar
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, ClassVar

from sqlalchemy.sql import func, or_, select

from src.config.database import db
from src.config.settings import get_config
from src.core.animap import descriptor_key, parse_mapping_descriptor
from src.exceptions import (
    AniListFilterError,
    AniListSearchError,
    BooruQueryEvaluationError,
    BooruQuerySyntaxError,
    MappingNotFoundError,
)
from src.models.db.animap import AnimapEntry, AnimapMapping, AnimapProvenance
from src.models.schemas.anilist import Media
from src.utils.booru_query import (
    And,
    KeyTerm,
    Node,
    Not,
    Or,
    collect_bare_terms,
    collect_key_terms,
    evaluate,
    parse_query,
)
from src.utils.cache import cache
from src.web.services.mappings_query_spec import (
    QueryFieldKind,
    QueryFieldSpec,
    get_query_field_map,
)
from src.web.state import get_app_state

__all__ = ["MappingsService", "get_mappings_service"]


@dataclass(frozen=True)
class EdgeView:
    """Flattened view of an outgoing mapping edge."""

    target_provider: str
    target_entry_id: str
    target_scope: str | None
    source_range: str
    destination_range: str | None
    sources: list[str]


@dataclass(frozen=True)
class MappingItem:
    """Flattened mapping entry with outgoing edges."""

    provider: str
    entry_id: str
    scope: str | None
    edges: list[EdgeView]
    custom: bool
    sources: list[str]
    anilist_id: int | None = None
    anilist: Media | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation.

        Returns:
            dict[str, Any]: Mapping item as a dictionary.
        """
        return {
            "provider": self.provider,
            "entry_id": self.entry_id,
            "scope": self.scope,
            "descriptor": descriptor_key((self.provider, self.entry_id, self.scope)),
            "edges": [edge.__dict__ for edge in self.edges],
            "custom": self.custom,
            "sources": self.sources,
            "anilist": self.anilist.model_dump() if self.anilist else None,
        }


class MappingsService:
    """Service to query the v3 mapping graph with booru-like queries."""

    _FIELD_MAP: ClassVar[Mapping[str, QueryFieldSpec]] = get_query_field_map()
    _ANILIST_KINDS: ClassVar[frozenset[QueryFieldKind]] = frozenset(
        {
            QueryFieldKind.ANILIST_STRING,
            QueryFieldKind.ANILIST_NUMERIC,
            QueryFieldKind.ANILIST_ENUM,
        }
    )
    _ANILIST_MAX_RESULTS: ClassVar[int] = 25000
    _CMP_RE: ClassVar[re.Pattern[str]] = re.compile(r"^(>=|>|<=|<)(\d+)$")
    _RANGE_RE: ClassVar[re.Pattern[str]] = re.compile(r"^(\d+)\.\.(\d+)$")

    def __init__(self) -> None:
        """Initialise mapping service with query specs and upstream URL."""
        self.upstream_url: str | None = get_config().mappings_url

    @staticmethod
    def _fetch_ids(ctx, stmt) -> set[int]:
        """Execute a statement and return integer identifiers."""
        return {int(val) for val in ctx.session.execute(stmt).scalars()}

    @staticmethod
    def _has_wildcards(value: str | None) -> bool:
        """Check if a string contains wildcard markers."""
        if value is None:
            return False
        return "*" in value or "?" in value

    @staticmethod
    def _normalize_null(value: str | None) -> str | None:
        """Interpret the literal 'null' as None for filters."""
        if value is None:
            return None
        if value.strip().lower() == "null":
            return None
        return value

    @staticmethod
    def _like_pattern(value: str) -> str:
        """Translate wildcards into SQL LIKE pattern."""
        return value.replace("*", "%").replace("?", "_")

    @staticmethod
    def _parse_numeric_filters(
        raw: Any,
    ) -> tuple[tuple[str, int] | None, tuple[int, int] | None, str]:
        """Parse comparison and range tokens from raw filter input."""
        text = "" if raw is None else str(raw)
        cmp_match = MappingsService._CMP_RE.match(text)
        range_match = MappingsService._RANGE_RE.match(text)
        cmp_filter = (
            (cmp_match.group(1), int(cmp_match.group(2))) if cmp_match else None
        )
        range_filter = (
            (int(range_match.group(1)), int(range_match.group(2)))
            if range_match
            else None
        )
        return cmp_filter, range_filter, text

    @staticmethod
    def _normalize_text_query(value: str) -> str:
        """Collapse whitespace and strip wildcard markers for AniList text search."""
        cleaned = value.replace("*", " ").replace("?", " ")
        return re.sub(r"\s+", " ", cleaned).strip()

    @staticmethod
    def _parse_int_value(raw_value: str) -> int | None:
        """Safely convert a string to integer if possible."""
        try:
            return int(raw_value)
        except TypeError, ValueError:
            return None

    @staticmethod
    def _parse_fuzzy_date_int(raw_value: str) -> int | None:
        """Parse fuzzy date input (YYYYMMDD) into AniList integer format."""
        digits = "".join(ch for ch in str(raw_value) if ch.isdigit())
        if not digits:
            return None
        if len(digits) > 8:
            digits = digits[:8]
        digits = digits.ljust(8, "0")
        try:
            return int(digits)
        except ValueError:
            return None

    @staticmethod
    def _normalize_fuzzy_date_number(value: int) -> int:
        """Expand shorthand numeric values (e.g., 2016) into fuzzy date ints."""
        parsed = MappingsService._parse_fuzzy_date_int(str(value))
        return parsed if parsed is not None else value

    @staticmethod
    def _datetime_to_fuzzy(dt: datetime) -> int:
        """Convert datetime into AniList fuzzy integer representation."""
        return dt.year * 10000 + dt.month * 100 + dt.day

    def _build_anilist_term_filters(
        self,
        spec: QueryFieldSpec,
        raw_value: str,
        multi_values: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Translate an AniList term into GraphQL filter arguments."""
        value = raw_value.strip()
        values_tuple = (
            tuple(dict.fromkeys(multi_values or ())) if multi_values else None
        )

        if not spec.anilist_field:
            raise AniListFilterError(f"AniList field mapping missing for '{spec.key}'")

        if spec.kind == QueryFieldKind.ANILIST_STRING:
            if values_tuple:
                if not spec.anilist_multi_field:
                    raise AniListFilterError(
                        f"AniList filter '{spec.key}' does not support multiple values"
                    )
                normalized = [self._normalize_text_query(item) for item in values_tuple]
                filtered = [item for item in normalized if item]
                if not filtered:
                    raise AniListFilterError(
                        f"AniList filter '{spec.key}' requires at least one value"
                    )
                unique = list(dict.fromkeys(filtered))
                return {spec.anilist_multi_field: unique}
            text = self._normalize_text_query(value)
            if not text:
                raise AniListFilterError(
                    f"AniList filter '{spec.key}' requires a non-empty value"
                )
            return {spec.anilist_field: text}

        if spec.kind == QueryFieldKind.ANILIST_ENUM:
            allowed = spec.values or ()
            if not allowed:
                raise AniListFilterError(
                    f"AniList filter '{spec.key}' is not configured with values"
                )
            lookup = {val: val for val in allowed}
            lookup.update({val.lower(): val for val in allowed})
            lookup.update({val.upper(): val for val in allowed})

            def _resolve_enum(candidate: str) -> str:
                if not candidate:
                    raise AniListFilterError(
                        f"AniList filter '{spec.key}' requires a value"
                    )
                resolved = lookup.get(candidate)
                if resolved is None:
                    resolved = lookup.get(candidate.lower())
                if resolved is None:
                    resolved = lookup.get(candidate.upper())
                if resolved is None:
                    raise AniListFilterError(
                        f"'{candidate}' is not a valid value for AniList filter "
                        f"'{spec.key}'"
                    )
                return resolved

            if values_tuple:
                if not spec.anilist_multi_field:
                    raise AniListFilterError(
                        f"AniList filter '{spec.key}' does not support multiple values"
                    )
                resolved_values = [_resolve_enum(item) for item in values_tuple]
                unique_values = list(dict.fromkeys(resolved_values))
                if not unique_values:
                    raise AniListFilterError(
                        f"AniList filter '{spec.key}' requires a value"
                    )
                return {spec.anilist_multi_field: unique_values}

            resolved = _resolve_enum(value)
            return {spec.anilist_field: resolved}

        if spec.kind == QueryFieldKind.ANILIST_NUMERIC:
            if values_tuple:
                raise AniListFilterError(
                    f"AniList filter '{spec.key}' does not support multiple values"
                )
            cmp_filter, range_filter, text_value = self._parse_numeric_filters(value)
            filters_dict = self._build_anilist_numeric_filters(
                spec, cmp_filter, range_filter, text_value
            )
            if not filters_dict:
                raise AniListFilterError(
                    f"AniList filter '{spec.key}' has an invalid numeric value"
                )
            return filters_dict

        raise AniListFilterError(f"AniList filter '{spec.key}' is not supported")

    async def _resolve_anilist_term(
        self,
        client,
        spec: QueryFieldSpec,
        raw_value: str,
        *,
        multi_values: tuple[str, ...] | None = None,
    ) -> set[int]:
        """Resolve AniList-backed query terms into AniList identifier sets."""
        filters = self._build_anilist_term_filters(spec, raw_value, multi_values)
        try:
            ids = await client.search_media_ids(
                filters=filters, max_results=self._ANILIST_MAX_RESULTS
            )
        except AniListFilterError, AniListSearchError:
            raise
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AniListSearchError(
                f"Failed to resolve AniList filter '{spec.key}'"
            ) from exc
        return {int(aid) for aid in ids}

    def _collect_anilist_and_groups(self, node: Node) -> list[list[KeyTerm]]:
        """Collect AniList key term groups that share a direct AND relationship."""
        groups: list[list[KeyTerm]] = []
        assigned: set[int] = set()

        def visit(current: Any) -> None:
            if isinstance(current, And):
                direct_terms: list[KeyTerm] = []
                for child in current.children:
                    if isinstance(child, KeyTerm):
                        spec = self._FIELD_MAP.get(child.key.lower())
                        if (
                            spec
                            and spec.kind in self._ANILIST_KINDS
                            and id(child) not in assigned
                        ):
                            direct_terms.append(child)
                    else:
                        visit(child)
                if len(direct_terms) >= 2:
                    groups.append(direct_terms)
                    assigned.update(id(term) for term in direct_terms)
                return

            if isinstance(current, Or):
                for child in current.children:
                    visit(child)
                return

            if isinstance(current, Not):
                visit(current.child)
                return

            if isinstance(current, list):
                for child in current:
                    visit(child)

        visit(node)
        return groups

    @staticmethod
    def _fuzzy_to_datetime(value: int, bias: str) -> datetime:
        """Convert fuzzy integer into datetime, biasing toward range edge."""
        year = max(1, value // 10000)
        month = (value // 100) % 100
        day = value % 100

        if month <= 0:
            month = 1 if bias == "lower" else 12
        if day <= 0:
            day = 1 if bias == "lower" else calendar.monthrange(year, month)[1]

        return datetime(year=year, month=month, day=day)

    @classmethod
    def _fuzzy_lower_threshold(cls, value: int) -> int:
        """Inclusive lower bound helper for AniList fuzzy dates."""
        try:
            dt = cls._fuzzy_to_datetime(value, "lower") - timedelta(days=1)
        except Exception:
            return value
        return cls._datetime_to_fuzzy(dt)

    @classmethod
    def _fuzzy_upper_threshold(cls, value: int) -> int:
        """Inclusive upper bound helper for AniList fuzzy dates."""
        try:
            dt = cls._fuzzy_to_datetime(value, "upper") + timedelta(days=1)
        except Exception:
            return value
        return cls._datetime_to_fuzzy(dt)

    def _build_anilist_numeric_filters(
        self,
        spec: QueryFieldSpec,
        cmp_filter: tuple[str, int] | None,
        range_filter: tuple[int, int] | None,
        raw_value: str,
    ) -> dict[str, Any] | None:
        """Translate numeric filters into AniList GraphQL arguments."""
        field = spec.anilist_field
        if not field:
            return None

        value_type = spec.anilist_value_type or "int"
        is_fuzzy = value_type == "fuzzy_date"

        def _inclusive_lower(num: int) -> int:
            if value_type == "fuzzy_date":
                return self._fuzzy_lower_threshold(num)
            return num - 1

        def _inclusive_upper(num: int) -> int:
            if value_type == "fuzzy_date":
                return self._fuzzy_upper_threshold(num)
            return num + 1

        if range_filter:
            lo, hi = range_filter
            if lo > hi:
                lo, hi = hi, lo
            if is_fuzzy:
                lo = self._normalize_fuzzy_date_number(lo)
                hi = self._normalize_fuzzy_date_number(hi)
            return {
                f"{field}_greater": _inclusive_lower(lo),
                f"{field}_lesser": _inclusive_upper(hi),
            }

        if cmp_filter:
            op, num = cmp_filter
            if is_fuzzy:
                num = self._normalize_fuzzy_date_number(num)
            if value_type == "fuzzy_date":
                if op == ">":
                    return {f"{field}_greater": num}
                if op == ">=":
                    return {f"{field}_greater": self._fuzzy_lower_threshold(num)}
                if op == "<":
                    return {f"{field}_lesser": num}
                if op == "<=":
                    return {f"{field}_lesser": self._fuzzy_upper_threshold(num)}
            else:
                if op == ">":
                    return {f"{field}_greater": num}
                if op == ">=":
                    return {f"{field}_greater": num - 1}
                if op == "<":
                    return {f"{field}_lesser": num}
                if op == "<=":
                    return {f"{field}_lesser": num + 1}
            return None

        if value_type == "fuzzy_date":
            parsed = self._parse_fuzzy_date_int(raw_value)
        else:
            parsed = self._parse_int_value(raw_value)
        if parsed is None:
            return None
        return {field: parsed}

    @staticmethod
    def _scalar_cmp(column, operator: str, num: int):
        """Build a scalar comparison expression for numeric columns."""
        if operator == ">":
            return column > num
        if operator == ">=":
            return column >= num
        if operator == "<":
            return column < num
        if operator == "<=":
            return column <= num
        return None

    def _filter_entry_column(
        self,
        ctx,
        column,
        raw_value: str | None,
        values: tuple[str | None, ...] | None = None,
    ) -> set[int]:
        """Filter entries by a scalar column supporting wildcards and IN lists."""
        stmt = select(AnimapEntry.id)
        if values:
            clauses = []
            for part in values:
                if part is None:
                    clauses.append(column.is_(None))
                elif self._has_wildcards(part):
                    clauses.append(column.like(self._like_pattern(part)))
                else:
                    clauses.append(column == part)
            if not clauses:
                return set()
            return self._fetch_ids(ctx, stmt.where(or_(*clauses)))

        if raw_value is None:
            return self._fetch_ids(ctx, stmt.where(column.is_(None)))

        if self._has_wildcards(raw_value):
            return self._fetch_ids(
                ctx, stmt.where(column.like(self._like_pattern(raw_value)))
            )

        return self._fetch_ids(ctx, stmt.where(column == raw_value))

    def _filter_edge_target(
        self,
        ctx,
        attr: str,
        raw_value: str | None,
        values: tuple[str | None, ...] | None = None,
    ) -> set[int]:
        """Filter entries that have outgoing edges matching destination attributes."""
        dest_column = getattr(AnimapEntry, attr)
        stmt = select(AnimapMapping.source_entry_id).join(
            AnimapEntry, AnimapEntry.id == AnimapMapping.destination_entry_id
        )

        clauses = []
        target_values = values or (raw_value,)
        for part in target_values:
            if part is None:
                clauses.append(dest_column.is_(None))
            elif self._has_wildcards(part):
                clauses.append(dest_column.like(self._like_pattern(part)))
            else:
                clauses.append(dest_column == part)

        if not clauses:
            return set()

        return self._fetch_ids(ctx, stmt.where(or_(*clauses)))

    def _filter_edge_range(
        self,
        ctx,
        attr: str,
        raw_value: str | None,
        values: tuple[str | None, ...] | None = None,
    ) -> set[int]:
        """Filter entries by source/destination ranges on mappings."""
        column = getattr(AnimapMapping, attr)
        stmt = select(AnimapMapping.source_entry_id)
        clauses = []
        target_values = values or (raw_value,)
        for part in target_values:
            if part is None:
                clauses.append(column.is_(None))
            elif self._has_wildcards(part):
                clauses.append(column.like(self._like_pattern(part)))
            else:
                clauses.append(column == part)
        if not clauses:
            return set()
        return self._fetch_ids(ctx, stmt.where(or_(*clauses)))

    def _entry_ids_for_anilist_ids(self, anilist_ids: Iterable[int]) -> set[int]:
        """Map AniList identifiers to entry IDs, including linked edges."""
        ids = {int(aid) for aid in anilist_ids if aid is not None}
        if not ids:
            return set()

        with db() as ctx:
            rows = (
                ctx.session.execute(
                    select(AnimapEntry).where(
                        AnimapEntry.provider == "anilist",
                        AnimapEntry.entry_id.in_(tuple(str(aid) for aid in ids)),
                    )
                )
                .scalars()
                .all()
            )
            entry_ids = {row.id for row in rows}

            if not entry_ids:
                return set()

            mappings = (
                ctx.session.execute(
                    select(AnimapMapping).where(
                        or_(
                            AnimapMapping.source_entry_id.in_(entry_ids),
                            AnimapMapping.destination_entry_id.in_(entry_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )

            for mapping in mappings:
                entry_ids.add(mapping.source_entry_id)
                entry_ids.add(mapping.destination_entry_id)

        return entry_ids

    async def _resolve_bare_term(self, term: str) -> set[int]:
        """Resolve bare AniList search terms to entry identifiers."""
        client = await get_app_state().ensure_public_anilist()
        text = self._normalize_text_query(term)
        if not text:
            return set()
        try:
            ids = await client.search_media_ids(
                filters={"search": text}, max_results=self._ANILIST_MAX_RESULTS
            )
        except AniListFilterError, AniListSearchError:
            raise
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AniListSearchError(
                f"Failed to resolve AniList search term '{term}'"
            ) from exc
        return self._entry_ids_for_anilist_ids(ids)

    def _resolve_db_term(self, ctx, term: KeyTerm) -> set[int]:
        """Resolve a KeyTerm into matching entry identifiers."""
        spec = self._FIELD_MAP.get(term.key.lower())
        if not spec:
            return set()

        raw_value = self._normalize_null(
            term.value if term.quoted else term.value.strip()
        )
        value_parts = (
            tuple(self._normalize_null(part) for part in term.values)
            if term.values
            else None
        )

        if spec.kind == QueryFieldKind.DB_SCALAR:
            if not spec.column:
                return set()
            return self._filter_entry_column(ctx, spec.column, raw_value, value_parts)

        if spec.kind == QueryFieldKind.DB_EDGE_TARGET:
            if not spec.edge_field:
                return set()
            return self._filter_edge_target(
                ctx, spec.edge_field, raw_value, value_parts
            )

        if spec.kind == QueryFieldKind.DB_EDGE_RANGE:
            if not spec.edge_field:
                return set()
            return self._filter_edge_range(ctx, spec.edge_field, raw_value, value_parts)

        return set()

    def _build_item(
        self,
        entry: AnimapEntry,
        edges: Iterable[AnimapMapping],
        provenance: Mapping[int, list[str]],
    ) -> MappingItem:
        """Construct a MappingItem from DB rows."""
        edge_views: list[EdgeView] = []
        seen_sources: list[str] = []
        entry_by_id: dict[int, AnimapEntry] = self._fetch_entries_for_edges(edges)

        for edge in edges:
            edge_sources = provenance.get(edge.id, [])
            for src in edge_sources:
                if src not in seen_sources:
                    seen_sources.append(src)

            target = entry_by_id.get(edge.destination_entry_id)
            if not target:
                continue
            edge_views.append(
                EdgeView(
                    target_provider=target.provider,
                    target_entry_id=target.entry_id,
                    target_scope=target.entry_scope,
                    source_range=edge.source_range,
                    destination_range=edge.destination_range,
                    sources=edge_sources,
                )
            )

        custom = (
            any(src != self.upstream_url for src in seen_sources)
            if seen_sources
            else False
        )

        anilist_id = self._resolve_anilist_id(entry, entry_by_id, edges)
        return MappingItem(
            provider=entry.provider,
            entry_id=entry.entry_id,
            scope=entry.entry_scope,
            edges=edge_views,
            custom=custom,
            sources=seen_sources,
            anilist_id=anilist_id,
        )

    def _resolve_anilist_id(
        self,
        entry: AnimapEntry,
        entry_by_id: Mapping[int, AnimapEntry],
        edges: Iterable[AnimapMapping],
    ) -> int | None:
        """Pick the first AniList identifier available for a mapping entry."""

        def _to_int(value: str | None) -> int | None:
            try:
                return int(value) if value is not None else None
            except TypeError, ValueError:
                return None

        if entry.provider == "anilist":
            return _to_int(entry.entry_id)

        for edge in edges:
            target = entry_by_id.get(edge.destination_entry_id)
            if target and target.provider == "anilist":
                aid = _to_int(target.entry_id)
                if aid is not None:
                    return aid

        return None

    async def _attach_anilist_metadata(
        self, items: list[MappingItem]
    ) -> list[MappingItem]:
        """Fetch AniList metadata for items with a resolvable AniList ID."""
        seen_ids: set[int] = set()
        anilist_ids: list[int] = []
        for item in items:
            if item.anilist_id is None:
                continue
            if item.anilist_id not in seen_ids:
                seen_ids.add(item.anilist_id)
                anilist_ids.append(item.anilist_id)

        if not anilist_ids:
            return items

        client = await get_app_state().ensure_public_anilist()
        metadata = await client.batch_get_anime(anilist_ids)
        by_id = {m.id: m for m in metadata}

        return [
            replace(item, anilist=by_id.get(item.anilist_id or -1)) for item in items
        ]

    def _fetch_entries_for_edges(
        self, edges: Iterable[AnimapMapping]
    ) -> dict[int, AnimapEntry]:
        ids: set[int] = set()
        for edge in edges:
            ids.add(edge.source_entry_id)
            ids.add(edge.destination_entry_id)
        if not ids:
            return {}
        with db() as ctx:
            rows = (
                ctx.session.execute(select(AnimapEntry).where(AnimapEntry.id.in_(ids)))
                .scalars()
                .all()
            )
        return {row.id: row for row in rows}

    def _load_edges_and_provenance(
        self, entry_ids: Iterable[int]
    ) -> tuple[Sequence[AnimapMapping], dict[int, list[str]]]:
        """Load mapping edges and provenance rows for the given entries."""
        ids = list(entry_ids)
        if not ids:
            return [], {}

        with db() as ctx:
            edge_rows = (
                ctx.session.execute(
                    select(AnimapMapping).where(AnimapMapping.source_entry_id.in_(ids))
                )
                .scalars()
                .all()
            )
            edge_ids = [edge.id for edge in edge_rows]
            prov_rows = (
                ctx.session.execute(
                    select(AnimapProvenance).where(
                        AnimapProvenance.mapping_id.in_(edge_ids)
                    )
                )
                .scalars()
                .all()
            )
            provenance: dict[int, list[str]] = {}
            for row in prov_rows:
                provenance.setdefault(row.mapping_id, []).append(row.source)

        return edge_rows, provenance

    async def _build_items_for_entries(
        self,
        entries: Iterable[AnimapEntry],
        with_anilist: bool,
        *,
        custom_only: bool,
    ) -> list[MappingItem]:
        """Construct MappingItem objects for the provided entries."""
        entry_list = list(entries)
        if not entry_list:
            return []

        entry_ids = [e.id for e in entry_list]
        edge_rows, provenance = self._load_edges_and_provenance(entry_ids)

        items: list[MappingItem] = []
        for entry in entry_list:
            entry_edges = [e for e in edge_rows if e.source_entry_id == entry.id]
            item = self._build_item(entry, entry_edges, provenance)
            if custom_only and not item.custom:
                continue
            items.append(item)

        if with_anilist:
            return await self._attach_anilist_metadata(items)
        return items

    def _filter_custom_entry_ids(self, entry_ids: Iterable[int]) -> set[int]:
        """Return the subset of entry IDs that have a custom provenance source."""
        ids = list(entry_ids)
        if not ids:
            return set()

        upstream = self.upstream_url
        prov_clause = (
            AnimapProvenance.source != upstream
            if upstream
            else AnimapProvenance.source.is_not(None)
        )

        custom_ids: set[int] = set()
        batch_size = 500
        with db() as ctx:
            for start in range(0, len(ids), batch_size):
                chunk = ids[start : start + batch_size]
                stmt = (
                    select(AnimapMapping.source_entry_id)
                    .join(
                        AnimapProvenance,
                        AnimapProvenance.mapping_id == AnimapMapping.id,
                    )
                    .where(AnimapMapping.source_entry_id.in_(chunk))
                    .where(prov_clause)
                )
                custom_ids.update(self._fetch_ids(ctx, stmt))

        return custom_ids

    def _order_entry_ids(
        self, ids: set[int], order_hint: dict[int, int] | None
    ) -> list[int]:
        if not order_hint:
            return sorted(ids)
        return sorted(ids, key=lambda eid: (order_hint.get(eid, 10**9), eid))

    async def _evaluate_query(
        self, q: str, custom_only: bool
    ) -> tuple[list[int], dict[int, int] | None]:
        """Evaluate a booru-like query into ordered entry identifiers."""
        node = parse_query(q)
        key_terms = collect_key_terms(node)
        term_results: dict[int, set[int]] = {}

        client = None
        # Resolve AniList terms first to hydrate the cache
        for term in key_terms:
            spec = self._FIELD_MAP.get(term.key.lower())
            if not spec or spec.kind not in self._ANILIST_KINDS:
                continue
            if client is None:
                client = await get_app_state().ensure_public_anilist()
            value_text = term.value if term.quoted else term.value.strip()
            ids = await self._resolve_anilist_term(
                client, spec, value_text, multi_values=term.values
            )
            entry_ids = self._entry_ids_for_anilist_ids(ids)
            term_results[id(term)] = entry_ids

        bare_cache: dict[str, set[int]] = {}
        for term_text in collect_bare_terms(node):
            bare_cache[term_text] = await self._resolve_bare_term(term_text)

        with db() as ctx:
            universe_ids = self._fetch_ids(ctx, select(AnimapEntry.id))

            def db_resolver(term: KeyTerm) -> set[int]:
                spec = self._FIELD_MAP.get(term.key.lower())
                if spec and spec.kind in self._ANILIST_KINDS:
                    cached = term_results.get(id(term))
                    return set(cached or set())
                return self._resolve_db_term(ctx, term)

            def anilist_resolver(term: str) -> list[int]:
                return list(bare_cache.get(term, set()))

            eval_res = evaluate(
                node,
                db_resolver=db_resolver,
                anilist_resolver=anilist_resolver,
                universe_ids=universe_ids,
            )

        matching_ids: set[int] = set(eval_res.ids)
        if custom_only and matching_ids:
            custom_ids = self._filter_custom_entry_ids(matching_ids)
            matching_ids = matching_ids & custom_ids

        ordered = self._order_entry_ids(matching_ids, eval_res.order_hint)
        return ordered, eval_res.order_hint if eval_res.used_bare else None

    async def list_mappings(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        custom_only: bool,
        with_anilist: bool = False,
        cancel_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """List mapping entries with pagination and optional booru-like query.

        Args:
            page (int): The page number (1-based).
            per_page (int): The number of items per page.
            q (str | None): The booru-like query string.
            custom_only (bool): Whether to include only custom mappings.
            with_anilist (bool): Whether to attach AniList metadata.
            cancel_check (Callable[[], Awaitable[bool]] | None): Optional async
                function to check for cancellation.

        Returns:
            tuple[list[dict[str, Any]], int]: A tuple of the list of mapping items
                and the total number of matching items.
        """

        async def ensure_not_cancelled() -> None:
            task = asyncio.current_task()
            if task and task.cancelled():
                raise asyncio.CancelledError
            if cancel_check and await cancel_check():
                raise asyncio.CancelledError

        await ensure_not_cancelled()

        if q and q.strip():
            try:
                ordered_ids, _ = await self._evaluate_query(q, custom_only)
            except BooruQuerySyntaxError, AniListFilterError, AniListSearchError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise BooruQueryEvaluationError(
                    "Failed to evaluate booru query"
                ) from exc

            total = len(ordered_ids)
            start = (page - 1) * per_page
            end = start + per_page
            page_ids = ordered_ids[start:end]
            if not page_ids:
                return [], total

            with db() as ctx:
                entries = (
                    ctx.session.execute(
                        select(AnimapEntry).where(AnimapEntry.id.in_(page_ids))
                    )
                    .scalars()
                    .all()
                )

            # preserve order
            entry_map = {e.id: e for e in entries}
            ordered_entries = [entry_map[eid] for eid in page_ids if eid in entry_map]
            items = await self._build_items_for_entries(
                ordered_entries, with_anilist, custom_only=custom_only
            )
            await ensure_not_cancelled()
            return [item.to_dict() for item in items], total

        # Default listing without booru query
        with db() as ctx:
            base_stmt = select(AnimapEntry)

            if custom_only:
                prov_clause = (
                    AnimapProvenance.source != self.upstream_url
                    if self.upstream_url
                    else AnimapProvenance.source.is_not(None)
                )
                custom_exists = (
                    select(1)
                    .select_from(AnimapProvenance)
                    .join(
                        AnimapMapping,
                        AnimapMapping.id == AnimapProvenance.mapping_id,
                    )
                    .where(AnimapMapping.source_entry_id == AnimapEntry.id)
                    .where(prov_clause)
                    .limit(1)
                    .exists()
                )
                base_stmt = base_stmt.where(custom_exists)

            total = ctx.session.execute(
                select(func.count()).select_from(base_stmt.subquery())
            ).scalar_one()

            entries = (
                ctx.session.execute(
                    base_stmt.order_by(AnimapEntry.provider, AnimapEntry.entry_id)
                    .offset((page - 1) * per_page)
                    .limit(per_page)
                )
                .scalars()
                .all()
            )

        if not entries:
            return [], total

        await ensure_not_cancelled()
        items = await self._build_items_for_entries(
            entries, with_anilist, custom_only=custom_only
        )
        await ensure_not_cancelled()
        return [item.to_dict() for item in items], total

    async def get_mapping(self, descriptor: str) -> dict[str, Any]:
        """Return a single mapping entry by descriptor.

        Args:
            descriptor (str): The mapping descriptor to fetch.

        Returns:
            dict[str, Any]: The mapping item.
        """
        parsed = parse_mapping_descriptor(descriptor)
        provider, entry_id, scope = parsed
        with db() as ctx:
            scope_clause = (
                AnimapEntry.entry_scope.is_(None)
                if scope is None
                else AnimapEntry.entry_scope == scope
            )
            entry = (
                ctx.session.execute(
                    select(AnimapEntry).where(
                        AnimapEntry.provider == provider,
                        AnimapEntry.entry_id == entry_id,
                        scope_clause,
                    )
                )
                .scalars()
                .first()
            )
            if not entry:
                raise MappingNotFoundError("Mapping not found")

        edge_rows, provenance = self._load_edges_and_provenance([entry.id])
        item = self._build_item(entry, edge_rows, provenance)
        return item.to_dict()


@cache
def get_mappings_service() -> MappingsService:
    """Return a singleton mappings service instance.

    Returns:
        MappingsService: The singleton service instance.
    """
    return MappingsService()
