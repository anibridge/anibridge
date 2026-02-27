"""Test doubles for sync client unit tests."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from anibridge.library import (
    HistoryEntry,
    LibraryEpisode,
    LibraryMovie,
    LibrarySeason,
    LibraryShow,
    MediaKind,
)
from anibridge.list import ListMediaType, ListStatus, ListTarget
from anibridge.utils.types import MappingDescriptor

from anibridge.app.core.animap import AnimapEdge


class FakeLibraryProvider:
    """Minimal library provider stub used by fake media entities."""

    NAMESPACE = "_fake-library"

    async def initialize(self) -> None:
        """No-op setup hook to satisfy the protocol."""

    def user(self) -> None:
        """Return `None` to keep the stub lightweight."""
        return None

    def mapping_descriptors(self, media: Any) -> list[MappingDescriptor]:
        """Return mapping descriptors declared on the media instance."""
        return list(media._mapping_descriptors or [])

    async def clear_cache(self) -> None:
        """No-op cache clear hook."""

    async def close(self) -> None:
        """No-op close hook."""


@dataclass
class FakeSection:
    """Simple section that satisfies the LibrarySection protocol."""

    key: str
    title: str = "Test Section"
    media_kind: MediaKind = MediaKind.SHOW
    _provider: FakeLibraryProvider = field(default_factory=FakeLibraryProvider)

    def provider(self) -> FakeLibraryProvider:
        """Return the provider that owns this fake section."""
        return self._provider


class FakeLibraryMediaBase:
    """Base implementation shared by fake library entities."""

    def __hash__(self) -> int:
        """Stable identity hash used by caching helpers."""
        return id(self)

    @property
    def key(self) -> str:
        return getattr(self, "_key", "")

    @key.setter
    def key(self, value: str) -> None:
        self._key = value

    @property
    def title(self) -> str:
        return getattr(self, "_title", "")

    @title.setter
    def title(self, value: str) -> None:
        self._title = value

    @property
    def media_kind(self) -> MediaKind:
        return self._media_kind

    @media_kind.setter
    def media_kind(self, value: MediaKind) -> None:
        self._media_kind = value

    def __init__(
        self,
        *,
        key: str,
        title: str,
        media_kind: MediaKind,
        on_watching: bool = False,
        on_watchlist: bool = False,
        user_rating: int | None = None,
        view_count: int = 0,
        ids: dict[str, str] | None = None,
        history: Sequence[HistoryEntry] | None = None,
        review: str | None = None,
        section: FakeSection | None = None,
        mapping_descriptors: Sequence[MappingDescriptor] | None = None,
    ) -> None:
        """Store common media attributes shared across fake entities."""
        self.key = key
        self.title = title
        self.media_kind = media_kind
        self._on_watching = on_watching
        self._on_watchlist = on_watchlist
        self._user_rating = user_rating
        self._view_count = view_count
        self._legacy_ids = dict(ids or {})
        self._history = list(history or [])
        self._review = review
        self._section = section or FakeSection(key="section-1")
        self._provider = FakeLibraryProvider()
        self._poster_image: str | None = None
        if mapping_descriptors:
            self._mapping_descriptors = tuple(mapping_descriptors)
        elif ids:
            self._mapping_descriptors = tuple(
                (provider, str(entry_id), None) for provider, entry_id in ids.items()
            )
        else:
            self._mapping_descriptors = None

    def provider(self) -> FakeLibraryProvider:
        """Return the provider that owns this fake media item."""
        return self._provider

    @property
    def on_watching(self) -> bool:
        """Return whether the item is currently being watched."""
        return self._on_watching

    @property
    def on_watchlist(self) -> bool:
        """Return whether the item is on the watchlist."""
        return self._on_watchlist

    @property
    def poster_image(self) -> str | None:
        """Return the poster image URL."""
        return self._poster_image

    @property
    def user_rating(self) -> int | None:
        """Return the user rating."""
        return self._user_rating

    @property
    def view_count(self) -> int:
        """Return the view count."""
        return self._view_count

    async def history(self) -> list[HistoryEntry]:
        """Return the watch history entries for this item."""
        return list(self._history)

    def media(self) -> FakeLibraryMediaBase:
        """Return a provider-native media object."""
        return self

    @property
    def review(self):
        """Return an awaitable user review for this item."""

        async def _review() -> str | None:
            return self._review

        return _review()

    def mapping_descriptors(self) -> list[MappingDescriptor]:
        """Return mapping descriptors for this item."""
        return list(self._mapping_descriptors or [])

    def section(self) -> FakeSection:
        """Return the section this item belongs to."""
        return self._section


class FakeLibraryMovie(FakeLibraryMediaBase, LibraryMovie):
    """Concrete movie stub."""

    def __init__(self, *, key: str, title: str, **kwargs: Any) -> None:
        """Initialize the fake movie with base media attributes."""
        super().__init__(key=key, title=title, media_kind=MediaKind.MOVIE, **kwargs)


class FakeLibraryShow(FakeLibraryMediaBase, LibraryShow):
    """Concrete show stub."""

    def __init__(
        self,
        *,
        key: str,
        title: str,
        episodes: Sequence[FakeLibraryEpisode] | None = None,
        seasons: Sequence[FakeLibrarySeason] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the fake show with base media attributes."""
        super().__init__(key=key, title=title, media_kind=MediaKind.SHOW, **kwargs)
        self._episodes: list[FakeLibraryEpisode] = list(episodes or [])
        self._seasons: list[FakeLibrarySeason] = list(seasons or [])

    def episodes(self) -> list[FakeLibraryEpisode]:
        """Return all episodes belonging to this show."""
        return list(self._episodes)

    def seasons(self) -> list[FakeLibrarySeason]:
        """Return all seasons belonging to this show."""
        return list(self._seasons)

    def attach_children(
        self,
        *,
        episodes: Sequence[FakeLibraryEpisode],
        seasons: Sequence[FakeLibrarySeason],
    ) -> None:
        """Attach child episodes and seasons to the show."""
        self._episodes = list(episodes)
        self._seasons = list(seasons)


