"""Synchronization statistics and tracking module."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from anibridge.library import LibraryEntry, MediaKind
from anibridge.list import ListEntry, ListStatus
from anibridge.utils.types import MappingDescriptor
from pydantic import BaseModel

from anibridge.app.models.db.sync_history import SyncOutcome

__all__ = [
    "BatchUpdate",
    "EntrySnapshot",
    "ItemIdentifier",
    "SyncProgress",
    "SyncStats",
]


@dataclass(slots=True)
class ItemIdentifier:
    """Immutable identifier for media items in sync operations.

    Provides a stable, hashable way to identify media items across
    the sync process without relying on fragile string representations.
    """

    key: str
    media_kind: MediaKind
    repr: str

    @classmethod
    def from_item(cls, item: LibraryEntry) -> ItemIdentifier:
        """Create an identifier from a library media entity."""
        kwargs: dict[str, Any] = {
            "key": item.key,
            "media_kind": item.media_kind,
            "repr": f"{item!r}",
        }
        return cls(**kwargs)

    @classmethod
    def from_items(cls, items: Sequence[LibraryEntry]) -> Sequence[ItemIdentifier]:
        """Create ItemIdentifiers from a sequence of library media objects.

        Args:
            items (Sequence[LibraryEntry]): List of library media objects

        Returns:
            Sequence[ItemIdentifier]: List of identifiers for the media items
        """
        return [cls.from_item(item) for item in items]

    def __hash__(self) -> int:
        """Generate a hash for the ItemIdentifier instance.

        Returns:
            int: Hash value of the instance
        """
        return hash(self.key)

    def __repr__(self) -> str:
        """Generate a string representation of the ItemIdentifier instance.

        Returns:
            str: String representation of the instance
        """
        if self.repr:
            return self.repr
        return super().__repr__()


class SyncStats(BaseModel):
    """Statistics tracker for synchronization operations.

    Uses an outcome-based approach where each item is tracked with its specific
    result, allowing for accurate reporting and easier debugging.
    """

    _item_outcomes: dict[ItemIdentifier, SyncOutcome] = {}

    def track_item(self, item_id: ItemIdentifier, outcome: SyncOutcome) -> None:
        """Track the outcome for a specific item.

        Args:
            item_id (ItemIdentifier): Identifier for the media item
            outcome (SyncOutcome): The synchronization outcome for this item
        """
        self._item_outcomes[item_id] = outcome

    def track_items(
        self, item_ids: Sequence[ItemIdentifier], outcome: SyncOutcome
    ) -> None:
        """Track the same outcome for multiple items.

        Args:
            item_ids (list[ItemIdentifier]): List of item identifiers
            outcome (SyncOutcome): The synchronization outcome for these items
        """
        for item_id in item_ids:
            self.track_item(item_id, outcome)

    def untrack_item(self, item_id: ItemIdentifier) -> None:
        """Remove an item from tracking.

        This is useful if an item was registered but later determined to be
        irrelevant or not part of the sync process.

        Args:
            item_id (ItemIdentifier): Identifier for the media item to untrack
        """
        if item_id in self._item_outcomes:
            del self._item_outcomes[item_id]

    def untrack_items(self, item_ids: Sequence[ItemIdentifier]) -> None:
        """Remove multiple items from tracking.

        Args:
            item_ids (Sequence[ItemIdentifier]): List of item identifiers to untrack
        """
        for item_id in item_ids:
            self.untrack_item(item_id)

    def filter_tracked_items(
        self, item_ids: Sequence[ItemIdentifier]
    ) -> list[ItemIdentifier]:
        """Return the subset of items that are still tracked."""
        return [item_id for item_id in item_ids if item_id in self._item_outcomes]

    def register_pending_items(self, item_ids: Sequence[ItemIdentifier]) -> None:
        """Register items as pending processing.

        This should be called at the start of processing to ensure all items
        that should be processed are tracked.

        Args:
            item_ids (Sequence[ItemIdentifier]): List of item identifiers to register
        """
        for item_id in item_ids:
            if item_id not in self._item_outcomes:
                self._item_outcomes[item_id] = SyncOutcome.PENDING

    def count_items_by_outcome(self, *outcomes: SyncOutcome) -> int:
        """Count top-level items matching the requested outcome filter."""
        allowed_outcomes = set(outcomes)
        return sum(
            1
            for item_id, item_outcome in self._item_outcomes.items()
            if item_id.media_kind in (MediaKind.SHOW, MediaKind.MOVIE)
            and (not allowed_outcomes or item_outcome in allowed_outcomes)
        )

    def get_items_by_outcome(self, *outcomes: SyncOutcome) -> list[ItemIdentifier]:
        """Get all items that had a specific outcome.

        Args:
            outcomes (SyncOutcome): One or more outcomes to filter by

        Returns:
            list[ItemIdentifier]: Items with the specified outcome(s)
        """
        if not outcomes:
            return [
                item_id
                for item_id in self._item_outcomes
                if item_id.media_kind in (MediaKind.SHOW, MediaKind.MOVIE)
            ]
        return [
            item_id
            for item_id, item_outcome in self._item_outcomes.items()
            if item_outcome in outcomes
            and item_id.media_kind in (MediaKind.SHOW, MediaKind.MOVIE)
        ]

    def count_grandchild_items_by_outcome(self, *outcomes: SyncOutcome) -> int:
        """Count grandchild items matching the requested outcome filter."""
        allowed_outcomes = set(outcomes)
        return sum(
            1
            for item_id, item_outcome in self._item_outcomes.items()
            if item_id.media_kind in (MediaKind.EPISODE, MediaKind.MOVIE)
            and (not allowed_outcomes or item_outcome in allowed_outcomes)
        )

    def get_grandchild_items_by_outcome(
        self, *outcome: SyncOutcome
    ) -> list[ItemIdentifier]:
        """Get all grandchild items (episodes/movies) that had a specific outcome.

        Args:
            outcome (SyncOutcome): One or more outcomes to filter by

        Returns:
            list[ItemIdentifier]: Grandchild items with the specified outcome(s)
        """
        if not outcome:
            return [
                item_id
                for item_id in self._item_outcomes
                if item_id.media_kind in (MediaKind.EPISODE, MediaKind.MOVIE)
            ]
        return [
            item_id
            for item_id, item_outcome in self._item_outcomes.items()
            if item_outcome in outcome
            and item_id.media_kind in (MediaKind.EPISODE, MediaKind.MOVIE)
        ]

    @property
    def synced(self) -> int:
        """Number of successfully synced items (including deleted)."""
        return self.count_items_by_outcome(SyncOutcome.SYNCED)

    @property
    def deleted(self) -> int:
        """Number of items deleted from AniList."""
        return self.count_items_by_outcome(SyncOutcome.DELETED)

    @property
    def skipped(self) -> int:
        """Number of items skipped (no changes needed)."""
        return self.count_items_by_outcome(SyncOutcome.SKIPPED)

    @property
    def not_found(self) -> int:
        """Number of items where no matching AniList entry was found."""
        return self.count_items_by_outcome(SyncOutcome.NOT_FOUND)

    @property
    def failed(self) -> int:
        """Number of items that failed to process."""
        return self.count_items_by_outcome(SyncOutcome.FAILED)

    @property
    def pending(self) -> int:
        """Number of items that are still pending processing."""
        return self.count_items_by_outcome(SyncOutcome.PENDING)

    @property
    def total_processed(self) -> int:
        """Total number of items processed (excluding pending)."""
        return self.count_items_by_outcome(
            SyncOutcome.SYNCED,
            SyncOutcome.SKIPPED,
            SyncOutcome.FAILED,
            SyncOutcome.NOT_FOUND,
            SyncOutcome.DELETED,
        )

    @property
    def total_items(self) -> int:
        """Total number of items tracked (including unprocessed)."""
        return self.count_items_by_outcome()

    @property
    def coverage(self) -> float:
        """Percentage of grandchild items that were successfully processed."""
        total = self.count_grandchild_items_by_outcome()
        if not total:
            return 1.0

        processed = self.count_grandchild_items_by_outcome(
            SyncOutcome.SYNCED,
            SyncOutcome.SKIPPED,
            SyncOutcome.DELETED,
        )

        return processed / total

    def combine(self, other: SyncStats) -> SyncStats:
        """Combine this stats instance with another.

        Args:
            other (SyncStats): Another SyncStats instance to combine with

        Returns:
            SyncStats: New instance with combined statistics
        """
        combined = SyncStats()
        combined._item_outcomes = {**self._item_outcomes, **other._item_outcomes}
        return combined

    def __add__(self, other: SyncStats) -> SyncStats:
        """Combine statistics using the + operator."""
        return self.combine(other)


class SyncProgress(BaseModel):
    """Live sync progress snapshot exposed to the web UI.

    Fields are serialized to JSON in scheduler status responses.
    """

    state: Literal["running", "idle"]
    started_at: datetime
    section_index: int
    section_count: int
    section_title: str | None
    stage: str
    section_items_total: int
    section_items_processed: int


@dataclass(slots=True)
class EntrySnapshot:
    """Snapshot of list entry fields used for comparison and history."""

    media_key: str
    status: ListStatus | None
    progress: int | None
    repeats: int | None
    review: str | None
    user_rating: int | None
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_entry(cls, entry: ListEntry) -> EntrySnapshot:
        """Create a snapshot from a list entry."""
        return cls(
            media_key=entry.media().key,
            status=entry.status,
            progress=entry.progress,
            repeats=entry.repeats,
            review=entry.review,
            user_rating=entry.user_rating,
            started_at=entry.started_at,
            finished_at=entry.finished_at,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntrySnapshot:
        """Create a snapshot from a raw dictionary."""
        return cls(
            media_key=data.get("media_key", ""),
            status=ListStatus(data["status"]) if data.get("status") else None,
            progress=data.get("progress"),
            repeats=data.get("repeats"),
            review=data.get("review"),
            user_rating=data.get("user_rating"),
            started_at=datetime.fromisoformat(data["started_at"])
            if data.get("started_at")
            else None,
            finished_at=datetime.fromisoformat(data["finished_at"])
            if data.get("finished_at")
            else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a raw dictionary representation."""
        return {
            "media_key": self.media_key,
            "status": self.status,
            "progress": self.progress,
            "repeats": self.repeats,
            "review": self.review,
            "user_rating": self.user_rating,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def serialize(self) -> dict[str, Any]:
        """Serialize values into JSON-friendly primitives."""

        def _serialize(value: Any) -> Any:
            if isinstance(value, datetime):
                dt = value
                dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
                return dt.isoformat()
            if isinstance(value, ListStatus):
                return value.value
            return value

        return {key: _serialize(value) for key, value in self.to_dict().items()}


@dataclass(slots=True)
class BatchUpdate[ParentMediaT: LibraryEntry, ChildMediaT: LibraryEntry]:
    """Container for deferred sync updates and associated metadata."""

    item: ParentMediaT
    child: ChildMediaT
    grandchildren: Sequence[LibraryEntry]
    before: EntrySnapshot | None
    after: EntrySnapshot
    entry: ListEntry
    source_entry: ListEntry | None
    list_media_key: str | None
    mapping_descriptors: tuple[MappingDescriptor, ...] = ()
    diagnostics: dict[str, str] = field(default_factory=dict)
