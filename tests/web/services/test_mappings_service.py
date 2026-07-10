"""Tests for v3 mappings service (authority-range graph)."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest

from anibridge.app.config.database import db
from anibridge.app.core.anilist import AnilistClient
from anibridge.app.exceptions import (
    AniListFilterError,
    AniListSearchError,
    BooruQueryEvaluationError,
)
from anibridge.app.models.db.animap import AnimapEntry, AnimapMapping, AnimapProvenance
from anibridge.app.models.schemas.anilist import Media, MediaTitle
from anibridge.app.utils.booru_query import And, KeyTerm, Not, Or, parse_query
from anibridge.app.web.services.mappings_query_spec import (
    QueryFieldKind,
    QueryFieldOperator,
    QueryFieldSpec,
    QueryFieldType,
)
from anibridge.app.web.services.mappings_service import (
    MappingItem,
    MappingsService,
    get_mappings_service,
)


@contextmanager
def _fresh_tables():
    with db() as ctx:
        ctx.session.query(AnimapProvenance).delete()
        ctx.session.query(AnimapMapping).delete()
        ctx.session.query(AnimapEntry).delete()
        ctx.session.commit()
    try:
        yield
    finally:
        with db() as ctx:
            ctx.session.query(AnimapProvenance).delete()
            ctx.session.query(AnimapMapping).delete()
            ctx.session.query(AnimapEntry).delete()
            ctx.session.commit()


def _seed_graph():
    with db() as ctx:
        a = AnimapEntry(authority="anilist", value="1", scope=None)
        b = AnimapEntry(authority="tmdb", value="10", scope=None)
        ctx.session.add_all([a, b])
        ctx.session.flush()
        mapping = AnimapMapping(
            source_entry_id=a.id,
            destination_entry_id=b.id,
            source_range="1",
            destination_range=None,
        )
        ctx.session.add(mapping)
        ctx.session.flush()
        ctx.session.add(AnimapProvenance(mapping_id=mapping.id, n=0, source="custom"))
        ctx.session.commit()


def _make_spec(**overrides: Any) -> QueryFieldSpec:
    """Build a minimal query field spec for helper-level tests."""
    payload: dict[str, Any] = {
        "key": "spec",
        "kind": QueryFieldKind.ANILIST_STRING,
        "type": QueryFieldType.STRING,
        "operators": (QueryFieldOperator.EQ,),
        "anilist_field": "search",
        "anilist_value_type": "string",
    }
    payload.update(overrides)
    return QueryFieldSpec(**payload)


@pytest.mark.asyncio
async def test_list_mappings_returns_edges_and_sources() -> None:
    """Listing mappings returns items with edges and source info."""
    service = MappingsService()
    with _fresh_tables():
        _seed_graph()
        items, total = await service.list_mappings(
            page=1, per_page=10, q=None, custom_only=False
        )
        assert total == 2
        descriptors = {item["descriptor"] for item in items}
        assert "anilist:1" in descriptors
        assert any(edge["target_authority"] == "tmdb" for edge in items[0]["edges"])


@pytest.mark.asyncio
async def test_custom_only_filters_items() -> None:
    """Listing mappings with custom_only filters out non-custom items."""
    service = MappingsService()
    with _fresh_tables():
        _seed_graph()
        # Add upstream provenance to second mapping to mark non-custom
        with db() as ctx:
            tmdb_entry = (
                ctx.session.query(AnimapEntry).filter_by(authority="tmdb").one()
            )
            anilist_entry = (
                ctx.session.query(AnimapEntry).filter_by(authority="anilist").one()
            )
            mapping = AnimapMapping(
                source_entry_id=tmdb_entry.id,
                destination_entry_id=anilist_entry.id,
                source_range="1",
                destination_range=None,
            )
            ctx.session.add(mapping)
            ctx.session.flush()
            ctx.session.add(
                AnimapProvenance(mapping_id=mapping.id, n=0, source="upstream")
            )
            ctx.session.commit()

        items, _ = await service.list_mappings(
            page=1, per_page=10, q=None, custom_only=True
        )
        assert all(item["custom"] for item in items)


@pytest.mark.asyncio
async def test_custom_only_applies_before_pagination() -> None:
    """Custom-only filtering happens before pagination slices results."""
    service = MappingsService()
    with _fresh_tables():
        with db() as ctx:
            upstream_entry = AnimapEntry(authority="aaa", value="1", scope=None)
            upstream_target = AnimapEntry(authority="dest", value="1", scope=None)
            custom_entry = AnimapEntry(authority="zzz", value="1", scope=None)
            custom_target = AnimapEntry(authority="dest", value="2", scope=None)
            ctx.session.add_all(
                [upstream_entry, upstream_target, custom_entry, custom_target]
            )
            ctx.session.flush()

            upstream_mapping = AnimapMapping(
                source_entry_id=upstream_entry.id,
                destination_entry_id=upstream_target.id,
                source_range="1",
                destination_range=None,
            )
            custom_mapping = AnimapMapping(
                source_entry_id=custom_entry.id,
                destination_entry_id=custom_target.id,
                source_range="1",
                destination_range=None,
            )
            ctx.session.add_all([upstream_mapping, custom_mapping])
            ctx.session.flush()

            upstream_source = service.upstream_url or "upstream"
            ctx.session.add(
                AnimapProvenance(
                    mapping_id=upstream_mapping.id, n=0, source=upstream_source
                )
            )
            ctx.session.add(
                AnimapProvenance(mapping_id=custom_mapping.id, n=0, source="custom")
            )
            ctx.session.commit()

        items, total = await service.list_mappings(
            page=1, per_page=1, q=None, custom_only=True
        )

        assert total == 1
        assert len(items) == 1
        assert items[0]["descriptor"] == (
            f"{custom_entry.authority}:{custom_entry.value}"
        )


@pytest.mark.asyncio
async def test_filter_custom_entry_ids_batches() -> None:
    """Custom filtering handles large ID sets without exceeding SQL var limits."""
    service = MappingsService()
    with _fresh_tables():
        total_entries = 501
        with db() as ctx:
            entries: list[AnimapEntry] = []
            for idx in range(total_entries):
                entry = AnimapEntry(authority=f"p{idx:04d}", value=str(idx), scope=None)
                target = AnimapEntry(authority="t", value=str(idx), scope=None)
                ctx.session.add_all([entry, target])
                entries.append(entry)
            ctx.session.flush()

            mappings: list[AnimapMapping] = []
            for entry in entries:
                target = (
                    ctx.session.query(AnimapEntry)
                    .filter_by(authority="t", value=entry.value)
                    .one()
                )
                mapping = AnimapMapping(
                    source_entry_id=entry.id,
                    destination_entry_id=target.id,
                    source_range="1",
                    destination_range=None,
                )
                mappings.append(mapping)
            ctx.session.add_all(mappings)
            ctx.session.flush()

            for n, mapping in enumerate(mappings):
                source = (
                    "custom" if n % 2 == 0 else (service.upstream_url or "upstream")
                )
                ctx.session.add(
                    AnimapProvenance(mapping_id=mapping.id, n=0, source=source)
                )
            ctx.session.commit()

            entry_ids = [e.id for e in entries]

        custom_ids = service._filter_custom_entry_ids(entry_ids)

        assert len(custom_ids) == total_entries // 2 + total_entries % 2


@pytest.mark.asyncio
async def test_search_by_descriptor_string() -> None:
    """Booru query supports source and target descriptor filters."""
    service = MappingsService()
    with _fresh_tables():
        with db() as ctx:
            plain = AnimapEntry(authority="tmdb", value="10", scope=None)
            scoped = AnimapEntry(authority="tmdb", value="11", scope="s1")
            target_plain = AnimapEntry(authority="anilist", value="1", scope=None)
            target_scoped = AnimapEntry(authority="anilist", value="2", scope="s1")
            ctx.session.add_all([plain, scoped, target_plain, target_scoped])
            ctx.session.flush()

            ctx.session.add_all(
                [
                    AnimapMapping(
                        source_entry_id=plain.id,
                        destination_entry_id=target_plain.id,
                        source_range="1",
                        destination_range=None,
                    ),
                    AnimapMapping(
                        source_entry_id=scoped.id,
                        destination_entry_id=target_scoped.id,
                        source_range="1",
                        destination_range=None,
                    ),
                ]
            )
            ctx.session.commit()

        items, total = await service.list_mappings(
            page=1,
            per_page=10,
            q="source.descriptor:tmdb:11:s1",
            custom_only=False,
        )

        assert total == 1
        assert len(items) == 1
        assert items[0]["descriptor"] == "tmdb:11:s1"

        items, total = await service.list_mappings(
            page=1,
            per_page=10,
            q="source.descriptor:tmdb:1*",
            custom_only=False,
        )

        assert total == 2
        assert {item["descriptor"] for item in items} == {"tmdb:10", "tmdb:11:s1"}

        items, total = await service.list_mappings(
            page=1,
            per_page=10,
            q="target.descriptor:anilist:2:s1",
            custom_only=False,
        )

        assert total == 1
        assert len(items) == 1
        assert items[0]["descriptor"] == "tmdb:11:s1"

        items, total = await service.list_mappings(
            page=1,
            per_page=10,
            q="target.descriptor:anilist:*",
            custom_only=False,
        )

        assert total == 2
        assert {item["descriptor"] for item in items} == {"tmdb:10", "tmdb:11:s1"}


def test_build_anilist_term_filters_string_and_enum() -> None:
    """AniList string and enum filters normalize and validate input."""
    service = MappingsService()
    string_spec = service._FIELD_MAP["anilist.title"]
    enum_spec = service._FIELD_MAP["anilist.format"]

    assert service._build_anilist_term_filters(string_spec, "  My*?  Title  ") == {
        "search": "My Title"
    }

    with pytest.raises(AniListFilterError):
        service._build_anilist_term_filters(
            string_spec, "foo", multi_values=("bar", "baz")
        )

    enum_filters = service._build_anilist_term_filters(
        enum_spec, "tv", multi_values=("tv", "MOVIE", "TV")
    )
    assert enum_filters == {"format_in": ["TV", "MOVIE"]}

    with pytest.raises(AniListFilterError):
        service._build_anilist_term_filters(enum_spec, "not-a-format")


def test_build_anilist_numeric_filters_fuzzy_ranges() -> None:
    """AniList numeric filters handle fuzzy ranges and comparisons."""
    service = MappingsService()
    spec = service._FIELD_MAP["anilist.start_date"]

    range_filters = service._build_anilist_term_filters(spec, "2020..2021")
    lo = service._normalize_fuzzy_date_number(2020)
    hi = service._normalize_fuzzy_date_number(2021)
    assert range_filters == {
        "startDate_greater": service._fuzzy_lower_threshold(lo),
        "startDate_lesser": service._fuzzy_upper_threshold(hi),
    }

    cmp_filters = service._build_anilist_term_filters(spec, ">=20200101")
    expected = service._fuzzy_lower_threshold(
        service._normalize_fuzzy_date_number(20200101)
    )
    assert cmp_filters == {"startDate_greater": expected}

    invalid_spec = service._FIELD_MAP["anilist.episodes"]
    with pytest.raises(AniListFilterError):
        service._build_anilist_term_filters(invalid_spec, "not-a-number")


def test_build_anilist_numeric_id_filters_support_multiple_values() -> None:
    """AniList ID filters should map IN queries to id_in."""
    service = MappingsService()
    spec = service._FIELD_MAP["anilist.id"]

    assert service._build_anilist_term_filters(
        spec,
        "1",
        multi_values=("1", "2", "1"),
    ) == {"id_in": [1, 2]}

    with pytest.raises(AniListFilterError):
        service._build_anilist_term_filters(
            spec,
            "1",
            multi_values=("1", "bad"),
        )


def test_collect_anilist_and_groups() -> None:
    """AniList key terms are grouped when directly AND-ed."""
    service = MappingsService()
    node = parse_query("anilist.id:1 anilist.format:TV source.authority:tmdb")
    groups = service._collect_anilist_and_groups(node)

    assert len(groups) == 1
    keys = {term.key for term in groups[0]}
    assert keys == {"anilist.id", "anilist.format"}


def test_filter_entry_and_edge_helpers() -> None:
    """Helper filters respect wildcards, nulls, and edge attributes."""
    service = MappingsService()
    with _fresh_tables(), db() as ctx:
        entry_a = AnimapEntry(authority="anilist", value="abc", scope=None)
        entry_b = AnimapEntry(authority="anilist", value="xyz", scope="s1")
        target = AnimapEntry(authority="tmdb", value="10", scope=None)
        ctx.session.add_all([entry_a, entry_b, target])
        ctx.session.flush()

        mapping_a = AnimapMapping(
            source_entry_id=entry_a.id,
            destination_entry_id=target.id,
            source_range="1..2",
            destination_range=None,
        )
        mapping_b = AnimapMapping(
            source_entry_id=entry_b.id,
            destination_entry_id=target.id,
            source_range="3",
            destination_range="3",
        )
        ctx.session.add_all([mapping_a, mapping_b])
        ctx.session.commit()

        wildcard_ids = service._filter_entry_column(ctx, AnimapEntry.value, "a*")
        assert wildcard_ids == {entry_a.id}

        null_scope_ids = service._filter_entry_column(ctx, AnimapEntry.scope, None)
        assert null_scope_ids == {entry_a.id, target.id}

        values_scope_ids = service._filter_entry_column(
            ctx, AnimapEntry.scope, None, ("s1", None)
        )
        assert values_scope_ids == {entry_a.id, entry_b.id, target.id}

        edge_ids = service._filter_edge_target(ctx, "authority", "tmdb")
        assert edge_ids == {entry_a.id, entry_b.id}

        edge_null_scope = service._filter_edge_target(ctx, "scope", None)
        assert edge_null_scope == {entry_a.id, entry_b.id}

        edge_range_ids = service._filter_edge_range(ctx, "destination_range", None)
        assert edge_range_ids == {entry_a.id}


def test_resolve_db_term_in_values_and_nulls() -> None:
    """DB term resolution handles IN lists and null literals."""
    service = MappingsService()
    with _fresh_tables(), db() as ctx:
        entry_a = AnimapEntry(authority="anilist", value="1", scope=None)
        entry_b = AnimapEntry(authority="tmdb", value="2", scope="s1")
        ctx.session.add_all([entry_a, entry_b])
        ctx.session.commit()

        in_term = KeyTerm(
            key="source.authority",
            value="anilist,tmdb",
            values=("anilist", "tmdb"),
        )
        in_ids = service._resolve_db_term(ctx, in_term)
        assert in_ids == {entry_a.id, entry_b.id}

        null_term = KeyTerm(key="source.scope", value="null")
        null_ids = service._resolve_db_term(ctx, null_term)
        assert null_ids == {entry_a.id}


def test_order_entry_ids_uses_hint() -> None:
    """Ordering uses order hints when provided."""
    service = MappingsService()
    assert service._order_entry_ids({3, 1, 2}, {2: 0, 3: 1}) == [2, 3, 1]


@pytest.mark.asyncio
async def test_attach_anilist_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """AniList metadata is attached to mapping items when available."""
    service = MappingsService()

    class DummyClient:
        async def batch_get_anime(self, ids: list[int]) -> list[Media]:
            return [Media(id=ids[0], title=MediaTitle(romaji="Mock"))]

    class DummyState:
        async def ensure_public_anilist(self) -> DummyClient:
            return DummyClient()

    monkeypatch.setattr(
        "anibridge.app.web.services.mappings_service.get_app_state",
        lambda: DummyState(),
    )

    items = [
        MappingItem(
            authority="anilist",
            value="1",
            scope=None,
            edges=[],
            custom=True,
            sources=["custom"],
            anilist_id=1,
        )
    ]

    enriched = await service._attach_anilist_metadata(items)
    assert enriched[0].anilist is not None
    assert enriched[0].anilist.title is not None
    assert enriched[0].anilist.title.romaji == "Mock"


@pytest.mark.asyncio
async def test_list_mappings_with_anilist_degrades_when_metadata_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AniList enrichment failures should not break mappings listing."""
    service = MappingsService()

    class DummyClient:
        async def batch_get_anime(self, ids: list[int]) -> list[Media]:
            raise RuntimeError(f"boom: {ids}")

    class DummyState:
        async def ensure_public_anilist(self) -> DummyClient:
            return DummyClient()

    monkeypatch.setattr(
        "anibridge.app.web.services.mappings_service.get_app_state",
        lambda: DummyState(),
    )

    with _fresh_tables():
        _seed_graph()
        items, total = await service.list_mappings(
            page=1,
            per_page=10,
            q=None,
            custom_only=False,
            with_anilist=True,
        )

    assert total == 2
    assert len(items) == 2
    assert all(item["anilist"] is None for item in items)


