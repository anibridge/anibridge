"""Tests for the sync history service."""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import pytest
from anibridge.library import MediaKind

from anibridge.app.config.database import db
from anibridge.app.exceptions import (
    HistoryItemNotFoundError,
    HistoryPermissionError,
    ProfileNotFoundError,
    SchedulerNotInitializedError,
)
from anibridge.app.models.db.pin import Pin
from anibridge.app.models.db.sync_history import SyncHistory, SyncOutcome
from anibridge.app.web.services.history_service import (
    HistoryService,
    get_history_service,
)
from anibridge.app.web.state import get_app_state, get_bridge


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

    def __init__(self, key: str, title: str | None = None) -> None:
        """Store the derived media information for a given key."""
        self._media = DummyMedia(
            key=key,
            title=title or f"List {key}",
            poster_image=f"L-{key}",
            external_url=f"http://list/{key}",
            labels={"format": "movie"},
        )
        self.title = self._media.title
        self.status = None
        self.progress = None
        self.repeats = None
        self.review = None
        self.user_rating = None
        self.started_at = None
        self.finished_at = None

    def media(self) -> DummyMedia:
        """Return the provider-native media object."""
        return self._media


class DummyListProvider:
    """List provider double returning deterministic entries."""

    NAMESPACE = "alist"

    def __init__(self) -> None:
        """Initialize deletion tracking for undo operations."""
        self.deleted_entries: list[str] = []
        self.updated_entries: list[tuple[str, DummyListEntry]] = []
        self.entries: dict[str, DummyListEntry] = {}
        self.titles: dict[str, str] = {}
        self._missing_keys: set[str] = set()

    def user(self):
        """Return pseudo user metadata."""
        return SimpleNamespace(title="ListUser")

    def _get_or_create_entry(self, key: str) -> DummyListEntry | None:
        key = str(key)
        if key in self._missing_keys:
            return None
        entry = self.entries.get(key)
        if entry is None:
            entry = DummyListEntry(key, title=self.titles.get(key))
            self.entries[key] = entry
        return entry

    async def get_entries_batch(self, keys):
        """Return entries for all requested keys."""
        return [self._get_or_create_entry(key) for key in keys]

    async def get_entry(self, key: str):
        """Return one entry by key."""
        return self._get_or_create_entry(key)

    async def update_entry(self, key: str, entry: DummyListEntry):
        """Track updated entries requested by undo operations."""
        key = str(key)
        self._missing_keys.discard(key)
        self.entries[key] = entry
        self.updated_entries.append((key, entry))

    async def delete_entry(self, key: str):
        """Track deletions requested by undo operations."""
        key = str(key)
        self.deleted_entries.append(key)
        self.entries.pop(key, None)
        self._missing_keys.add(key)


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
        self.titles: dict[str, str] = {}

    async def get_sections(self):
        """Return available sections."""
        return self.sections

    async def list_items(self, section, keys):
        """Return fake library items for the requested keys."""
        return [
            DummyLibraryItem(
                key=k,
                title=self.titles.get(k, f"Library {k}"),
                _media=DummyMedia(
                    key=k,
                    title=self.titles.get(k, f"Library {k}"),
                    poster_image=f"P-{k}",
                    external_url=f"http://library/{k}",
                    labels={"genre": "drama"},
                ),
            )
            for k in keys
        ]


class DummyBridge(SimpleNamespace):
    """Bridge container connecting providers to the scheduler stub."""


class DummyScheduler(SimpleNamespace):
    """Scheduler test double for retry-item scheduling."""

    async def run_maintenance(self, work, *, source: str, timeout_=None):
        """Execute maintenance immediately while recording its source."""
        del timeout_
        self.maintenance_sources.append(source)
        return await work()

    async def trigger_profile_sync(self, profile: str, **kwargs):
        self.calls.append({"profile": profile, **kwargs})


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
    scheduler = DummyScheduler(
        bridge_clients={"profile": bridge}, calls=[], maintenance_sources=[]
    )
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
        limit=10,
        include_library_media=True,
        include_list_media=True,
        include_stats=True,
    )
    assert len(page.items) == 1
    assert page.has_more is False
    assert page.latest_id is not None
    item = page.items[0]
    assert item.library_media is not None
    assert item.library_media.title == "Library lib1"
    assert item.list_media is not None
    assert item.list_media.title == "List lst1"
    assert item.pinned_fields == ["status"]
    assert item.info == {"source": "test-seed"}
    assert item.ephemeral is False


