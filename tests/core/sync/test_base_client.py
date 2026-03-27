"""Tests covering helper utilities on `anibridge.app.core.sync.base`."""

import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from anibridge.library import MediaKind
from anibridge.list import ListEntry as ListEntryProtocol
from anibridge.list import ListMediaType, ListStatus

from anibridge.app.config.settings import SyncField, SyncRulesConfig
from anibridge.app.core.sync.base import BaseSyncClient
from anibridge.app.core.sync.rules import SyncRuleEngine
from anibridge.app.core.sync.stats import BatchUpdate, EntrySnapshot, ItemIdentifier
from anibridge.app.core.sync.targeting import (
    SyncTarget,
    diff_snapshots,
    find_best_search_result,
    resolve_list_targets,
)
from anibridge.app.models.db.pin import Pin
from anibridge.app.models.db.sync_history import SyncHistory, SyncOutcome
from anibridge.app.utils.terminal import ARROW
from tests.core.sync.fakes import (
    FakeAnimapClient,
    FakeLibraryEpisode,
    FakeLibraryMovie,
    FakeLibraryProvider,
    FakeLibrarySeason,
    FakeLibraryShow,
    FakeListEntry,
    FakeListProvider,
)


class StubSyncClient(BaseSyncClient[Any, Any, Any]):
    """Concrete implementation of BaseSyncClient for exercising helpers."""

    def __init__(self, *args, **kwargs) -> None:
        """Initialize the stub and capture queued mapping results."""
        super().__init__(*args, **kwargs)
        self._map_results: list[
            tuple[
                Any,
                Sequence[Any],
                SyncTarget,
            ]
        ] = []
        self._trackable_items: list[ItemIdentifier] = []
        self._status_override: ListStatus | None = ListStatus.CURRENT
        self._progress_override: int | None = 1
        self._repeats_override: int | None = 0
        self._review_override: str | None = None
        self._user_rating_override: int | None = 50
        self._started_at_override: datetime | None = None
        self._finished_at_override: datetime | None = None

    async def _get_all_trackable_items(self, item: Any) -> list[ItemIdentifier]:
        return list(self._trackable_items)

    async def _collect_prefetch_keys(self, item: Any) -> Sequence[str]:
        return []

    async def map_media(
        self, item: Any
    ) -> AsyncIterator[
        tuple[
            Any,
            Sequence[Any],
            SyncTarget,
        ]
    ]:
        """Yield any queued mapping results for testing purposes."""
        for result in self._map_results:
            yield result

    async def search_media(self, item: Any, child_item: Any):
        """No-op search hook."""
        return None

    async def _calculate_status(self, **kwargs):
        return self._status_override

    async def _calculate_user_rating(self, **kwargs):
        return self._user_rating_override

    async def _calculate_progress(self, **kwargs):
        return self._progress_override

    async def _calculate_repeats(self, **kwargs):
        return self._repeats_override

    async def _calculate_started_at(self, **kwargs):
        return self._started_at_override

    async def _calculate_finished_at(self, **kwargs):
        return self._finished_at_override

    async def _calculate_review(self, **kwargs):
        return self._review_override

    def _debug_log_title(self, item: Any, child_item: Any | None = None) -> str:
        return str(item)

    def _debug_log_ids(
        self,
        *,
        item: Any,
        child_item: Any,
        entry: ListEntryProtocol | None,
        media_key=None,
    ) -> str:
        return f"library_key: {getattr(child_item, 'key', 'unknown')}"


@pytest.fixture
def stub_client() -> StubSyncClient:
    """Instantiate a sync client with fake providers for helper tests."""
    provider = FakeListProvider()
    library_provider = FakeLibraryProvider()
    animap = FakeAnimapClient()
    return StubSyncClient(
        library_provider=library_provider,
        list_provider=provider,
        animap_client=animap,
        full_scan=False,
        destructive_sync=False,
        search_fallback_threshold=70,
        batch_requests=False,
        dry_run=False,
        profile_name="tester",
    )


def make_movie(**kwargs) -> FakeLibraryMovie:
    """Helper to construct fake movie instances succinctly."""
    return FakeLibraryMovie(key=kwargs.pop("key", "movie-1"), title="Movie", **kwargs)


def set_sync_rules(stub_client: StubSyncClient, payload: dict[str, Any]) -> None:
    """Attach declarative sync rules to the stub client."""
    rules = SyncRulesConfig.model_validate(payload)
    stub_client._sync_rule_engine = SyncRuleEngine(
        variables=rules.resolved_vars(),
        field_rules=rules.field_rules(),
    )


