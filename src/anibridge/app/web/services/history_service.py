"""Grouped sync history service."""

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
from sqlalchemy.orm import selectinload
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
from anibridge.app.models.db.sync_history import (
    SyncHistoryGroup,
    SyncHistoryOperation,
    SyncHistoryRun,
    SyncOutcome,
    SyncResourceKind,
)
from anibridge.app.models.schemas.provider import ProviderMediaMetadata
from anibridge.app.utils.async_tasks import schedule_task
from anibridge.app.web.state import get_app_state, get_bridge

__all__ = [
    "HistoryGroup",
    "HistoryOperation",
    "HistoryPage",
    "HistoryService",
    "get_history_service",
]

log = get_logger(__name__)

_OUTCOME_SEVERITY = {
    SyncOutcome.SKIPPED: 0,
    SyncOutcome.SYNCED: 1,
    SyncOutcome.DELETED: 1,
    SyncOutcome.UNDONE: 1,
    SyncOutcome.PENDING: 2,
    SyncOutcome.NOT_FOUND: 3,
    SyncOutcome.FAILED: 4,
}


class HistoryOperation(msgspec.Struct):
    """Serializable sync operation within a parent item group."""

    id: Annotated[int, msgspec.Meta(ge=1)]
    group_id: Annotated[int, msgspec.Meta(ge=1)]
    profile_name: Annotated[str, msgspec.Meta(min_length=1)]
    resource_kind: Annotated[str, msgspec.Meta(min_length=1)]
    action: Annotated[str, msgspec.Meta(min_length=1)]
    outcome: Annotated[str, msgspec.Meta(min_length=1)]
    timestamp: Annotated[str, msgspec.Meta(min_length=1)]
    source_namespace: str | None = None
    source_ref: RefPayload | None = None
    target_namespace: str | None = None
    target_ref: RefPayload | None = None
    source_surface: str | None = None
    target_surface: str | None = None
    resource_key: str | None = None
    before_state: RecordSnapshot | None = None
    after_state: RecordSnapshot | None = None
    info: dict[str, str] | None = None
    error_message: str | None = None
    ephemeral: bool = False
    pinned_fields: list[str] | None = None


class HistoryGroup(msgspec.Struct):
    """Serializable parent item group in the sync timeline."""

    id: Annotated[int, msgspec.Meta(ge=1)]
    run_id: Annotated[int, msgspec.Meta(ge=1)]
    profile_name: Annotated[str, msgspec.Meta(min_length=1)]
    outcome: Annotated[str, msgspec.Meta(min_length=1)]
    timestamp: Annotated[str, msgspec.Meta(min_length=1)]
    source_namespace: str | None = None
    source_parent_ref: RefPayload | None = None
    target_namespace: str | None = None
    target_parent_ref: RefPayload | None = None
    animap_authority: str | None = None
    animap_value: str | None = None
    animap_scope: str | None = None
    operation_count: int = 0
    record_count: int = 0
    event_count: int = 0
    node_count: int = 0
    error_count: int = 0
    info: dict[str, str] | None = None
    ephemeral: bool = False
    source_media: ProviderMediaMetadata | None = None
    target_media: ProviderMediaMetadata | None = None
    operations: list[HistoryOperation] = msgspec.field(default_factory=list)


class HistoryPage(msgspec.Struct):
    """Cursor-based grouped history slice wrapper."""

    groups: list[HistoryGroup]
    limit: int
    has_more: bool
    next_before_id: int | None = None
    latest_group_id: int | None = None
    stats: dict[str, int] | None = None
    resource_stats: dict[str, int] | None = None


def _snapshot_from_json(payload: Mapping[str, Any] | None) -> RecordSnapshot | None:
    """Deserialize a database JSON record snapshot payload."""
    if not payload:
        return None
    return msgspec.convert(payload, type=RecordSnapshot)


def _aggregate_outcome(outcomes: Sequence[SyncOutcome]) -> SyncOutcome:
    if not outcomes:
        return SyncOutcome.SKIPPED
    return max(outcomes, key=lambda outcome: _OUTCOME_SEVERITY[outcome])


