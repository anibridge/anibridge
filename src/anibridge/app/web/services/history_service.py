"""Sync history service."""

from collections import defaultdict
from collections.abc import Sequence
from typing import Annotated, Any

import msgspec
from anibridge.library.base import LibraryMedia
from anibridge.list.base import ListMedia
from anibridge.utils.cache import cache
from sqlalchemy.sql import select
from sqlalchemy.sql.functions import func

from anibridge.app.config.database import db
from anibridge.app.core.sync.stats import EntrySnapshot
from anibridge.app.exceptions import (
    HistoryItemNotFoundError,
    HistoryPermissionError,
    SchedulerNotInitializedError,
)
from anibridge.app.logging import get_logger
from anibridge.app.models.db.pin import Pin
from anibridge.app.models.db.sync_history import SyncHistory, SyncOutcome
from anibridge.app.models.schemas.provider import ProviderMediaMetadata
from anibridge.app.utils.async_tasks import schedule_task
from anibridge.app.web.state import get_app_state, get_bridge

__all__ = ["HistoryService", "get_history_service"]

log = get_logger(__name__)


class HistoryItem(msgspec.Struct):
    """Serializable history entry with optional provider metadata."""

    id: Annotated[
        int,
        msgspec.Meta(
            ge=1,
            description="Monotonic database identifier for the history item.",
            examples=[42],
        ),
    ]
    profile_name: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Profile that produced the history item.",
            examples=["default"],
        ),
    ]
    outcome: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Normalized sync outcome for the item.",
            examples=["synced"],
        ),
    ]
    timestamp: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="ISO-8601 timestamp when the history item was recorded.",
            examples=["2026-01-01T00:00:00+00:00"],
        ),
    ]
    library_namespace: (
        Annotated[
            str,
            msgspec.Meta(
                description="Library provider namespace for the synced item.",
                examples=["plex"],
            ),
        ]
        | None
    ) = None
    library_section_key: (
        Annotated[
            str,
            msgspec.Meta(
                description="Provider-specific library section identifier.",
                examples=["tv"],
            ),
        ]
        | None
    ) = None
    library_media_key: (
        Annotated[
            str,
            msgspec.Meta(
                description="Provider-specific library media identifier.",
                examples=["ratingKey:1234"],
            ),
        ]
        | None
    ) = None
    list_namespace: (
        Annotated[
            str,
            msgspec.Meta(
                description="List provider namespace for the synced entry.",
                examples=["anilist"],
            ),
        ]
        | None
    ) = None
    list_media_key: (
        Annotated[
            str,
            msgspec.Meta(
                description="Provider-specific list entry identifier.",
                examples=["5114"],
            ),
        ]
        | None
    ) = None
    animap_provider: (
        Annotated[
            str,
            msgspec.Meta(
                description="AniMap provider namespace used during resolution.",
                examples=["anilist"],
            ),
        ]
        | None
    ) = None
    animap_id: (
        Annotated[
            str,
            msgspec.Meta(
                description="AniMap entry identifier used during resolution.",
                examples=["5114"],
            ),
        ]
        | None
    ) = None
    animap_scope: (
        Annotated[
            str,
            msgspec.Meta(
                description="AniMap scope applied to the mapping, when present.",
                examples=["s1"],
            ),
        ]
        | None
    ) = None
    media_kind: (
        Annotated[
            str,
            msgspec.Meta(
                description="Normalized media kind for the synchronized item.",
                examples=["show"],
            ),
        ]
        | None
    ) = None
    before_state: (
        Annotated[
            dict[str, Any],
            msgspec.Meta(
                description="Serialized list entry state before sync was applied.",
                examples=[{"status": "PLANNING", "progress": 11}],
            ),
        ]
        | None
    ) = None
    after_state: (
        Annotated[
            dict[str, Any],
            msgspec.Meta(
                description="Serialized list entry state after sync was applied.",
                examples=[{"status": "CURRENT", "progress": 12}],
            ),
        ]
        | None
    ) = None
    info: (
        Annotated[
            dict[str, str],
            msgspec.Meta(
                description="Additional string metadata recorded alongside the event.",
                examples=[{"reason": "pinned field skipped"}],
            ),
        ]
        | None
    ) = None
    error_message: (
        Annotated[
            str,
            msgspec.Meta(
                description="Error message captured for failed history items.",
                examples=["Rate limit exceeded"],
            ),
        ]
        | None
    ) = None
    ephemeral: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the history item exists only in memory.",
            examples=[False],
        ),
    ] = False
    library_media: (
        Annotated[
            ProviderMediaMetadata,
            msgspec.Meta(
                description="Resolved library-side media metadata when requested.",
                examples=[{"namespace": "plex", "key": "ratingKey:1234"}],
            ),
        ]
        | None
    ) = None
    list_media: (
        Annotated[
            ProviderMediaMetadata,
            msgspec.Meta(
                description="Resolved list-side media metadata when requested.",
                examples=[{"namespace": "anilist", "key": "5114"}],
            ),
        ]
        | None
    ) = None
    pinned_fields: (
        Annotated[
            list[str],
            msgspec.Meta(
                description="Pinned sync fields that prevented updates for the item.",
                examples=[["status", "progress"]],
            ),
        ]
        | None
    ) = None