class CaptureHandler(logging.Handler):
    """Capture log records for assertions."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class _ExplosiveRuleMedia(FakeLibraryMovie):
    """Media stub whose expensive properties raise if touched."""

    def __init__(self) -> None:
        super().__init__(key="lib-1", title="Explosive")

    @property
    def on_watching(self) -> bool:
        raise AssertionError("on_watching should not be evaluated")

    @property
    def on_watchlist(self) -> bool:
        raise AssertionError("on_watchlist should not be evaluated")

    @property
    def user_rating(self) -> int | None:
        raise AssertionError("user_rating should not be evaluated")

    @property
    def view_count(self) -> int:
        raise AssertionError("view_count should not be evaluated")


class _CountedRuleMedia(_ExplosiveRuleMedia):
    """Media stub that counts expensive property accesses."""

    def __init__(self) -> None:
        super().__init__()
        self.on_watching_reads = 0

    @property
    def on_watching(self) -> bool:
        self.on_watching_reads += 1
        return True


class _CountedWatchlistMedia(_ExplosiveRuleMedia):
    """Media stub that counts watchlist property accesses."""

    def __init__(self) -> None:
        super().__init__()
        self.on_watchlist_reads = 0

    @property
    def on_watchlist(self) -> bool:
        self.on_watchlist_reads += 1
        return True


class _IndexedRuleMedia(FakeLibraryMovie):
    """Media stub exposing grandchild index fields used by ctx rules."""

    def __init__(self, *, key: str, season_index: int, index: int) -> None:
        super().__init__(key=key, title="Indexed")
        self.season_index = season_index
        self.index = index


def test_diff_snapshots_returns_changed_fields() -> None:
    """`diff_snapshots` only includes differences for requested fields."""
    before = EntrySnapshot(
        media_key="123",
        status=ListStatus.CURRENT,
        progress=3,
        repeats=0,
        review=None,
        user_rating=50,
        started_at=datetime(2025, 1, 1, tzinfo=UTC),
        finished_at=None,
    )
    after = EntrySnapshot(
        media_key="123",
        status=ListStatus.COMPLETED,
        progress=6,
        repeats=0,
        review="Updated",
        user_rating=80,
        started_at=datetime(2025, 1, 1, tzinfo=UTC),
        finished_at=datetime(2025, 2, 1, tzinfo=UTC),
    )

    diff = diff_snapshots(before, after, {"status", "progress", "finished_at"})

    assert diff == {
        "status": (ListStatus.CURRENT, ListStatus.COMPLETED),
        "progress": (3, 6),
        "finished_at": (None, datetime(2025, 2, 1, tzinfo=UTC)),
    }


def test_should_update_field_respects_skip_fields(
    stub_client: StubSyncClient,
) -> None:
    """Pinned fields should not be applied even when values differ."""
    skip_fields = {SyncField.STATUS.value}

    assert not stub_client._should_apply_field(
        SyncField.STATUS,
        ListStatus.COMPLETED,
        ListStatus.CURRENT,
        skip_fields,
    )[0]


def test_should_update_field_allows_regular_updates(
    stub_client: StubSyncClient,
) -> None:
    """Regular updates should apply when no generic guard blocks them."""
    assert stub_client._should_apply_field(SyncField.PROGRESS, 5, 4, set())[0]

    stub_client.destructive_sync = True
    assert stub_client._should_apply_field(SyncField.PROGRESS, 1, None, set())[0]


def test_should_update_field_blocks_nulling_when_not_destructive(
    stub_client: StubSyncClient,
) -> None:
    """Nulling an existing field should require destructive_sync."""
    stub_client.destructive_sync = False
    assert not stub_client._should_apply_field(
        SyncField.REVIEW,
        None,
        "existing review",
        set(),
    )[0]

    stub_client.destructive_sync = True
    assert stub_client._should_apply_field(
        SyncField.REVIEW,
        None,
        "existing review",
        set(),
    )[0]


def test_get_pinned_fields_uses_cache(stub_client: StubSyncClient, sync_db) -> None:
    """Pinned fields should be loaded from the database and cached."""
    with sync_db as ctx:
        ctx.session.add(
            Pin(
                profile_name=stub_client.profile_name,
                list_namespace=stub_client.list_provider.NAMESPACE,
                list_media_key="123",
                fields=["status"],
            )
        )
        ctx.session.commit()

    fields = stub_client._get_pinned_fields(stub_client.list_provider.NAMESPACE, "123")
    cached = stub_client._get_pinned_fields(stub_client.list_provider.NAMESPACE, "123")

    assert fields == ["status"]
    assert cached == ["status"]


@pytest.mark.asyncio
async def test_prefetch_entries_handles_provider_error(
    stub_client: StubSyncClient,
) -> None:
    """Prefetch should swallow provider errors."""

    async def _collect(_item):
        return ["1"]

    async def _boom(_keys):
        raise RuntimeError("boom")

    stub_client._collect_prefetch_keys = _collect  # ty:ignore[invalid-assignment]
    stub_client.list_provider.get_entries_batch = _boom  # ty:ignore[invalid-assignment]

    await stub_client.prefetch_entries([make_movie()])


@pytest.mark.asyncio
async def test_sync_media_deletes_entry_when_destructive(
    stub_client: StubSyncClient, sync_db
) -> None:
    """Destructive sync should delete entries when status becomes None."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="200",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    entry.status = ListStatus.CURRENT
    provider.entries["200"] = entry
    stub_client.destructive_sync = True
    stub_client._status_override = None

    outcome = await stub_client.sync_media(
        item=make_movie(),
        child_item=make_movie(),
        grandchild_items=(),
        entry=cast(ListEntryProtocol, entry),
        list_media_key="200",
        mapping_descriptors=None,
    )

    assert outcome is SyncOutcome.DELETED
    assert provider.deleted_keys == ["200"]