@pytest.mark.asyncio
async def test_history_service_get_page_includes_ephemeral_flag(history_env):
    """History pages should expose whether a row is ephemeral."""
    _seed_history_row(ephemeral=True)
    service = HistoryService()

    page = await service.get_page(
        profile="profile",
        limit=10,
        include_library_media=False,
        include_list_media=False,
    )

    assert len(page.items) == 1
    assert page.items[0].ephemeral is True


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
        limit=10,
        outcome=SyncOutcome.SKIPPED.value,
        include_library_media=False,
        include_list_media=False,
    )

    assert len(page.items) == 1
    assert all(item.outcome == SyncOutcome.SKIPPED.value for item in page.items)


@pytest.mark.asyncio
async def test_history_service_get_page_stats_are_fresh_after_write(history_env):
    """Stats should reflect newly written rows without requiring cache clears."""
    _seed_history_row(outcome=SyncOutcome.SYNCED)
    service = HistoryService()

    first_page = await service.get_page(
        profile="profile",
        limit=10,
        include_library_media=False,
        include_list_media=False,
        include_stats=True,
    )

    _seed_history_row(
        clear=False,
        library_media_key="lib2",
        list_media_key="lst2",
        outcome=SyncOutcome.FAILED,
    )

    second_page = await service.get_page(
        profile="profile",
        limit=10,
        include_library_media=False,
        include_list_media=False,
        include_stats=True,
    )

    assert first_page.stats == {SyncOutcome.SYNCED.value: 1}
    assert len(second_page.items) == 2
    assert second_page.stats == {
        SyncOutcome.SYNCED.value: 1,
        SyncOutcome.FAILED.value: 1,
    }


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
    assert item.info.get("source_history_id") == str(row_id)
    assert item.info.get("source_outcome") == SyncOutcome.SYNCED.value
    assert history_env.list_provider.deleted_entries == ["lst1"]
    assert history_env.scheduler.maintenance_sources == [
        f"history_undo:profile:{row_id}"
    ]


@pytest.mark.asyncio
async def test_history_service_undo_item_in_dry_run_is_ephemeral(history_env):
    """Undo history rows should be ephemeral when the profile is in dry-run mode."""
    history_env.bridge.profile_config.destructive_sync = True
    history_env.bridge.profile_config.dry_run = True
    row_id = _seed_history_row(before_state=None)
    service = HistoryService()

    item = await service.undo_item("profile", row_id)

    assert item.ephemeral is True
    assert item.info is not None
    assert history_env.list_provider.deleted_entries == []


@pytest.mark.asyncio
async def test_history_service_undo_item_clears_cached_list_metadata(history_env):
    """Undoing a deletion should evict cached metadata for removed list entries."""
    history_env.bridge.profile_config.destructive_sync = True
    row_id = _seed_history_row(before_state=None)
    service = HistoryService()

    page_before = await service.get_page(
        profile="profile",
        limit=10,
        include_library_media=False,
        include_list_media=True,
    )
    assert page_before.items[0].list_media is not None

    await service.undo_item("profile", row_id)

    page_after = await service.get_page(
        profile="profile",
        limit=10,
        include_library_media=False,
        include_list_media=True,
    )

    assert history_env.list_provider.deleted_entries == ["lst1"]
    assert all(
        item.list_media is None
        for item in page_after.items
        if item.list_media_key == "lst1"
    )


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


