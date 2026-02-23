"""Sync history service with TTL caching."""

from collections import defaultdict
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from fastapi.param_functions import Query
from pydantic import BaseModel
from sqlalchemy.sql import select
from sqlalchemy.sql.functions import func

from src import log
from src.config.database import db
from src.core.sync.stats import EntrySnapshot
from src.exceptions import (
    HistoryItemNotFoundError,
    HistoryPermissionError,
    SchedulerNotInitializedError,
)
from src.models.db.pin import Pin
from src.models.db.sync_history import SyncHistory, SyncOutcome
from src.models.schemas.provider import ProviderMediaMetadata
from src.utils.async_tasks import schedule_task
from src.utils.cache import cache, lru_cache, ttl_cache
from src.web.state import get_app_state, get_bridge

if TYPE_CHECKING:
    from anibridge.library import LibrarySection

__all__ = ["HistoryService", "get_history_service"]


class HistoryItem(BaseModel):
    """Serializable history entry with optional provider metadata."""

    id: int
    profile_name: str
    library_namespace: str | None = None
    library_section_key: str | None = None
    library_media_key: str | None = None
    list_namespace: str | None = None
    list_media_key: str | None = None
    animap_entry_id: int | None = None
    media_kind: str | None = None
    outcome: str
    before_state: dict | None = None
    after_state: dict | None = None
    info: dict[str, str] | None = None
    error_message: str | None = None
    timestamp: str
    library_media: ProviderMediaMetadata | None = None
    list_media: ProviderMediaMetadata | None = None
    pinned_fields: list[str] | None = None


class HistoryPage(BaseModel):
    """Pagination wrapper for history items."""

    items: list[HistoryItem]
    total: int
    page: int
    per_page: int
    pages: int
    stats: dict[str, int]


