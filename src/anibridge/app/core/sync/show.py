"""Sync client for episodic shows using provider abstractions."""

from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime

from anibridge.library import HistoryEntry, LibraryEpisode, LibrarySeason, LibraryShow
from anibridge.list import ListEntry, ListMediaType, ListStatus
from anibridge.utils.cache import lru_cache
from anibridge.utils.mappings import (
    AnibridgeDescriptorMapping,
    descriptor_key,
)
from anibridge.utils.types import MappingDescriptor

from anibridge.app.core.sync.base import BaseSyncClient
from anibridge.app.core.sync.stats import ItemIdentifier
from anibridge.app.core.sync.targeting import (
    ResolvedListTarget,
    SyncTarget,
    find_best_search_result,
    resolve_list_targets_batch,
)

__all__ = ["ShowSyncClient"]


@dataclass(slots=True)
class _SeasonGroup:
    child_item: LibrarySeason
    first_index: int
    episodes: list[LibraryEpisode]
    entry: ListEntry
    media_key: str
    mapping_descriptors: dict[MappingDescriptor, None]
    mappings: dict[
        tuple[str, str, tuple[tuple[str, str], ...]],
        AnibridgeDescriptorMapping,
    ]


class ShowSyncClient(BaseSyncClient[LibraryShow, LibrarySeason, LibraryEpisode]):
    """Synchronize show items between a library provider and a list provider."""

    async def clear_cache(self) -> None:
        """Clear all sync client caches."""
        await super().clear_cache()
        self._calculate_progress.cache_clear()
        self.__get_wanted_episodes.cache_clear()
        self.__get_wanted_seasons.cache_clear()
        self._filter_history_by_episodes.cache_clear()

    async def map_media(
        self, item: LibraryShow
    ) -> AsyncIterator[
        tuple[
            LibrarySeason,
            Sequence[LibraryEpisode],
            SyncTarget,
        ]
    ]:
        """Yield mapping candidates for a show.

        Args:
            item (LibraryShow): Show whose seasons should be resolved.

        Yields:
            tuple[LibrarySeason, Sequence[LibraryEpisode], SyncTarget]:
                Season-level mapping candidates.
        """
        seasons = self.__get_wanted_seasons(item)
        if not seasons:
            return

        wanted_indexes = set(seasons)
        episodes_by_season: dict[int, list[LibraryEpisode]] = defaultdict(list)
        for ep in self.__get_wanted_episodes(item):
            if ep.season_index in wanted_indexes:
                episodes_by_season[ep.season_index].append(ep)

        season_payloads = [
            (
                season_index,
                season,
                season_episodes,
                (*season.mapping_descriptors(), *item.mapping_descriptors()),
            )
            for season_index in sorted(wanted_indexes)
            if (season_episodes := episodes_by_season.get(season_index))
            and (season := seasons[season_index])
        ]

        resolved_batches = await resolve_list_targets_batch(
            animap_client=self.animap_client,
            list_provider=self.list_provider,
            descriptor_sets=[payload[3] for payload in season_payloads],
        )

        groups: dict[str, _SeasonGroup] = {}

        for (season_index, season, season_episodes, _), resolved_targets in zip(
            season_payloads,
            resolved_batches,
            strict=False,
        ):
            viable_targets = self._resolve_active_targets(
                item=item,
                season=season,
                season_episodes=season_episodes,
                resolved_targets=resolved_targets,
            )
            self._untrack_skipped_episodes(
                season_episodes=season_episodes,
                viable_targets=viable_targets,
                resolved_targets=resolved_targets,
            )

            targets: list[
                tuple[ResolvedListTarget, ListEntry, list[LibraryEpisode]]
            ] = []
            for target, filtered in viable_targets:
                entry = await self._cache.get_entry(target.list_media_key)
                if entry is None:
                    continue
                targets.append((target, entry, filtered))

            should_search_fallback = not targets and (
                not resolved_targets or viable_targets
            )
            if should_search_fallback:
                entry = await self.search_media(item, season)
                if entry:
                    key = str(entry.media().key)
                    self._cache.cache_entry(entry)
                    targets = [
                        (
                            ResolvedListTarget(
                                list_media_key=key,
                                mapping_descriptors=(),
                                mappings=(),
                            ),
                            entry,
                            season_episodes,
                        )
                    ]

            if not targets:
                continue

            for target, entry, filtered in targets:
                media_key = entry.media().key
                if (group := groups.get(media_key)) is None:
                    groups[media_key] = _SeasonGroup(
                        child_item=season,
                        first_index=season_index,
                        episodes=filtered,
                        entry=entry,
                        media_key=media_key,
                        mapping_descriptors=dict.fromkeys(target.mapping_descriptors),
                        mappings={
                            self._mapping_signature(mapping_rule): mapping_rule
                            for mapping_rule in target.mappings
                        },
                    )
                    continue

                if season_index < group.first_index:
                    group.child_item, group.first_index = season, season_index
                group.episodes.extend(filtered)
                group.entry = entry
                for descriptor in target.mapping_descriptors:
                    group.mapping_descriptors.setdefault(descriptor, None)
                for mapping_rule in target.mappings:
                    group.mappings.setdefault(
                        self._mapping_signature(mapping_rule),
                        mapping_rule,
                    )

        for group in sorted(groups.values(), key=lambda g: g.first_index):
            eps = sorted(group.episodes, key=lambda ep: (ep.season_index, ep.index))
            yield (
                group.child_item,
                tuple(eps),
                SyncTarget(
                    list_media_key=group.media_key,
                    entry=group.entry,
                    mapping_descriptors=tuple(group.mapping_descriptors),
                    mappings=tuple(group.mappings.values()),
                ),
            )

    @staticmethod
    def _mapping_signature(
        mapping: AnibridgeDescriptorMapping,
    ) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        """Return a stable key for deduplicating descriptor mappings."""
        return (
            descriptor_key(mapping.source),
            descriptor_key(mapping.target),
            tuple(
                (mapping_entry.source_key, mapping_entry.target_value)
                for mapping_entry in mapping.mappings
            ),
        )

    async def search_media(
        self, item: LibraryShow, child_item: LibrarySeason
    ) -> ListEntry | None:
        """Locate a fallback list entry for a season.

        Args:
            item (LibraryShow): Parent show being synchronized.
            child_item (LibrarySeason): Season used to narrow fallback candidates.

        Returns:
            ListEntry | None: Matching list entry, if one meets the threshold.
        """
        if self.search_fallback_threshold < 0 or child_item.index == 0:
            return None

        results = await self.list_provider.search(item.title)
        episode_count = len(child_item.episodes())
        tv_results: list[ListEntry] = []
        filtered: list[ListEntry] = []
        for entry in results:
            media = entry.media()
            if media.media_type != ListMediaType.TV:
                continue
            tv_results.append(entry)
            if media.total_units is None or media.total_units == episode_count:
                filtered.append(entry)
        candidates = filtered or tv_results
        return find_best_search_result(
            item.title,
            candidates,
            self.search_fallback_threshold,
        )

    def _filter_episodes_by_ranges(
        self,
        episodes: Sequence[LibraryEpisode],
        mappings: Sequence[AnibridgeDescriptorMapping],
    ) -> list[LibraryEpisode]:
        """Filter episodes using source mapping ranges."""
        if not mappings:
            return list(episodes)

        all_source_ranges = [
            mapping_entry.source_range
            for descriptor_mapping in mappings
            for mapping_entry in descriptor_mapping.mappings
            if mapping_entry.target_ratio != 0
        ]
        if not all_source_ranges:
            return []
        return [
            episode
            for episode in episodes
            if any(
                source_range.contains(episode.index)
                for source_range in all_source_ranges
            )
        ]

    async def _get_all_trackable_items(
        self, item: LibraryShow
    ) -> Sequence[ItemIdentifier]:
        episodes = self.__get_wanted_episodes(item)
        if not episodes:
            return []
        return ItemIdentifier.from_items(episodes)

    async def _collect_prefetch_keys(self, item: LibraryShow) -> Sequence[str]:
        seasons = self.__get_wanted_seasons(item)
        if not seasons:
            return []
        wanted_indexes = sorted(seasons)
        episodes_by_season: dict[int, list[LibraryEpisode]] = defaultdict(list)
        for episode in self.__get_wanted_episodes(item):
            if episode.season_index in seasons:
                episodes_by_season[episode.season_index].append(episode)

        season_payloads = [
            (
                season_index,
                seasons[season_index],
                episodes_by_season[season_index],
                (
                    *seasons[season_index].mapping_descriptors(),
                    *item.mapping_descriptors(),
                ),
            )
            for season_index in wanted_indexes
            if episodes_by_season.get(season_index)
        ]
        if not season_payloads:
            return []

        resolved_batches = await resolve_list_targets_batch(
            animap_client=self.animap_client,
            list_provider=self.list_provider,
            descriptor_sets=[payload[3] for payload in season_payloads],
        )
        collected = {
            str(target.list_media_key)
            for (_, season, season_episodes, _), targets in zip(
                season_payloads,
                resolved_batches,
                strict=False,
            )
            for target, _ in self._resolve_active_targets(
                item=item,
                season=season,
                season_episodes=season_episodes,
                resolved_targets=targets,
            )
        }
        return tuple(sorted(collected))

    def _resolve_active_targets(
        self,
        *,
        item: LibraryShow,
        season: LibrarySeason,
        season_episodes: Sequence[LibraryEpisode],
        resolved_targets: Sequence[ResolvedListTarget],
    ) -> list[tuple[ResolvedListTarget, list[LibraryEpisode]]]:
        """Return resolved targets whose mapped episodes should be considered."""
        active_targets: list[tuple[ResolvedListTarget, list[LibraryEpisode]]] = []
        for target in resolved_targets:
            filtered = self._episodes_for_target(
                item=item,
                season=season,
                season_episodes=season_episodes,
                target=target,
            )
            if filtered:
                active_targets.append((target, filtered))
        return active_targets

    def _untrack_skipped_episodes(
        self,
        *,
        season_episodes: Sequence[LibraryEpisode],
        viable_targets: Sequence[tuple[ResolvedListTarget, list[LibraryEpisode]]],
        resolved_targets: Sequence[ResolvedListTarget],
    ) -> None:
        """Remove mapped episodes from stats when they were excluded from sync."""
        if not resolved_targets:
            return

        included_keys = {
            episode.key for _target, episodes in viable_targets for episode in episodes
        }
        skipped = [
            episode for episode in season_episodes if episode.key not in included_keys
        ]
        if skipped:
            self.sync_stats.untrack_items(ItemIdentifier.from_items(skipped))

    def _episodes_for_target(
        self,
        *,
        item: LibraryShow,
        season: LibrarySeason,
        season_episodes: Sequence[LibraryEpisode],
        target: ResolvedListTarget,
    ) -> list[LibraryEpisode]:
        """Return episodes that should be considered for a resolved target."""
        filtered = self._filter_episodes_by_ranges(
            season_episodes,
            target.mappings,
        )
        if not filtered:
            return []
        if self._should_skip_inactive_mapping_range(item, season, filtered, target):
            return []
        return filtered

    def _should_skip_inactive_mapping_range(
        self,
        item: LibraryShow,
        season: LibrarySeason,
        episodes: Sequence[LibraryEpisode],
        target: ResolvedListTarget,
    ) -> bool:
        """Skip mapped subranges that have no actionable activity."""
        if not target.mappings:
            return False
        if self.full_scan or self.destructive_sync or self.empty_sync:
            return False
        if item.on_watching or season.on_watching:
            return False
        if item.on_watchlist or season.on_watchlist:
            return False
        if item.user_rating is not None or season.user_rating is not None:
            return False
        return not any(
            episode.view_count
            or episode.on_watching
            or episode.on_watchlist
            or episode.user_rating is not None
            for episode in episodes
        )

    async def _calculate_status(
        self,
        *,
        item: LibraryShow,
        child_item: LibrarySeason,
        grandchild_items: Sequence[LibraryEpisode],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> ListStatus | None:
        watched_units = self._calculate_watched_units(grandchild_items, mappings)
        watched_episode_count = sum(
            1 for episode in grandchild_items if episode.view_count
        )
        min_view_count = min(
            (episode.view_count for episode in grandchild_items if episode.view_count),
            default=0,
        )
        on_watching = item.on_watching and any(
            episode.on_watching for episode in grandchild_items
        )
        is_finished = len(grandchild_items) == watched_episode_count
        _total_units = entry.media().total_units
        is_completed = _total_units is not None and watched_units >= _total_units

        # We've watched all required episodes at least once
        if is_completed:
            # We're in the middle of re-watching
            if on_watching and min_view_count >= 1:
                return ListStatus.REPEATING
            return ListStatus.COMPLETED

        # We're in the middle of the first watchthrough
        if on_watching:
            return ListStatus.CURRENT

        # We've stopped watching partway through or have no more available episodes
        if watched_units:
            # Either the list or library has incomplete data; assume current
            if is_finished:  # and not is_completed
                return ListStatus.CURRENT
            # We've dropped the show but the user still wants to watch it later
            if item.on_watchlist or child_item.on_watchlist:
                return ListStatus.PAUSED
            return ListStatus.DROPPED

        # We've had no activity on this show yet but it's on the watchlist
        if item.on_watchlist or child_item.on_watchlist:
            return ListStatus.PLANNING

        if self.empty_sync and entry.status is None:
            return ListStatus.PLANNING

        # No activity; leave it untracked
        return None

    async def _calculate_user_rating(
        self,
        *,
        item: LibraryShow,
        child_item: LibrarySeason,
        grandchild_items: Sequence[LibraryEpisode],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> int | None:
        scores = [
            episode.user_rating for episode in grandchild_items if episode.user_rating
        ]
        if scores:
            return round(sum(scores) / len(scores))
        if child_item.user_rating:
            return child_item.user_rating
        if item.user_rating:
            return item.user_rating
        return None

    @lru_cache(maxsize=1)  # De-duplicate call from status
    async def _calculate_progress(
        self,
        *,
        item: LibraryShow,
        child_item: LibrarySeason,
        grandchild_items: Sequence[LibraryEpisode],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> int | None:
        watched = self._calculate_watched_units(grandchild_items, mappings)
        total_units = entry.media().total_units or len(grandchild_items)
        if total_units:
            return min(watched, total_units)
        return watched or None

    async def _calculate_repeats(
        self,
        *,
        item: LibraryShow,
        child_item: LibrarySeason,
        grandchild_items: Sequence[LibraryEpisode],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> int | None:
        view_counts = [
            episode.view_count for episode in grandchild_items if episode.view_count
        ]
        return min(view_counts) - 1 if view_counts else None

    async def _calculate_started_at(
        self,
        *,
        item: LibraryShow,
        child_item: LibrarySeason,
        grandchild_items: Sequence[LibraryEpisode],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> datetime | None:
        history = await self._filter_history_by_episodes(item, grandchild_items)
        if not history:
            return None
        return min(record.viewed_at for record in history)

    async def _calculate_finished_at(
        self,
        *,
        item: LibraryShow,
        child_item: LibrarySeason,
        grandchild_items: Sequence[LibraryEpisode],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> datetime | None:
        history = await self._filter_history_by_episodes(item, grandchild_items)
        if not history:
            return None
        return max(record.viewed_at for record in history)

    async def _calculate_review(
        self,
        *,
        item: LibraryShow,
        child_item: LibrarySeason,
        grandchild_items: Sequence[LibraryEpisode],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> str | None:
        if entry.media().total_units == 1 and len(grandchild_items) == 1:
            review = await grandchild_items[0].review
            if review:
                return review
        return await child_item.review or await item.review

    def _calculate_watched_units(
        self,
        episodes: Sequence[LibraryEpisode],
        mappings: Sequence[AnibridgeDescriptorMapping] | None,
    ) -> int:
        """Calculate the number of watched units, taking mapping ratios into account."""
        watched_episodes = [episode for episode in episodes if episode.view_count]
        watched_count = len(watched_episodes)
        if watched_count == 0:
            return 0
        if not mappings:
            return watched_count

        mapping_entries = [
            mapping_entry
            for descriptor_mapping in mappings
            for mapping_entry in descriptor_mapping.mappings
        ]
        if not mapping_entries:
            return watched_count

        weighted_entries = [
            (mapping_entry.source_range, mapping_entry.source_weight)
            for mapping_entry in mapping_entries
        ]

        if all(weight == 1.0 for _source_range, weight in weighted_entries):
            # Basic case where there's no ratio weight
            return watched_count

        watched_units = 0.0
        weight_by_index: dict[int, float] = {}  # Cache
        for episode in watched_episodes:
            if episode.index not in weight_by_index:
                weight = 1.0
                for source_range, source_weight in weighted_entries:
                    if source_range.contains(episode.index):
                        weight = source_weight
                        break
                weight_by_index[episode.index] = weight
            else:
                weight = weight_by_index[episode.index]
            watched_units += weight
        return int(watched_units + 1e-9)  # Floating point precision

    def _debug_log_title(
        self,
        item: LibraryShow,
        child_item: LibrarySeason | None = None,
    ) -> str:
        return (
            f"$$'{item.title}'$$"
            if child_item is None
            else f"$$'{item.title} (S{child_item.index})'$$"
        )

    def _debug_log_ids(
        self,
        *,
        item: LibraryShow,
        child_item: LibrarySeason | None,
        entry: ListEntry | None,
        media_key: str | None,
    ) -> str:
        formatted: list[str] = []
        if media_key:
            formatted.append(
                descriptor_key((self.list_provider.NAMESPACE, str(media_key), None))
            )
        formatted.extend(
            descriptor_key(descriptor) for descriptor in item.mapping_descriptors()
        )
        return f"$${{{', '.join(formatted)}}}$$"

    @lru_cache(maxsize=1)
    def __get_wanted_seasons(self, item: LibraryShow) -> dict[int, LibrarySeason]:
        """Return seasons that should participate in sync."""
        seasons: dict[int, LibrarySeason] = {}
        for season in item.seasons():
            episodes = season.episodes()
            if not episodes:
                continue
            if (
                self.full_scan
                or self.destructive_sync
                or any(episode.view_count for episode in episodes)
            ):
                seasons[season.index] = season
        return seasons

    @lru_cache(maxsize=1)
    def __get_wanted_episodes(self, item: LibraryShow) -> list[LibraryEpisode]:
        """Return episodes belonging to wanted seasons."""
        seasons = self.__get_wanted_seasons(item)
        if not seasons:
            return []
        return [
            episode for episode in item.episodes() if episode.season_index in seasons
        ]

    @lru_cache(maxsize=32)
    async def _filter_history_by_episodes(
        self, item: LibraryShow, episodes: Sequence[LibraryEpisode]
    ) -> list[HistoryEntry]:
        """Return history entries that belong to the supplied episodes."""
        episode_keys = {episode.key for episode in episodes}
        history = await item.history()
        filtered = [entry for entry in history if entry.library_key in episode_keys]
        filtered.sort(key=lambda record: record.viewed_at)
        return filtered
