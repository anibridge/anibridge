"""Tests covering helper utilities on `anibridge.app.core.sync.base`."""

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from anibridge.library import MediaKind
from anibridge.list import ListEntry as ListEntryProtocol
from anibridge.list import ListMediaType, ListStatus

from anibridge.app.config.settings import SyncField
from anibridge.app.core.sync.base import BaseSyncClient, SyncTarget, diff_snapshots
from anibridge.app.core.sync.stats import BatchUpdate, EntrySnapshot, ItemIdentifier
from anibridge.app.models.db.pin import Pin
from anibridge.app.models.db.sync_history import SyncHistory, SyncOutcome
from anibridge.app.utils.terminal import ARROW
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


def test_should_update_field_respects_comparison_rules(
    stub_client: StubSyncClient,
) -> None:
    """Field updates honor skip lists and sync-field comparison rules."""
    skip_fields = {SyncField.STATUS.value}
    stub_client.sync_fields = {"progress": {"_lt": False}}

    assert not stub_client._should_apply_field(SyncField.STATUS, 5, 4, skip_fields)

    assert stub_client._should_apply_field(SyncField.PROGRESS, 5, 4, set())
    assert not stub_client._should_apply_field(SyncField.PROGRESS, 3, 4, set())

    stub_client.destructive_sync = True
    assert stub_client._should_apply_field(SyncField.PROGRESS, 1, None, set())


def test_should_update_field_blocks_status_value_via_sync_fields(
    stub_client: StubSyncClient,
) -> None:
    """`sync_fields.status.<value>: false` should block that target status."""
    stub_client.sync_fields = {"status": {"dropped": False}}

    assert not stub_client._should_apply_field(
        SyncField.STATUS,
        ListStatus.DROPPED,
        ListStatus.CURRENT,
        set(),
    )


def test_should_update_field_blocks_operator_via_sync_fields(
    stub_client: StubSyncClient,
) -> None:
    """`sync_fields.<field>._lt: false` should block decreasing updates."""
    stub_client.sync_fields = {"started_at": {"_gt": False}}

    assert not stub_client._should_apply_field(
        SyncField.STARTED_AT,
        datetime(2025, 1, 2, tzinfo=UTC),
        datetime(2025, 1, 1, tzinfo=UTC),
        set(),
    )


@pytest.mark.parametrize(
    ("rule_key", "new_value", "current_value"),
    [
        ("_lte", 4, 4),
        ("_gte", 4, 4),
        ("_eq", 4, 4),
        ("_ne", 5, 4),
    ],
)
def test_should_update_field_blocks_additional_operators(
    stub_client: StubSyncClient,
    rule_key: str,
    new_value: int,
    current_value: int,
) -> None:
    """Operator toggles should block updates that match their condition."""
    stub_client.sync_fields = {"progress": {rule_key: False}}

    assert not stub_client._should_apply_field(
        SyncField.PROGRESS,
        new_value,
        current_value,
        set(),
    )


def test_should_update_field_respects_boolean_field_disable(
    stub_client: StubSyncClient,
) -> None:
    """`sync_fields.<field>: false` should block all writes for that field."""
    stub_client.sync_fields = {"progress": False}

    assert not stub_client._should_apply_field(
        SyncField.PROGRESS,
        10,
        2,
        set(),
    )


