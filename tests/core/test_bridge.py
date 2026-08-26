"""Tests for the BridgeClient orchestration logic."""

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from litestar.connection.request import Request

import anibridge.app.core.bridge as bridge_module
from anibridge.app.config.settings import SyncRulesConfig
from anibridge.app.core.bridge import BridgeClient
from anibridge.app.core.sync.stats import ItemIdentifier, SyncStats
from anibridge.app.exceptions import MediaTypeError
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

    @property
    def key(self) -> str:
        """Return a stable media identifier."""
        return self.title


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


class FailingSyncClient(FakeSyncClient):
    """Sync client variant that can fail during prefetch."""

    def __init__(self, *, prefetch_error: Exception | None = None) -> None:
        super().__init__()
        self._prefetch_error = prefetch_error

    async def prefetch_entries(self, items: list[FakeMedia]) -> None:
        if self._prefetch_error is not None:
            raise self._prefetch_error
        await super().prefetch_entries(items)


class ItemFailingSyncClient(FakeSyncClient):
    """Sync client variant that fails while processing an item."""

    async def process_media(self, item: FakeMedia) -> None:
        raise RuntimeError(f"failed to process {item.title}")


@pytest.fixture
def in_memory_db(monkeypatch: pytest.MonkeyPatch, in_memory_db_factory):
    """Provide an in-memory database patched into the bridge module."""
    return in_memory_db_factory(monkeypatch, bridge_module)


def _make_bridge_client(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: FakeLibraryProvider,
    list_provider: FakeListProvider,
    shared_animap_client: Any | None = None,
    **profile_overrides: Any,
) -> BridgeClient:
    """Construct a BridgeClient with patched fake providers."""
    monkeypatch.setattr(bridge_module, "build_library_provider", lambda _: provider)
    monkeypatch.setattr(bridge_module, "build_list_provider", lambda _: list_provider)

    profile_config_values = {
        "library_provider": "fake",
        "list_provider": "fake",
        "library_provider_config": {},
        "list_provider_config": {},
        "poll_interval": 60,
        "sync_rules": SyncRulesConfig(),
        "full_scan": False,
        "empty_sync": False,
        "destructive_sync": False,
        "search_fallback_threshold": -1,
        "batch_requests": False,
        "backup_retention_days": -1,
        "dry_run": False,
    }
    profile_config_values.update(profile_overrides)
    profile_config = SimpleNamespace(**profile_config_values)
    return BridgeClient(
        profile_name="default",
        profile_config=cast("bridge_module.AnibridgeProfileConfig", profile_config),
        global_config=cast(
            "bridge_module.AnibridgeConfig", SimpleNamespace(data_path=tmp_path)
        ),
        shared_animap_client=cast(
            Any, object() if shared_animap_client is None else shared_animap_client
        ),
    )


def test_last_synced_round_trip(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Persisted last synced timestamps should be restored."""
    movie_section = FakeSection("Movies", bridge_module.MediaKind.MOVIE)
    provider = FakeLibraryProvider([movie_section], {"Movies": []})
    list_provider = FakeListProvider(backup_payload="")
    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
    )

    assert client.last_synced is None

    stamped = datetime(2025, 1, 1, tzinfo=UTC)
    client._set_last_synced(stamped)

    fresh = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
    )

    assert fresh.last_synced == stamped


def test_backup_list_skips_when_not_supported(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """NotImplemented backups should be ignored."""
    movie_section = FakeSection("Movies", bridge_module.MediaKind.MOVIE)
    provider = FakeLibraryProvider([movie_section], {"Movies": []})
    list_provider = FakeListProvider(backup_payload=NotImplementedError())
    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
        backup_retention_days=7,
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
    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
        backup_retention_days=0,
    )

    asyncio.run(client._backup_list())

    backup_root = tmp_path / "backups" / "default"
    assert backup_root.exists()
    assert any(path.suffix == ".json" for path in backup_root.iterdir())


def test_backup_list_prunes_expired_backups(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expired backups should be deleted when retention is enabled."""
    provider = FakeLibraryProvider([], {})
    list_provider = FakeListProvider(backup_payload="{}")
    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
        backup_retention_days=7,
    )

    backup_root = tmp_path / "backups" / "default"
    backup_root.mkdir(parents=True)

    stale = backup_root / "anibridge_default_fake-list_20240101010101.json"
    fresh = backup_root / "anibridge_default_fake-list_20260226010101.json"
    other_provider = backup_root / "anibridge_default_other_20240101010101.json"

    stale.write_text("{}", encoding="utf-8")
    fresh.write_text("{}", encoding="utf-8")
    other_provider.write_text("{}", encoding="utf-8")

    stale_ts = (datetime.now(UTC) - timedelta(days=30)).timestamp()
    fresh_ts = datetime.now(UTC).timestamp()
    os.utime(stale, (stale_ts, stale_ts))
    os.utime(fresh, (fresh_ts, fresh_ts))

    asyncio.run(client._backup_list())

    assert not stale.exists()
    assert fresh.exists()
    assert other_provider.exists()