class HistoryService:
    """Service to paginate and operate on grouped sync history."""

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

    async def _build_history_groups(
        self,
        profile: str,
        rows: Sequence[SyncHistoryGroup],
        *,
        include_source_media: bool = True,
        include_target_media: bool = True,
    ) -> list[HistoryGroup]:
        """Convert ORM group rows into API DTOs."""
        if not rows:
            return []

        bridge = get_bridge(profile)
        source_refs: list[Ref] = []
        target_refs: list[Ref] = []
        for row in rows:
            if row.source_namespace == bridge.source_provider.NAMESPACE:
                source_ref = ref_from_payload(row.source_parent_ref)
                if source_ref is not None:
                    source_refs.append(source_ref)
            if row.target_namespace == bridge.target_provider.NAMESPACE:
                target_ref = ref_from_payload(row.target_parent_ref)
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

        exact_pin_index, anchor_pin_index = self._pin_indexes(profile, rows)

        groups: list[HistoryGroup] = []
        for row in rows:
            source_parent = ref_payload_from_json(row.source_parent_ref)
            target_parent = ref_payload_from_json(row.target_parent_ref)
            source_parent_value = ref_from_payload(source_parent)
            target_parent_value = ref_from_payload(target_parent)

            operations: list[HistoryOperation] = []
            for operation in sorted(row.operations, key=lambda item: item.timestamp):
                source_ref = ref_payload_from_json(operation.source_ref)
                target_ref = ref_payload_from_json(operation.target_ref)
                target_ref_value = ref_from_payload(target_ref)
                operations.append(
                    HistoryOperation(
                        id=operation.id,
                        group_id=operation.group_id,
                        profile_name=operation.profile_name,
                        resource_kind=str(operation.resource_kind),
                        action=str(operation.action),
                        outcome=str(operation.outcome),
                        timestamp=operation.timestamp.isoformat(),
                        source_namespace=operation.source_namespace,
                        source_ref=source_ref,
                        target_namespace=operation.target_namespace,
                        target_ref=target_ref,
                        source_surface=operation.source_surface,
                        target_surface=operation.target_surface,
                        resource_key=operation.resource_key,
                        before_state=_snapshot_from_json(operation.before_state),
                        after_state=_snapshot_from_json(operation.after_state),
                        info=operation.info,
                        error_message=operation.error_message,
                        ephemeral=operation.ephemeral,
                        pinned_fields=self._pinned_fields(
                            namespace=operation.target_namespace,
                            ref=target_ref_value,
                            exact_pin_index=exact_pin_index,
                            anchor_pin_index=anchor_pin_index,
                        )
                        if operation.resource_kind == SyncResourceKind.RECORD
                        else None,
                    )
                )

            groups.append(
                HistoryGroup(
                    id=row.id,
                    run_id=row.run_id,
                    profile_name=row.profile_name,
                    source_namespace=row.source_namespace,
                    source_parent_ref=source_parent,
                    target_namespace=row.target_namespace,
                    target_parent_ref=target_parent,
                    animap_authority=row.animap_authority,
                    animap_value=row.animap_value,
                    animap_scope=row.animap_scope,
                    outcome=str(row.outcome),
                    operation_count=row.operation_count,
                    record_count=row.record_count,
                    event_count=row.event_count,
                    node_count=row.node_count,
                    error_count=row.error_count,
                    info=row.info,
                    ephemeral=row.ephemeral,
                    timestamp=row.timestamp.isoformat(),
                    source_media=(
                        source_media.get(ref_to_key(source_parent_value))
                        if source_parent_value is not None
                        else None
                    ),
                    target_media=(
                        target_media.get(ref_to_key(target_parent_value))
                        if target_parent_value is not None
                        else None
                    ),
                    operations=operations,
                )
            )
        return groups

    def _pin_indexes(
        self,
        profile: str,
        rows: Sequence[SyncHistoryGroup],
    ) -> tuple[dict[tuple[str, RefKey], list[str]], dict[tuple[str, str], list[str]]]:
        has_target_refs = any(
            operation.target_ref
            for row in rows
            for operation in row.operations
            if operation.resource_kind == SyncResourceKind.RECORD
        )
        if not has_target_refs:
            return {}, {}

        with db() as ctx:
            pin_rows = ctx.session.query(Pin).filter(Pin.profile_name == profile).all()

        exact_pin_index: dict[tuple[str, RefKey], list[str]] = {}
        anchor_pin_index: dict[tuple[str, str], list[str]] = {}
        for pin in pin_rows:
            pin_ref = ref_from_payload(pin.target_ref)
            if pin_ref is None:
                continue
            pin_ref_key = ref_to_key(pin_ref)
            pin_fields = list(pin.fields or [])
            exact_pin_index[(pin.target_namespace, pin_ref_key)] = pin_fields
            if pin_ref_key.is_anchor:
                anchor_pin_index[(pin.target_namespace, pin_ref_key.key)] = pin_fields
        return exact_pin_index, anchor_pin_index

    @staticmethod
    def _pinned_fields(
        *,
        namespace: str | None,
        ref: Ref | None,
        exact_pin_index: dict[tuple[str, RefKey], list[str]],
        anchor_pin_index: dict[tuple[str, str], list[str]],
    ) -> list[str] | None:
        if namespace is None or ref is None:
            return None
        ref_key = ref_to_key(ref)
        return exact_pin_index.get((namespace, ref_key)) or anchor_pin_index.get(
            (namespace, ref_key.key)
        )

    async def _fetch_profile_stats(
        self,
        profile: str,
        source_namespace: str,
        target_namespace: str,
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Fetch grouped outcome and resource statistics."""
        with db() as ctx:
            stats_rows = (
                ctx.session.query(
                    SyncHistoryGroup.outcome, func.count(SyncHistoryGroup.id)
                )
                .filter(
                    SyncHistoryGroup.profile_name == profile,
                    SyncHistoryGroup.source_namespace == source_namespace,
                    SyncHistoryGroup.target_namespace == target_namespace,
                )
                .group_by(SyncHistoryGroup.outcome)
                .all()
            )
            resource_rows = (
                ctx.session.query(
                    SyncHistoryOperation.resource_kind,
                    func.count(SyncHistoryOperation.id),
                )
                .filter(SyncHistoryOperation.profile_name == profile)
                .group_by(SyncHistoryOperation.resource_kind)
                .all()
            )
        return (
            {str(outcome): count for outcome, count in stats_rows},
            {str(resource_kind): count for resource_kind, count in resource_rows},
        )

    async def _resolve_filters(
        self,
        profile: str,
        *,
        outcome: str | None = None,
        source_namespace: str | None = None,
        target_namespace: str | None = None,
        resource_kind: str | None = None,
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
            SyncHistoryGroup.profile_name == profile,
            SyncHistoryGroup.source_namespace == effective_source_namespace,
            SyncHistoryGroup.target_namespace == effective_target_namespace,
        ]
        if outcome:
            base_filters.append(SyncHistoryGroup.outcome == outcome)
        if resource_kind:
            base_filters.append(
                SyncHistoryGroup.operations.any(
                    SyncHistoryOperation.resource_kind == resource_kind
                )
            )

        return effective_source_namespace, effective_target_namespace, base_filters

    async def get_latest_id(
        self,
        profile: str,
        *,
        outcome: str | None = None,
        source_namespace: str | None = None,
        target_namespace: str | None = None,
        resource_kind: str | None = None,
    ) -> int | None:
        """Return the most recent history group id for the requested scope."""
        _, _, base_filters = await self._resolve_filters(
            profile,
            outcome=outcome,
            source_namespace=source_namespace,
            target_namespace=target_namespace,
            resource_kind=resource_kind,
        )
        with db() as ctx:
            latest_stmt = select(func.max(SyncHistoryGroup.id)).where(*base_filters)
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
        resource_kind: str | None = None,
        include_source_media: bool = True,
        include_target_media: bool = True,
        include_stats: bool = False,
    ) -> HistoryPage:
        """Return cursor-based grouped history slice."""
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
            resource_kind=resource_kind,
        )

        if before_id is not None:
            base_filters.append(SyncHistoryGroup.id < before_id)
        if after_id is not None:
            base_filters.append(SyncHistoryGroup.id > after_id)

        latest_filters = [
            SyncHistoryGroup.profile_name == profile,
            SyncHistoryGroup.source_namespace == source_namespace,
            SyncHistoryGroup.target_namespace == target_namespace,
        ]
        if outcome:
            latest_filters.append(SyncHistoryGroup.outcome == outcome)
        if resource_kind:
            latest_filters.append(
                SyncHistoryGroup.operations.any(
                    SyncHistoryOperation.resource_kind == resource_kind
                )
            )

        with db() as ctx:
            latest_stmt = select(func.max(SyncHistoryGroup.id)).where(*latest_filters)
            latest_id = ctx.session.execute(latest_stmt).scalar_one_or_none()
            stmt = (
                select(SyncHistoryGroup)
                .options(selectinload(SyncHistoryGroup.operations))
                .where(*base_filters)
                .order_by(SyncHistoryGroup.timestamp.desc())
                .limit(limit + 1)
            )
            rows = ctx.session.execute(stmt).scalars().all()
            rows = list(rows)

        has_more = len(rows) > limit
        rows = rows[:limit]
        dto_groups = await self._build_history_groups(
            profile,
            rows,
            include_source_media=include_source_media,
            include_target_media=include_target_media,
        )

        stats: dict[str, int] | None = None
        resource_stats: dict[str, int] | None = None
        if include_stats:
            stats, resource_stats = await self._fetch_profile_stats(
                profile,
                source_namespace,
                target_namespace,
            )
        return HistoryPage(
            groups=dto_groups,
            limit=limit,
            has_more=has_more,
            next_before_id=rows[-1].id if rows and has_more else None,
            latest_group_id=latest_id,
            stats=stats,
            resource_stats=resource_stats,
        )

    async def delete_group(self, profile: str, group_id: int) -> None:
        """Delete a history group for a profile."""
        log.info("Deleting history group id=%s for profile %s", group_id, profile)
        with db() as ctx:
            row = (
                ctx.session.query(SyncHistoryGroup)
                .filter(
                    SyncHistoryGroup.profile_name == profile,
                    SyncHistoryGroup.id == group_id,
                )
                .first()
            )
            if not row:
                raise HistoryItemNotFoundError("Not found")
            ctx.session.delete(row)
            ctx.session.commit()

    async def delete_operation(self, profile: str, operation_id: int) -> None:
        """Delete one history operation for a profile."""
        log.info(
            "Deleting history operation id=%s for profile %s", operation_id, profile
        )
        with db() as ctx:
            row = (
                ctx.session.query(SyncHistoryOperation)
                .filter(
                    SyncHistoryOperation.profile_name == profile,
                    SyncHistoryOperation.id == operation_id,
                )
                .first()
            )
            if not row:
                raise HistoryItemNotFoundError("Not found")
            group_id = row.group_id
            ctx.session.delete(row)
            ctx.session.flush()
            self._refresh_group(ctx, group_id)
            ctx.session.commit()

    async def retry_group(self, profile: str, group_id: int) -> None:
        """Retry a failed history group by re-triggering a targeted source scan."""
        log.info("Retrying history group id=%s for profile %s", group_id, profile)

        scheduler = get_app_state().scheduler
        if scheduler is None:
            raise SchedulerNotInitializedError("Scheduler not available")

        with db() as ctx:
            row = (
                ctx.session.query(SyncHistoryGroup)
                .filter(
                    SyncHistoryGroup.profile_name == profile,
                    SyncHistoryGroup.id == group_id,
                )
                .first()
            )
        if row is None:
            raise HistoryItemNotFoundError("Not found")

        bridge = get_bridge(profile)
        if row.source_namespace != bridge.source_provider.NAMESPACE:
            raise HistoryPermissionError(
                "History group belongs to a different source provider"
            )
        if row.outcome not in (SyncOutcome.FAILED, SyncOutcome.NOT_FOUND):
            raise HistoryPermissionError(
                "Retry is only available for failed or not found groups"
            )

        source_ref = ref_from_payload(row.source_parent_ref)
        if source_ref is None:
            raise HistoryPermissionError(
                "Cannot retry history group without a source ref"
            )

        schedule_task(
            scheduler.trigger_profile_sync(
                profile,
                request=SyncRequest(
                    trigger=SyncTrigger.MANUAL,
                    source_refs=(source_ref,),
                ),
                source="history:retry_group",
            ),
            name=f"retry_history_group:{profile}:{group_id}",
        )

    async def undo_operation(self, profile: str, operation_id: int) -> None:
        """Undo a successful record operation by restoring target record state."""
        log.info(
            "Undoing history operation id=%s for profile %s", operation_id, profile
        )

        scheduler = get_app_state().scheduler
        if scheduler is None:
            raise SchedulerNotInitializedError("Scheduler not available")

        with db() as ctx:
            row = (
                ctx.session.query(SyncHistoryOperation)
                .filter(
                    SyncHistoryOperation.profile_name == profile,
                    SyncHistoryOperation.id == operation_id,
                )
                .first()
            )
        if row is None:
            raise HistoryItemNotFoundError("Not found")

        bridge = get_bridge(profile)
        if row.resource_kind != SyncResourceKind.RECORD:
            raise HistoryPermissionError("Undo is only available for record operations")
        if row.source_namespace != bridge.source_provider.NAMESPACE:
            raise HistoryPermissionError(
                "History operation belongs to a different source provider"
            )
        if row.target_namespace != bridge.target_provider.NAMESPACE:
            raise HistoryPermissionError(
                "History operation belongs to a different target provider"
            )
        if row.outcome not in (SyncOutcome.SYNCED, SyncOutcome.DELETED):
            raise HistoryPermissionError(
                "Undo is only available for synced or deleted operations"
            )

        source_ref = ref_from_payload(row.source_ref)
        target_ref = ref_from_payload(row.target_ref)
        if source_ref is None or target_ref is None:
            raise HistoryPermissionError(
                "Cannot undo history operation without source and target refs"
            )

        before_state = _snapshot_from_json(row.before_state)
        after_state = _snapshot_from_json(row.after_state)
        if before_state is None and after_state is None:
            raise HistoryPermissionError(
                "Cannot undo history operation without record state"
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
                source="history:undo_operation",
            ),
            name=f"undo_history_operation:{profile}:{operation_id}",
        )

    async def purge_ephemeral_items(self) -> int:
        """Delete ephemeral history groups and runs."""
        with db() as ctx:
            count = (
                ctx.session.query(SyncHistoryGroup)
                .filter(SyncHistoryGroup.ephemeral.is_(True))
                .count()
            )
            if not count:
                return 0
            (
                ctx.session.query(SyncHistoryGroup)
                .filter(SyncHistoryGroup.ephemeral.is_(True))
                .delete(synchronize_session=False)
            )
            (
                ctx.session.query(SyncHistoryRun)
                .filter(SyncHistoryRun.ephemeral.is_(True))
                .delete(synchronize_session=False)
            )
            ctx.session.commit()
        return count

    def _refresh_group(self, ctx: Any, group_id: int) -> None:
        group = ctx.session.get(SyncHistoryGroup, group_id)
        if group is None:
            return
        operations = tuple(group.operations)
        if not operations:
            ctx.session.delete(group)
            return
        group.operation_count = len(operations)
        group.record_count = sum(
            1
            for operation in operations
            if operation.resource_kind == SyncResourceKind.RECORD
        )
        group.event_count = sum(
            1
            for operation in operations
            if operation.resource_kind == SyncResourceKind.EVENT
        )
        group.node_count = sum(
            1
            for operation in operations
            if operation.resource_kind == SyncResourceKind.NODE
        )
        group.error_count = sum(
            1
            for operation in operations
            if operation.outcome in (SyncOutcome.FAILED, SyncOutcome.NOT_FOUND)
        )
        group.outcome = _aggregate_outcome(
            tuple(operation.outcome for operation in operations)
        )
        group.ephemeral = all(operation.ephemeral for operation in operations)
        group.timestamp = max(operations, key=lambda operation: operation.id).timestamp


@cache
def get_history_service() -> HistoryService:
    """Get the singleton HistoryService instance."""
    return HistoryService()