@pytest.mark.asyncio
async def test_sync_media_does_not_hydrate_unused_ctx_media_fields(
    stub_client: StubSyncClient,
) -> None:
    """Context hydration should skip expensive fields when rules don't use them."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="200-unused-ctx",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    provider.entries[entry.media().key] = entry

    set_sync_rules(
        stub_client,
        {
            "status": [
                {
                    "name": "status without ctx",
                    "if": 'computed.status == "current"',
                    "set": "computed.status",
                }
            ]
        },
    )

    media = _ExplosiveRuleMedia()
    outcome = await stub_client.sync_media(
        item=media,
        child_item=media,
        grandchild_items=(media,),
        entry=cast(ListEntryProtocol, entry),
        list_media_key=entry.media().key,
    )

    assert outcome is SyncOutcome.SYNCED


@pytest.mark.asyncio
async def test_sync_media_hydrates_ctx_media_fields_when_rule_uses_them(
    stub_client: StubSyncClient,
) -> None:
    """Context hydration should evaluate expensive fields when rules reference them."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="200-used-ctx",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    provider.entries[entry.media().key] = entry

    set_sync_rules(
        stub_client,
        {
            "status": [
                {
                    "name": "status with ctx",
                    "if": "ctx.item.on_watching",
                    "set": "computed.status",
                }
            ]
        },
    )

    media = _CountedRuleMedia()
    outcome = await stub_client.sync_media(
        item=media,
        child_item=media,
        grandchild_items=(media,),
        entry=cast(ListEntryProtocol, entry),
        list_media_key=entry.media().key,
    )

    assert outcome is SyncOutcome.SYNCED
    assert media.on_watching_reads > 0


@pytest.mark.asyncio
async def test_sync_media_preserves_cheap_ctx_fields(
    stub_client: StubSyncClient,
) -> None:
    """Rules should still be able to read always-available ctx media fields."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="200-cheap-ctx",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    provider.entries[entry.media().key] = entry

    set_sync_rules(
        stub_client,
        {
            "status": [
                {
                    "name": "cheap ctx title check",
                    "if": (
                        'ctx.item.title == "Movie" and ctx.child.media_kind == "movie"'
                    ),
                    "set": "computed.status",
                }
            ]
        },
    )

    media = FakeLibraryMovie(key="cheap-ctx", title="Movie")
    outcome = await stub_client.sync_media(
        item=media,
        child_item=media,
        grandchild_items=(media,),
        entry=cast(ListEntryProtocol, entry),
        list_media_key=entry.media().key,
    )

    assert outcome is SyncOutcome.SYNCED


@pytest.mark.asyncio
async def test_sync_media_preserves_grandchild_index_context(
    stub_client: StubSyncClient,
) -> None:
    """Rules should still be able to read indexed grandchild ctx fields."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="200-grandchild-ctx",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    provider.entries[entry.media().key] = entry

    set_sync_rules(
        stub_client,
        {
            "status": [
                {
                    "name": "grandchild season index check",
                    "if": (
                        "ctx.grandchildren[0].season_index == 2 and "
                        "ctx.grandchildren[0].index == 7"
                    ),
                    "set": "computed.status",
                }
            ]
        },
    )

    item = make_movie(key="grandchild-parent")
    child = make_movie(key="grandchild-child")
    grandchild = _IndexedRuleMedia(key="grandchild-1", season_index=2, index=7)
    outcome = await stub_client.sync_media(
        item=item,
        child_item=child,
        grandchild_items=(grandchild,),
        entry=cast(ListEntryProtocol, entry),
        list_media_key=entry.media().key,
    )

    assert outcome is SyncOutcome.SYNCED


@pytest.mark.asyncio
async def test_sync_media_hydrates_ctx_fields_referenced_through_vars(
    stub_client: StubSyncClient,
) -> None:
    """Vars that reference ctx should still trigger the needed context hydration."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="200-vars-ctx",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    provider.entries[entry.media().key] = entry

    set_sync_rules(
        stub_client,
        {
            "vars": {
                "watchlisted": "ctx.item.on_watchlist",
            },
            "status": [
                {
                    "name": "ctx through vars",
                    "if": "vars.watchlisted",
                    "set": "computed.status",
                }
            ],
        },
    )

    media = _CountedWatchlistMedia()
    outcome = await stub_client.sync_media(
        item=media,
        child_item=media,
        grandchild_items=(media,),
        entry=cast(ListEntryProtocol, entry),
        list_media_key=entry.media().key,
    )

    assert outcome is SyncOutcome.SYNCED
    assert media.on_watchlist_reads > 0


@pytest.mark.asyncio
async def test_sync_media_deletes_entry_evicts_cache(
    stub_client: StubSyncClient,
) -> None:
    """Destructive delete should evict the deleted entry from cache."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="200-cache",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    entry.status = ListStatus.CURRENT
    stub_client._cache.cache_entry(cast(ListEntryProtocol, entry))
    stub_client.destructive_sync = True
    stub_client._status_override = None

    outcome = await stub_client.sync_media(
        item=make_movie(),
        child_item=make_movie(),
        grandchild_items=(),
        entry=cast(ListEntryProtocol, entry),
        list_media_key="200-cache",
        mapping_descriptors=None,
    )

    assert outcome is SyncOutcome.DELETED
    assert await stub_client._cache.get_entry("200-cache") is None