@pytest.mark.asyncio
async def test_parse_webhook_delegates(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Webhook parsing should delegate to the library provider."""
    provider = FakeLibraryProvider([], {}, webhook_result=(False, None))
    list_provider = FakeListProvider(backup_payload="")
    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
    )

    assert (await client.parse_webhook(cast(Request, SimpleNamespace()))) == (
        False,
        None,
    )


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

    movie_sync = FakeSyncClient(outcome=SyncOutcome.SYNCED)
    show_sync = FakeSyncClient(outcome=SyncOutcome.SYNCED)

    monkeypatch.setattr(bridge_module, "MovieSyncClient", lambda **_: movie_sync)
    monkeypatch.setattr(bridge_module, "ShowSyncClient", lambda **_: show_sync)

    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
        batch_requests=True,
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
async def test_sync_section_first_poll_defaults_to_one_poll_interval_ago(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """First poll syncs should start from one poll interval ago."""
    movie_section = FakeSection("Movies", bridge_module.MediaKind.MOVIE)
    provider = FakeLibraryProvider(
        sections=[movie_section],
        items_by_section={"Movies": []},
    )
    list_provider = FakeListProvider(backup_payload="")

    fixed_now = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(bridge_module, "datetime", FrozenDateTime)

    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
        poll_interval=60,
    )

    await client._sync_section(
        cast("bridge_module.LibrarySection", movie_section),
        poll=True,
        movie_sync=cast("bridge_module.MovieSyncClient", FakeSyncClient()),
        show_sync=cast("bridge_module.ShowSyncClient", FakeSyncClient()),
        keys=None,
        section_index=1,
        section_count=1,
    )

    assert provider.list_calls[0]["min_last_modified"] == fixed_now - timedelta(
        seconds=75
    )


@pytest.mark.asyncio
async def test_sync_section_skips_unsupported_section(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unsupported section kinds should return empty stats."""
    section = FakeSection("Other", bridge_module.MediaKind.SEASON)
    provider = FakeLibraryProvider(sections=[section], items_by_section={})
    list_provider = FakeListProvider(backup_payload="")
    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
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
    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
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
    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
    )

    asyncio.run(client._backup_list())

    assert not (tmp_path / "backups").exists()