@pytest.mark.asyncio
async def test_history_service_library_metadata_uses_library_key(history_env):
    """Library metadata enrichment should resolve by provider library key."""
    row_library_key = "entry-key"
    _seed_history_row(library_media_key=row_library_key)

    async def _list_items_with_mismatched_media_key(section, keys):
        entry_key = str(keys[0])
        return [
            DummyLibraryItem(
                key=entry_key,
                title=f"Library {entry_key}",
                _media=DummyMedia(
                    key="guid://lib-entry-key",
                    title=f"Library {entry_key}",
                    poster_image=f"P-{entry_key}",
                    external_url=f"http://library/{entry_key}",
                    labels={"genre": "drama"},
                ),
            )
        ]

    history_env.bridge.library_provider.list_items = (
        _list_items_with_mismatched_media_key
    )

    service = HistoryService()
    page = await service.get_page(
        profile="profile",
        limit=10,
        include_library_media=True,
        include_list_media=False,
    )

    assert len(page.items) == 1
    assert page.items[0].library_media is not None
    assert page.items[0].library_media.key == row_library_key
    assert page.items[0].library_media.external_url == "http://library/entry-key"


@pytest.mark.asyncio
async def test_history_service_purge_ephemeral_items_removes_only_ephemeral(
    history_env,
):
    """Purging ephemeral items should keep persisted history rows intact."""
    _seed_history_row(ephemeral=True)
    _seed_history_row(
        clear=False,
        library_media_key="lib2",
        list_media_key="lst2",
        ephemeral=False,
    )
    service = HistoryService()

    removed = await service.purge_ephemeral_items()

    assert removed == 1
    with db() as ctx:
        rows = (
            ctx.session.query(SyncHistory)
            .order_by(SyncHistory.library_media_key.asc())
            .all()
        )
        assert len(rows) == 1
        assert rows[0].library_media_key == "lib2"
        assert rows[0].ephemeral is False


def test_get_history_service_returns_singleton():
    """The cached service factory should return a singleton."""
    assert get_history_service() is get_history_service()


@pytest.mark.asyncio
async def test_history_service_helper_short_circuits(history_env):
    """Empty batches and mismatched namespaces should return no metadata."""
    service = HistoryService()

    assert await service._build_history_items("profile", []) == []
    assert await service._fetch_list_metadata_batch("profile", "alist", ()) == {}
    assert await service._fetch_list_metadata_batch("profile", "wrong", ("lst1",)) == {}
    assert (
        await service._fetch_library_metadata_batch(
            "profile",
            "_dummy-library",
            None,
            ("lib1",),
        )
        == {}
    )
    assert (
        await service._fetch_library_metadata_batch(
            "profile",
            "wrong",
            "1",
            ("lib1",),
        )
        == {}
    )


@pytest.mark.asyncio
async def test_history_service_get_latest_id_and_cursor_filters(history_env):
    """Latest-id lookups and cursor paging should honor the requested bounds."""
    row1 = _seed_history_row(
        library_media_key="lib1",
        list_media_key="lst1",
        outcome=SyncOutcome.SYNCED,
    )
    row2 = _seed_history_row(
        clear=False,
        library_media_key="lib2",
        list_media_key="lst2",
        outcome=SyncOutcome.FAILED,
    )
    service = HistoryService()

    assert await service.get_latest_id("profile") == row2
    assert (
        await service.get_latest_id("profile", outcome=SyncOutcome.FAILED.value) == row2
    )

    before_page = await service.get_page(
        profile="profile",
        limit=10,
        before_id=row2,
        include_library_media=False,
        include_list_media=False,
    )
    after_page = await service.get_page(
        profile="profile",
        limit=10,
        after_id=row1,
        include_library_media=False,
        include_list_media=False,
    )

    assert [item.id for item in before_page.items] == [row1]
    assert [item.id for item in after_page.items] == [row2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"limit": 0}, "limit must be >= 1"),
        ({"limit": 251}, "limit must be <= 250"),
        ({"before_id": 1, "after_id": 2}, "mutually exclusive"),
    ],
)
async def test_history_service_get_page_validates_inputs(
    kwargs: dict[str, Any],
    message: str,
    history_env,
) -> None:
    """Invalid page parameters should raise a clear ValueError."""
    service = HistoryService()

    with pytest.raises(ValueError, match=message):
        await service.get_page(
            profile="profile",
            include_library_media=False,
            include_list_media=False,
            **kwargs,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"list_namespace": "other"}, "different list provider"),
        ({"library_namespace": "other"}, "different library provider"),
        ({"outcome": SyncOutcome.SKIPPED}, "only supported"),
        ({"before_state": None, "after_state": None}, "does not contain undo data"),
    ],
)
async def test_history_service_undo_item_permission_branches(
    overrides: dict[str, Any],
    message: str,
    history_env,
) -> None:
    """Undo should reject rows that do not meet the provider and state requirements."""
    row_id = _seed_history_row(**overrides)
    service = HistoryService()

    with pytest.raises(HistoryPermissionError, match=message):
        await service.undo_item("profile", row_id)