class HistoryPage(msgspec.Struct):
    """Cursor-based history slice wrapper."""

    items: Annotated[
        list[HistoryItem],
        msgspec.Meta(
            description="Ordered history items for the requested page.",
            examples=[
                [
                    {
                        "id": 42,
                        "profile_name": "default",
                        "outcome": "synced",
                        "timestamp": "2026-01-01T00:00:00+00:00",
                    }
                ]
            ],
        ),
    ]
    limit: Annotated[
        int,
        msgspec.Meta(
            ge=1,
            description="Requested page size used to build the history slice.",
            examples=[25],
        ),
    ]
    has_more: Annotated[
        bool,
        msgspec.Meta(
            description="Whether another older page of history exists.",
            examples=[True],
        ),
    ]
    next_before_id: (
        Annotated[
            int,
            msgspec.Meta(
                ge=1,
                description="Cursor to request the next older page of history.",
                examples=[41],
            ),
        ]
        | None
    ) = None
    latest_id: (
        Annotated[
            int,
            msgspec.Meta(
                ge=1,
                description=(
                    "Most recent history item ID currently known for the profile."
                ),
                examples=[42],
            ),
        ]
        | None
    ) = None
    stats: (
        Annotated[
            dict[str, int],
            msgspec.Meta(
                description="Grouped outcome counters for the selected history window.",
                examples=[{"synced": 12, "failed": 1}],
            ),
        ]
        | None
    ) = None


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
            namespaces = tuple(list_pairs)
            keys = {key for media_keys in list_pairs.values() for key in media_keys}
            if keys:
                with db() as ctx:
                    pin_rows = (
                        ctx.session.query(Pin)
                        .filter(
                            Pin.profile_name == profile,
                            Pin.list_namespace.in_(namespaces),
                            Pin.list_media_key.in_(tuple(keys)),
                        )
                        .all()
                    )
                    pin_map = {
                        (pin.list_namespace, pin.list_media_key): list(pin.fields or [])
                        for pin in pin_rows
                    }

        list_media_map: dict[tuple[str, str], ListMedia] = {}
        if include_list_media:
            for namespace, keys in list_pairs.items():
                if not keys:
                    continue
                metadata = await self._fetch_list_metadata_batch(
                    profile, namespace, tuple(sorted(keys))
                )
                for key, payload in metadata.items():
                    list_media_map[(namespace, key)] = payload

        library_media_map: dict[tuple[str, str], LibraryMedia] = {}
        if include_library_media:
            for (namespace, section_key), keys in library_pairs.items():
                if not keys:
                    continue
                metadata = await self._fetch_library_metadata_batch(
                    profile, namespace, section_key, tuple(sorted(keys))
                )
                for key, payload in metadata.items():
                    library_media_map[(namespace, key)] = payload

        dto_items: list[HistoryItem] = []
        for row in rows:
            list_media: ListMedia | None = None
            if row.list_namespace and row.list_media_key:
                list_media = list_media_map.get(
                    (row.list_namespace, row.list_media_key)
                )
            library_media: LibraryMedia | None = None
            if row.library_namespace and row.library_media_key:
                library_media = library_media_map.get(
                    (row.library_namespace, row.library_media_key)
                )

            list_metadata: ProviderMediaMetadata | None = None
            if list_media and row.list_media_key is not None:
                list_metadata = ProviderMediaMetadata(
                    namespace=row.list_namespace,
                    key=row.list_media_key,
                    title=list_media.title,
                    poster_url=list_media.poster_image,
                    external_url=list_media.external_url,
                    labels=(
                        list(list_media.labels)
                        if list_media.labels is not None
                        else None
                    ),
                )
            library_metadata: ProviderMediaMetadata | None = None
            if library_media:
                library_metadata = ProviderMediaMetadata(
                    namespace=row.library_namespace,
                    key=row.library_media_key,
                    title=library_media.title,
                    # Special condition for posters since it's known to be expensive.
                    # Only include if list media doesn't have a poster.
                    poster_url=library_media.poster_image
                    if not list_media or not list_media.poster_image
                    else None,
                    external_url=library_media.external_url,
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
                    animap_provider=row.animap_provider,
                    animap_id=row.animap_id,
                    animap_scope=row.animap_scope,
                    media_kind=row.media_kind.value if row.media_kind else None,
                    outcome=str(row.outcome),
                    before_state=row.before_state,
                    after_state=row.after_state,
                    info=row.info,
                    error_message=row.error_message,
                    ephemeral=row.ephemeral,
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

    async def _fetch_list_metadata_batch(
        self,
        profile: str,
        namespace: str,
        media_keys: tuple[str, ...],
    ) -> dict[str, ListMedia]:
        """Fetch list provider metadata for a batch of media keys."""
        if not media_keys:
            return {}
        bridge = get_bridge(profile)
        if namespace != bridge.list_provider.NAMESPACE:
            return {}

        entries = await bridge.list_provider.get_entries_batch(media_keys)
        metadata: dict[str, ListMedia] = {}
        for entry in entries:
            if entry is None:
                continue
            media = entry.media()
            metadata[media.key] = media
        return metadata

    async def _fetch_library_metadata_batch(
        self,
        profile: str,
        namespace: str,
        section_key: str | None,
        media_keys: tuple[str, ...],
    ) -> dict[str, LibraryMedia]:
        if not media_keys:
            return {}
        if section_key is None:
            return {}
        bridge = get_bridge(profile)
        if namespace != bridge.library_provider.NAMESPACE:
            return {}

        sections = await bridge.library_provider.get_sections()
        section = next((s for s in sections if s.key == section_key), None)
        if section is None:
            return {}

        metadata: dict[str, LibraryMedia] = {}
        items = await bridge.library_provider.list_items(section, keys=list(media_keys))
        for item in items:
            media = item.media()
            metadata[str(item.key)] = media
        return metadata

    async def _fetch_profile_stats(
        self,
        profile: str,
        library_namespace: str,
        list_namespace: str,
    ) -> dict[str, int]:
        """Cached profile statistics fetch."""
        with db() as ctx:
            stats_rows = (
                ctx.session.query(SyncHistory.outcome, func.count(SyncHistory.id))
                .filter(
                    SyncHistory.profile_name == profile,
                    SyncHistory.library_namespace == library_namespace,
                    SyncHistory.list_namespace == list_namespace,
                )
                .group_by(SyncHistory.outcome)
                .all()
            )
            return {str(outcome): count for outcome, count in stats_rows}

    async def _resolve_filters(
        self,
        profile: str,
        *,
        outcome: str | None = None,
        library_namespace: str | None = None,
        list_namespace: str | None = None,
    ) -> tuple[str, str, list[Any]]:
        """Resolve provider filters and produce common SQLAlchemy predicates."""
        bridge = get_bridge(profile)
        effective_library_namespace = (
            library_namespace or bridge.library_provider.NAMESPACE
        )
        effective_list_namespace = list_namespace or bridge.list_provider.NAMESPACE

        base_filters = [
            SyncHistory.profile_name == profile,
            SyncHistory.library_namespace == effective_library_namespace,
            SyncHistory.list_namespace == effective_list_namespace,
        ]
        if outcome:
            base_filters.append(SyncHistory.outcome == outcome)

        return effective_library_namespace, effective_list_namespace, base_filters

    async def get_latest_id(
        self,
        profile: str,
        *,
        outcome: str | None = None,
        library_namespace: str | None = None,
        list_namespace: str | None = None,
    ) -> int | None:
        """Return the most recent history row id for the requested filter scope."""
        _, _, base_filters = await self._resolve_filters(
            profile,
            outcome=outcome,
            library_namespace=library_namespace,
            list_namespace=list_namespace,
        )
        with db() as ctx:
            latest_stmt = select(func.max(SyncHistory.id)).where(*base_filters)
            return ctx.session.execute(latest_stmt).scalar_one_or_none()

    async def get_page(
        self,
        profile: str,
        limit: int = 25,
        before_id: int | None = None,
        after_id: int | None = None,
        outcome: str | None = None,
        library_namespace: str | None = None,
        list_namespace: str | None = None,
        include_library_media: bool = True,
        include_list_media: bool = True,
        include_stats: bool = False,
    ) -> HistoryPage:
        """Return cursor-based history slice enriched as requested.

        Args:
            profile (str): The profile name to filter history entries.
            limit (int): The max number of entries to return.
            before_id (int | None): If provided, only return rows where id < before_id.
            after_id (int | None): If provided, only return rows where id > after_id.
            outcome (str | None): Optional filter for the sync outcome.
            library_namespace (str | None): Optional filter for library provider.
            list_namespace (str | None): Optional filter for list provider.
            include_library_media (bool): Include library provider metadata when True.
            include_list_media (bool): Include list provider metadata when True.
            include_stats (bool): Include grouped outcome counts when True.

        Returns:
            HistoryPage: The history slice response.

        Raises:
            SchedulerNotInitializedError: If the scheduler is not running.
            ProfileNotFoundError: If the profile is unknown.
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if limit > 250:
            raise ValueError("limit must be <= 250")
        if before_id is not None and after_id is not None:
            raise ValueError("before_id and after_id are mutually exclusive")

        (
            effective_library_namespace,
            effective_list_namespace,
            base_filters,
        ) = await self._resolve_filters(
            profile,
            outcome=outcome,
            library_namespace=library_namespace,
            list_namespace=list_namespace,
        )

        if before_id is not None:
            base_filters.append(SyncHistory.id < before_id)
        if after_id is not None:
            base_filters.append(SyncHistory.id > after_id)

        latest_filters = [
            SyncHistory.profile_name == profile,
            SyncHistory.library_namespace == effective_library_namespace,
            SyncHistory.list_namespace == effective_list_namespace,
        ]
        if outcome:
            latest_filters.append(SyncHistory.outcome == outcome)

        with db() as ctx:
            latest_stmt = select(func.max(SyncHistory.id)).where(*latest_filters)
            latest_id = ctx.session.execute(latest_stmt).scalar_one_or_none()

            stmt = (
                select(SyncHistory)
                .where(*base_filters)
                .order_by(SyncHistory.timestamp.desc())
                .limit(limit + 1)
            )
            rows = ctx.session.execute(stmt).scalars().all()

        has_more = len(rows) > limit
        rows = rows[:limit]

        dto_items = await self._build_history_items(
            profile,
            rows,
            include_library_media=include_library_media,
            include_list_media=include_list_media,
        )

        stats: dict[str, int] | None = None
        if include_stats:
            stats = await self._fetch_profile_stats(
                profile,
                effective_library_namespace,
                effective_list_namespace,
            )

        next_before_id = rows[-1].id if rows and has_more else None

        page_obj = HistoryPage(
            items=dto_items,
            limit=limit,
            has_more=has_more,
            next_before_id=next_before_id,
            latest_id=latest_id,
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
        scheduler = get_app_state().scheduler
        if not scheduler:
            raise SchedulerNotInitializedError("Scheduler not available")
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
            msgspec.convert(row.before_state, type=EntrySnapshot)
            if row.before_state
            else None
        )
        after_snapshot = (
            msgspec.convert(row.after_state, type=EntrySnapshot)
            if row.after_state
            else None
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

        async def _apply_provider_undo() -> None:
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

        await scheduler.run_maintenance(
            _apply_provider_undo,
            source=f"history_undo:{profile}:{item_id}",
        )

        with db() as ctx:
            source_info = {
                key: value for key, value in (row.info or {}).items() if value
            }
            undo_row = SyncHistory(
                profile_name=row.profile_name,
                library_namespace=row.library_namespace,
                library_section_key=row.library_section_key,
                library_media_key=row.library_media_key,
                list_namespace=row.list_namespace,
                list_media_key=row.list_media_key,
                animap_provider=row.animap_provider,
                animap_id=row.animap_id,
                animap_scope=row.animap_scope,
                media_kind=row.media_kind,
                outcome=SyncOutcome.UNDONE,
                before_state=row.after_state,
                after_state=row.before_state,
                ephemeral=bridge.profile_config.dry_run,
                info={
                    **source_info,
                    "source_history_id": str(row.id),
                    "source_outcome": str(row.outcome),
                },
            )
            ctx.session.add(undo_row)
            ctx.session.commit()
            ctx.session.refresh(undo_row)

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

    async def purge_ephemeral_items(self) -> int:
        """Delete ephemeral history rows."""
        with db() as ctx:
            count = (
                ctx.session.query(SyncHistory)
                .filter(SyncHistory.ephemeral.is_(True))
                .count()
            )
            if not count:
                return 0
            (
                ctx.session.query(SyncHistory)
                .filter(SyncHistory.ephemeral.is_(True))
                .delete(synchronize_session=False)
            )
            ctx.session.commit()
        return count


@cache
def get_history_service() -> HistoryService:
    """Get the singleton HistoryService instance.

    Returns:
        HistoryService: The history service instance.
    """
    return HistoryService()