@pytest.mark.asyncio
async def test_sync_media_empty_sync_wins_over_destructive_delete(
    stub_client: StubSyncClient,
) -> None:
    """empty_sync should retain idle entries as planning instead of deleting them."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="200-empty",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    entry.status = ListStatus.COMPLETED
    entry.progress = 1
    provider.entries["200-empty"] = entry

    stub_client.destructive_sync = True
    stub_client.empty_sync = True
    stub_client._status_override = ListStatus.PLANNING
    stub_client._progress_override = None

    outcome = await stub_client.sync_media(
        item=make_movie(),
        child_item=make_movie(),
        grandchild_items=(),
        entry=cast(ListEntryProtocol, entry),
        list_media_key="200-empty",
        mapping_descriptors=None,
    )

    assert outcome is SyncOutcome.SYNCED
    assert provider.deleted_keys == []
    assert provider.updated_entries and provider.updated_entries[0][0] == "200-empty"
    assert entry.status == ListStatus.PLANNING
    assert entry.progress is None


@pytest.mark.asyncio
async def test_sync_media_skips_sync_rule_disabled_field_calculator(
    stub_client: StubSyncClient,
) -> None:
    """sync_rules.<field>: false should skip invoking that field calculator."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="205b",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    entry.status = ListStatus.CURRENT
    set_sync_rules(stub_client, {"review": False})

    async def _boom_review(**kwargs):
        raise AssertionError("review calculator should not be called")

    stub_client._field_calculators[SyncField.REVIEW] = _boom_review

    outcome = await stub_client.sync_media(
        item=make_movie(),
        child_item=make_movie(),
        grandchild_items=(),
        entry=cast(ListEntryProtocol, entry),
        list_media_key="205b",
        mapping_descriptors=None,
    )

    assert outcome in (SyncOutcome.SKIPPED, SyncOutcome.SYNCED)


@pytest.mark.asyncio
async def test_sync_media_skips_when_no_status(
    stub_client: StubSyncClient,
) -> None:
    """Non-destructive sync should skip when no status is computed."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="201",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    stub_client._status_override = None

    outcome = await stub_client.sync_media(
        item=make_movie(),
        child_item=make_movie(),
        grandchild_items=(),
        entry=cast(ListEntryProtocol, entry),
        list_media_key="201",
        mapping_descriptors=None,
    )

    assert outcome is SyncOutcome.SKIPPED


@pytest.mark.asyncio
async def test_apply_update_dry_run_records_ephemeral_history(
    stub_client: StubSyncClient, sync_db
) -> None:
    """Dry-run updates should record ephemeral history without applying changes."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="300",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    entry.status = ListStatus.CURRENT
    before = EntrySnapshot.from_entry(cast(ListEntryProtocol, entry))
    entry.progress = 1
    after = EntrySnapshot.from_entry(cast(ListEntryProtocol, entry))

    plan = BatchUpdate(
        item=make_movie(),
        child=make_movie(),
        grandchildren=(),
        before=before,
        after=after,
        entry=cast(ListEntryProtocol, entry),
        source_entry=cast(ListEntryProtocol, entry),
        list_media_key="300",
        mapping_descriptors=(),
    )

    stub_client.dry_run = True
    outcome = await stub_client._apply_update(
        plan,
        diff_str="progress: 0 → 1",
        debug_title="Movie",
        debug_ids="id",
    )

    assert outcome is SyncOutcome.SYNCED
    assert provider.updated_entries == []
    with sync_db as ctx:
        history = ctx.session.query(SyncHistory).all()
        assert len(history) == 1
        assert history[0].outcome is SyncOutcome.SYNCED
        assert history[0].ephemeral is True
        assert history[0].info is not None
        assert history[0].info.get("operation") == "update_entry"
        assert history[0].info.get("mode") == "single"
        assert history[0].info.get("dry_run") == "true"


@pytest.mark.asyncio
async def test_batch_sync_dry_run_clears_queue_and_records_history(
    stub_client: StubSyncClient, sync_db
) -> None:
    """Batch sync dry-run should clear the pending queue and write history."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="400",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    entry.status = ListStatus.CURRENT
    before = EntrySnapshot.from_entry(cast(ListEntryProtocol, entry))
    entry.progress = 1
    after = EntrySnapshot.from_entry(cast(ListEntryProtocol, entry))

    stub_client._pending_updates = [
        BatchUpdate(
            item=make_movie(),
            child=make_movie(),
            grandchildren=(),
            before=before,
            after=after,
            entry=cast(ListEntryProtocol, entry),
            source_entry=cast(ListEntryProtocol, entry),
            list_media_key="400",
            mapping_descriptors=(),
        )
    ]
    stub_client.dry_run = True

    await stub_client.batch_sync()

    assert stub_client._pending_updates == []
    assert provider.batch_updates == []
    with sync_db as ctx:
        history = ctx.session.query(SyncHistory).all()
        assert len(history) == 1
        assert history[0].outcome is SyncOutcome.SYNCED
        assert history[0].ephemeral is True
        assert history[0].info is not None
        assert history[0].info.get("mode") == "batch"
        assert history[0].info.get("dry_run") == "true"


@pytest.mark.asyncio
async def test_batch_sync_failure_raises(stub_client: StubSyncClient, sync_db) -> None:
    """Batch sync errors should propagate and clear the queue."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="500",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    entry.status = ListStatus.CURRENT
    before = EntrySnapshot.from_entry(cast(ListEntryProtocol, entry))
    entry.progress = 1
    after = EntrySnapshot.from_entry(cast(ListEntryProtocol, entry))

    stub_client._pending_updates = [
        BatchUpdate(
            item=make_movie(),
            child=make_movie(),
            grandchildren=(),
            before=before,
            after=after,
            entry=cast(ListEntryProtocol, entry),
            source_entry=cast(ListEntryProtocol, entry),
            list_media_key="500",
            mapping_descriptors=(),
        )
    ]

    async def _boom(_entries):
        raise RuntimeError("boom")

    stub_client.list_provider.update_entries_batch = _boom  # ty:ignore[invalid-assignment]

    with pytest.raises(RuntimeError):
        await stub_client.batch_sync()

    assert stub_client._pending_updates == []


