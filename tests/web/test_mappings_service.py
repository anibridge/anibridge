"""Tests for v3 mappings service (provider-range graph)."""

from contextlib import contextmanager

import pytest

from anibridge.app.config.database import db
from anibridge.app.exceptions import AniListFilterError
from anibridge.app.models.db.animap import AnimapEntry, AnimapMapping, AnimapProvenance
from anibridge.app.models.schemas.anilist import Media, MediaTitle
from anibridge.app.utils.booru_query import KeyTerm, parse_query
from anibridge.app.web.services.mappings_service import MappingItem, MappingsService


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
        a = AnimapEntry(provider="anilist", entry_id="1", entry_scope=None)
        b = AnimapEntry(provider="tmdb", entry_id="10", entry_scope=None)
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
        assert any(edge["target_provider"] == "tmdb" for edge in items[0]["edges"])


@pytest.mark.asyncio
async def test_get_mapping_filters_by_descriptor() -> None:
    """Getting a mapping by descriptor returns the correct item."""
    service = MappingsService()
    with _fresh_tables():
        _seed_graph()
        item = await service.get_mapping("anilist:1")
        assert item["descriptor"] == "anilist:1"
        assert item["edges"][0]["target_provider"] == "tmdb"


@pytest.mark.asyncio
async def test_custom_only_filters_items() -> None:
    """Listing mappings with custom_only filters out non-custom items."""
    service = MappingsService()
    with _fresh_tables():
        _seed_graph()
        # Add upstream provenance to second mapping to mark non-custom
        with db() as ctx:
            tmdb_entry = ctx.session.query(AnimapEntry).filter_by(provider="tmdb").one()
            anilist_entry = (
                ctx.session.query(AnimapEntry).filter_by(provider="anilist").one()
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
            upstream_entry = AnimapEntry(provider="aaa", entry_id="1", entry_scope=None)
            upstream_target = AnimapEntry(
                provider="dest", entry_id="1", entry_scope=None
            )
            custom_entry = AnimapEntry(provider="zzz", entry_id="1", entry_scope=None)
            custom_target = AnimapEntry(provider="dest", entry_id="2", entry_scope=None)
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
            f"{custom_entry.provider}:{custom_entry.entry_id}"
        )


@pytest.mark.asyncio
async def test_filter_custom_entry_ids_batches() -> None:
    """Custom filtering handles large ID sets without exceeding SQL var limits."""
    service = MappingsService()
    with _fresh_tables():
        total_entries = 1200
        with db() as ctx:
            entries: list[AnimapEntry] = []
            for idx in range(total_entries):
                entry = AnimapEntry(
                    provider=f"p{idx:04d}", entry_id=str(idx), entry_scope=None
                )
                target = AnimapEntry(provider="t", entry_id=str(idx), entry_scope=None)
                ctx.session.add_all([entry, target])
                entries.append(entry)
            ctx.session.flush()

            mappings: list[AnimapMapping] = []
            for entry in entries:
                target = (
                    ctx.session.query(AnimapEntry)
                    .filter_by(provider="t", entry_id=entry.entry_id)
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
async def test_search_by_entry_id() -> None:
    """Booru query supports filtering by Animap entry ID."""
    service = MappingsService()
    with _fresh_tables():
        _seed_graph()
        with db() as ctx:
            entry_id = (
                ctx.session.query(AnimapEntry.id)
                .filter(AnimapEntry.provider == "anilist")
                .scalar()
            )

        items, total = await service.list_mappings(
            page=1, per_page=10, q=f"id:{entry_id}", custom_only=False
        )

        assert total == 1
        assert len(items) == 1
        assert items[0]["descriptor"].startswith("anilist:")


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


def test_collect_anilist_and_groups() -> None:
    """AniList key terms are grouped when directly AND-ed."""
    service = MappingsService()
    node = parse_query("anilist.id:1 anilist.format:TV source.provider:tmdb")
    groups = service._collect_anilist_and_groups(node)

    assert len(groups) == 1
    keys = {term.key for term in groups[0]}
    assert keys == {"anilist.id", "anilist.format"}


def test_filter_entry_and_edge_helpers() -> None:
    """Helper filters respect wildcards, nulls, and edge attributes."""
    service = MappingsService()
    with _fresh_tables(), db() as ctx:
        entry_a = AnimapEntry(provider="anilist", entry_id="abc", entry_scope=None)
        entry_b = AnimapEntry(provider="anilist", entry_id="xyz", entry_scope="s1")
        target = AnimapEntry(provider="tmdb", entry_id="10", entry_scope=None)
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

        wildcard_ids = service._filter_entry_column(ctx, AnimapEntry.entry_id, "a*")
        assert wildcard_ids == {entry_a.id}

        null_scope_ids = service._filter_entry_column(
            ctx, AnimapEntry.entry_scope, None
        )
        assert null_scope_ids == {entry_a.id, target.id}

        values_scope_ids = service._filter_entry_column(
            ctx, AnimapEntry.entry_scope, None, ("s1", None)
        )
        assert values_scope_ids == {entry_a.id, entry_b.id, target.id}

        edge_ids = service._filter_edge_target(ctx, "provider", "tmdb")
        assert edge_ids == {entry_a.id, entry_b.id}

        edge_null_scope = service._filter_edge_target(ctx, "entry_scope", None)
        assert edge_null_scope == {entry_a.id, entry_b.id}

        edge_range_ids = service._filter_edge_range(ctx, "destination_range", None)
        assert edge_range_ids == {entry_a.id}


def test_resolve_db_term_in_values_and_nulls() -> None:
    """DB term resolution handles IN lists and null literals."""
    service = MappingsService()
    with _fresh_tables(), db() as ctx:
        entry_a = AnimapEntry(provider="anilist", entry_id="1", entry_scope=None)
        entry_b = AnimapEntry(provider="tmdb", entry_id="2", entry_scope="s1")
        ctx.session.add_all([entry_a, entry_b])
        ctx.session.commit()

        in_term = KeyTerm(
            key="source.provider",
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
            provider="anilist",
            entry_id="1",
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
