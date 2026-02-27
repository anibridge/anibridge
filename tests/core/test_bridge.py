"""Tests for the BridgeClient orchestration logic."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import sessionmaker

import anibridge.app.core.bridge as bridge_module
from anibridge.app.core.bridge import BridgeClient
from anibridge.app.core.sync.stats import ItemIdentifier, SyncStats
from anibridge.app.exceptions import MediaTypeError
from anibridge.app.models.db.base import Base
from anibridge.app.models.db.sync_history import SyncOutcome


@dataclass
class FakeUser:
    """Simple user stub with a title."""

    title: str


@dataclass
class FakeSection:
    """Simple library section stub."""

    title: str
    media_kind: Any


@dataclass
class FakeMedia:
    """Simple media stub with kind and title."""

    title: str
    media_kind: Any


class FakeLibraryProvider:
    """Library provider stub that returns preconfigured sections/items."""

    NAMESPACE = "fake-library"

    def __init__(
        self,
        sections: list[FakeSection],
        items_by_section: dict[str, list[FakeMedia]],
        webhook_result: tuple[bool, list[str] | None] | None = None,
    ) -> None:
        self._sections = sections
        self._items_by_section = items_by_section
        self._webhook_result = webhook_result or (True, ["item-1"])
        self.list_calls: list[dict[str, Any]] = []
        self.initialized = False
        self.closed = False

    async def initialize(self) -> None:
        self.initialized = True

    async def close(self) -> None:
        self.closed = True

    def user(self) -> FakeUser:
        return FakeUser("LibraryUser")

    async def get_sections(self) -> list[FakeSection]:
        return list(self._sections)

    async def list_items(
        self,
        section: FakeSection,
        *,
        min_last_modified: datetime | None = None,
        require_watched: bool = False,
        keys: list[str] | None = None,
    ) -> list[FakeMedia]:
        self.list_calls.append(
            {
                "section": section,
                "min_last_modified": min_last_modified,
                "require_watched": require_watched,
                "keys": keys,
            }
        )
        return list(self._items_by_section.get(section.title, []))

    async def parse_webhook(self, _request) -> tuple[bool, list[str] | None]:
        return self._webhook_result


class FakeListProvider:
    """List provider stub for backup and cache interactions."""

    NAMESPACE = "fake-list"

    def __init__(self, backup_payload: str | Exception | None = None) -> None:
        self._backup_payload = backup_payload
        self.cleared = False
        self.closed = False
        self.initialized = False

    async def initialize(self) -> None:
        self.initialized = True

    async def close(self) -> None:
        self.closed = True

    def user(self) -> FakeUser:
        return FakeUser("ListUser")

    async def backup_list(self) -> str:
        if isinstance(self._backup_payload, Exception):
            raise self._backup_payload
        return cast(str, self._backup_payload)

    async def clear_cache(self) -> None:
        self.cleared = True


class FakeSyncClient:
    """Sync client stub tracking calls and stats."""

    def __init__(self, *, outcome: SyncOutcome = SyncOutcome.SYNCED) -> None:
        self.prefetched: list[list[FakeMedia]] = []
        self.processed: list[FakeMedia] = []
        self.batch_called = False
        self.flush_called = False
        self.outcome = outcome
        self.sync_stats = SyncStats()

    async def clear_cache(self) -> None:
        return None

    async def prefetch_entries(self, items: list[FakeMedia]) -> None:
        self.prefetched.append(list(items))

    async def process_media(self, item: FakeMedia) -> None:
        if item.media_kind not in {
            bridge_module.MediaKind.MOVIE,
            bridge_module.MediaKind.SHOW,
        }:
            raise MediaTypeError("Unsupported media")
        self.processed.append(item)
        identifier = ItemIdentifier(
            key=item.title,
            media_kind=bridge_module.MediaKind.MOVIE,
            repr=item.title,
        )
        self.sync_stats.track_item(identifier, self.outcome)

    async def batch_sync(self) -> None:
        self.batch_called = True

    def flush_failure_history_cleanup(self) -> None:
        self.flush_called = True


@pytest.fixture
def in_memory_db(monkeypatch: pytest.MonkeyPatch):
    """Provide an in-memory database patched into the bridge module."""
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

    monkeypatch.setattr(bridge_module, "db", lambda: db_instance)

    try:
        yield db_instance
    finally:
        session = getattr(db_instance, "_session", None)
        if session is not None:
            session.close()
        engine.dispose()


def _make_profile_config(**overrides: Any) -> SimpleNamespace:
    defaults = {
        "library_provider": "fake",
        "list_provider": "fake",
        "library_provider_config": {},
        "list_provider_config": {},
        "sync_fields": {},
        "full_scan": False,
        "empty_sync": False,
        "destructive_sync": False,
        "search_fallback_threshold": -1,
        "batch_requests": False,
        "backup_retention_days": -1,
        "dry_run": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_global_config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(data_path=tmp_path)


def test_last_synced_round_trip(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Persisted last synced timestamps should be restored."""
    movie_section = FakeSection("Movies", bridge_module.MediaKind.MOVIE)
    provider = FakeLibraryProvider([movie_section], {"Movies": []})
    list_provider = FakeListProvider(backup_payload="")

    monkeypatch.setattr(bridge_module, "build_library_provider", lambda _: provider)
    monkeypatch.setattr(bridge_module, "build_list_provider", lambda _: list_provider)

    profile_config = _make_profile_config()
    client = BridgeClient(
        profile_name="default",
        profile_config=cast("bridge_module.AniBridgeProfileConfig", profile_config),
        global_config=cast(
            "bridge_module.AniBridgeConfig", _make_global_config(tmp_path)
        ),
        shared_animap_client=cast(Any, object()),
    )

    assert client.last_synced is None

    stamped = datetime(2025, 1, 1, tzinfo=UTC)
    client._set_last_synced(stamped)

    fresh = BridgeClient(
        profile_name="default",
        profile_config=cast("bridge_module.AniBridgeProfileConfig", profile_config),
        global_config=cast(
            "bridge_module.AniBridgeConfig", _make_global_config(tmp_path)
        ),
        shared_animap_client=cast(Any, object()),
    )

    assert fresh.last_synced == stamped