def test_basic_query_helpers_cover_parsing_edges() -> None:
    """Low-level parsing helpers should normalize common edge cases."""
    service = MappingsService()

    assert service._has_wildcards(None) is False
    assert service._has_wildcards("tm*") is True
    assert service._normalize_null(None) is None
    assert service._normalize_null(" null ") is None
    assert service._normalize_null("value") == "value"
    assert service._like_pattern("a*b?") == "a%b_"
    assert service._parse_numeric_filters(None) == (None, None, "")
    assert service._parse_numeric_filters(">=12") == ((">=", 12), None, ">=12")
    assert service._parse_numeric_filters("4..2") == (None, (4, 2), "4..2")
    assert service._normalize_text_query("  a*?   b  ") == "a b"
    assert service._parse_int_value("12") == 12
    assert service._parse_int_value("nope") is None
    assert service._parse_fuzzy_date_int("2024-02") == 20240200
    assert service._parse_fuzzy_date_int("2024010199") == 20240101
    assert service._parse_fuzzy_date_int("abc") is None
    assert service._normalize_fuzzy_date_number(2024) == 20240000


def test_build_anilist_term_filters_reject_invalid_specs() -> None:
    """AniList filter building should reject unsupported or malformed inputs."""
    service = MappingsService()

    with pytest.raises(AniListFilterError):
        service._build_anilist_term_filters(
            _make_spec(anilist_field=None),
            "value",
        )

    with pytest.raises(AniListFilterError):
        service._build_anilist_term_filters(
            _make_spec(anilist_multi_field="search_in"),
            "value",
            multi_values=(" * ", " ? "),
        )

    with pytest.raises(AniListFilterError):
        service._build_anilist_term_filters(
            _make_spec(
                kind=QueryFieldKind.ANILIST_ENUM,
                type=QueryFieldType.ENUM,
                values=(),
                anilist_field="format",
                anilist_value_type="enum",
            ),
            "tv",
        )

    with pytest.raises(AniListFilterError):
        service._build_anilist_term_filters(
            _make_spec(
                kind=QueryFieldKind.ANILIST_ENUM,
                type=QueryFieldType.ENUM,
                values=("TV",),
                anilist_field="format",
                anilist_value_type="enum",
            ),
            "tv",
            multi_values=("tv",),
        )

    with pytest.raises(AniListFilterError):
        service._build_anilist_term_filters(
            _make_spec(
                kind=QueryFieldKind.ANILIST_NUMERIC,
                type=QueryFieldType.INT,
                anilist_field="episodes",
                anilist_value_type="int",
            ),
            "1",
            multi_values=("1", "2"),
        )

    with pytest.raises(AniListFilterError):
        service._build_anilist_term_filters(
            _make_spec(
                kind=QueryFieldKind.DB_SCALAR,
                type=QueryFieldType.STRING,
                anilist_field="authority",
            ),
            "tmdb",
        )