class FakeLibrarySeason(FakeLibraryMediaBase, LibrarySeason):
    """Concrete season stub referencing parent show."""

    def __init__(
        self,
        *,
        key: str,
        title: str,
        index: int,
        show: FakeLibraryShow,
        episodes: Sequence[FakeLibraryEpisode] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the fake season with base media attributes."""
        super().__init__(key=key, title=title, media_kind=MediaKind.SEASON, **kwargs)
        self.index = index
        self._show = show
        self._episodes = list(episodes or [])

    def episodes(self) -> list[FakeLibraryEpisode]:
        """Return all episodes belonging to this season."""
        return list(self._episodes)

    def show(self) -> FakeLibraryShow:
        """Return the parent show of this season."""
        return self._show


class FakeLibraryEpisode(FakeLibraryMediaBase, LibraryEpisode):
    """Concrete episode stub referencing parent show and season."""

    def __init__(
        self,
        *,
        key: str,
        title: str,
        index: int,
        season_index: int,
        show: FakeLibraryShow,
        season: FakeLibrarySeason,
        **kwargs: Any,
    ) -> None:
        """Initialize the fake episode with base media attributes."""
        super().__init__(key=key, title=title, media_kind=MediaKind.EPISODE, **kwargs)
        self.index = index
        self.season_index = season_index
        self._show = show
        self._season = season

    def show(self) -> FakeLibraryShow:
        """Return the parent show of this episode."""
        return self._show

    def season(self) -> FakeLibrarySeason:
        """Return the parent season of this episode."""
        return self._season


class FakeListProvider:
    """Minimal ListProvider stub."""

    NAMESPACE = "_fake-list"
    MAPPING_PROVIDERS = frozenset({"anilist", "tmdb", "_fake-mapping"})

    def __init__(self) -> None:
        """Initialize the provider with tracking storage."""
        self.entries: dict[str, FakeListEntry] = {}
        self.search_results: list[FakeListEntry] = []
        self.updated_entries: list[tuple[str, FakeListEntry]] = []
        self.batch_updates: list[list[FakeListEntry]] = []
        self.deleted_keys: list[str] = []
        self.derived_keys: list[str] = []
        self.resolved_targets: dict[MappingDescriptor, list[str]] = {}

    def mapping_providers(self) -> frozenset[str]:
        """Return the mapping providers supported by this fake provider."""
        return self.MAPPING_PROVIDERS

    async def get_entry(self, key: str) -> FakeListEntry | None:
        """Return the entry for the given key, or None if not found."""
        return self.entries.get(key)

    async def derive_keys(self, descriptors: Sequence[MappingDescriptor]) -> set[str]:
        """Resolve provider keys from mapping descriptors."""
        if not descriptors or not self.derived_keys:
            return set()
        return set(self.derived_keys)

    async def resolve_mapping_descriptors(
        self, descriptors: Sequence[MappingDescriptor]
    ) -> Sequence[ListTarget]:
        """Resolve descriptors into list keys for testing."""
        if not descriptors:
            return []
        if self.resolved_targets:
            results: list[ListTarget] = []
            for descriptor in descriptors:
                for media_key in self.resolved_targets.get(descriptor, []):
                    results.append(
                        ListTarget(descriptor=descriptor, media_key=media_key)
                    )
            return results
        if not self.derived_keys:
            return []
        return [
            ListTarget(descriptor=(provider, entry_id, scope), media_key=media_key)
            for provider, entry_id, scope in descriptors
            if provider in self.MAPPING_PROVIDERS
            for media_key in self.derived_keys
        ]

    async def update_entry(self, key: str, entry: FakeListEntry) -> FakeListEntry:
        """Record the updated entry and return it."""
        self.updated_entries.append((key, entry))
        return entry

    async def update_entries_batch(
        self, entries: Sequence[FakeListEntry]
    ) -> Sequence[FakeListEntry]:
        """Record the batch update and return the entries."""
        self.batch_updates.append(list(entries))
        return entries

    async def delete_entry(self, key: str) -> None:
        """Record the deleted entry key."""
        self.deleted_keys.append(key)

    async def search(self, query: str) -> Sequence[FakeListEntry]:
        """Return the pre-seeded search results."""
        return list(self.search_results)


class FakeListMedia:
    """Simple list media implementation for FakeListEntry."""

    def __init__(
        self,
        *,
        provider: FakeListProvider,
        key: str,
        title: str,
        media_type: ListMediaType,
        total_units: int | None = None,
    ) -> None:
        """Initialize the fake list media with basic attributes."""
        self.key = key
        self.title = title
        self._provider = provider
        self.media_type = media_type
        self.poster_image = None
        self.total_units = total_units

    def provider(self) -> FakeListProvider:
        """Return the provider that owns this fake list media."""
        return self._provider


class FakeListEntry:
    """List entry implementation exposing mutable fields for testing."""

    def __init__(
        self,
        *,
        provider: FakeListProvider,
        key: str,
        title: str,
        media_type: ListMediaType,
        total_units: int | None = None,
    ) -> None:
        """Initialize the fake list entry with basic attributes."""
        self.key = key
        self.title = title
        self._provider = provider
        self._media = FakeListMedia(
            provider=provider,
            key=key,
            title=title,
            media_type=media_type,
            total_units=total_units,
        )
        self._status: ListStatus | None = None
        self._progress: int | None = None
        self._repeats: int | None = None
        self._review: str | None = None
        self._user_rating: int | None = None
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self.total_units: int | None = total_units

    def provider(self) -> FakeListProvider:
        """Return the provider that owns this fake list entry."""
        return self._provider

    def media(self) -> FakeListMedia:
        """Return the provider-native media object."""
        return self._media

    @property
    def status(self) -> ListStatus | None:
        """Return the status of the list entry."""
        return self._status

    @status.setter
    def status(self, value: ListStatus | None) -> None:
        self._status = value

    @property
    def progress(self) -> int | None:
        """Return the progress of the list entry."""
        return self._progress

    @progress.setter
    def progress(self, value: int | None) -> None:
        self._progress = value

    @property
    def repeats(self) -> int | None:
        """Return the number of repeats for the list entry."""
        return self._repeats

    @repeats.setter
    def repeats(self, value: int | None) -> None:
        self._repeats = value

    @property
    def review(self) -> str | None:
        """Return the review of the list entry."""
        return self._review

    @review.setter
    def review(self, value: str | None) -> None:
        self._review = value

    @property
    def user_rating(self) -> int | None:
        """Return the user rating of the list entry."""
        return self._user_rating

    @user_rating.setter
    def user_rating(self, value: int | None) -> None:
        self._user_rating = value

    @property
    def started_at(self) -> datetime | None:
        """Return the start time of the list entry."""
        return self._started_at

    @started_at.setter
    def started_at(self, value: datetime | None) -> None:
        self._started_at = value

    @property
    def finished_at(self) -> datetime | None:
        """Return the finish time of the list entry."""
        return self._finished_at

    @finished_at.setter
    def finished_at(self, value: datetime | None) -> None:
        self._finished_at = value

    @property
    def total_units(self) -> int | None:
        """Return or set the total units on the provider-native media object."""
        return getattr(self._media, "total_units", None)

    @total_units.setter
    def total_units(self, value: int | None) -> None:
        self._media.total_units = value


class FakeAnimapClient:
    """Stub that resolves mapping descriptors to a configured set."""

    def __init__(self, resolved: Sequence[MappingDescriptor] | None = None) -> None:
        self._resolved = tuple(resolved or [])

    def resolve_target_descriptors(
        self,
        descriptors: Sequence[MappingDescriptor],
    ) -> tuple[MappingDescriptor, ...]:
        return self._resolved

    def resolve_edges(
        self,
        descriptors: Sequence[MappingDescriptor],
        *,
        target_providers: set[str] | frozenset[str] | None = None,
    ) -> tuple[AnimapEdge, ...]:
        return tuple()

    def resolve_edges_grouped(
        self,
        descriptors: Sequence[MappingDescriptor],
        *,
        target_providers: set[str] | frozenset[str] | None = None,
    ) -> dict[MappingDescriptor, dict[MappingDescriptor, list[str]]]:
        return {}


def make_history_entry(key: str, *, ts: datetime) -> HistoryEntry:
    """Helper for constructing timezone-aware history entries."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return HistoryEntry(library_key=key, viewed_at=ts)