def test_backup_list_skips_when_not_supported(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """NotImplemented backups should be ignored."""
    movie_section = FakeSection("Movies", bridge_module.MediaKind.MOVIE)
    provider = FakeLibraryProvider([movie_section], {"Movies": []})
    list_provider = FakeListProvider(backup_payload=NotImplementedError())

    monkeypatch.setattr(bridge_module, "build_library_provider", lambda _: provider)
    monkeypatch.setattr(bridge_module, "build_list_provider", lambda _: list_provider)

    client = BridgeClient(
        profile_name="default",
        profile_config=cast(
            "bridge_module.AniBridgeProfileConfig", _make_profile_config()
        ),
        global_config=cast(
            "bridge_module.AniBridgeConfig", _make_global_config(tmp_path)
        ),
        shared_animap_client=cast(Any, object()),
    )

    asyncio.run(client._backup_list())

    backup_root = tmp_path / "backups"
    assert not backup_root.exists()


def test_backup_list_writes_payload(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Successful backups should write to disk."""
    provider = FakeLibraryProvider([], {})
    list_provider = FakeListProvider(backup_payload="{}")

    monkeypatch.setattr(bridge_module, "build_library_provider", lambda _: provider)
    monkeypatch.setattr(bridge_module, "build_list_provider", lambda _: list_provider)

    # Default config disables backups, so override to enable for this test
    config = _make_profile_config()
    config.backup_retention_days = 0

    client = BridgeClient(
        profile_name="default",
        profile_config=cast("bridge_module.AniBridgeProfileConfig", config),
        global_config=cast(
            "bridge_module.AniBridgeConfig", _make_global_config(tmp_path)
        ),
        shared_animap_client=cast(Any, object()),
    )

    asyncio.run(client._backup_list())

    backup_root = tmp_path / "backups" / "default"
    assert backup_root.exists()
    assert any(path.suffix == ".json" for path in backup_root.iterdir())


@pytest.mark.asyncio
async def test_parse_webhook_delegates(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Webhook parsing should delegate to the library provider."""
    provider = FakeLibraryProvider([], {}, webhook_result=(False, None))
    list_provider = FakeListProvider(backup_payload="")

    monkeypatch.setattr(bridge_module, "build_library_provider", lambda _: provider)
    monkeypatch.setattr(bridge_module, "build_list_provider", lambda _: list_provider)

    client = BridgeClient(
        profile_name="default",
        profile_config=cast(
            "bridge_module.AniBridgeProfileConfig", _make_profile_config()
        ),
        global_config=cast(
            "bridge_module.AniBridgeConfig", _make_global_config(tmp_path)
        ),
        shared_animap_client=cast(Any, object()),
    )

    assert (
        await client.parse_webhook(cast("bridge_module.Request", SimpleNamespace()))
    ) == (False, None)


@pytest.mark.asyncio
async def test_sync_section_batches_and_handles_errors(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Batch mode should prefetch, process, and finalize items."""
    movie_section = FakeSection("Movies", bridge_module.MediaKind.MOVIE)
    provider = FakeLibraryProvider(
        sections=[movie_section],
        items_by_section={
            "Movies": [
                FakeMedia("Movie", bridge_module.MediaKind.MOVIE),
                FakeMedia("Season", bridge_module.MediaKind.SEASON),
            ]
        },
    )
    list_provider = FakeListProvider(backup_payload="")

    monkeypatch.setattr(bridge_module, "build_library_provider", lambda _: provider)
    monkeypatch.setattr(bridge_module, "build_list_provider", lambda _: list_provider)

    movie_sync = FakeSyncClient(outcome=SyncOutcome.SYNCED)
    show_sync = FakeSyncClient(outcome=SyncOutcome.SYNCED)

    monkeypatch.setattr(bridge_module, "MovieSyncClient", lambda **_: movie_sync)
    monkeypatch.setattr(bridge_module, "ShowSyncClient", lambda **_: show_sync)

    client = BridgeClient(
        profile_name="default",
        profile_config=cast(
            "bridge_module.AniBridgeProfileConfig",
            _make_profile_config(batch_requests=True),
        ),
        global_config=cast(
            "bridge_module.AniBridgeConfig", _make_global_config(tmp_path)
        ),
        shared_animap_client=cast(Any, object()),
    )

    stats = await client._sync_section(
        cast("bridge_module.LibrarySection", movie_section),
        poll=True,
        movie_sync=cast("bridge_module.MovieSyncClient", movie_sync),
        show_sync=cast("bridge_module.ShowSyncClient", show_sync),
        keys=None,
        section_index=1,
        section_count=1,
    )

    assert stats is movie_sync.sync_stats
    assert movie_sync.prefetched
    assert movie_sync.batch_called is True
    assert movie_sync.flush_called is True
    assert provider.list_calls[0]["min_last_modified"] is not None


@pytest.mark.asyncio
async def test_sync_section_skips_unsupported_section(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unsupported section kinds should return empty stats."""
    section = FakeSection("Other", bridge_module.MediaKind.SEASON)
    provider = FakeLibraryProvider(sections=[section], items_by_section={})
    list_provider = FakeListProvider(backup_payload="")

    monkeypatch.setattr(bridge_module, "build_library_provider", lambda _: provider)
    monkeypatch.setattr(bridge_module, "build_list_provider", lambda _: list_provider)

    client = BridgeClient(
        profile_name="default",
        profile_config=cast(
            "bridge_module.AniBridgeProfileConfig", _make_profile_config()
        ),
        global_config=cast(
            "bridge_module.AniBridgeConfig", _make_global_config(tmp_path)
        ),
        shared_animap_client=cast(Any, object()),
    )

    stats = await client._sync_section(
        cast("bridge_module.LibrarySection", section),
        poll=False,
        movie_sync=cast("bridge_module.MovieSyncClient", FakeSyncClient()),
        show_sync=cast("bridge_module.ShowSyncClient", FakeSyncClient()),
        keys=None,
        section_index=1,
        section_count=1,
    )

    assert stats.total_items == 0


@pytest.mark.asyncio
async def test_initialize_and_close_calls_providers(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Initialize should call provider hooks and close should tear down."""
    provider = FakeLibraryProvider([], {})
    list_provider = FakeListProvider(backup_payload="")

    monkeypatch.setattr(bridge_module, "build_library_provider", lambda _: provider)
    monkeypatch.setattr(bridge_module, "build_list_provider", lambda _: list_provider)

    client = BridgeClient(
        profile_name="default",
        profile_config=cast(
            "bridge_module.AniBridgeProfileConfig", _make_profile_config()
        ),
        global_config=cast(
            "bridge_module.AniBridgeConfig", _make_global_config(tmp_path)
        ),
        shared_animap_client=cast(Any, object()),
    )

    await client.initialize()

    assert provider.initialized is True
    assert list_provider.initialized is True

    await client.close()

    assert provider.closed is True
    assert list_provider.closed is True


def test_backup_list_skips_empty_payload(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty backups should not create files."""
    provider = FakeLibraryProvider([], {})
    list_provider = FakeListProvider(backup_payload="")

    monkeypatch.setattr(bridge_module, "build_library_provider", lambda _: provider)
    monkeypatch.setattr(bridge_module, "build_list_provider", lambda _: list_provider)

    client = BridgeClient(
        profile_name="default",
        profile_config=cast(
            "bridge_module.AniBridgeProfileConfig", _make_profile_config()
        ),
        global_config=cast(
            "bridge_module.AniBridgeConfig", _make_global_config(tmp_path)
        ),
        shared_animap_client=cast(Any, object()),
    )

    asyncio.run(client._backup_list())

    assert not (tmp_path / "backups").exists()


def test_backup_list_handles_provider_error(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Provider errors should be handled without raising."""
    provider = FakeLibraryProvider([], {})
    list_provider = FakeListProvider(backup_payload=RuntimeError("boom"))

    monkeypatch.setattr(bridge_module, "build_library_provider", lambda _: provider)
    monkeypatch.setattr(bridge_module, "build_list_provider", lambda _: list_provider)

    client = BridgeClient(
        profile_name="default",
        profile_config=cast(
            "bridge_module.AniBridgeProfileConfig", _make_profile_config()
        ),
        global_config=cast(
            "bridge_module.AniBridgeConfig", _make_global_config(tmp_path)
        ),
        shared_animap_client=cast(Any, object()),
    )

    asyncio.run(client._backup_list())

    assert not (tmp_path / "backups").exists()


def test_backup_list_handles_write_errors(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Write errors should be logged and ignored."""
    provider = FakeLibraryProvider([], {})
    list_provider = FakeListProvider(backup_payload="{}")

    monkeypatch.setattr(bridge_module, "build_library_provider", lambda _: provider)
    monkeypatch.setattr(bridge_module, "build_list_provider", lambda _: list_provider)

    def _raise_write(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(Path, "write_text", _raise_write)

    client = BridgeClient(
        profile_name="default",
        profile_config=cast(
            "bridge_module.AniBridgeProfileConfig", _make_profile_config()
        ),
        global_config=cast(
            "bridge_module.AniBridgeConfig", _make_global_config(tmp_path)
        ),
        shared_animap_client=cast(Any, object()),
    )

    asyncio.run(client._backup_list())


@pytest.mark.asyncio
async def test_sync_section_handles_show_items(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Show sections should use the show sync client."""
    show_section = FakeSection("Shows", bridge_module.MediaKind.SHOW)
    provider = FakeLibraryProvider(
        sections=[show_section],
        items_by_section={"Shows": [FakeMedia("Show", bridge_module.MediaKind.SHOW)]},
    )
    list_provider = FakeListProvider(backup_payload="")

    monkeypatch.setattr(bridge_module, "build_library_provider", lambda _: provider)
    monkeypatch.setattr(bridge_module, "build_list_provider", lambda _: list_provider)

    client = BridgeClient(
        profile_name="default",
        profile_config=cast(
            "bridge_module.AniBridgeProfileConfig", _make_profile_config()
        ),
        global_config=cast(
            "bridge_module.AniBridgeConfig", _make_global_config(tmp_path)
        ),
        shared_animap_client=cast(Any, object()),
    )

    movie_sync = FakeSyncClient()
    show_sync = FakeSyncClient()

    stats = await client._sync_section(
        cast("bridge_module.LibrarySection", show_section),
        poll=False,
        movie_sync=cast("bridge_module.MovieSyncClient", movie_sync),
        show_sync=cast("bridge_module.ShowSyncClient", show_sync),
        keys=None,
        section_index=1,
        section_count=1,
    )

    assert stats is show_sync.sync_stats
    assert show_sync.processed
    assert not movie_sync.processed


@pytest.mark.asyncio
async def test_sync_section_handles_item_errors(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Errors during item sync should be swallowed and continue."""
    movie_section = FakeSection("Movies", bridge_module.MediaKind.MOVIE)
    provider = FakeLibraryProvider(
        sections=[movie_section],
        items_by_section={"Movies": [FakeMedia("Bad", bridge_module.MediaKind.SEASON)]},
    )
    list_provider = FakeListProvider(backup_payload="")

    monkeypatch.setattr(bridge_module, "build_library_provider", lambda _: provider)
    monkeypatch.setattr(bridge_module, "build_list_provider", lambda _: list_provider)

    client = BridgeClient(
        profile_name="default",
        profile_config=cast(
            "bridge_module.AniBridgeProfileConfig", _make_profile_config()
        ),
        global_config=cast(
            "bridge_module.AniBridgeConfig", _make_global_config(tmp_path)
        ),
        shared_animap_client=cast(Any, object()),
    )

    movie_sync = FakeSyncClient()

    stats = await client._sync_section(
        cast("bridge_module.LibrarySection", movie_section),
        poll=False,
        movie_sync=cast("bridge_module.MovieSyncClient", movie_sync),
        show_sync=cast("bridge_module.ShowSyncClient", FakeSyncClient()),
        keys=None,
        section_index=1,
        section_count=1,
    )

    assert stats is movie_sync.sync_stats
    assert not movie_sync.processed


@pytest.mark.asyncio
async def test_sync_updates_last_synced_and_progress(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Successful syncs should update last_synced and progress state."""
    movie_section = FakeSection("Movies", bridge_module.MediaKind.MOVIE)
    provider = FakeLibraryProvider(
        sections=[movie_section],
        items_by_section={
            "Movies": [FakeMedia("Movie", bridge_module.MediaKind.MOVIE)]
        },
    )
    list_provider = FakeListProvider(backup_payload="")

    monkeypatch.setattr(bridge_module, "build_library_provider", lambda _: provider)
    monkeypatch.setattr(bridge_module, "build_list_provider", lambda _: list_provider)

    movie_sync = FakeSyncClient(outcome=SyncOutcome.NOT_FOUND)
    show_sync = FakeSyncClient(outcome=SyncOutcome.NOT_FOUND)

    monkeypatch.setattr(bridge_module, "MovieSyncClient", lambda **_: movie_sync)
    monkeypatch.setattr(bridge_module, "ShowSyncClient", lambda **_: show_sync)

    client = BridgeClient(
        profile_name="default",
        profile_config=cast(
            "bridge_module.AniBridgeProfileConfig", _make_profile_config()
        ),
        global_config=cast(
            "bridge_module.AniBridgeConfig", _make_global_config(tmp_path)
        ),
        shared_animap_client=cast(Any, object()),
    )

    await client.sync()

    assert client.last_synced is not None
    assert client.current_sync is not None
    assert client.current_sync.state == "idle"
    assert client.current_sync.stage == "completed"


@pytest.mark.asyncio
async def test_sync_failure_sets_completed_state(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Failures should still mark the sync progress as completed."""
    movie_section = FakeSection("Movies", bridge_module.MediaKind.MOVIE)
    provider = FakeLibraryProvider([movie_section], {"Movies": []})
    list_provider = FakeListProvider(backup_payload="")

    monkeypatch.setattr(bridge_module, "build_library_provider", lambda _: provider)
    monkeypatch.setattr(bridge_module, "build_list_provider", lambda _: list_provider)

    client = BridgeClient(
        profile_name="default",
        profile_config=cast(
            "bridge_module.AniBridgeProfileConfig", _make_profile_config()
        ),
        global_config=cast(
            "bridge_module.AniBridgeConfig", _make_global_config(tmp_path)
        ),
        shared_animap_client=cast(Any, object()),
    )

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(client, "_sync_section", _boom)

    with pytest.raises(RuntimeError):
        await client.sync()

    assert client.current_sync is not None
    assert client.current_sync.stage == "completed"
    assert client.current_sync.state == "idle"
