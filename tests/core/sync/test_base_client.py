"""Tests covering helper utilities on `src.core.sync.base`."""

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from anibridge.library import MediaKind
from anibridge.list import (
    ListEntry as ListEntryProtocol,
)
from anibridge.list import (
    ListMediaType,
    ListStatus,
)

from src.config.settings import SyncField
from src.core.animap import MappingGraph
from src.core.sync.base import BaseSyncClient, diff_snapshots
from src.core.sync.stats import EntrySnapshot, ItemIdentifier
from src.models.db.pin import Pin
from src.models.db.sync_history import SyncHistory, SyncOutcome
from src.utils.terminal import ARROW
from tests.core.sync.fakes import (
    FakeAnimapClient,
    FakeLibraryMovie,
    FakeLibraryProvider,
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
                MappingGraph | None,
                ListEntryProtocol | None,
                str | None,
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

    async def map_media(
        self, item: Any
    ) -> AsyncIterator[
        tuple[
            Any,
            Sequence[Any],
            MappingGraph | None,
            ListEntryProtocol | None,
            str | None,
        ]
    ]:
        """Yield any queued mapping results for testing purposes."""
        if False:
            yield item, (), None, None, None
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

    def _derive_scope(self, *, item: Any, child_item: Any | None) -> str | None:
        return None

    def _debug_log_title(self, item: Any, child_item: Any | None = None) -> str:
        return str(item)

    def _debug_log_ids(
        self,
        *,
        item: Any,
        child_item: Any,
        entry: ListEntryProtocol | None,
        mapping=None,
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
        excluded_sync_fields=[],
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


def test_diff_snapshots_returns_changed_fields() -> None:
    """``diff_snapshots`` only includes differences for requested fields."""
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


def test_should_update_field_respects_comparison_rules(
    stub_client: StubSyncClient,
) -> None:
    """Field updates honor skip lists, comparison operators, and destructive mode."""
    skip_fields = {SyncField.STATUS.value}
    status_rule = stub_client._FIELD_RULES[SyncField.STATUS]
    progress_rule = stub_client._FIELD_RULES[SyncField.PROGRESS]

    assert not stub_client._should_apply_field(
        SyncField.STATUS, status_rule, 5, 4, skip_fields
    )

    assert stub_client._should_apply_field(
        SyncField.PROGRESS, progress_rule, 5, 4, set()
    )
    assert not stub_client._should_apply_field(
        SyncField.PROGRESS, progress_rule, 3, 4, set()
    )

    stub_client.destructive_sync = True
    assert stub_client._should_apply_field(
        SyncField.PROGRESS, progress_rule, 1, None, set()
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
    pick = stub_client._best_search_result("Perfect Match", entries)
    assert pick is exact

    stub_client.search_fallback_threshold = 95
    assert (
        stub_client._best_search_result("Perfect Match", [cast(ListEntryProtocol, off)])
        is None
    )


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
        mapping=None,
    )

    assert result is SyncOutcome.SYNCED
    assert provider.updated_entries and provider.updated_entries[0][0] == "movie-entry"

    with sync_db as ctx:
        history = ctx.session.query(SyncHistory).all()
        assert len(history) == 1
        assert history[0].outcome == SyncOutcome.SYNCED


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
        mapping=None,
    )

    assert result is SyncOutcome.SKIPPED
    assert provider.updated_entries == []


@pytest.mark.asyncio
async def test_sync_media_deletes_when_destructive(
    stub_client: StubSyncClient, sync_db
) -> None:
    """Destructive sync removes entries whose status resolves to ``None``."""
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
        mapping=None,
    )

    assert result is SyncOutcome.DELETED
    assert "movie-entry" in provider.deleted_keys

    with sync_db as ctx:
        history = ctx.session.query(SyncHistory).all()
        assert history[0].outcome == SyncOutcome.DELETED


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
        mapping=None,
    )

    assert result is SyncOutcome.SYNCED
    assert [update.entry for update in stub_client._pending_updates] == [entry]
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
        mapping=None,
    )

    await stub_client.batch_sync()

    assert provider.batch_updates and provider.batch_updates[0][0] is entry
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

    stub_client._cleanup_failure_history(item=movie)
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


@pytest.mark.asyncio
async def test_process_media_skips_untrackable_items(
    stub_client: StubSyncClient,
) -> None:
    """Items with no eligible children are marked as skipped early."""
    movie = make_movie()

    await stub_client.process_media(movie)

    assert stub_client.sync_stats.skipped == 1
