"""Sync client for library movies using provider abstractions."""

from collections.abc import AsyncIterator, Sequence
from datetime import datetime

from anibridge.library import LibraryMovie
from anibridge.list import ListEntry, ListMediaType, ListStatus
from anibridge.utils.cache import lru_cache
from anibridge.utils.mappings import AnibridgeDescriptorMapping, descriptor_key

from anibridge.app.core.sync.base import BaseSyncClient
from anibridge.app.core.sync.stats import ItemIdentifier
from anibridge.app.core.sync.targeting import (
    SyncTarget,
    find_best_search_result,
    resolve_list_targets,
)

__all__ = ["MovieSyncClient"]


class MovieSyncClient(BaseSyncClient[LibraryMovie, LibraryMovie, LibraryMovie]):
    """Synchronize movie items between a library provider and a list provider."""

    async def map_media(
        self, item: LibraryMovie
    ) -> AsyncIterator[
        tuple[
            LibraryMovie,
            Sequence[LibraryMovie],
            SyncTarget,
        ]
    ]:
        """Map a library movie to one or more list targets.

        Args:
            item (LibraryMovie): Library movie to resolve.

        Yields:
            tuple[LibraryMovie, Sequence[LibraryMovie], SyncTarget]: Mapping
                candidates for sync.
        """
        targets = await resolve_list_targets(
            animap_client=self.animap_client,
            list_provider=self.list_provider,
            media_items=(item,),
        )
        if targets:
            yielded = False
            for target in targets:
                entry = await self._cache.get_entry(target.list_media_key)
                if entry is None:
                    continue
                yielded = True
                list_media_key = entry.media().key
                yield (
                    item,
                    (item,),
                    SyncTarget(
                        list_media_key=list_media_key,
                        entry=entry,
                        mapping_descriptors=target.mapping_descriptors,
                        mappings=target.mappings,
                    ),
                )
            if yielded:
                return

        entry = await self.search_media(item, item)
        if entry is not None:
            self._cache.cache_entry(entry)
            yield (
                item,
                (item,),
                SyncTarget(
                    list_media_key=entry.media().key,
                    entry=entry,
                ),
            )

    async def _collect_prefetch_keys(self, item: LibraryMovie) -> Sequence[str]:
        """Collect mapping descriptors and search keys for a library movie."""
        targets = await resolve_list_targets(
            animap_client=self.animap_client,
            list_provider=self.list_provider,
            media_items=(item,),
        )
        return tuple(sorted({target.list_media_key for target in targets}))

    async def search_media(
        self, item: LibraryMovie, child_item: LibraryMovie
    ) -> ListEntry | None:
        """Fallback search for matching movie entries.

        Args:
            item (LibraryMovie): Parent movie item to search for.
            child_item (LibraryMovie): Child movie item, identical for movie sync.

        Returns:
            ListEntry | None: Matching movie entry, if one meets the threshold.
        """
        if self.search_fallback_threshold < 0:
            return None

        results = await self.list_provider.search(item.title)
        movie_results = [
            entry
            for entry in results
            if entry.media().media_type == ListMediaType.MOVIE
        ]
        return find_best_search_result(
            item.title,
            movie_results,
            self.search_fallback_threshold,
        )

    async def _get_all_trackable_items(
        self, item: LibraryMovie
    ) -> Sequence[ItemIdentifier]:
        return [ItemIdentifier.from_item(item)]

    @lru_cache(maxsize=32)
    async def _get_history(self, item: LibraryMovie):
        return await item.history()

    async def _calculate_status(
        self,
        *,
        item: LibraryMovie,
        child_item: LibraryMovie,
        grandchild_items: Sequence[LibraryMovie],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> ListStatus | None:
        has_views = item.view_count > 0
        history = await self._get_history(item)
        has_history = bool(history)

        if has_views and item.on_watching:
            return ListStatus.REPEATING
        if has_views:
            return ListStatus.COMPLETED
        if item.on_watching:
            return ListStatus.CURRENT
        if item.on_watchlist:
            return ListStatus.PLANNING
        if has_history:
            return ListStatus.DROPPED
        if self.empty_sync and entry.status is None:
            return ListStatus.PLANNING
        return None

    async def _calculate_user_rating(
        self,
        *,
        item: LibraryMovie,
        child_item: LibraryMovie,
        grandchild_items: Sequence[LibraryMovie],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> int | None:
        return item.user_rating

    async def _calculate_progress(
        self,
        *,
        item: LibraryMovie,
        child_item: LibraryMovie,
        grandchild_items: Sequence[LibraryMovie],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> int | None:
        total_units = entry.media().total_units or len(grandchild_items) or 1
        return total_units if item.view_count > 0 else None

    async def _calculate_repeats(
        self,
        *,
        item: LibraryMovie,
        child_item: LibraryMovie,
        grandchild_items: Sequence[LibraryMovie],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> int | None:
        return item.view_count - 1 if item.view_count > 0 else None

    async def _calculate_started_at(
        self,
        *,
        item: LibraryMovie,
        child_item: LibraryMovie,
        grandchild_items: Sequence[LibraryMovie],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> datetime | None:
        history = await self._get_history(item)
        if not history:
            return None
        return min(record.viewed_at for record in history)

    async def _calculate_finished_at(
        self,
        *,
        item: LibraryMovie,
        child_item: LibraryMovie,
        grandchild_items: Sequence[LibraryMovie],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> datetime | None:
        history = await self._get_history(item)
        if not history:
            return None
        return max(record.viewed_at for record in history)

    async def _calculate_review(
        self,
        *,
        item: LibraryMovie,
        child_item: LibraryMovie,
        grandchild_items: Sequence[LibraryMovie],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> str | None:
        return await item.review

    def _debug_log_title(
        self,
        item: LibraryMovie,
        child_item: LibraryMovie | None = None,
    ) -> str:
        return f"$$'{item.title}'$$"

    def _debug_log_ids(
        self,
        *,
        item: LibraryMovie,
        child_item: LibraryMovie | None,
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
