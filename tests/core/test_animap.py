"""Tests for the Animap client mapping sync."""

import asyncio
import copy
import json
from hashlib import md5
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.sql import select

from anibridge.app.config import database as database_module
from anibridge.app.config.database import AnibridgeDb
from anibridge.app.core import animap as animap_module
from anibridge.app.core.animap import AnimapClient, AnimapEdge
from anibridge.app.core.mappings import MappingsClient
from anibridge.app.models.db.animap import AnimapEntry, AnimapMapping, AnimapProvenance
from anibridge.app.models.db.housekeeping import Housekeeping


class FakeMappingsClient:
    """Lightweight stub for the mappings client used during tests."""

    def __init__(
        self,
        mappings: dict[str, Any],
        provenance: dict[str, list[str]],
    ) -> None:
        """Initialize the fake client with predefined mappings and provenance."""
        self.mappings = mappings
        self.provenance = provenance
        self.load_calls = 0
        self._content_hash: str = ""

    async def load_mappings(self) -> dict[str, Any]:
        """Return the predefined mappings."""
        self.load_calls += 1

        self._content_hash = md5(
            json.dumps(self.mappings, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return copy.deepcopy(self.mappings)

    def get_content_hash(self) -> str:
        """Return a hash of the mappings."""
        return self._content_hash

    def get_provenance(self) -> dict[str, list[str]]:
        """Return the predefined provenance."""
        return self.provenance

    async def close(self) -> None:
        """No-op for closing the fake client."""
        return None

    def clear_cache(self) -> None:
        """Clear cache."""
        self._content_hash = ""


@pytest.fixture
def in_memory_db(monkeypatch: pytest.MonkeyPatch, in_memory_db_factory):
    """Provide an in-memory database patched into the application."""
    return in_memory_db_factory(monkeypatch, database_module, animap_module)


@pytest.fixture
def animap_client(
    tmp_path: Path, in_memory_db: AnibridgeDb, request: pytest.FixtureRequest
) -> AnimapClient:
    """Provide an AnimapClient instance for testing."""
    client = AnimapClient(data_path=tmp_path, upstream_url=None)

    def _finalize() -> None:
        asyncio.run(client.close())

    request.addfinalizer(_finalize)
    return client


def _mapping_data():
    return {
        "anilist:1": {"tmdb:10": {"1": "1"}},
        "anilist:2:s1": {"tvdb:20:s1": {"1-12": "1-12"}},
    }


def _write_mapping_file(base: Path, data: dict) -> Path:
    mappings_path = base / "mappings.json"
    mappings_path.write_bytes(json.dumps(data, sort_keys=True).encode("utf-8"))
    return mappings_path


def _fetch_edges(ctx) -> list[AnimapEdge]:
    entries = {
        entry.id: entry for entry in ctx.session.execute(select(AnimapEntry)).scalars()
    }
    mappings = ctx.session.execute(select(AnimapMapping)).scalars().all()
    edges: list[AnimapEdge] = []
    for mapping in mappings:
        src = entries[mapping.source_entry_id]
        dst = entries[mapping.destination_entry_id]
        edges.append(
            AnimapEdge(
                source=(src.authority, src.value, src.scope),
                destination=(dst.authority, dst.value, dst.scope),
                source_range=mapping.source_range,
                destination_range=mapping.destination_range,
            )
        )
    return edges


def test_resolve_edges_and_grouping(
    animap_client: AnimapClient, tmp_path: Path, in_memory_db: AnibridgeDb
) -> None:
    """Resolved edges should honor target filters and grouping."""
    mapping_data = {
        "anilist:1": {
            "tmdb:10": {"1-12": None},
            "tvdb:20": {"1": "1-12"},
        }
    }
    _write_mapping_file(tmp_path, mapping_data)

    asyncio.run(animap_client.sync_db())

    descriptors = [("anilist", "1", None)]
    edges = animap_client.resolve_edges(descriptors)
    filtered = animap_client.resolve_edges(descriptors, target_authorities={"tmdb"})
    grouped = animap_client.resolve_edges_grouped(descriptors)

    assert len(edges) == 1
    assert len(filtered) == 0
    assert ("tmdb", "10", None) not in grouped
    assert ("tvdb", "20", None) in grouped
    assert ("anilist", "1", None) in grouped[("tvdb", "20", None)]


def test_select_entry_ids_returns_ids(
    animap_client: AnimapClient, tmp_path: Path, in_memory_db: AnibridgeDb
) -> None:
    """Entry lookup should return IDs for known descriptors."""
    mapping_data = {"anilist:1": {"tmdb:10": {"1": None}}}
    _write_mapping_file(tmp_path, mapping_data)
    asyncio.run(animap_client.sync_db())

    with in_memory_db as ctx:
        ids = AnimapClient._select_entry_ids(
            ctx.session,
            [("anilist", "1", None), ("missing", "2", None)],
        )

    assert len(ids) == 1


def test_build_edges_handles_invalid_payloads(
    animap_client: AnimapClient,
) -> None:
    """Invalid descriptors and ranges should be skipped during build."""
    mappings = {
        "bad": "value",
        "anilist:1": "not-a-dict",
        "anilist:2": {"bad-target": {"1": None}},
        "anilist:3": {"tmdb:3": {"": None, "1,2": "bad"}},
    }

    _descriptors, edges, provenance, invalid_count = animap_client._build_edges(
        mappings, {}
    )

    assert invalid_count >= 3
    assert edges == set()
    assert provenance == {}


def test_sync_db_creates_entries_mappings_and_provenance(
    animap_client: AnimapClient, tmp_path: Path, in_memory_db: AnibridgeDb
) -> None:
    """Syncing the database creates entries, mappings, and provenance rows."""
    mapping_data = _mapping_data()
    mappings_path = _write_mapping_file(tmp_path, mapping_data)

    asyncio.run(animap_client.sync_db())

    expected_hash = md5(
        json.dumps(mapping_data, sort_keys=True).encode("utf-8")
    ).hexdigest()

    with in_memory_db as ctx:
        entries = (
            ctx.session.execute(
                select(AnimapEntry).order_by(AnimapEntry.authority, AnimapEntry.value)
            )
            .scalars()
            .all()
        )
        provenance_rows = (
            ctx.session.execute(
                select(AnimapProvenance).order_by(
                    AnimapProvenance.mapping_id, AnimapProvenance.n
                )
            )
            .scalars()
            .all()
        )
        hash_entry = ctx.session.get(Housekeeping, "animap_mappings_hash")
        edges = _fetch_edges(ctx)

    assert hash_entry is not None
    assert hash_entry.value == expected_hash

    assert {(e.authority, e.value, e.scope) for e in entries} == {
        ("anilist", "1", None),
        ("tmdb", "10", None),
        ("anilist", "2", "s1"),
        ("tvdb", "20", "s1"),
    }

    edge_keys = {
        (
            edge.source[0],
            edge.source[1],
            edge.source[2],
            edge.destination[0],
            edge.destination[1],
            edge.destination[2],
            edge.source_range,
            edge.destination_range,
        )
        for edge in edges
    }
    assert edge_keys == {
        ("anilist", "1", None, "tmdb", "10", None, "1", "1"),
        ("anilist", "2", "s1", "tvdb", "20", "s1", "1-12", "1-12"),
    }

    expected_source = str(mappings_path.resolve())
    assert [row.source for row in provenance_rows] == [
        expected_source,
        expected_source,
    ]
    assert [row.n for row in provenance_rows] == [0, 0]


def test_sync_db_refreshes_provenance_when_hash_matches(
    animap_client: AnimapClient, in_memory_db: AnibridgeDb
) -> None:
    """Syncing the database again with the same mappings refreshes provenance."""
    base_mappings = {"anilist:1": {"tmdb:1": {"1": "1"}}}
    fake_client = FakeMappingsClient(
        mappings=base_mappings,
        provenance={"anilist:1": ["/initial.json"]},
    )
    animap_client.mappings_client = cast(MappingsClient, fake_client)
    asyncio.run(animap_client.sync_db())

    fake_client.provenance = {"anilist:1": ["/updated.json", "/extra.json"]}
    asyncio.run(animap_client.sync_db())

    with in_memory_db as ctx:
        provenance_rows = (
            ctx.session.execute(
                select(AnimapProvenance).order_by(
                    AnimapProvenance.mapping_id, AnimapProvenance.n
                )
            )
            .scalars()
            .all()
        )
        housekeeping = ctx.session.get(Housekeeping, "animap_mappings_hash")

    assert housekeeping is not None
    assert [row.source for row in provenance_rows] == [
        "/updated.json",
        "/extra.json",
    ]
    assert [row.n for row in provenance_rows] == [0, 1]


def test_sync_db_skips_work_when_hashes_match(
    animap_client: AnimapClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged mappings/provenance should skip expensive rebuild checks."""
    base_mappings = {"anilist:1": {"tmdb:1": {"1": "1"}}}
    fake_client = FakeMappingsClient(
        mappings=base_mappings,
        provenance={"anilist:1": ["/same.json"]},
    )
    animap_client.mappings_client = cast(MappingsClient, fake_client)
    asyncio.run(animap_client.sync_db())

    def _should_not_build(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("_build_edges should not run when hashes match")

    monkeypatch.setattr(animap_client, "_build_edges", _should_not_build)

    asyncio.run(animap_client.sync_db())


def test_sync_db_skips_invalid_range_strings(
    animap_client: AnimapClient, tmp_path: Path, in_memory_db: AnibridgeDb
) -> None:
    """Invalid source/destination ranges are ignored during sync."""
    mapping_data = {
        "anilist:1": {
            "tmdb:10": {
                "1,2": "1-2",
                "1-6|2": "1-3|2,4-6|2",
                "2": "1,2",
                "3": "1,,2",
            }
        }
    }
    _write_mapping_file(tmp_path, mapping_data)

    asyncio.run(animap_client.sync_db())

    with in_memory_db as ctx:
        edges = _fetch_edges(ctx)

    edge_ranges = {(edge.source_range, edge.destination_range) for edge in edges}
    assert edge_ranges == {("2", "1,2")}


def test_build_edges_skips_null_destination_ranges(
    animap_client: AnimapClient,
) -> None:
    """Null destination ranges should behave as deleted/non-existent mappings."""
    mappings = {
        "anilist:1": {
            "tmdb:10": {
                "1-12": None,
            }
        }
    }

    _descriptors, edges, provenance, invalid_count = animap_client._build_edges(
        mappings,
        {"anilist:1": ["/tmp/mappings.json"]},
    )

    assert invalid_count == 0
    assert edges == set()
    assert provenance == {}