class HistoryService:
    """Service to paginate and enrich sync history records."""

    async def _build_history_items(
        self,
        profile: str,
        rows: Sequence[SyncHistory],
        *,
        include_library_media: bool = True,
        include_list_media: bool = True,
    ) -> list[HistoryItem]:
        """Convert ORM rows into API DTOs with optional metadata enrichment."""
        if not rows:
            return []

        list_pairs: dict[str, set[str]] = defaultdict(set)
        library_pairs: dict[tuple[str, str | None], set[str]] = defaultdict(set)
        for row in rows:
            if row.list_namespace and row.list_media_key:
                list_pairs[row.list_namespace].add(row.list_media_key)
            if row.library_namespace and row.library_media_key:
                library_pairs[(row.library_namespace, row.library_section_key)].add(
                    row.library_media_key
                )

        pin_map: dict[tuple[str, str], list[str]] = {}
        if list_pairs:
            namespaces = list(list_pairs.keys())
            keys = {
                key
                for namespace in namespaces
                for key in list_pairs.get(namespace, set())
            }
            if keys:
                with db() as ctx:
                    pin_rows = (
                        ctx.session.query(Pin)
                        .filter(
                            Pin.profile_name == profile,
                            Pin.list_namespace.in_(namespaces),
                            Pin.list_media_key.in_(list(keys)),
                        )
                        .all()
                    )
                    pin_map = {
                        (pin.list_namespace, pin.list_media_key): list(pin.fields or [])
                        for pin in pin_rows
                    }

        list_metadata_map: dict[tuple[str, str], ProviderMediaMetadata] = {}
        if include_list_media:
            for namespace, keys in list_pairs.items():
                if not keys:
                    continue
                metadata = await self._fetch_list_metadata_batch(
                    profile, namespace, tuple(sorted(keys))
                )
                for key, payload in metadata.items():
                    list_metadata_map[(namespace, key)] = payload

        library_metadata_map: dict[tuple[str, str], ProviderMediaMetadata] = {}
        if include_library_media:
            for (namespace, section_key), keys in library_pairs.items():
                if not keys:
                    continue
                metadata = await self._fetch_library_metadata_batch(
                    profile, namespace, section_key, tuple(sorted(keys))
                )
                for key, payload in metadata.items():
                    library_metadata_map[(namespace, key)] = payload

        dto_items: list[HistoryItem] = []
        for row in rows:
            list_metadata = None
            if row.list_namespace and row.list_media_key:
                list_metadata = list_metadata_map.get(
                    (row.list_namespace, row.list_media_key)
                )
            library_metadata = None
            if row.library_namespace and row.library_media_key:
                library_metadata = library_metadata_map.get(
                    (row.library_namespace, row.library_media_key)
                )

            dto_items.append(
                HistoryItem(
                    id=row.id,
                    profile_name=row.profile_name,
                    library_namespace=row.library_namespace,
                    library_section_key=row.library_section_key,
                    library_media_key=row.library_media_key,
                    list_namespace=row.list_namespace,
                    list_media_key=row.list_media_key,
                    animap_entry_id=row.animap_entry_id,
                    media_kind=row.media_kind.value if row.media_kind else None,
                    outcome=str(row.outcome),
                    before_state=row.before_state,
                    after_state=row.after_state,
                    info=row.info,
                    error_message=row.error_message,
                    timestamp=row.timestamp.isoformat(),
                    library_media=library_metadata,
                    list_media=list_metadata,
                    pinned_fields=(
                        pin_map.get((row.list_namespace, row.list_media_key))
                        if row.list_namespace and row.list_media_key
                        else None
                    ),
                )
            )

        return dto_items

    @lru_cache(maxsize=64)
    async def _fetch_list_metadata_batch(
        self,
        profile: str,
        namespace: str,
        media_keys: tuple[str, ...],
    ) -> dict[str, ProviderMediaMetadata]:
        """Fetch list provider metadata for a batch of media keys."""
        if not media_keys:
            return {}
        bridge = get_bridge(profile)
        if namespace != bridge.list_provider.NAMESPACE:
            return {}

        entries = await bridge.list_provider.get_entries_batch(list(media_keys))
        metadata: dict[str, ProviderMediaMetadata] = {}
        for entry in entries:
            if entry is None:
                continue
            media = entry.media()
            metadata[media.key] = ProviderMediaMetadata(
                namespace=bridge.list_provider.NAMESPACE,
                key=media.key,
                title=media.title,
                poster_url=media.poster_image,
                external_url=media.external_url,
                labels=(list(media.labels) if media.labels else None),
            )
        return metadata

    @lru_cache(maxsize=64)
    async def _fetch_library_metadata_batch(
        self,
        profile: str,
        namespace: str,
        section_key: str | None,
        media_keys: tuple[str, ...],
    ) -> dict[str, ProviderMediaMetadata]:
        if not media_keys:
            return {}
        bridge = get_bridge(profile)
        if namespace != bridge.library_provider.NAMESPACE:
            return {}

        sections = await bridge.library_provider.get_sections()
        target_sections: list[LibrarySection]
        if section_key is None:
            target_sections = list(sections)
        else:
            target_sections = [
                section for section in sections if section.key == section_key
            ]
            if not target_sections:
                return {}

        remaining = set(media_keys)
        metadata: dict[str, ProviderMediaMetadata] = {}
        for section in target_sections:
            if not remaining:
                break
            items = await bridge.library_provider.list_items(
                section, keys=list(remaining)
            )
            for item in items:
                key = str(item.key)
                if key not in remaining:
                    continue
                remaining.discard(key)
                metadata[key] = ProviderMediaMetadata(
                    namespace=bridge.library_provider.NAMESPACE,
                    key=key,
                    title=item.title,
                    poster_url=item.media().poster_image,
                    external_url=item.media().external_url,
                )
        return metadata

    @ttl_cache(ttl=60)
    async def _fetch_profile_stats(self, profile: str) -> dict[str, int]:
        """Cached profile statistics fetch."""
        with db() as ctx:
            stats_rows = (
                ctx.session.query(SyncHistory.outcome, func.count(SyncHistory.id))
                .filter(SyncHistory.profile_name == profile)
                .group_by(SyncHistory.outcome)
                .all()
            )
            stats = {str(outcome): count for outcome, count in stats_rows}
            return stats

    async def get_page(
        self,
        profile: str,
        page: int = Query(1, ge=1),
        per_page: int = Query(20, ge=1, le=250),
        outcome: str | None = None,
        library_namespace: str | None = None,
        list_namespace: str | None = None,
        include_library_media: bool = True,
        include_list_media: bool = True,
    ) -> HistoryPage:
        """Return paginated history entries enriched as requested.

        Args:
            profile (str): The profile name to filter history entries.
            page (int): The page number to retrieve.
            per_page (int): The number of entries per page.
            outcome (str | None): Optional filter for the sync outcome.
            library_namespace (str | None): Optional filter for library provider.
            list_namespace (str | None): Optional filter for list provider.
            include_library_media (bool): Include library provider metadata when True.
            include_list_media (bool): Include list provider metadata when True.

        Returns:
            HistoryPage: The paginated history entries.

        Raises:
            SchedulerNotInitializedError: If the scheduler is not running.
            ProfileNotFoundError: If the profile is unknown.
        """
        base_filters = [
            SyncHistory.profile_name == profile,
        ]
        if outcome:
            base_filters.append(SyncHistory.outcome == outcome)
        if library_namespace:
            base_filters.append(SyncHistory.library_namespace == library_namespace)
        if list_namespace:
            base_filters.append(SyncHistory.list_namespace == list_namespace)

        with db() as ctx:
            # Get cached stats
            stats = await self._fetch_profile_stats(profile)

            count_stmt = (
                select(func.count()).select_from(SyncHistory).where(*base_filters)
            )
            total = ctx.session.execute(count_stmt).scalar_one()

            stmt = (
                select(SyncHistory)
                .where(*base_filters)
                .order_by(SyncHistory.timestamp.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            )
            rows = ctx.session.execute(stmt).scalars().all()
        dto_items = await self._build_history_items(
            profile,
            rows,
            include_library_media=include_library_media,
            include_list_media=include_list_media,
        )

        page_obj = HistoryPage(
            items=dto_items,
            total=total,
            page=page,
            per_page=per_page,
            pages=(total + per_page - 1) // per_page,
            stats=stats,
        )
        return page_obj

    async def delete_item(self, profile: str, item_id: int) -> None:
        """Delete a single history item for a profile.

        Args:
            profile (str): The profile name.
            item_id (int): The ID of the history item to delete.

        Raises:
            HistoryItemNotFoundError: If the item does not exist.
        """
        log.info(
            "Deleting history item id=%s for profile %s",
            item_id,
            profile,
        )
        with db() as ctx:
            row = (
                ctx.session.query(SyncHistory)
                .filter(SyncHistory.profile_name == profile, SyncHistory.id == item_id)
                .first()
            )
            if not row:
                raise HistoryItemNotFoundError("Not found")
            ctx.session.delete(row)
            ctx.session.commit()

        # Invalidate related caches after deletion
        await self.clear_profile_cache(profile)

    async def undo_item(self, profile: str, item_id: int) -> HistoryItem:
        """Undo a history item by reverting or deleting the AniList entry.

        Args:
            profile (str): Profile name
            item_id (int): History row id to undo

        Returns:
            HistoryItem: Newly created history record representing the undo action.

        Raises:
            SchedulerNotInitializedError: If the scheduler is not running.
            ProfileNotFoundError: If the profile is unknown.
            HistoryItemNotFoundError: If the specified item does not exist.
        """
        log.info(
            "Undoing history item id=%s for profile %s",
            item_id,
            profile,
        )
        bridge = get_bridge(profile)
        list_provider = bridge.list_provider

        with db() as ctx:
            row = (
                ctx.session.query(SyncHistory)
                .filter(SyncHistory.profile_name == profile, SyncHistory.id == item_id)
                .first()
            )
            if not row:
                raise HistoryItemNotFoundError("Not found")

        if not row.list_media_key:
            raise HistoryItemNotFoundError(
                "Cannot undo history item without list media key"
            )
        if row.profile_name != profile:
            raise HistoryPermissionError("Profile mismatch for history item")
        if row.list_namespace != list_provider.NAMESPACE:
            raise HistoryPermissionError(
                "History item belongs to a different list provider"
            )
        if row.library_namespace != bridge.library_provider.NAMESPACE:
            raise HistoryPermissionError(
                "History item belongs to a different library provider"
            )
        if row.outcome not in (SyncOutcome.SYNCED, SyncOutcome.DELETED):
            raise HistoryPermissionError(
                "Undo is only supported for synced or deleted items"
            )

        before_snapshot = (
            EntrySnapshot.from_dict(row.before_state) if row.before_state else None
        )
        after_snapshot = (
            EntrySnapshot.from_dict(row.after_state) if row.after_state else None
        )

        if not row.before_state and not after_snapshot:
            raise HistoryPermissionError("History item does not contain undo data")
        if not before_snapshot and not row.list_media_key:
            raise HistoryItemNotFoundError(
                "Cannot undo history item without list media key"
            )
        if not before_snapshot and not bridge.profile_config.destructive_sync:
            raise HistoryPermissionError(
                "Cannot undo history item that requires deletion when destructive "
                "sync is disabled"
            )

        if before_snapshot is None:
            log.success(
                "Deleting list entry %s as part of undo",
                row.list_media_key,
            )
            if bridge.profile_config.dry_run:
                log.info(
                    "Dry run enabled; skipping deletion of list entry %s",
                    row.list_media_key,
                )
            else:
                await list_provider.delete_entry(row.list_media_key)
        else:
            log.success(
                "Restoring list entry %s to previous state",
                before_snapshot.media_key,
            )
            if bridge.profile_config.dry_run:
                log.info(
                    "Dry run enabled; skipping restoration of list entry %s",
                    before_snapshot.media_key,
                )
            else:
                entry = await list_provider.get_entry(before_snapshot.media_key)
                if entry is None:
                    raise HistoryItemNotFoundError(
                        "List entry no longer exists on the provider"
                    )
                entry.status = before_snapshot.status
                entry.progress = before_snapshot.progress
                entry.repeats = before_snapshot.repeats
                entry.review = before_snapshot.review
                entry.user_rating = before_snapshot.user_rating
                entry.started_at = before_snapshot.started_at
                entry.finished_at = before_snapshot.finished_at
                await list_provider.update_entry(before_snapshot.media_key, entry)

        with db() as ctx:
            source_info = {
                str(key): str(value)
                for key, value in (row.info or {}).items()
                if str(key).strip() and value is not None
            }
            undo_row = SyncHistory(
                profile_name=row.profile_name,
                library_namespace=row.library_namespace,
                library_section_key=row.library_section_key,
                library_media_key=row.library_media_key,
                list_namespace=row.list_namespace,
                list_media_key=row.list_media_key,
                animap_entry_id=row.animap_entry_id,
                media_kind=row.media_kind,
                outcome=SyncOutcome.UNDONE,
                before_state=row.after_state,
                after_state=row.before_state,
                info={
                    **source_info,
                    "operation": "undo",
                    "source_history_id": str(row.id),
                    "source_outcome": str(row.outcome),
                },
            )
            ctx.session.add(undo_row)
            ctx.session.commit()
            ctx.session.refresh(undo_row)

        self._fetch_profile_stats.cache_clear()

        dto_items = await self._build_history_items(profile, [undo_row])
        return dto_items[0]

    async def retry_item(self, profile: str, item_id: int) -> None:
        """Retry a failed history item by re-triggering a targeted sync."""
        log.info(
            "Retrying history item id=%s for profile %s",
            item_id,
            profile,
        )

        scheduler = get_app_state().scheduler
        if not scheduler:
            raise SchedulerNotInitializedError("Scheduler not available")

        with db() as ctx:
            row = (
                ctx.session.query(SyncHistory)
                .filter(SyncHistory.profile_name == profile, SyncHistory.id == item_id)
                .first()
            )
        if not row:
            raise HistoryItemNotFoundError("Not found")

        bridge = get_bridge(profile)

        if row.library_namespace != bridge.library_provider.NAMESPACE:
            raise HistoryPermissionError(
                "History item belongs to a different library provider"
            )
        if row.outcome not in (SyncOutcome.FAILED, SyncOutcome.NOT_FOUND):
            raise HistoryPermissionError(
                "Retry is only available for failed or not found items"
            )
        if not row.library_media_key:
            raise HistoryPermissionError(
                "Cannot retry history item without library media key"
            )

        schedule_task(
            scheduler.trigger_profile_sync(
                profile,
                poll=False,
                library_keys=[row.library_media_key],
                source="history:retry_item",
            ),
            name=f"retry_history_item:{profile}:{item_id}",
        )

    async def clear_profile_cache(self, profile: str) -> None:
        """Clear all cached data for a specific profile.

        Args:
            profile: Profile name to clear cache for
        """
        self._fetch_list_metadata_batch.cache_clear()
        self._fetch_library_metadata_batch.cache_clear()
        self._fetch_profile_stats.cache_clear()

    async def clear_all_caches(self) -> None:
        """Clear all caches."""
        self._fetch_list_metadata_batch.cache_clear()
        self._fetch_library_metadata_batch.cache_clear()
        self._fetch_profile_stats.cache_clear()

    def get_cache_info(self) -> dict[str, Any]:
        """Get cache statistics for monitoring.

        Returns:
            Dictionary with cache hit/miss statistics
        """
        return {
            "list_cache": self._fetch_list_metadata_batch.cache_info(),
            "library_cache": self._fetch_library_metadata_batch.cache_info(),
            "stats_cache": self._fetch_profile_stats.cache_info(),
        }


@cache
def get_history_service() -> HistoryService:
    """Get the singleton HistoryService instance.

    Returns:
        HistoryService: The history service instance.
    """
    return HistoryService()
