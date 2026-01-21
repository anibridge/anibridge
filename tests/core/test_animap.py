"""Tests for the Animap client (descriptor graph)."""

import asyncio
import importlib
import json
from hashlib import md5
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql import select

from src.config.database import AniBridgeDB
from src.core.animap import AnimapClient, AnimapEdge
from src.core.mappings import MappingsClient
from src.models.db.animap import AnimapEntry, AnimapMapping, AnimapProvenance
from src.models.db.base import Base
from src.models.db.housekeeping import Housekeeping


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

    async def load_mappings(self) -> dict[str, Any]:
        """Return the predefined mappings."""
        self.load_calls += 1
        return self.mappings

    def get_provenance(self) -> dict[str, list[str]]:
        """Return the predefined provenance."""
        return self.provenance

    async def close(self) -> None:
        """No-op for closing the fake client."""
        return None


@pytest.fixture
def in_memory_db(monkeypatch: pytest.MonkeyPatch):
    """Provide an in-memory database patched into the application."""
    engine = create_engine("sqlite:///:memory:", future=True)

    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )

    class _DB:
        def __init__(self) -> None:
            self._session = None

        def __enter__(self):
            self._session = session_factory()
            return self

        def __exit__(self, exc_type, exc_val, exc_tb) -> None:
            if self._session is not None:
                self._session.close()
                self._session = None

        @property
        def session(self):
            if self._session is None:
                self._session = session_factory()
            return self._session

    db_instance = _DB()

    database_module = importlib.import_module("src.config.database")
    animap_module = importlib.import_module("src.core.animap")

    monkeypatch.setattr(database_module, "db", lambda: db_instance)
    monkeypatch.setattr(animap_module, "db", lambda: db_instance)

    try:
        yield db_instance
    finally:
        session = getattr(db_instance, "_session", None)
        if session is not None:
            session.close()
        engine.dispose()


@pytest.fixture
def animap_client(
    tmp_path: Path, in_memory_db: AniBridgeDB, request: pytest.FixtureRequest
) -> AnimapClient:
    """Provide an AnimapClient instance for testing."""
    client = AnimapClient(data_path=tmp_path, upstream_url=None)

    def _finalize() -> None:
        asyncio.run(client.close())

    request.addfinalizer(_finalize)
    return client


def _mapping_data():
    return {
        "anilist:1": {"tmdb:10": {"1": None}},
        "anilist:2:s1": {"tvdb:20:s1": {"1-12": "1-12"}},
    }


def _write_mapping_file(base: Path, data: dict) -> Path:
    mappings_path = base / "mappings.json"
    mappings_path.write_text(json.dumps(data), encoding="utf-8")
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
                source=(src.provider, src.entry_id, src.entry_scope),
                destination=(dst.provider, dst.entry_id, dst.entry_scope),
                source_range=mapping.source_range,
                destination_range=mapping.destination_range,
            )
        )
    return edges


def test_sync_db_creates_entries_mappings_and_provenance(
    animap_client: AnimapClient, tmp_path: Path, in_memory_db: AniBridgeDB
) -> None:
    """Syncing the database creates entries, mappings, and provenance rows."""
    mapping_data = _mapping_data()
    mappings_path = _write_mapping_file(tmp_path, mapping_data)

    asyncio.run(animap_client.sync_db())

    expected_hash = md5(json.dumps(mapping_data, sort_keys=True).encode()).hexdigest()

    with in_memory_db as ctx:
        entries = (
            ctx.session.execute(
                select(AnimapEntry).order_by(AnimapEntry.provider, AnimapEntry.entry_id)
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

    assert {(e.provider, e.entry_id, e.entry_scope) for e in entries} == {
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
        ("anilist", "1", None, "tmdb", "10", None, "1", None),
        ("anilist", "2", "s1", "tvdb", "20", "s1", "1-12", "1-12"),
    }

    expected_source = str(mappings_path.resolve())
    assert [row.source for row in provenance_rows] == [
        expected_source,
        expected_source,
    ]
    assert [row.n for row in provenance_rows] == [0, 0]


def test_get_graph_for_descriptors_returns_edges(
    animap_client: AnimapClient, tmp_path: Path, in_memory_db: AniBridgeDB
) -> None:
    """Mapping graph lookups honor scoped descriptors."""
    _write_mapping_file(tmp_path, _mapping_data())
    asyncio.run(animap_client.sync_db())

    edges = animap_client.get_graph_for_descriptors([("anilist", "1", None)]).edges
    assert len(edges) == 1
    assert edges[0].source[0] == "anilist"
    assert edges[0].destination[0] == "tmdb"

    tvdb_edges = animap_client.get_graph_for_descriptors([("tvdb", "20", "s1")]).edges
    assert {
        (
            e.source[0],
            e.destination[0],
            e.source_range,
            e.destination_range,
        )
        for e in tvdb_edges
    } == {("anilist", "tvdb", "1-12", "1-12")}


def test_sync_db_refreshes_provenance_when_hash_matches(
    animap_client: AnimapClient, in_memory_db: AniBridgeDB
) -> None:
    """Syncing the database again with the same mappings refreshes provenance."""
    base_mappings = {"anilist:1": {"tmdb:1": {"1": None}}}
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


def test_sync_db_skips_invalid_range_strings(
    animap_client: AnimapClient, tmp_path: Path, in_memory_db: AniBridgeDB
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
    assert edge_ranges == {
        ("1-6|2", "1-3|2,4-6|2"),
        ("2", "1,2"),
    }


def test_sync_db_logs_distinct_mapping_changes(
    animap_client: AnimapClient,
    in_memory_db: AniBridgeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync log summarizes distinct mapping changes by source/target."""
    initial_mappings = {
        "anilist:1": {"tmdb:10": {"1": None}},
        "anilist:2": {"tvdb:20": {"1-12": "1-12"}},
        "anilist:3": {"tmdb:30": {"1": None}},
    }
    updated_mappings = {
        "anilist:2": {"tvdb:20": {"1-13": "1-13"}},
        "anilist:3": {"tmdb:30": {"1": None}},
        "anilist:4": {"tmdb:40": {"1": None}},
    }

    fake_client = FakeMappingsClient(mappings=initial_mappings, provenance={})
    animap_client.mappings_client = cast(MappingsClient, fake_client)

    messages: list[str] = []

    def _capture_success(*args, **kwargs) -> None:
        if args:
            messages.append(str(args[0]))

    animap_module = importlib.import_module("src.core.animap")
    monkeypatch.setattr(animap_module.log, "success", _capture_success)

    asyncio.run(animap_client.sync_db())

    fake_client.mappings = updated_mappings
    asyncio.run(animap_client.sync_db())

    assert messages
    assert "1 removed" in messages[-1]
    assert "1 updated" in messages[-1]
    assert "1 created" in messages[-1]