def test_backup_list_handles_provider_error(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Provider errors should be handled without raising."""
    provider = FakeLibraryProvider([], {})
    list_provider = FakeListProvider(backup_payload=RuntimeError("boom"))
    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
        backup_retention_days=7,
    )

    asyncio.run(client._backup_list())

    assert not (tmp_path / "backups").exists()


def test_backup_list_handles_write_errors(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Write errors should be logged and ignored."""
    provider = FakeLibraryProvider([], {})
    list_provider = FakeListProvider(backup_payload="{}")

    def _raise_write(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(Path, "write_text", _raise_write)
    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
        backup_retention_days=7,
    )

    asyncio.run(client._backup_list())


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_provider", ["library", "list"])
async def test_initialize_surfaces_provider_initialization_errors(
    failing_provider: str,
    in_memory_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Initialization failures should be re-raised for the failing provider."""

    class BrokenLibraryProvider(FakeLibraryProvider):
        async def initialize(self) -> None:
            if failing_provider == "library":
                raise RuntimeError("library boom")
            await super().initialize()

    class BrokenListProvider(FakeListProvider):
        async def initialize(self) -> None:
            if failing_provider == "list":
                raise RuntimeError("list boom")
            await super().initialize()

    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=BrokenLibraryProvider([], {}),
        list_provider=BrokenListProvider(backup_payload=""),
        backup_retention_days=7,
    )

    with pytest.raises(RuntimeError):
        await client.initialize()


@pytest.mark.asyncio
async def test_close_swallows_provider_close_errors(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Close errors should be logged and swallowed for both providers."""

    class BrokenLibraryProvider(FakeLibraryProvider):
        async def close(self) -> None:
            self.closed = True
            raise RuntimeError("library close")

    class BrokenListProvider(FakeListProvider):
        async def close(self) -> None:
            self.closed = True
            raise RuntimeError("list close")

    provider = BrokenLibraryProvider([], {})
    list_provider = BrokenListProvider(backup_payload="")
    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
    )

    await client.close()

    assert provider.closed is True
    assert list_provider.closed is True


@pytest.mark.asyncio
async def test_async_context_manager_closes_client(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exiting the async context manager should close the providers."""
    provider = FakeLibraryProvider([], {})
    list_provider = FakeListProvider(backup_payload="")
    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
    )

    async with client as entered:
        assert entered is client

    assert provider.closed is True
    assert list_provider.closed is True


def test_cleanup_old_backups_handles_missing_roots_and_oserrors(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Backup cleanup should ignore missing paths and unlink/stat failures."""
    provider = FakeLibraryProvider([], {})
    list_provider = FakeListProvider(backup_payload="{}")
    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
        backup_retention_days=7,
    )

    client._cleanup_old_backups(tmp_path / "missing")

    stale = tmp_path / "backups" / "default" / "anibridge_default_fake-list_old.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}", encoding="utf-8")
    stale_ts = (datetime.now(UTC) - timedelta(days=30)).timestamp()
    os.utime(stale, (stale_ts, stale_ts))

    broken_stat = (
        tmp_path / "backups" / "default" / "anibridge_default_fake-list_bad.json"
    )
    broken_stat.write_text("{}", encoding="utf-8")

    real_stat = Path.stat
    real_unlink = Path.unlink

    def fake_stat(path: Path):
        if path == broken_stat:
            raise OSError("stat failed")
        return real_stat(path)

    def fake_unlink(path: Path, *args, **kwargs):
        if path == stale:
            raise OSError("unlink failed")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(Path, "unlink", fake_unlink)

    client._cleanup_old_backups(stale.parent)


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
    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
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
    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
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
async def test_parse_webhook_returns_false_when_provider_raises(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Webhook parsing failures should be converted into a safe default."""

    class BrokenLibraryProvider(FakeLibraryProvider):
        async def parse_webhook(self, _request) -> tuple[bool, list[str] | None]:
            raise RuntimeError("boom")

    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=BrokenLibraryProvider([], {}),
        list_provider=FakeListProvider(backup_payload=""),
    )

    result = await client.parse_webhook(cast(Request, SimpleNamespace()))

    assert result == (False, None)


@pytest.mark.asyncio
async def test_sync_uses_full_scan_keys_and_prefetch_error_paths(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sync should keep going when prefetch fails and full-scan key filters are used."""
    movie_section = FakeSection("Movies", bridge_module.MediaKind.MOVIE)
    provider = FakeLibraryProvider(
        sections=[movie_section],
        items_by_section={
            "Movies": [FakeMedia("Movie", bridge_module.MediaKind.MOVIE)]
        },
    )
    list_provider = FakeListProvider(backup_payload="")
    movie_sync = FailingSyncClient(prefetch_error=RuntimeError("prefetch boom"))
    show_sync = FakeSyncClient()

    monkeypatch.setattr(bridge_module, "MovieSyncClient", lambda **_: movie_sync)
    monkeypatch.setattr(bridge_module, "ShowSyncClient", lambda **_: show_sync)

    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
        full_scan=True,
        batch_requests=True,
    )

    await client.sync(library_keys=["Movie"])

    assert provider.list_calls[0]["require_watched"] is False
    assert provider.list_calls[0]["keys"] == ["Movie"]
    assert movie_sync.flush_called is True
    assert movie_sync.batch_called is True


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

    movie_sync = FakeSyncClient(outcome=SyncOutcome.NOT_FOUND)
    show_sync = FakeSyncClient(outcome=SyncOutcome.NOT_FOUND)

    monkeypatch.setattr(bridge_module, "MovieSyncClient", lambda **_: movie_sync)
    monkeypatch.setattr(bridge_module, "ShowSyncClient", lambda **_: show_sync)

    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
    )

    await client.sync()

    assert client.last_synced is not None
    assert client.current_sync is None


@pytest.mark.asyncio
async def test_sync_preserves_last_synced_when_item_fails(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Caught item failures should leave the previous poll cursor unchanged."""
    movie_section = FakeSection("Movies", bridge_module.MediaKind.MOVIE)
    provider = FakeLibraryProvider(
        sections=[movie_section],
        items_by_section={
            "Movies": [FakeMedia("Movie", bridge_module.MediaKind.MOVIE)]
        },
    )
    list_provider = FakeListProvider(backup_payload="")
    movie_sync = ItemFailingSyncClient()

    monkeypatch.setattr(bridge_module, "MovieSyncClient", lambda **_: movie_sync)
    monkeypatch.setattr(
        bridge_module, "ShowSyncClient", lambda **_: ItemFailingSyncClient()
    )

    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
    )
    previous_cursor = datetime(2025, 1, 1, tzinfo=UTC)
    client._set_last_synced(previous_cursor)

    await client.sync(poll=True)

    assert movie_sync.sync_stats.is_complete is False
    assert client.last_synced == previous_cursor
    assert client._get_last_synced() == previous_cursor


@pytest.mark.asyncio
async def test_sync_failure_sets_completed_state(
    in_memory_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Failures should still mark the sync progress as completed."""
    movie_section = FakeSection("Movies", bridge_module.MediaKind.MOVIE)
    provider = FakeLibraryProvider([movie_section], {"Movies": []})
    list_provider = FakeListProvider(backup_payload="")
    client = _make_bridge_client(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider=provider,
        list_provider=list_provider,
    )

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(client, "_sync_section", _boom)

    with pytest.raises(RuntimeError):
        await client.sync()

    assert client.current_sync is None
