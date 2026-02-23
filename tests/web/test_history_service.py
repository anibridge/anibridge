"""Tests for the sync history service."""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import pytest
from anibridge.library import MediaKind

from src.config.database import db
from src.exceptions import (
    HistoryItemNotFoundError,
    HistoryPermissionError,
    ProfileNotFoundError,
    SchedulerNotInitializedError,
)
from src.models.db.pin import Pin
from src.models.db.sync_history import SyncHistory, SyncOutcome
from src.web.services.history_service import HistoryService, get_history_service
from src.web.state import get_app_state, get_bridge


@dataclass
class DummyMedia:
    """Minimal provider media representation used in tests."""

    key: str
    title: str
    poster_image: str
    external_url: str
    labels: dict[str, str]


class DummyListEntry:
    """List provider entry that exposes rich media."""

    def __init__(self, key: str) -> None:
        """Store the derived media information for a given key."""
        self._media = DummyMedia(
            key=key,
            title=f"List {key}",
            poster_image=f"L-{key}",
            external_url=f"http://list/{key}",
            labels={"format": "movie"},
        )
        self.title = self._media.title

    def media(self) -> DummyMedia:
        """Return the provider-native media object."""
        return self._media


class DummyListProvider:
    """List provider double returning deterministic entries."""

    NAMESPACE = "alist"

    def __init__(self) -> None:
        """Initialize deletion tracking for undo operations."""
        self.deleted_entries: list[str] = []

    def user(self):
        """Return pseudo user metadata."""
        return SimpleNamespace(title="ListUser")

    async def get_entries_batch(self, keys):
        """Return entries for all requested keys."""
        return [DummyListEntry(key) for key in keys]

    async def delete_entry(self, key: str):
        """Track deletions requested by undo operations."""
        self.deleted_entries.append(key)


@dataclass
class DummyLibraryItem:
    """Library item metadata used for enrichment."""

    key: str
    title: str
    _media: DummyMedia

    def media(self) -> DummyMedia:
        """Return the provider-native media object."""
        return self._media


@dataclass
class DummyLibrarySection:
    """Library section metadata with media kind."""

    key: str
    title: str
    media_kind: MediaKind = MediaKind.MOVIE


class DummyLibraryProvider:
    """Library provider double that scopes to a single section."""

    NAMESPACE = "_dummy-library"

    def __init__(self) -> None:
        """Initialize the provider with one default section."""
        self.sections = [DummyLibrarySection(key="1", title="Movies")]

    async def get_sections(self):
        """Return available sections."""
        return self.sections

    async def list_items(self, section, keys):
        """Return fake library items for the requested keys."""
        return [
            DummyLibraryItem(
                key=k,
                title=f"Library {k}",
                _media=DummyMedia(
                    key=k,
                    title=f"Library {k}",
                    poster_image=f"P-{k}",
                    external_url=f"http://library/{k}",
                    labels={"genre": "drama"},
                ),
            )
            for k in keys
        ]


class DummyBridge(SimpleNamespace):
    """Bridge container connecting providers to the scheduler stub."""


@pytest.fixture()
def history_env(monkeypatch: pytest.MonkeyPatch):
    """Attach a scheduler containing a single bridge for history tests."""
    list_provider = DummyListProvider()
    library_provider = DummyLibraryProvider()
    bridge = DummyBridge(
        list_provider=list_provider,
        library_provider=library_provider,
        profile_config=SimpleNamespace(dry_run=False, destructive_sync=False),
    )
    scheduler = SimpleNamespace(bridge_clients={"profile": bridge})
    state = get_app_state()
    state.scheduler = cast(Any, scheduler)
    yield SimpleNamespace(
        scheduler=scheduler,
        bridge=bridge,
        list_provider=list_provider,
    )
    state.scheduler = None


def _seed_history_row(*, clear: bool = True, **overrides) -> int:
    with db() as ctx:
        if clear:
            ctx.session.query(SyncHistory).delete()
            ctx.session.query(Pin).delete()
            ctx.session.commit()
        payload = {
            "profile_name": "profile",
            "library_namespace": "_dummy-library",
            "library_section_key": "1",
            "library_media_key": "lib1",
            "list_namespace": "alist",
            "list_media_key": "lst1",
            "media_kind": MediaKind.MOVIE,
            "outcome": SyncOutcome.SYNCED,
            "before_state": {"progress": 0},
            "after_state": {"progress": 1},
            "info": {"source": "test-seed"},
            "error_message": None,
        }
        payload.update(overrides)
        row = SyncHistory(**payload)
        ctx.session.add(row)
        if payload.get("list_media_key"):
            ctx.session.add(
                Pin(
                    profile_name=payload["profile_name"],
                    list_namespace=payload["list_namespace"],
                    list_media_key=payload["list_media_key"],
                    fields=["status"],
                )
            )
        ctx.session.commit()
        return row.id


