"""Unit tests for provider-contract sync stats components."""

from datetime import date

import msgspec
from anibridge.provider.base import (
    FacetName,
    Node,
    Part,
    Progress,
    Rating,
    Record,
    RecordField,
    Ref,
    State,
    Status,
    Step,
    Structure,
)

from anibridge.app.core.sync.stats import RecordSnapshot, SyncItem, SyncStats
from anibridge.app.models.db.sync_history import SyncOutcome


def test_item_identifier_from_record_parts_falls_back_to_record_ref() -> None:
    """Record identifiers should render the anchor as the first path segment."""
    identifier = SyncItem.from_record_parts(
        namespace="source",
        node=Node(ref=Ref.anchor("node1"), kind="anime", title="Node One"),
        record=Record(ref=Ref.anchor("record1"), surface="user_state"),
    )[0]

    assert identifier.namespace == "source"
    assert identifier.ref == Ref.anchor("record1")
    assert repr(identifier) == '<source:anime=record1 "Node One">'


def test_item_identifier_from_record_parts_uses_tracker_format() -> None:
    """Coverage part identifiers should include namespace, path, and title."""
    identifiers = SyncItem.from_record_parts(
        namespace="plex",
        node=Node(
            ref=Ref.anchor("12345"),
            kind="show",
            title='Cowboy "Bebop"',
            facets={
                FacetName.STRUCTURE: Structure(
                    axes=("season", "episode"),
                    parts=(
                        Part(
                            position=(Step("season", 1), Step("episode", 4)),
                            title="Gateway Shuffle",
                        ),
                    ),
                ),
            },
        ),
        record=Record(
            ref=Ref.anchor("12345"),
            surface="user_state",
            values={RecordField.PROGRESS: Progress(current=4, total=26)},
        ),
    )

    assert identifiers == (
        SyncItem(
            namespace="plex",
            ref=Ref.at("12345", ("season", 1), ("episode", 4)),
            repr=(
                "<plex:show=12345/season=1/episode=4 "
                '"Cowboy \\"Bebop\\" - Gateway Shuffle">'
            ),
        ),
    )
    assert repr(identifiers[0]) == (
        '<plex:show=12345/season=1/episode=4 "Cowboy \\"Bebop\\" - Gateway Shuffle">'
    )


def test_sync_item_identity_ignores_record_channel_and_display_label() -> None:
    """Sync item tracking should collapse to provider namespace and ref."""
    first = SyncItem(
        namespace="source",
        ref=Ref.anchor("same"),
        repr='<source:show=same "First">',
    )
    second = SyncItem(
        namespace="source",
        ref=Ref.anchor("same"),
        repr='<source:show=same "Second">',
    )

    stats = SyncStats()
    stats.register_pending_items([first])
    stats.track_item(second, SyncOutcome.SYNCED)

    assert first == second
    assert stats.synced == 1
    assert stats.count() == 1


def test_record_snapshot_stores_restorable_values_and_display_view() -> None:
    """Snapshots should preserve values for restore and expose display data."""
    snapshot = RecordSnapshot.from_record(
        Record(
            ref=Ref.anchor("a"),
            surface="user_state",
            key="record-key",
            values={
                RecordField.STATUS: State(native="watching", status=Status.ACTIVE),
                RecordField.PROGRESS: Progress(current=3, total=12),
                RecordField.RATING: Rating(8, (0, 10, 1)),
                RecordField.STARTED_AT: date(2026, 1, 2),
                RecordField.NOTES: "solid",
            },
        )
    )

    assert snapshot.key == "record-key"
    assert snapshot.values_for_display() == {
        "status": "active",
        "progress": 3,
        "rating": 8,
        "started_at": date(2026, 1, 2),
        "notes": "solid",
    }
    assert snapshot.values_for_restore()[RecordField.PROGRESS] == Progress(
        current=3,
        total=12,
    )
    decoded = msgspec.convert(msgspec.to_builtins(snapshot), type=RecordSnapshot)
    assert decoded.values_for_restore() == snapshot.values_for_restore()


def test_sync_stats_tracking_and_coverage() -> None:
    """SyncStats tracks per-ref outcomes and aggregates counts."""
    pending = SyncItem(
        namespace="source",
        ref=Ref.anchor("pending"),
        repr="Pending",
    )
    synced = SyncItem(
        namespace="source",
        ref=Ref.anchor("synced"),
        repr="Synced",
    )
    failed = SyncItem(
        namespace="source",
        ref=Ref.anchor("failed"),
        repr="Failed",
    )
    stats = SyncStats()
    stats.register_pending_items([pending, synced, failed])
    stats.track_item(synced, SyncOutcome.SYNCED)
    stats.track_item(failed, SyncOutcome.FAILED)

    assert stats.synced == 1
    assert stats.failed == 1
    assert stats.count(SyncOutcome.PENDING) == 1
    assert stats.items(SyncOutcome.FAILED) == [failed]
    assert stats.coverage == 1 / 3


def test_sync_stats_coverage_defaults_to_complete_when_empty() -> None:
    """Empty sync runs should report complete coverage."""
    assert SyncStats().coverage == 1.0
