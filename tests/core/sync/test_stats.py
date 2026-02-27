"""Unit tests for `anibridge.app.core.sync.stats` components."""

from datetime import UTC, datetime
from typing import cast

import pytest
from anibridge.library import LibraryEntry, MediaKind
from anibridge.list import (
    ListEntry,
    ListMediaType,
    ListStatus,
)

from anibridge.app.core.sync.stats import (
    EntrySnapshot,
    ItemIdentifier,
    SyncOutcome,
    SyncStats,
)
from tests.core.sync.fakes import (
    FakeLibraryEpisode,
    FakeLibrarySeason,
    FakeLibraryShow,
    FakeListEntry,
    FakeListProvider,
)


@pytest.fixture
def list_entry() -> ListEntry:
    """Provide a populated fake list entry for snapshot tests."""
    provider = FakeListProvider()
    entry = FakeListEntry(
        provider=provider,
        key="42",
        title="Test Show",
        media_type=ListMediaType.TV,
        total_units=12,
    )
    entry.status = ListStatus.CURRENT
    entry.progress = 6
    entry.repeats = 1
    entry.review = "So far so good"
    entry.user_rating = 80
    entry.started_at = datetime(2025, 1, 1, tzinfo=UTC)
    entry.finished_at = None
    return cast(ListEntry, entry)


def test_item_identifier_from_episode_includes_parent_metadata() -> None:
    """Episode identifiers include parent/season metadata for history tracking."""
    show = FakeLibraryShow(key="show-1", title="Show")
    season = FakeLibrarySeason(key="season-1", title="S1", index=1, show=show)
    episode = FakeLibraryEpisode(
        key="ep-1",
        title="Episode 1",
        index=1,
        season_index=1,
        show=show,
        season=season,
    )
    show.attach_children(episodes=[episode], seasons=[season])

    identifier = ItemIdentifier.from_item(cast(LibraryEntry, episode))

    assert identifier.key == "ep-1"
    assert identifier.media_kind == episode.media_kind


def test_entry_snapshot_round_trip(list_entry: ListEntry) -> None:
    """Snapshots capture list entry state and serialize to JSON primitives."""
    snapshot = EntrySnapshot.from_entry(list_entry)
    as_dict = snapshot.to_dict()
    assert as_dict["media_key"] == "42"
    assert as_dict["status"] == ListStatus.CURRENT
    assert as_dict["progress"] == 6

    serialized = snapshot.serialize()
    assert serialized["started_at"] == "2025-01-01T00:00:00+00:00"
    assert serialized["finished_at"] is None

    reconstructed = EntrySnapshot.from_dict(serialized)
    assert reconstructed.status == ListStatus.CURRENT
    assert reconstructed.progress == 6


def test_sync_stats_tracking_and_counts() -> None:
    """SyncStats tracks per-item outcomes and aggregates counts/coverage."""
    pending_item = ItemIdentifier(
        key="pending",
        media_kind=MediaKind.MOVIE,
        repr="Pending Movie",
    )
    synced_item = ItemIdentifier(
        key="synced",
        media_kind=MediaKind.MOVIE,
        repr="Synced Movie",
    )
    stats = SyncStats()
    stats.register_pending_items([pending_item])
    stats.track_item(synced_item, SyncOutcome.SYNCED)
    stats.track_items([pending_item], SyncOutcome.SKIPPED)
    stats.track_item(
        ItemIdentifier(key="episode", media_kind=MediaKind.EPISODE, repr="An Episode"),
        SyncOutcome.SYNCED,
    )

    assert stats.synced == 1
    assert stats.skipped == 1
    assert stats.pending == 0
    assert stats.total_items == 2
    assert stats.coverage == 1.0


def test_sync_stats_untrack_items() -> None:
    """Untracking removes entries from outcome maps."""
    stats = SyncStats()
    movie = ItemIdentifier(key="1", media_kind=MediaKind.MOVIE, repr="Movie")
    show = ItemIdentifier(key="2", media_kind=MediaKind.SHOW, repr="Show")
    stats.track_items([movie, show], SyncOutcome.FAILED)

    stats.untrack_item(movie)
    stats.untrack_items([show])

    assert stats.total_items == 0


def test_sync_stats_get_items_by_outcome_filters_types() -> None:
    """Grandchild items are excluded from top-level item queries."""
    stats = SyncStats()
    show_item = ItemIdentifier(key="show", media_kind=MediaKind.SHOW, repr="Show")
    episode_item = ItemIdentifier(
        key="episode",
        media_kind=MediaKind.EPISODE,
        repr="E1",
    )
    stats.track_item(show_item, SyncOutcome.SYNCED)
    stats.track_item(episode_item, SyncOutcome.SYNCED)

    top_level = stats.get_items_by_outcome()
    assert show_item in top_level
    assert episode_item not in top_level


def test_sync_stats_not_found_and_total_processed() -> None:
    """Derived properties aggregate multiple outcomes."""
    stats = SyncStats()
    stats.track_item(
        ItemIdentifier(key="nf", media_kind=MediaKind.MOVIE, repr="Missing"),
        SyncOutcome.NOT_FOUND,
    )
    stats.track_item(
        ItemIdentifier(key="fail", media_kind=MediaKind.MOVIE, repr="Fail"),
        SyncOutcome.FAILED,
    )

    assert stats.not_found == 1
    assert stats.total_processed == 2


def test_sync_stats_coverage_handles_no_grandchildren() -> None:
    """When no episodes are tracked, coverage defaults to 100%."""
    stats = SyncStats()
    assert stats.coverage == 1.0


def test_sync_stats_combine_merges_maps() -> None:
    """Combining stats merges the tracked outcome dictionaries."""
    stats_a = SyncStats()
    stats_b = SyncStats()
    item_a = ItemIdentifier(key="a", media_kind=MediaKind.MOVIE, repr="A")
    item_b = ItemIdentifier(key="b", media_kind=MediaKind.MOVIE, repr="B")
    stats_a.track_item(item_a, SyncOutcome.FAILED)
    stats_b.track_item(item_b, SyncOutcome.DELETED)

    combined = stats_a + stats_b

    assert combined.failed == 1
    assert combined.deleted == 1
    assert combined.total_items == 2