def test_render_diff_includes_changes(stub_client: StubSyncClient) -> None:
    """Rendered diffs should include updated attributes."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="600",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    entry.status = ListStatus.CURRENT
    before = EntrySnapshot.from_entry(cast(ListEntryProtocol, entry))
    entry.progress = 1
    after = EntrySnapshot.from_entry(cast(ListEntryProtocol, entry))

    plan = BatchUpdate(
        item=make_movie(),
        child=make_movie(),
        grandchildren=(),
        before=before,
        after=after,
        entry=cast(ListEntryProtocol, entry),
        source_entry=cast(ListEntryProtocol, entry),
        list_media_key="600",
        mapping_descriptors=(),
    )

    diff = stub_client._render_diff(plan)

    assert "progress" in diff


@pytest.mark.asyncio
async def test_batch_sync_success(stub_client: StubSyncClient, sync_db) -> None:
    """Batch sync should update entries and clear the queue."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="700",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    entry.status = ListStatus.CURRENT
    before = EntrySnapshot.from_entry(cast(ListEntryProtocol, entry))
    entry.progress = 1
    after = EntrySnapshot.from_entry(cast(ListEntryProtocol, entry))

    stub_client._pending_updates = [
        BatchUpdate(
            item=make_movie(),
            child=make_movie(),
            grandchildren=(),
            before=before,
            after=after,
            entry=cast(ListEntryProtocol, entry),
            source_entry=cast(ListEntryProtocol, entry),
            list_media_key="700",
            mapping_descriptors=(),
        )
    ]

    await stub_client.batch_sync()

    assert provider.batch_updates
    assert stub_client._pending_updates == []


@pytest.mark.asyncio
async def test_apply_update_raises_on_provider_error(
    stub_client: StubSyncClient, sync_db
) -> None:
    """Provider update errors should be surfaced to callers."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="800",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    entry.status = ListStatus.CURRENT
    before = EntrySnapshot.from_entry(cast(ListEntryProtocol, entry))
    entry.progress = 1
    after = EntrySnapshot.from_entry(cast(ListEntryProtocol, entry))

    plan = BatchUpdate(
        item=make_movie(),
        child=make_movie(),
        grandchildren=(),
        before=before,
        after=after,
        entry=cast(ListEntryProtocol, entry),
        source_entry=cast(ListEntryProtocol, entry),
        list_media_key="800",
        mapping_descriptors=(),
    )

    async def _boom(_key, _entry):
        raise RuntimeError("boom")

    stub_client.list_provider.update_entry = _boom  # ty:ignore[invalid-assignment]

    with pytest.raises(RuntimeError):
        await stub_client._apply_update(
            plan,
            diff_str="progress: 0 → 1",
            debug_title="Movie",
            debug_ids="id",
        )


def test_format_diff_serializes_status_and_datetimes(
    stub_client: StubSyncClient,
) -> None:
    """Formatted diffs render enums and datetimes consistently."""
    diff = {
        "status": (ListStatus.CURRENT, ListStatus.COMPLETED),
        "finished_at": (
            datetime(2025, 2, 1),
            datetime(2025, 2, 1, tzinfo=UTC),
        ),
    }
    result = stub_client._format_diff(diff)
    assert f"status: current {ARROW} completed" in result
    assert (
        "finished_at: 2025-02-01T00:00:00+00:00 "
        f"{ARROW} 2025-02-01T00:00:00+00:00" in result
    )


def test_best_search_result_applies_threshold(stub_client: StubSyncClient) -> None:
    """Fuzzy matching respects the configured fallback threshold."""
    provider = FakeListProvider()
    exact = FakeListEntry(
        provider=provider,
        key="1",
        title="Perfect Match",
        media_type=ListMediaType.MOVIE,
    )
    off = FakeListEntry(
        provider=provider,
        key="2",
        title="Different",
        media_type=ListMediaType.MOVIE,
    )

    stub_client.search_fallback_threshold = 80
    entries = [cast(ListEntryProtocol, exact), cast(ListEntryProtocol, off)]
    pick = find_best_search_result(
        "Perfect Match",
        entries,
        stub_client.search_fallback_threshold,
    )
    assert pick is exact

    stub_client.search_fallback_threshold = 95
    assert (
        find_best_search_result(
            "Perfect Match",
            [cast(ListEntryProtocol, off)],
            stub_client.search_fallback_threshold,
        )
        is None
    )


@pytest.mark.asyncio
async def test_resolve_list_targets_supports_one_to_many(
    stub_client: StubSyncClient,
) -> None:
    """List resolution returns multiple targets for a single descriptor."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    descriptor = ("anilist", "100", None)
    provider.resolved_targets = {descriptor: ["100", "101"]}
    movie = make_movie(mapping_descriptors=[descriptor])

    targets = await resolve_list_targets(
        animap_client=stub_client.animap_client,
        list_provider=stub_client.list_provider,
        media_items=(movie,),
    )
    keys = {target.list_media_key for target in targets}

    assert keys == {"100", "101"}