@pytest.mark.asyncio
async def test_resolve_anilist_term_wraps_generic_errors() -> None:
    """Unexpected AniList failures should be wrapped as search errors."""
    service = MappingsService()
    spec = service._FIELD_MAP["anilist.title"]

    class BrokenClient:
        async def search_media_ids(self, **_kwargs):
            raise RuntimeError("boom")

    with pytest.raises(AniListSearchError):
        await service._resolve_anilist_term(
            cast(AnilistClient, BrokenClient()), spec, "title"
        )


@pytest.mark.asyncio
async def test_resolve_anilist_term_propagates_cancellation() -> None:
    """Cancelled AniList work should bubble up unchanged."""
    service = MappingsService()
    spec = service._FIELD_MAP["anilist.title"]

    class CancelledClient:
        async def search_media_ids(self, **_kwargs):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await service._resolve_anilist_term(
            cast(AnilistClient, CancelledClient()), spec, "title"
        )


def test_collect_anilist_and_groups_handles_nested_nodes() -> None:
    """Grouping should walk OR, NOT, and list containers."""
    service = MappingsService()
    grouped = service._collect_anilist_and_groups(
        cast(
            Any,
            [
                Or(
                    children=[
                        And(
                            children=[
                                KeyTerm("anilist.id", "1"),
                                KeyTerm("anilist.format", "TV"),
                            ]
                        ),
                        Not(
                            child=And(
                                children=[
                                    KeyTerm("anilist.status", "FINISHED"),
                                    KeyTerm("anilist.title", "foo"),
                                ]
                            )
                        ),
                    ]
                )
            ],
        )
    )

    assert len(grouped) == 2
    assert {term.key for term in grouped[0]} == {"anilist.id", "anilist.format"}
    assert {term.key for term in grouped[1]} == {"anilist.status", "anilist.title"}