@pytest.mark.asyncio
async def test_history_service_undo_item_restore_dry_run_skips_provider_write(
    history_env,
):
    """Dry-run undo restores should not mutate the remote list provider."""
    history_env.bridge.profile_config.dry_run = True
    service = HistoryService()
    row_id = _seed_history_row()

    item = await service.undo_item("profile", row_id)

    assert item.ephemeral is True
    assert history_env.list_provider.updated_entries == []


@pytest.mark.asyncio
async def test_history_service_undo_item_restore_requires_provider_entry(history_env):
    """Undo restore should fail if the provider entry no longer exists."""
    row_id = _seed_history_row(before_state={"media_key": "lst1", "progress": 0})
    history_env.list_provider._missing_keys.add("lst1")
    service = HistoryService()

    with pytest.raises(HistoryItemNotFoundError, match="no longer exists"):
        await service.undo_item("profile", row_id)


@pytest.mark.asyncio
async def test_history_service_retry_item_branches(
    history_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry should validate scheduler state and schedule targeted syncs."""
    service = HistoryService()
    state = get_app_state()
    original_scheduler = state.scheduler
    state.scheduler = None
    try:
        with pytest.raises(SchedulerNotInitializedError):
            await service.retry_item("profile", 1)
    finally:
        state.scheduler = original_scheduler

    with pytest.raises(HistoryItemNotFoundError):
        await service.retry_item("profile", 9999)

    wrong_ns_id = _seed_history_row(
        library_namespace="wrong",
        outcome=SyncOutcome.FAILED,
    )
    with pytest.raises(HistoryPermissionError, match="different library provider"):
        await service.retry_item("profile", wrong_ns_id)

    wrong_outcome_id = _seed_history_row(
        clear=False,
        library_media_key="lib2",
        list_media_key="lst2",
        outcome=SyncOutcome.SYNCED,
    )
    with pytest.raises(HistoryPermissionError, match="only available"):
        await service.retry_item("profile", wrong_outcome_id)

    scheduled: dict[str, Any] = {}

    def fake_schedule_task(coro, *, name: str) -> None:
        scheduled["name"] = name
        scheduled["coro"] = coro
        coro.close()

    scheduler = DummyScheduler(bridge_clients={"profile": history_env.bridge}, calls=[])
    state.scheduler = cast(Any, scheduler)
    monkeypatch.setattr(
        "anibridge.app.web.services.history_service.schedule_task",
        fake_schedule_task,
    )

    retry_id = _seed_history_row(
        clear=False,
        library_media_key="lib4",
        list_media_key="lst4",
        outcome=SyncOutcome.FAILED,
    )
    await service.retry_item("profile", retry_id)

    assert scheduled["name"] == f"retry_history_item:profile:{retry_id}"


@pytest.mark.asyncio
async def test_history_service_purge_ephemeral_items_returns_zero_when_empty(
    history_env,
):
    """Purge should return zero when there are no ephemeral rows to delete."""
    service = HistoryService()

    assert await service.purge_ephemeral_items() == 0