def test_get_pinned_fields_caches_results(stub_client: StubSyncClient, sync_db) -> None:
    """Pinned field lookups use the database once and then hit the cache."""
    with sync_db as ctx:
        ctx.session.add(
            Pin(
                profile_name="tester",
                list_namespace="anilist",
                list_media_key="100",
                fields=["status", "progress"],
            )
        )
        ctx.session.commit()

    fields = stub_client._get_pinned_fields("anilist", "100")
    assert fields == ["status", "progress"]
    assert stub_client._get_pinned_fields("anilist", "100") == ["status", "progress"]

    # Delete the row to prove cached values are reused.
    with sync_db as ctx:
        ctx.session.query(Pin).delete()
        ctx.session.commit()

    assert stub_client._get_pinned_fields("anilist", "100") == ["status", "progress"]
    assert stub_client._get_pinned_fields("anilist", None) == []


@pytest.mark.asyncio
async def test_sync_media_updates_entry_and_history(
    stub_client: StubSyncClient, sync_db
) -> None:
    """Syncing a movie writes the diff and records history."""
    movie = make_movie(view_count=2, user_rating=80)
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="movie-entry",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )

    result = await stub_client.sync_media(
        item=movie,
        child_item=movie,
        grandchild_items=(movie,),
        entry=cast(ListEntryProtocol, entry),
        list_media_key=entry.media().key,
    )

    assert result is SyncOutcome.SYNCED
    assert provider.updated_entries and provider.updated_entries[0][0] == "movie-entry"

    with sync_db as ctx:
        history = ctx.session.query(SyncHistory).all()
        assert len(history) == 1
        assert history[0].outcome == SyncOutcome.SYNCED
        assert history[0].info is not None
        assert history[0].info.get("operation") == "update_entry"
        assert history[0].info.get("mode") == "single"


@pytest.mark.asyncio
async def test_sync_media_info_reports_rule_and_status_blocks(
    stub_client: StubSyncClient, sync_db
) -> None:
    """Sync diagnostics should include blocked fields and rule metadata."""
    movie = make_movie(view_count=2, user_rating=80)
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="movie-entry",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    entry.progress = 0
    set_sync_rules(
        stub_client,
        {
            "progress": [
                {
                    "name": "Freeze progress if status is planning",
                    "if": "current.status == 'planning'",
                    "set": "current.progress",
                }
            ],
            "review": False,
        },
    )

    result = await stub_client.sync_media(
        item=movie,
        child_item=movie,
        grandchild_items=(movie,),
        entry=cast(ListEntryProtocol, entry),
        list_media_key=entry.media().key,
    )

    assert result is SyncOutcome.SYNCED
    with sync_db as ctx:
        record = ctx.session.query(SyncHistory).one()
        assert record.info is not None
        assert "progress(no_match)" not in (record.info.get("sync_rules_blocked") or "")
        assert "review" in (record.info.get("disabled_fields") or "")
        assert "user_rating(requires_completed)" in (
            record.info.get("status_gate_blocked") or ""
        )


@pytest.mark.asyncio
async def test_sync_media_skips_when_entry_up_to_date(
    stub_client: StubSyncClient,
) -> None:
    """Entries that already match the calculators are skipped."""
    movie = make_movie()
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="movie-entry",
        title="Movie",
        media_type=ListMediaType.MOVIE,
    )
    entry.status = ListStatus.CURRENT
    entry.progress = 1
    entry.repeats = 0
    entry.user_rating = 50

    result = await stub_client.sync_media(
        item=movie,
        child_item=movie,
        grandchild_items=(movie,),
        entry=cast(ListEntryProtocol, entry),
        list_media_key=entry.media().key,
    )

    assert result is SyncOutcome.SKIPPED
    assert provider.updated_entries == []