def test_fuzzy_thresholds_fall_back_when_date_conversion_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid fuzzy dates should return the original value unchanged."""
    monkeypatch.setattr(
        MappingsService,
        "_fuzzy_to_datetime",
        classmethod(lambda cls, value, bias: (_ for _ in ()).throw(ValueError(value))),
    )

    assert MappingsService._fuzzy_lower_threshold(20240000) == 20240000
    assert MappingsService._fuzzy_upper_threshold(20240000) == 20240000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ((">", 8), {"episodes_greater": 8}),
        ((">=", 8), {"episodes_greater": 7}),
        (("<", 8), {"episodes_lesser": 8}),
        (("<=", 8), {"episodes_lesser": 9}),
    ],
)
def test_build_anilist_numeric_filters_for_scalar_comparisons(
    raw: tuple[str, int], expected: dict[str, int]
) -> None:
    """Numeric AniList comparisons should map to the expected GraphQL filters."""
    service = MappingsService()
    spec = service._FIELD_MAP["anilist.episodes"]

    assert service._build_anilist_numeric_filters(spec, raw, None, "8") == expected


def test_build_anilist_numeric_filters_handles_missing_fields_and_fuzzy_values() -> (
    None
):
    """Numeric helper should handle fuzzy dates, direct values, and bad specs."""
    service = MappingsService()
    fuzzy_spec = service._FIELD_MAP["anilist.start_date"]

    assert service._build_anilist_numeric_filters(
        fuzzy_spec,
        ("<=", 20240101),
        None,
        "20240101",
    ) == {
        "startDate_lesser": service._fuzzy_upper_threshold(20240101),
    }
    assert service._build_anilist_numeric_filters(
        fuzzy_spec,
        None,
        (2024, 2023),
        "2024..2023",
    ) == {
        "startDate_greater": service._fuzzy_lower_threshold(20230000),
        "startDate_lesser": service._fuzzy_upper_threshold(20240000),
    }
    assert service._build_anilist_numeric_filters(
        service._FIELD_MAP["anilist.id"],
        None,
        None,
        "99",
    ) == {"id": 99}
    assert (
        service._build_anilist_numeric_filters(
            _make_spec(
                kind=QueryFieldKind.ANILIST_NUMERIC,
                type=QueryFieldType.INT,
                anilist_field=None,
                anilist_value_type="int",
            ),
            None,
            None,
            "1",
        )
        is None
    )
    assert (
        service._build_anilist_numeric_filters(
            service._FIELD_MAP["anilist.episodes"],
            ("!=", 1),
            None,
            "1",
        )
        is None
    )


def test_scalar_cmp_builds_supported_operators() -> None:
    """Scalar comparisons should return SQL expressions only for supported ops."""
    service = MappingsService()

    assert service._scalar_cmp(AnimapEntry.id, ">", 1) is not None
    assert service._scalar_cmp(AnimapEntry.id, ">=", 1) is not None
    assert service._scalar_cmp(AnimapEntry.id, "<", 1) is not None
    assert service._scalar_cmp(AnimapEntry.id, "<=", 1) is not None
    assert service._scalar_cmp(AnimapEntry.id, "=", 1) is None


def test_filter_helpers_support_wildcards_and_empty_inputs() -> None:
    """DB helpers should support wildcard destination matching and empty sets."""
    service = MappingsService()
    with _fresh_tables(), db() as ctx:
        source = AnimapEntry(authority="imdb", value="src", scope=None)
        dest = AnimapEntry(authority="tmdb", value="target-1", scope="s1")
        ctx.session.add_all([source, dest])
        ctx.session.flush()
        ctx.session.add(
            AnimapMapping(
                source_entry_id=source.id,
                destination_entry_id=dest.id,
                source_range="12",
                destination_range="1-3",
            )
        )
        ctx.session.commit()

        assert service._filter_edge_target(ctx, "authority", "tm*") == {source.id}
        assert service._filter_edge_range(ctx, "source_range", "1*") == {source.id}
        assert service._filter_edge_range(ctx, "destination_range", "1-*") == {
            source.id
        }


def test_entry_ids_for_anilist_ids_handles_empty_and_missing_values() -> None:
    """AniList ID expansion should short-circuit on empty or unmatched input."""
    service = MappingsService()

    assert service._entry_ids_for_anilist_ids([]) == set()
    with _fresh_tables():
        assert service._entry_ids_for_anilist_ids([999]) == set()
        _seed_graph()
        expanded = service._entry_ids_for_anilist_ids([1])
        assert len(expanded) == 2


@pytest.mark.asyncio
async def test_resolve_bare_term_handles_blank_and_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare AniList search should return empty for blanks and wrap failures."""
    service = MappingsService()

    class State:
        async def ensure_public_anilist(self):
            return self

        async def search_media_ids(self, **_kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "anibridge.app.web.services.mappings_service.get_app_state",
        lambda: State(),
    )

    assert await service._resolve_bare_term("   ") == set()
    with pytest.raises(AniListSearchError):
        await service._resolve_bare_term("cowboy bebop")