def test_should_update_field_operator_checks_ignore_none_comparisons(
    stub_client: StubSyncClient,
) -> None:
    """Range operators should not block initial writes when one side is None."""
    stub_client.sync_fields = {"progress": {"_lt": False, "_gt": False}}

    assert stub_client._should_apply_field(
        SyncField.PROGRESS,
        1,
        None,
        set(),
    )


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
    )

    stub_client.destructive_sync = True
    assert stub_client._should_apply_field(
        SyncField.REVIEW,
        None,
        "existing review",
        set(),
    )


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

    stub_client._collect_prefetch_keys = _collect  # type: ignore[method-assign]
    stub_client.list_provider.get_entries_batch = _boom  # type: ignore[method-assign]

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
async def test_sync_media_skips_disabled_field_calculator(
    stub_client: StubSyncClient,
) -> None:
    """Disabled sync_fields should skip invoking that field calculator."""
    provider = cast(FakeListProvider, stub_client.list_provider)
    entry = FakeListEntry(
        provider=provider,
        key="205",
        title="Movie",
        media_type=ListMediaType.MOVIE,
        total_units=1,
    )
    entry.status = ListStatus.CURRENT
    stub_client.sync_fields = {"review": False}

    async def _boom_review(**kwargs):
        raise AssertionError("review calculator should not be called")

    stub_client._field_calculators[SyncField.REVIEW] = _boom_review

    outcome = await stub_client.sync_media(
        item=make_movie(),
        child_item=make_movie(),
        grandchild_items=(),
        entry=cast(ListEntryProtocol, entry),
        list_media_key="205",
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
async def test_apply_update_dry_run_returns_skipped(
    stub_client: StubSyncClient,
) -> None:
    """Dry-run updates should return SKIPPED without applying changes."""
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

    assert outcome is SyncOutcome.SKIPPED


@pytest.mark.asyncio
async def test_batch_sync_dry_run_clears_queue(
    stub_client: StubSyncClient,
) -> None:
    """Batch sync dry-run should clear the pending queue."""
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
            list_media_key="400",
            mapping_descriptors=(),
        )
    ]
    stub_client.dry_run = True

    await stub_client.batch_sync()

    assert stub_client._pending_updates == []


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
            list_media_key="500",
            mapping_descriptors=(),
        )
    ]

    async def _boom(_entries):
        raise RuntimeError("boom")

    stub_client.list_provider.update_entries_batch = _boom  # type: ignore[method-assign]

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
        list_media_key="800",
        mapping_descriptors=(),
    )

    async def _boom(_key, _entry):
        raise RuntimeError("boom")

    stub_client.list_provider.update_entry = _boom  # type: ignore[method-assign]

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
    pick = stub_client._best_search_result("Perfect Match", entries)
    assert pick is exact

    stub_client.search_fallback_threshold = 95
    assert (
        stub_client._best_search_result("Perfect Match", [cast(ListEntryProtocol, off)])
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

    targets = await stub_client._resolve_list_targets(movie)
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
    stub_client.sync_fields = {"progress": {"_gt": False}, "review": False}
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
        assert "progress(_gt)" in (record.info.get("sync_rules_blocked") or "")
        assert "review" in (record.info.get("sync_fields_disabled") or "")
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
        list_media_key=entry.media().key,
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

    stub_client._cleanup_failure_history(item=movie, list_media_key="entry")
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


@pytest.mark.asyncio
async def test_promote_rewatch_promotes_current_to_repeating_when_completed(
    stub_client: StubSyncClient, sync_db
) -> None:
    """promote_rewatch=True changes CURRENT to REPEATING if COMPLETED."""
    stub_client.promote_rewatch = True
    stub_client._status_override = ListStatus.CURRENT

    provider = cast(Any, stub_client.list_provider)
    movie = make_movie()
    entry = FakeListEntry(
        provider=provider,
        key="m1",
        title="Movie",
        media_type=ListMediaType.MOVIE,
    )
    entry.status = ListStatus.COMPLETED

    stub_client._trackable_items = [ItemIdentifier.from_item(cast(Any, movie))]
    stub_client._map_results = [
        (movie, (movie,), SyncTarget(list_media_key="m1", entry=entry))
    ]
    provider.entries["m1"] = entry

    outcome = await stub_client.sync_media(
        item=movie,
        child_item=movie,
        grandchild_items=(movie,),
        entry=entry,
        list_media_key="m1",
    )

    assert outcome == SyncOutcome.SYNCED
    assert entry.status == ListStatus.REPEATING


@pytest.mark.asyncio
async def test_promote_rewatch_preserves_repeating_when_already_repeating(
    stub_client: StubSyncClient, sync_db
) -> None:
    """promote_rewatch=True should keep REPEATING when entry is already REPEATING."""
    stub_client.promote_rewatch = True
    stub_client._status_override = ListStatus.CURRENT
    stub_client._progress_override = 3

    provider = cast(Any, stub_client.list_provider)
    movie = make_movie()
    entry = FakeListEntry(
        provider=provider,
        key="m1",
        title="Movie",
        media_type=ListMediaType.MOVIE,
    )
    entry.status = ListStatus.REPEATING
    entry.progress = 1

    stub_client._trackable_items = [ItemIdentifier.from_item(cast(Any, movie))]
    stub_client._map_results = [
        (movie, (movie,), SyncTarget(list_media_key="m1", entry=entry))
    ]
    provider.entries["m1"] = entry

    outcome = await stub_client.sync_media(
        item=movie,
        child_item=movie,
        grandchild_items=(movie,),
        entry=entry,
        list_media_key="m1",
    )

    assert outcome == SyncOutcome.SYNCED
    assert entry.status == ListStatus.REPEATING
    assert entry.progress == 3


@pytest.mark.asyncio
async def test_promote_rewatch_disabled_does_not_change_current(
    stub_client: StubSyncClient, sync_db
) -> None:
    """promote_rewatch=False should not promote CURRENT to REPEATING."""
    stub_client.promote_rewatch = False
    stub_client._status_override = ListStatus.CURRENT

    provider = cast(Any, stub_client.list_provider)
    movie = make_movie()
    entry = FakeListEntry(
        provider=provider,
        key="m1",
        title="Movie",
        media_type=ListMediaType.MOVIE,
    )
    entry.status = ListStatus.COMPLETED

    stub_client._trackable_items = [ItemIdentifier.from_item(cast(Any, movie))]
    stub_client._map_results = [
        (movie, (movie,), SyncTarget(list_media_key="m1", entry=entry))
    ]
    provider.entries["m1"] = entry

    outcome = await stub_client.sync_media(
        item=movie,
        child_item=movie,
        grandchild_items=(movie,),
        entry=entry,
        list_media_key="m1",
    )

    assert outcome == SyncOutcome.SYNCED
    assert entry.status == ListStatus.CURRENT


@pytest.mark.asyncio
async def test_promote_rewatch_respects_sync_fields_repeating_disabled(
    stub_client: StubSyncClient, sync_db
) -> None:
    """sync_fields.status.repeating=False should block promote_rewatch promotion."""
    stub_client.promote_rewatch = True
    stub_client.sync_fields = {"status": {"repeating": False}}
    stub_client._status_override = ListStatus.CURRENT

    provider = cast(Any, stub_client.list_provider)
    movie = make_movie()
    entry = FakeListEntry(
        provider=provider,
        key="m1",
        title="Movie",
        media_type=ListMediaType.MOVIE,
    )
    entry.status = ListStatus.COMPLETED

    stub_client._trackable_items = [ItemIdentifier.from_item(cast(Any, movie))]
    stub_client._map_results = [
        (movie, (movie,), SyncTarget(list_media_key="m1", entry=entry))
    ]
    provider.entries["m1"] = entry

    await stub_client.sync_media(
        item=movie,
        child_item=movie,
        grandchild_items=(movie,),
        entry=entry,
        list_media_key="m1",
    )

    # Promotion to REPEATING is blocked by sync_fields, so status stays COMPLETED
    # Other fields may still sync, so outcome can be SYNCED
    assert entry.status == ListStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_status",
    [ListStatus.PLANNING, ListStatus.DROPPED, ListStatus.PAUSED],
)
async def test_promote_rewatch_does_not_affect_non_completed_statuses(
    stub_client: StubSyncClient, sync_db, initial_status: ListStatus
) -> None:
    """promote_rewatch should only apply when entry was COMPLETED or REPEATING."""
    stub_client.promote_rewatch = True
    stub_client._status_override = ListStatus.CURRENT

    provider = cast(Any, stub_client.list_provider)
    movie = make_movie()
    entry = FakeListEntry(
        provider=provider,
        key="m1",
        title="Movie",
        media_type=ListMediaType.MOVIE,
    )
    entry.status = initial_status

    stub_client._trackable_items = [ItemIdentifier.from_item(cast(Any, movie))]
    stub_client._map_results = [
        (movie, (movie,), SyncTarget(list_media_key="m1", entry=entry))
    ]
    provider.entries["m1"] = entry

    outcome = await stub_client.sync_media(
        item=movie,
        child_item=movie,
        grandchild_items=(movie,),
        entry=entry,
        list_media_key="m1",
    )

    assert outcome == SyncOutcome.SYNCED
    assert entry.status == ListStatus.CURRENT