@pytest.mark.asyncio
async def test_sync_media_applies_declarative_rules(
    stub_client: StubSyncClient,
) -> None:
    """Declarative rules should transform status and review values."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    movie = make_movie(review="x" * 240)
    entry = FakeListEntry(
        provider=provider,
        key="rule-entry",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    entry.status = ListStatus.COMPLETED
    entry.progress = 1
    stub_client._status_override = ListStatus.CURRENT
    stub_client._progress_override = 1
    stub_client._review_override = "x" * 240
    stub_client._user_rating_override = None
    set_sync_rules(
        stub_client,
        {
            "vars": {
                "is_review_long": (
                    "computed.review is not None and len(computed.review) > 200"
                ),
            },
            "status": [
                {
                    "name": "Promote rewatch to completed",
                    "if": (
                        'current.status in ("repeating", "completed") '
                        'and computed.status == "current"'
                    ),
                    "set": "repeating",
                }
            ],
            "review": [
                {
                    "name": "Truncate long reviews",
                    "if": "vars.is_review_long",
                    "set": 'computed.review[:197] + "..."',
                }
            ],
        },
    )

    result = await stub_client.sync_media(
        item=movie,
        child_item=movie,
        grandchild_items=(movie,),
        entry=cast(ListEntryProtocol, entry),
        list_media_key="rule-entry",
    )

    assert result is SyncOutcome.SYNCED
    assert entry.status == ListStatus.REPEATING
    assert entry.review == ("x" * 197) + "..."


@pytest.mark.asyncio
async def test_sync_media_allows_vars_to_reference_missing_computed_fields(
    stub_client: StubSyncClient,
) -> None:
    """Rule vars should treat missing computed fields as `None`."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    movie = make_movie()
    entry = FakeListEntry(
        provider=provider,
        key="rule-missing-computed",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    entry.status = ListStatus.COMPLETED
    entry.progress = 1
    stub_client._status_override = ListStatus.CURRENT
    stub_client._progress_override = 1
    stub_client._review_override = "ignored"
    set_sync_rules(
        stub_client,
        {
            "vars": {
                "has_review": (
                    "computed.review is not None and len(computed.review) > 0"
                ),
            },
            "status": [
                {
                    "name": "Promote rewatch",
                    "if": (
                        'not vars.has_review and current.status == "completed" '
                        'and computed.status == "current"'
                    ),
                    "set": "repeating",
                }
            ],
        },
    )

    result = await stub_client.sync_media(
        item=movie,
        child_item=movie,
        grandchild_items=(movie,),
        entry=cast(ListEntryProtocol, entry),
        list_media_key="rule-missing-computed",
    )

    assert result is SyncOutcome.SYNCED
    assert entry.status == ListStatus.REPEATING
    assert provider.updated_entries and provider.updated_entries[0][0] == (
        "rule-missing-computed"
    )


@pytest.mark.asyncio
async def test_sync_media_exposes_ctx_item_child_and_grandchildren(
    stub_client: StubSyncClient,
) -> None:
    """Rule expressions should receive shimmed item context under ctx."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    show = FakeLibraryShow(key="show-ctx", title="Ctx Show")
    season = FakeLibrarySeason(
        key="season-ctx",
        title="Season 1",
        index=1,
        show=show,
    )
    episodes = [
        FakeLibraryEpisode(
            key="episode-1",
            title="Episode 1",
            index=1,
            season_index=1,
            show=show,
            season=season,
            view_count=1,
        ),
        FakeLibraryEpisode(
            key="episode-2",
            title="Episode 2",
            index=2,
            season_index=1,
            show=show,
            season=season,
            view_count=1,
        ),
    ]
    show.attach_children(episodes=episodes, seasons=[season])

    entry = FakeListEntry(
        provider=provider,
        key="ctx-entry",
        title="Ctx Show",
        media_type=ListMediaType.TV,
        total_units=2,
    )
    entry.status = ListStatus.PLANNING
    entry.progress = 1
    stub_client._status_override = ListStatus.CURRENT
    stub_client._progress_override = 1

    set_sync_rules(
        stub_client,
        {
            "status": [
                {
                    "name": "Use ctx metadata",
                    "if": (
                        'ctx.list_media_key == "ctx-entry" '
                        'and ctx.item.title == "Ctx Show" '
                        "and ctx.child.index == 1 "
                        "and len(ctx.grandchildren) == 2 "
                        'and ctx.grandchildren[0].title == "Episode 1" '
                        "and ctx.grandchildren[1].index == 2 "
                        "and ctx.grandchildren[0].season_index == 1"
                    ),
                    "set": "completed",
                }
            ]
        },
    )

    result = await stub_client.sync_media(
        item=show,
        child_item=season,
        grandchild_items=tuple(episodes),
        entry=cast(ListEntryProtocol, entry),
        list_media_key="ctx-entry",
    )

    assert result is SyncOutcome.SYNCED
    assert entry.status == ListStatus.COMPLETED
    assert provider.updated_entries and provider.updated_entries[0][0] == "ctx-entry"


@pytest.mark.asyncio
async def test_sync_media_uses_default_value_when_no_sync_rule_matches(
    stub_client: StubSyncClient,
) -> None:
    """Configured field rules should fall back to computed values when unmatched."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    movie = make_movie(review="x" * 220)
    entry = FakeListEntry(
        provider=provider,
        key="rule-blocked",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    entry.status = ListStatus.CURRENT
    entry.progress = 1
    entry.repeats = 0
    entry.user_rating = 50
    entry.review = "existing"
    stub_client._status_override = ListStatus.CURRENT
    stub_client._progress_override = 1
    stub_client._repeats_override = 0
    stub_client._review_override = "x" * 220
    stub_client._user_rating_override = 50
    set_sync_rules(
        stub_client,
        {
            "vars": {
                "has_short_review": (
                    "computed.review is not None and len(computed.review) < 200"
                ),
            },
            "review": [
                {
                    "name": "Tag short reviews",
                    "if": "vars.has_short_review",
                    "set": 'computed.review + " [short]"',
                }
            ],
        },
    )

    result = await stub_client.sync_media(
        item=movie,
        child_item=movie,
        grandchild_items=(movie,),
        entry=cast(ListEntryProtocol, entry),
        list_media_key="rule-blocked",
    )

    assert result is SyncOutcome.SYNCED
    assert entry.review == "x" * 220
    assert provider.updated_entries and provider.updated_entries[0][0] == "rule-blocked"


@pytest.mark.asyncio
async def test_sync_media_deletes_when_destructive(
    stub_client: StubSyncClient, sync_db
) -> None:
    """Destructive sync removes entries whose status resolves to `None`."""
    stub_client.destructive_sync = True
    stub_client._status_override = None

    movie = make_movie()
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="movie-entry",
        title="Movie",
        media_type=ListMediaType.MOVIE,
    )
    entry.status = ListStatus.CURRENT

    result = await stub_client.sync_media(
        item=movie,
        child_item=movie,
        grandchild_items=(movie,),
        entry=cast(ListEntryProtocol, entry),
        list_media_key=entry.media().key,
    )

    assert result is SyncOutcome.DELETED
    assert "movie-entry" in provider.deleted_keys

    with sync_db as ctx:
        history = ctx.session.query(SyncHistory).all()
        assert history[0].outcome == SyncOutcome.DELETED


@pytest.mark.asyncio
async def test_sync_media_dry_run_delete_records_ephemeral_history(
    stub_client: StubSyncClient, sync_db
) -> None:
    """Dry-run destructive deletes should create ephemeral delete history."""
    stub_client.destructive_sync = True
    stub_client.dry_run = True
    stub_client._status_override = None

    movie = make_movie()
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="movie-entry",
        title="Movie",
        media_type=ListMediaType.MOVIE,
    )
    entry.status = ListStatus.CURRENT

    result = await stub_client.sync_media(
        item=movie,
        child_item=movie,
        grandchild_items=(movie,),
        entry=cast(ListEntryProtocol, entry),
        list_media_key=entry.media().key,
    )

    assert result is SyncOutcome.DELETED
    assert provider.deleted_keys == []

    with sync_db as ctx:
        history = ctx.session.query(SyncHistory).all()
        assert len(history) == 1
        assert history[0].outcome is SyncOutcome.DELETED
        assert history[0].ephemeral is True
        assert history[0].info is not None
        assert history[0].info.get("operation") == "delete_entry"
        assert history[0].info.get("dry_run") == "true"