def test_resolve_db_term_handles_unknown_and_misconfigured_fields() -> None:
    """DB term resolution should safely return no matches for bad field specs."""
    service = MappingsService()
    field_map = {
        "missing-column": _make_spec(
            key="missing-column",
            kind=QueryFieldKind.DB_SCALAR,
            type=QueryFieldType.STRING,
            anilist_field=None,
            column=None,
        ),
        "missing-edge-target": _make_spec(
            key="missing-edge-target",
            kind=QueryFieldKind.DB_EDGE_TARGET,
            type=QueryFieldType.STRING,
            anilist_field=None,
            edge_field=None,
        ),
        "missing-edge-range": _make_spec(
            key="missing-edge-range",
            kind=QueryFieldKind.DB_EDGE_RANGE,
            type=QueryFieldType.STRING,
            anilist_field=None,
            edge_field=None,
        ),
    }

    original = MappingsService._FIELD_MAP
    MappingsService._FIELD_MAP = field_map
    try:
        with db() as ctx:
            assert service._resolve_db_term(ctx, KeyTerm("unknown", "x")) == set()
            assert (
                service._resolve_db_term(ctx, KeyTerm("missing-column", "x")) == set()
            )
            assert (
                service._resolve_db_term(ctx, KeyTerm("missing-edge-target", "x"))
                == set()
            )
            assert (
                service._resolve_db_term(ctx, KeyTerm("missing-edge-range", "x"))
                == set()
            )
    finally:
        MappingsService._FIELD_MAP = original


