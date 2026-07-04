"""Sync history service."""

from collections.abc import Mapping, Sequence
from typing import Annotated, Any

import msgspec
from anibridge.provider.base import (
    Artwork,
    FacetName,
    NodeQuery,
    Ref,
    SupportsReads,
)
from anibridge.utils.cache import cache
from sqlalchemy.sql import select
from sqlalchemy.sql.functions import func

from anibridge.app.config.database import db
from anibridge.app.core.sync import (
    RecordUndoRequest,
    RefKey,
    RefPayload,
    SyncRequest,
    SyncTrigger,
    ref_from_payload,
    ref_payload_from_json,
    ref_to_key,
)
from anibridge.app.core.sync.stats import RecordSnapshot
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

    id: Annotated[int, msgspec.Meta(ge=1)]
    profile_name: Annotated[str, msgspec.Meta(min_length=1)]
    outcome: Annotated[str, msgspec.Meta(min_length=1)]
    timestamp: Annotated[str, msgspec.Meta(min_length=1)]
    source_namespace: str | None = None
    source_ref: RefPayload | None = None
    target_namespace: str | None = None
    target_ref: RefPayload | None = None
    source_record_surface: str | None = None
    target_record_surface: str | None = None
    animap_provider: str | None = None
    animap_id: str | None = None
    animap_scope: str | None = None
    before_state: RecordSnapshot | None = None
    after_state: RecordSnapshot | None = None
    info: dict[str, str] | None = None
    error_message: str | None = None
    ephemeral: bool = False
    source_media: ProviderMediaMetadata | None = None
    target_media: ProviderMediaMetadata | None = None
    pinned_fields: list[str] | None = None


class HistoryPage(msgspec.Struct):
    """Cursor-based history slice wrapper."""

    items: list[HistoryItem]
    limit: int
    has_more: bool
    next_before_id: int | None = None
    latest_id: int | None = None
    stats: dict[str, int] | None = None


def _snapshot_from_json(payload: Mapping[str, Any] | None) -> RecordSnapshot | None:
    """Deserialize a database JSON record snapshot payload."""
    if not payload:
        return None
    return msgspec.convert(payload, type=RecordSnapshot)