@pytest.mark.asyncio
async def test_history_service_get_page_enriches_metadata(history_env):
    """History pages include provider metadata and cached pin data."""
    _seed_history_row()
    service = HistoryService()

    page = await service.get_page(
        profile="profile",
        page=1,
        per_page=10,
        include_library_media=True,
        include_list_media=True,
    )
    assert page.total == 1
    item = page.items[0]
    assert item.library_media is not None
    assert item.library_media.title == "Library lib1"
    assert item.list_media is not None
    assert item.list_media.title == "List lst1"
    assert item.pinned_fields == ["status"]
    assert item.info == {"source": "test-seed"}

    cache_info = service.get_cache_info()
    assert cache_info["list_cache"].hits >= 0
    await service.clear_profile_cache("profile")


@pytest.mark.asyncio
async def test_history_service_delete_item_removes_row(history_env):
    """delete_item removes the record and flushes caches."""
    row_id = _seed_history_row()
    service = HistoryService()

    await service.delete_item("profile", row_id)

    with db() as ctx:
        assert ctx.session.query(SyncHistory).count() == 0


def test_history_serviceget_bridge_requires_scheduler():
    """get_bridge raises when the scheduler is missing."""
    state = get_app_state()
    original = state.scheduler
    state.scheduler = None
    try:
        with pytest.raises(SchedulerNotInitializedError):
            get_bridge("profile")
    finally:
        state.scheduler = original


def test_history_serviceget_bridge_requires_known_profile(history_env):
    """get_bridge raises when the profile is not configured."""
    history_env.scheduler.bridge_clients = {}
    try:
        with pytest.raises(ProfileNotFoundError):
            get_bridge("missing")
    finally:
        history_env.scheduler.bridge_clients = {"profile": history_env.bridge}


@pytest.mark.asyncio
async def test_history_service_get_page_filters_by_outcome(history_env):
    """Outcome filters should constrain the query results."""
    _seed_history_row(outcome=SyncOutcome.SYNCED)
    _seed_history_row(
        clear=False,
        library_media_key="lib2",
        list_media_key="lst2",
        outcome=SyncOutcome.SKIPPED,
    )
    service = HistoryService()

    page = await service.get_page(
        profile="profile",
        page=1,
        per_page=10,
        outcome=SyncOutcome.SKIPPED.value,
        include_library_media=False,
        include_list_media=False,
    )

    assert page.total == 1
    assert all(item.outcome == SyncOutcome.SKIPPED.value for item in page.items)


@pytest.mark.asyncio
async def test_history_service_delete_item_missing_row(history_env):
    """delete_item raises when the record does not exist."""
    service = HistoryService()

    with pytest.raises(HistoryItemNotFoundError):
        await service.delete_item("profile", 9999)


@pytest.mark.asyncio
async def test_history_service_undo_item_requires_list_key(history_env):
    """undo_item rejects history rows lacking a list media key."""
    row_id = _seed_history_row(list_media_key=None)
    service = HistoryService()

    with pytest.raises(HistoryItemNotFoundError):
        await service.undo_item("profile", row_id)


@pytest.mark.asyncio
async def test_history_service_undo_item_deletes_entry_and_fails(history_env):
    """undo_item deletion raises permission error when destructive sync is disabled."""
    row_id = _seed_history_row(before_state=None)
    service = HistoryService()

    with pytest.raises(HistoryPermissionError):
        await service.undo_item("profile", row_id)

    assert history_env.list_provider.deleted_entries == []


@pytest.mark.asyncio
async def test_history_service_undo_item_records_info(history_env):
    """Undo entries keep an audit trail in the info payload."""
    history_env.bridge.profile_config.destructive_sync = True
    row_id = _seed_history_row(before_state=None)
    service = HistoryService()

    item = await service.undo_item("profile", row_id)

    assert item.info is not None
    assert item.info.get("operation") == "undo"
    assert item.info.get("source_history_id") == str(row_id)
    assert item.info.get("source_outcome") == SyncOutcome.SYNCED.value
    assert history_env.list_provider.deleted_entries == ["lst1"]


@pytest.mark.asyncio
async def test_history_service_fetch_helpers_handle_mismatches(history_env):
    """Metadata helpers should return list metadata and filter library sections."""
    service = HistoryService()

    list_result = await service._fetch_list_metadata_batch(
        "profile", "alist", ("lst1",)
    )
    assert list_result["lst1"].title == "List lst1"

    library_result = await service._fetch_library_metadata_batch(
        "profile", "_dummy-library", "missing", ("lib1",)
    )
    assert library_result == {}

    await service.clear_all_caches()


@pytest.mark.asyncio
async def test_history_service_clear_all_caches(history_env):
    """clear_all_caches resets cache state metrics."""
    service = HistoryService()
    await service._fetch_profile_stats("profile")
    await service.clear_all_caches()
    info = service.get_cache_info()
    assert info["stats_cache"].currsize == 0


def test_get_history_service_returns_singleton():
    """The cached service factory should return a singleton."""
    assert get_history_service() is get_history_service()