@pytest.mark.asyncio
async def test_sync_media_batches_when_enabled(
    stub_client: StubSyncClient,
) -> None:
    """Batch mode queues updates instead of issuing them immediately."""
    stub_client.batch_requests = True
    movie = make_movie(view_count=1)
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="movie-entry",
        title="Movie",
        media_type=ListMediaType.MOVIE,
    )

    result = await stub_client.sync_media(
        item=movie,
        child_item=movie,
        grandchild_items=(movie,),
        entry=cast(ListEntryProtocol, entry),
        list_media_key=entry.media().key,
    )

    assert result is SyncOutcome.SYNCED
    assert [update.source_entry for update in stub_client._pending_updates] == [entry]
    assert stub_client._pending_updates[0].entry is not entry
    assert entry.progress is None
    assert stub_client._pending_updates
    assert provider.updated_entries == []


@pytest.mark.asyncio
async def test_batch_sync_flushes_history(stub_client: StubSyncClient, sync_db) -> None:
    """Queued entries are persisted and history rows are written."""
    stub_client.batch_requests = True
    movie = make_movie(view_count=1)
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="movie-entry",
        title="Movie",
        media_type=ListMediaType.MOVIE,
    )

    await stub_client.sync_media(
        item=movie,
        child_item=movie,
        grandchild_items=(movie,),
        entry=cast(ListEntryProtocol, entry),
        list_media_key=entry.media().key,
    )

    await stub_client.batch_sync()

    assert provider.batch_updates and provider.batch_updates[0][0] is not entry
    assert entry.progress == 1
    assert not [update.entry for update in stub_client._pending_updates]
    assert not stub_client._pending_updates

    with sync_db as ctx:
        history = ctx.session.query(SyncHistory).all()
        assert history and history[0].outcome == SyncOutcome.SYNCED


def test_flush_failure_history_cleanup_batched_removal(
    stub_client: StubSyncClient, sync_db
) -> None:
    """Batched cleanup removes queued failure history rows."""
    movie = make_movie()
    library_section_key = movie.section().key
    library_media_key = str(movie.key)
    library_namespace = stub_client.library_provider.NAMESPACE
    list_namespace = stub_client.list_provider.NAMESPACE

    with sync_db as ctx:
        ctx.session.add_all(
            [
                SyncHistory(
                    profile_name=stub_client.profile_name,
                    library_namespace=library_namespace,
                    library_section_key=library_section_key,
                    library_media_key=library_media_key,
                    list_namespace=list_namespace,
                    list_media_key="entry",
                    media_kind=MediaKind.MOVIE,
                    outcome=SyncOutcome.NOT_FOUND,
                ),
                SyncHistory(
                    profile_name=stub_client.profile_name,
                    library_namespace=library_namespace,
                    library_section_key=library_section_key,
                    library_media_key=library_media_key,
                    list_namespace=list_namespace,
                    list_media_key="entry",
                    media_kind=MediaKind.MOVIE,
                    outcome=SyncOutcome.FAILED,
                ),
            ]
        )
        ctx.session.commit()

    stub_client._history.queue_failure_history_cleanup(
        item=movie, list_media_key="entry"
    )
    stub_client.flush_failure_history_cleanup()

    with sync_db as ctx:
        assert ctx.session.query(SyncHistory).count() == 0


@pytest.mark.asyncio
async def test_process_media_marks_not_found(
    stub_client: StubSyncClient, sync_db
) -> None:
    """Items without matches create NOT_FOUND history rows."""
    movie = make_movie()
    stub_client._trackable_items = [ItemIdentifier.from_item(cast(Any, movie))]

    await stub_client.process_media(movie)

    assert stub_client.sync_stats.not_found == 1
    with sync_db as ctx:
        record = ctx.session.query(SyncHistory).one()
        assert record.outcome == SyncOutcome.NOT_FOUND
        assert record.info is not None
        assert record.info.get("operation") == "resolve_target"
        assert record.info.get("reason") == "no_matching_list_entry"
        assert record.info.get("grandchild_count") is None


@pytest.mark.asyncio
async def test_process_media_skips_untrackable_items(
    stub_client: StubSyncClient,
) -> None:
    """Items with no eligible children are marked as skipped early."""
    movie = make_movie()

    await stub_client.process_media(movie)

    assert stub_client.sync_stats.skipped == 1