class HistoryService:
    """Service to paginate and operate on sync history records."""

    async def _fetch_node_metadata(
        self,
        *,
        namespace: str,
        provider: Any,
        refs: Sequence[Ref],
    ) -> dict[RefKey, ProviderMediaMetadata]:
        """Fetch provider node metadata for history refs when supported."""
        if not refs or not isinstance(provider, SupportsReads):
            return {}

        deduped: dict[RefKey, Ref] = {}
        for ref in refs:
            deduped.setdefault(ref_to_key(ref), ref)

        page = await provider.fetch(
            NodeQuery(
                refs=tuple(deduped.values()),
                facets=frozenset({FacetName.ARTWORK}),
            )
        )
        metadata: dict[RefKey, ProviderMediaMetadata] = {}
        for node in page.items:
            artwork = node.facets.get(FacetName.ARTWORK)
            key = ref_to_key(node.ref)
            metadata[key] = ProviderMediaMetadata(
                namespace=namespace,
                key=node.ref.key,
                title=node.title,
                poster_url=artwork.poster if isinstance(artwork, Artwork) else None,
                external_url=node.url,
                labels=list(node.labels) if node.labels else None,
            )
        return metadata

    async def _build_history_items(
        self,
        profile: str,
        rows: Sequence[SyncHistory],
        *,
        include_source_media: bool = True,
        include_target_media: bool = True,
    ) -> list[HistoryItem]:
        """Convert ORM rows into API DTOs."""
        if not rows:
            return []

        bridge = get_bridge(profile)
        source_refs: list[Ref] = []
        target_refs: list[Ref] = []
        for row in rows:
            if row.source_namespace == bridge.source_provider.NAMESPACE:
                source_ref = ref_from_payload(row.source_ref)
                if source_ref is not None:
                    source_refs.append(source_ref)
            if row.target_namespace == bridge.target_provider.NAMESPACE:
                target_ref = ref_from_payload(row.target_ref)
                if target_ref is not None:
                    target_refs.append(target_ref)
        source_media = (
            await self._fetch_node_metadata(
                namespace=bridge.source_provider.NAMESPACE,
                provider=bridge.source_provider,
                refs=source_refs,
            )
            if include_source_media
            else {}
        )
        target_media = (
            await self._fetch_node_metadata(
                namespace=bridge.target_provider.NAMESPACE,
                provider=bridge.target_provider,
                refs=target_refs,
            )
            if include_target_media
            else {}
        )

        exact_pin_index: dict[tuple[str, RefKey], list[str]] = {}
        anchor_pin_index: dict[tuple[str, str], list[str]] = {}
        has_target_refs = False
        for row in rows:
            if row.target_namespace and ref_payload_from_json(row.target_ref):
                has_target_refs = True
                break

        if has_target_refs:
            with db() as ctx:
                pin_rows = (
                    ctx.session.query(Pin).filter(Pin.profile_name == profile).all()
                )
            for pin in pin_rows:
                pin_ref = ref_from_payload(pin.target_ref)
                if pin_ref is None:
                    continue
                pin_ref_key = ref_to_key(pin_ref)
                pin_fields = list(pin.fields or [])
                exact_pin_index[(pin.target_namespace, pin_ref_key)] = pin_fields
                if pin_ref_key.is_anchor:
                    anchor_pin_index[(pin.target_namespace, pin_ref_key.key)] = (
                        pin_fields
                    )

        items: list[HistoryItem] = []
        for row in rows:
            source_ref = ref_payload_from_json(row.source_ref)
            target_ref = ref_payload_from_json(row.target_ref)
            before_state = _snapshot_from_json(row.before_state)
            after_state = _snapshot_from_json(row.after_state)
            source_ref_value = ref_from_payload(source_ref)
            target_ref_value = ref_from_payload(target_ref)

            pinned_fields: list[str] | None = None
            target_ref_key = (
                ref_to_key(target_ref_value) if target_ref_value is not None else None
            )
            if row.target_namespace is not None and target_ref_key is not None:
                pinned_fields = exact_pin_index.get(
                    (row.target_namespace, target_ref_key)
                )
                if pinned_fields is None:
                    pinned_fields = anchor_pin_index.get(
                        (row.target_namespace, target_ref_key.key)
                    )

            items.append(
                HistoryItem(
                    id=row.id,
                    profile_name=row.profile_name,
                    source_namespace=row.source_namespace,
                    source_ref=source_ref,
                    target_namespace=row.target_namespace,
                    target_ref=target_ref,
                    source_record_surface=row.source_record_surface,
                    target_record_surface=row.target_record_surface,
                    animap_provider=row.animap_provider,
                    animap_id=row.animap_id,
                    animap_scope=row.animap_scope,
                    outcome=str(row.outcome),
                    before_state=before_state,
                    after_state=after_state,
                    info=row.info,
                    error_message=row.error_message,
                    ephemeral=row.ephemeral,
                    timestamp=row.timestamp.isoformat(),
                    source_media=(
                        source_media.get(ref_to_key(source_ref_value))
                        if source_ref_value is not None
                        else None
                    ),
                    target_media=(
                        target_media.get(ref_to_key(target_ref_value))
                        if target_ref_value is not None
                        else None
                    ),
                    pinned_fields=pinned_fields,
                )
            )
        return items

    async def _fetch_profile_stats(
        self,
        profile: str,
        source_namespace: str,
        target_namespace: str,
    ) -> dict[str, int]:
        """Fetch grouped outcome statistics."""
        with db() as ctx:
            stats_rows = (
                ctx.session.query(SyncHistory.outcome, func.count(SyncHistory.id))
                .filter(
                    SyncHistory.profile_name == profile,
                    SyncHistory.source_namespace == source_namespace,
                    SyncHistory.target_namespace == target_namespace,
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
        source_namespace: str | None = None,
        target_namespace: str | None = None,
    ) -> tuple[str, str, list[Any]]:
        """Resolve provider filters and produce SQLAlchemy predicates."""
        bridge = get_bridge(profile)
        effective_source_namespace = (
            source_namespace or bridge.source_provider.NAMESPACE
        )
        effective_target_namespace = (
            target_namespace or bridge.target_provider.NAMESPACE
        )

        base_filters = [
            SyncHistory.profile_name == profile,
            SyncHistory.source_namespace == effective_source_namespace,
            SyncHistory.target_namespace == effective_target_namespace,
        ]
        if outcome:
            base_filters.append(SyncHistory.outcome == outcome)

        return effective_source_namespace, effective_target_namespace, base_filters

    async def get_latest_id(
        self,
        profile: str,
        *,
        outcome: str | None = None,
        source_namespace: str | None = None,
        target_namespace: str | None = None,
    ) -> int | None:
        """Return the most recent history row id for the requested filter scope."""
        _, _, base_filters = await self._resolve_filters(
            profile,
            outcome=outcome,
            source_namespace=source_namespace,
            target_namespace=target_namespace,
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
        source_namespace: str | None = None,
        target_namespace: str | None = None,
        include_source_media: bool = True,
        include_target_media: bool = True,
        include_stats: bool = False,
    ) -> HistoryPage:
        """Return cursor-based history slice."""
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if limit > 250:
            raise ValueError("limit must be <= 250")
        if before_id is not None and after_id is not None:
            raise ValueError("before_id and after_id are mutually exclusive")

        source_namespace, target_namespace, base_filters = await self._resolve_filters(
            profile,
            outcome=outcome,
            source_namespace=source_namespace,
            target_namespace=target_namespace,
        )

        if before_id is not None:
            base_filters.append(SyncHistory.id < before_id)
        if after_id is not None:
            base_filters.append(SyncHistory.id > after_id)

        latest_filters = [
            SyncHistory.profile_name == profile,
            SyncHistory.source_namespace == source_namespace,
            SyncHistory.target_namespace == target_namespace,
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
            include_source_media=include_source_media,
            include_target_media=include_target_media,
        )

        stats = (
            await self._fetch_profile_stats(profile, source_namespace, target_namespace)
            if include_stats
            else None
        )
        return HistoryPage(
            items=dto_items,
            limit=limit,
            has_more=has_more,
            next_before_id=rows[-1].id if rows and has_more else None,
            latest_id=latest_id,
            stats=stats,
        )

    async def delete_item(self, profile: str, item_id: int) -> None:
        """Delete a single history item for a profile."""
        log.info("Deleting history item id=%s for profile %s", item_id, profile)
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

    async def retry_item(self, profile: str, item_id: int) -> None:
        """Retry a failed history item by re-triggering a targeted source scan."""
        log.info("Retrying history item id=%s for profile %s", item_id, profile)

        scheduler = get_app_state().scheduler
        if scheduler is None:
            raise SchedulerNotInitializedError("Scheduler not available")

        with db() as ctx:
            row = (
                ctx.session.query(SyncHistory)
                .filter(SyncHistory.profile_name == profile, SyncHistory.id == item_id)
                .first()
            )
        if row is None:
            raise HistoryItemNotFoundError("Not found")

        bridge = get_bridge(profile)
        if row.source_namespace != bridge.source_provider.NAMESPACE:
            raise HistoryPermissionError(
                "History item belongs to a different source provider"
            )
        if row.outcome not in (SyncOutcome.FAILED, SyncOutcome.NOT_FOUND):
            raise HistoryPermissionError(
                "Retry is only available for failed or not found items"
            )

        source_ref = ref_from_payload(row.source_ref)
        if source_ref is None:
            raise HistoryPermissionError(
                "Cannot retry history item without a source ref"
            )

        schedule_task(
            scheduler.trigger_profile_sync(
                profile,
                request=SyncRequest(
                    trigger=SyncTrigger.MANUAL,
                    source_refs=(source_ref,),
                ),
                source="history:retry_item",
            ),
            name=f"retry_history_item:{profile}:{item_id}",
        )

    async def undo_item(self, profile: str, item_id: int) -> None:
        """Undo a successful history item by restoring its target record state."""
        log.info("Undoing history item id=%s for profile %s", item_id, profile)

        scheduler = get_app_state().scheduler
        if scheduler is None:
            raise SchedulerNotInitializedError("Scheduler not available")

        with db() as ctx:
            row = (
                ctx.session.query(SyncHistory)
                .filter(SyncHistory.profile_name == profile, SyncHistory.id == item_id)
                .first()
            )
        if row is None:
            raise HistoryItemNotFoundError("Not found")

        bridge = get_bridge(profile)
        if row.source_namespace != bridge.source_provider.NAMESPACE:
            raise HistoryPermissionError(
                "History item belongs to a different source provider"
            )
        if row.target_namespace != bridge.target_provider.NAMESPACE:
            raise HistoryPermissionError(
                "History item belongs to a different target provider"
            )
        if row.outcome not in (SyncOutcome.SYNCED, SyncOutcome.DELETED):
            raise HistoryPermissionError(
                "Undo is only available for synced or deleted items"
            )

        source_ref = ref_from_payload(row.source_ref)
        target_ref = ref_from_payload(row.target_ref)
        if source_ref is None or target_ref is None:
            raise HistoryPermissionError(
                "Cannot undo history item without source and target refs"
            )

        before_state = _snapshot_from_json(row.before_state)
        after_state = _snapshot_from_json(row.after_state)
        if before_state is None and after_state is None:
            raise HistoryPermissionError(
                "Cannot undo history item without record state"
            )

        schedule_task(
            scheduler.trigger_profile_sync(
                profile,
                request=SyncRequest(
                    trigger=SyncTrigger.MANUAL,
                    source_refs=(),
                    record_undos=(
                        RecordUndoRequest(
                            source_ref=source_ref,
                            target_ref=target_ref,
                            before=before_state,
                            after=after_state,
                        ),
                    ),
                ),
                source="history:undo_item",
            ),
            name=f"undo_history_item:{profile}:{item_id}",
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
    """Get the singleton HistoryService instance."""
    return HistoryService()