def test_build_item_and_resolve_anilist_id_handle_missing_targets() -> None:
    """Items should skip missing targets and ignore invalid AniList identifiers."""
    service = MappingsService()
    service.upstream_url = "upstream"
    entry = AnimapEntry(id=1, authority="tmdb", value="10", scope=None)
    invalid_target = AnimapEntry(
        id=2, authority="anilist", value="not-a-number", scope=None
    )
    edge = AnimapMapping(
        id=1,
        source_entry_id=1,
        destination_entry_id=2,
        source_range="1",
        destination_range=None,
    )

    item = service._build_item(
        entry,
        [edge],
        provenance={1: ["upstream"]},
    )

    assert item.edges == []
    assert item.custom is False
    assert item.anilist_id is None
    assert service._resolve_anilist_id(entry, {2: invalid_target}, [edge]) is None
    assert (
        service._resolve_anilist_id(
            AnimapEntry(id=3, authority="anilist", value="bad", scope=None),
            {},
            [],
        )
        is None
    )


@pytest.mark.asyncio
async def test_attach_anilist_metadata_short_circuits_and_propagates_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AniList enrichment should no-op without IDs and re-raise cancellation."""
    service = MappingsService()
    item = MappingItem(
        authority="tmdb",
        value="10",
        scope=None,
        edges=[],
        custom=False,
        sources=[],
    )
    assert await service._attach_anilist_metadata([item]) == [item]

    class DummyClient:
        async def batch_get_anime(self, ids: list[int]) -> list[Media]:
            raise asyncio.CancelledError

    class DummyState:
        async def ensure_public_anilist(self) -> DummyClient:
            return DummyClient()

    monkeypatch.setattr(
        "anibridge.app.web.services.mappings_service.get_app_state",
        lambda: DummyState(),
    )
    cancelled_item = MappingItem(
        authority="tmdb",
        value="10",
        scope=None,
        edges=[],
        custom=False,
        sources=[],
        anilist_id=1,
    )

    with pytest.raises(asyncio.CancelledError):
        await service._attach_anilist_metadata([item, cancelled_item])


@pytest.mark.asyncio
async def test_fetch_build_helpers_handle_empty_and_custom_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Helper builders should short-circuit on empty inputs and custom filtering."""
    service = MappingsService()
    entry = AnimapEntry(id=1, authority="tmdb", value="1", scope=None)
    item = MappingItem(
        authority="tmdb",
        value="1",
        scope=None,
        edges=[],
        custom=False,
        sources=[],
    )

    assert service._fetch_entries_for_edges([]) == {}
    assert service._load_edges_and_provenance([]) == ([], {})
    assert await service._build_items_for_entries([], False, custom_only=False) == []

    monkeypatch.setattr(service, "_load_edges_and_provenance", lambda _ids: ([], {}))
    monkeypatch.setattr(service, "_build_item", lambda *_args: item)
    assert (
        await service._build_items_for_entries([entry], False, custom_only=True) == []
    )


@pytest.mark.asyncio
async def test_evaluate_query_uses_cached_results_and_custom_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Query evaluation should reuse AniList term results and custom-only filtering."""
    service = MappingsService()
    ani_term = KeyTerm("anilist.id", "1")
    db_term = KeyTerm("source.authority", "tmdb")
    node = object()

    class DummyState:
        async def ensure_public_anilist(self):
            return object()

    monkeypatch.setattr(
        "anibridge.app.web.services.mappings_service.parse_query",
        lambda q: node,
    )
    monkeypatch.setattr(
        "anibridge.app.web.services.mappings_service.collect_key_terms",
        lambda n: [ani_term, db_term],
    )
    monkeypatch.setattr(
        "anibridge.app.web.services.mappings_service.collect_bare_terms",
        lambda n: ["bebop"],
    )
    monkeypatch.setattr(
        "anibridge.app.web.services.mappings_service.get_app_state",
        lambda: DummyState(),
    )
    monkeypatch.setattr(
        service,
        "_resolve_anilist_term",
        lambda *args, **kwargs: asyncio.sleep(0, result={10}),
    )
    monkeypatch.setattr(service, "_entry_ids_for_anilist_ids", lambda ids: {1, 2})
    monkeypatch.setattr(
        service,
        "_resolve_bare_term",
        lambda term: asyncio.sleep(0, result={3}),
    )
    monkeypatch.setattr(service, "_fetch_ids", lambda ctx, stmt: {1, 2, 3, 4})
    monkeypatch.setattr(service, "_resolve_db_term", lambda ctx, term: {4})
    monkeypatch.setattr(service, "_filter_custom_entry_ids", lambda ids: {4})

    def fake_evaluate(node_arg, *, db_resolver, anilist_resolver, universe_factory):
        assert node_arg is node
        assert db_resolver(ani_term) == {1, 2}
        assert db_resolver(db_term) == {4}
        assert anilist_resolver("bebop") == [3]
        assert universe_factory() == {1, 2, 3, 4}
        return SimpleNamespace(ids={1, 4}, order_hint={4: 0}, used_bare=True)

    monkeypatch.setattr(
        "anibridge.app.web.services.mappings_service.evaluate",
        fake_evaluate,
    )

    ordered_ids, order_hint = await service._evaluate_query("q", custom_only=True)

    assert ordered_ids == [4]
    assert order_hint == {4: 0}


@pytest.mark.asyncio
async def test_list_mappings_handles_query_errors_empty_pages_and_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listing should translate unexpected query errors and short-circuit cleanly."""
    service = MappingsService()

    async def cancelled() -> bool:
        return True

    with pytest.raises(asyncio.CancelledError):
        await service.list_mappings(
            page=1,
            per_page=10,
            q=None,
            custom_only=False,
            cancel_check=cancelled,
        )

    async def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_evaluate_query", boom)
    with pytest.raises(BooruQueryEvaluationError):
        await service.list_mappings(page=1, per_page=10, q="x", custom_only=False)

    monkeypatch.setattr(
        service,
        "_evaluate_query",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=([1], None)),
    )
    items, total = await service.list_mappings(
        page=2,
        per_page=10,
        q="x",
        custom_only=False,
    )
    assert items == []
    assert total == 1

    with _fresh_tables():
        items, total = await service.list_mappings(
            page=1,
            per_page=10,
            q=None,
            custom_only=False,
        )
    assert items == []
    assert total == 0


@pytest.mark.asyncio
async def test_get_mappings_service_returns_singleton() -> None:
    """The cached factory should be stable."""
    assert get_mappings_service() is get_mappings_service()
