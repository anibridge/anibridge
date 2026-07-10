"""Sync history persistence helpers for normalized provider resources."""

from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, cast

import msgspec
from anibridge.provider.base import ExternalId, Node, Record, Ref

from anibridge.app.core.sync import RefKey, ref_to_json, ref_to_key
from anibridge.app.core.sync.stats import RecordSnapshot
from anibridge.app.logging import get_logger
from anibridge.app.models.db.sync_history import (
    SyncHistoryGroup,
    SyncHistoryOperation,
    SyncHistoryRun,
    SyncOperationAction,
    SyncOutcome,
    SyncResourceKind,
)

__all__ = ["FAILURE_HISTORY_CLEANUP_BATCH_SIZE", "SyncHistoryManager", "to_builtins"]

FAILURE_HISTORY_CLEANUP_BATCH_SIZE = 256
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


def to_builtins(value: Any) -> Any:
    """Serialize normalized provider values for JSON columns."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        dt = (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )
        return dt.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Ref):
        return ref_to_json(value)
    if isinstance(value, msgspec.Struct):
        return to_builtins(msgspec.to_builtins(value))
    if is_dataclass(value):
        return to_builtins(asdict(value))
    if isinstance(value, Mapping):
        return {str(to_builtins(key)): to_builtins(item) for key, item in value.items()}
    if isinstance(value, tuple | list | frozenset | set):
        return [to_builtins(item) for item in value]
    return str(value)


def _info_string(value: object) -> str:
    """Return one JSON info value as a string."""
    built = to_builtins(value)
    if built is None:
        return ""
    if isinstance(built, str):
        return built
    if isinstance(built, int | float | bool):
        return str(built).lower() if isinstance(built, bool) else str(built)
    return msgspec.json.encode(built).decode()


def _info_mapping(*items: Mapping[str, object] | None) -> dict[str, str]:
    """Merge debug info mappings while preserving the database string contract."""
    info: dict[str, str] = {}
    for mapping in items:
        if not mapping:
            continue
        for key, value in mapping.items():
            if not key:
                continue
            text = _info_string(value)
            if text:
                info[str(key)] = text
    return info


def _anchor_ref(ref: Ref) -> Ref:
    """Return the parent anchor for a provider ref."""
    return ref if ref.is_anchor else Ref.anchor(ref.key)


def _aggregate_outcome(outcomes: tuple[SyncOutcome, ...]) -> SyncOutcome:
    """Return the highest-severity outcome for a group or run."""
    if not outcomes:
        return SyncOutcome.SKIPPED
    return max(outcomes, key=lambda outcome: _OUTCOME_SEVERITY[outcome])


class SyncHistoryManager:
    """Persist and clean up grouped synchronization history."""

    def __init__(
        self,
        *,
        profile_name: str,
        source_namespace: str,
        target_namespace: str,
        db_factory: Callable[[], Any],
    ) -> None:
        """Initialize history persistence helpers."""
        self.profile_name = profile_name
        self.source_namespace = source_namespace
        self.target_namespace = target_namespace
        self._db_factory = db_factory
        self._run_id: int | None = None
        self._failure_history_cleanup_queue: set[tuple[RefKey, RefKey | None]] = set()

    def start_run(
        self,
        *,
        trigger: str | None = None,
        source: str | None = None,
        info: Mapping[str, object] | None = None,
        ephemeral: bool = False,
    ) -> int:
        """Create the run row for this manager if needed."""
        if self._run_id is not None:
            return self._run_id

        with self._db_factory() as ctx:
            row = SyncHistoryRun(
                profile_name=self.profile_name,
                source_namespace=self.source_namespace,
                target_namespace=self.target_namespace,
                trigger=trigger,
                source=source,
                outcome=SyncOutcome.SKIPPED,
                info=_info_mapping(info),
                ephemeral=ephemeral,
            )
            ctx.session.add(row)
            ctx.session.flush()
            self._run_id = row.id
            ctx.session.commit()
        return self._run_id

    def complete_run(self) -> None:
        """Mark the current run complete and aggregate its group outcomes."""
        if self._run_id is None:
            return

        with self._db_factory() as ctx:
            row = ctx.session.get(SyncHistoryRun, self._run_id)
            if row is None:
                return
            outcomes = tuple(group.outcome for group in row.groups)
            row.outcome = _aggregate_outcome(outcomes)
            row.completed_at = datetime.now(UTC)
            ctx.session.commit()

    async def create_sync_history(
        self,
        *,
        source_node: Node,
        source_record: Record | None,
        target_ref: Ref | None,
        snapshots: tuple[RecordSnapshot | None, RecordSnapshot | None],
        outcome: SyncOutcome,
        external_id: ExternalId | None = None,
        error_message: str | None = None,
        info: Mapping[str, str] | None = None,
        ephemeral: bool = False,
        dedupe_failures: bool = True,
    ) -> None:
        """Persist a record operation using the grouped history schema."""
        await self.record_record_operation(
            source_node=source_node,
            source_record=source_record,
            target_ref=target_ref,
            snapshots=snapshots,
            outcome=outcome,
            external_id=external_id,
            error_message=error_message,
            info=info,
            ephemeral=ephemeral,
            dedupe_failures=dedupe_failures,
            action=SyncOperationAction.UNDO
            if outcome == SyncOutcome.UNDONE
            else SyncOperationAction.DELETE
            if outcome == SyncOutcome.DELETED
            else SyncOperationAction.UPSERT,
        )

    async def record_not_found(
        self,
        *,
        source_node: Node,
        source_record: Record | None,
        external_id: ExternalId | None = None,
        error_message: str | None = None,
        info: Mapping[str, object] | None = None,
        ephemeral: bool = False,
    ) -> None:
        """Persist a targetless not-found group without a child operation."""
        self.start_run(ephemeral=ephemeral)
        source_ref = source_record.ref if source_record else source_node.ref
        history_info = _info_mapping(
            {
                "source_title": source_node.title,
                "source_node_kind": source_node.kind,
                "source_record_key": source_record.key if source_record else None,
                "source_ref": repr(source_ref),
            },
            info,
            {"error_message": error_message},
        )
        now = datetime.now(UTC)

        with self._db_factory() as ctx:
            group = self._get_or_create_group(
                ctx,
                source_parent_ref=_anchor_ref(source_ref),
                target_parent_ref=None,
                external_id=external_id,
                ephemeral=ephemeral,
                timestamp=now,
            )
            group.outcome = SyncOutcome.NOT_FOUND
            group.error_count = 1
            group.info = history_info
            group.ephemeral = ephemeral
            group.timestamp = now
            ctx.session.commit()

    async def record_record_operation(
        self,
        *,
        source_node: Node,
        source_record: Record | None,
        target_ref: Ref | None,
        snapshots: tuple[RecordSnapshot | None, RecordSnapshot | None],
        outcome: SyncOutcome,
        action: SyncOperationAction = SyncOperationAction.UPSERT,
        external_id: ExternalId | None = None,
        error_message: str | None = None,
        info: Mapping[str, object] | None = None,
        ephemeral: bool = False,
        dedupe_failures: bool = True,
    ) -> None:
        """Persist one record sync operation."""
        before_snapshot, after_snapshot = snapshots
        before_state = to_builtins(before_snapshot) if before_snapshot else None
        after_state = to_builtins(after_snapshot) if after_snapshot else None
        source_ref = source_record.ref if source_record else source_node.ref
        source_surface = source_record.surface if source_record else None
        if after_snapshot is not None:
            target_surface = after_snapshot.surface
        elif before_snapshot is not None:
            target_surface = before_snapshot.surface
        else:
            target_surface = None

        history_info = _info_mapping(
            {
                "source_title": source_node.title,
                "source_node_kind": source_node.kind,
                "source_record_key": source_record.key if source_record else None,
                "source_ref": repr(source_ref),
                "target_ref": repr(target_ref) if target_ref else None,
                "resource_kind": SyncResourceKind.RECORD,
                "action": action,
            },
            info,
        )

        await self.record_operation(
            source_ref=source_ref,
            target_ref=target_ref,
            resource_kind=SyncResourceKind.RECORD,
            action=action,
            outcome=outcome,
            source_surface=source_surface,
            target_surface=target_surface,
            resource_key=source_record.key if source_record else None,
            before_state=before_state,
            after_state=after_state,
            external_id=external_id,
            error_message=error_message,
            info=history_info,
            ephemeral=ephemeral,
            dedupe_failures=dedupe_failures,
        )

    async def record_event_operation(
        self,
        *,
        source_ref: Ref,
        target_ref: Ref | None,
        action: SyncOperationAction,
        outcome: SyncOutcome,
        event_kind: str,
        event_at: datetime | None = None,
        dedupe_key: str | None = None,
        resource_key: str | None = None,
        error_message: str | None = None,
        info: Mapping[str, object] | None = None,
        ephemeral: bool = False,
        dedupe_failures: bool = True,
    ) -> None:
        """Persist one event sync operation."""
        await self.record_operation(
            source_ref=source_ref,
            target_ref=target_ref,
            resource_kind=SyncResourceKind.EVENT,
            action=action,
            outcome=outcome,
            source_surface=event_kind,
            target_surface=event_kind,
            resource_key=resource_key or dedupe_key,
            external_id=None,
            error_message=error_message,
            info=_info_mapping(
                {
                    "event_kind": event_kind,
                    "event_at": event_at,
                    "dedupe_key": dedupe_key,
                    "source_ref": repr(source_ref),
                    "target_ref": repr(target_ref) if target_ref else None,
                    "resource_kind": SyncResourceKind.EVENT,
                    "action": action,
                },
                info,
            ),
            ephemeral=ephemeral,
            dedupe_failures=dedupe_failures,
        )

    async def record_node_operation(
        self,
        *,
        source_ref: Ref,
        target_ref: Ref | None,
        action: SyncOperationAction,
        outcome: SyncOutcome,
        node_kind: str,
        resource_key: str | None = None,
        error_message: str | None = None,
        info: Mapping[str, object] | None = None,
        ephemeral: bool = False,
        dedupe_failures: bool = True,
    ) -> None:
        """Persist one target node mutation operation."""
        await self.record_operation(
            source_ref=source_ref,
            target_ref=target_ref,
            resource_kind=SyncResourceKind.NODE,
            action=action,
            outcome=outcome,
            source_surface=node_kind,
            target_surface=node_kind,
            resource_key=resource_key,
            external_id=None,
            error_message=error_message,
            info=_info_mapping(
                {
                    "node_kind": node_kind,
                    "source_ref": repr(source_ref),
                    "target_ref": repr(target_ref) if target_ref else None,
                    "resource_kind": SyncResourceKind.NODE,
                    "action": action,
                },
                info,
            ),
            ephemeral=ephemeral,
            dedupe_failures=dedupe_failures,
        )

    async def record_operation(
        self,
        *,
        source_ref: Ref,
        target_ref: Ref | None,
        resource_kind: SyncResourceKind,
        action: SyncOperationAction,
        outcome: SyncOutcome,
        source_surface: str | None = None,
        target_surface: str | None = None,
        resource_key: str | None = None,
        before_state: Mapping[str, Any] | None = None,
        after_state: Mapping[str, Any] | None = None,
        external_id: ExternalId | None = None,
        error_message: str | None = None,
        info: Mapping[str, object] | None = None,
        ephemeral: bool = False,
        dedupe_failures: bool = True,
    ) -> None:
        """Persist one sync operation and refresh its parent group."""
        if outcome == SyncOutcome.SKIPPED:
            return

        self.start_run(ephemeral=ephemeral)
        if outcome == SyncOutcome.SYNCED:
            self.queue_failure_history_cleanup(
                source_ref=source_ref,
                target_ref=target_ref,
            )

        source_parent_ref = _anchor_ref(source_ref)
        target_parent_ref = _anchor_ref(target_ref) if target_ref is not None else None
        now = datetime.now(UTC)

        with self._db_factory() as ctx:
            group = self._get_or_create_group(
                ctx,
                source_parent_ref=source_parent_ref,
                target_parent_ref=target_parent_ref,
                external_id=external_id,
                ephemeral=ephemeral,
                timestamp=now,
            )

            if dedupe_failures and outcome in (
                SyncOutcome.NOT_FOUND,
                SyncOutcome.FAILED,
            ):
                existing = self._matching_failure_operation(
                    ctx,
                    source_ref=source_ref,
                    target_ref=target_ref,
                    resource_kind=resource_kind,
                    outcome=outcome,
                )
                if existing is not None:
                    existing.group_id = group.id
                    existing_row = cast(Any, existing)
                    existing_row.action = action
                    existing_row.source_surface = source_surface
                    existing_row.target_surface = target_surface
                    existing_row.resource_key = resource_key
                    existing_row.before_state = before_state
                    existing_row.after_state = after_state
                    existing_row.info = _info_mapping(info)
                    existing_row.error_message = error_message
                    existing_row.ephemeral = ephemeral
                    existing_row.timestamp = now
                    self._refresh_group(ctx, group.id)
                    ctx.session.commit()
                    return

            operation = SyncHistoryOperation(
                group_id=group.id,
                profile_name=self.profile_name,
                resource_kind=resource_kind,
                action=action,
                source_namespace=self.source_namespace,
                source_ref=ref_to_json(source_ref),
                target_namespace=self.target_namespace,
                target_ref=ref_to_json(target_ref) if target_ref else None,
                source_surface=source_surface,
                target_surface=target_surface,
                resource_key=resource_key,
                outcome=outcome,
                before_state=before_state,
                after_state=after_state,
                info=_info_mapping(info),
                error_message=error_message,
                ephemeral=ephemeral,
                timestamp=now,
            )
            ctx.session.add(operation)
            ctx.session.flush()
            self._refresh_group(ctx, group.id)
            ctx.session.commit()

    def _get_or_create_group(
        self,
        ctx: Any,
        *,
        source_parent_ref: Ref,
        target_parent_ref: Ref | None,
        external_id: ExternalId | None,
        ephemeral: bool,
        timestamp: datetime,
    ) -> SyncHistoryGroup:
        source_parent_json = ref_to_json(source_parent_ref)
        target_parent_json = (
            ref_to_json(target_parent_ref) if target_parent_ref else None
        )

        query = ctx.session.query(SyncHistoryGroup).filter(
            SyncHistoryGroup.run_id == self._run_id,
            SyncHistoryGroup.source_namespace == self.source_namespace,
            SyncHistoryGroup.source_parent_ref == source_parent_json,
            SyncHistoryGroup.target_namespace == self.target_namespace,
            SyncHistoryGroup.animap_authority
            == (external_id.authority if external_id else None),
            SyncHistoryGroup.animap_value
            == (external_id.value if external_id else None),
            SyncHistoryGroup.animap_scope
            == (external_id.scope if external_id else None),
        )
        if target_parent_json is None:
            query = query.filter(SyncHistoryGroup.target_parent_ref.is_(None))
        else:
            query = query.filter(
                SyncHistoryGroup.target_parent_ref == target_parent_json
            )

        existing = query.first()
        if existing is not None:
            return existing

        group = SyncHistoryGroup(
            run_id=self._run_id,
            profile_name=self.profile_name,
            source_namespace=self.source_namespace,
            source_parent_ref=source_parent_json,
            target_namespace=self.target_namespace,
            target_parent_ref=target_parent_json,
            animap_authority=external_id.authority if external_id else None,
            animap_value=external_id.value if external_id else None,
            animap_scope=external_id.scope if external_id else None,
            outcome=SyncOutcome.SKIPPED,
            operation_count=0,
            record_count=0,
            event_count=0,
            node_count=0,
            error_count=0,
            info={},
            ephemeral=ephemeral,
            timestamp=timestamp,
        )
        ctx.session.add(group)
        ctx.session.flush()
        return group

    def _matching_failure_operation(
        self,
        ctx: Any,
        *,
        source_ref: Ref,
        target_ref: Ref | None,
        resource_kind: SyncResourceKind,
        outcome: SyncOutcome,
    ) -> SyncHistoryOperation | None:
        query = ctx.session.query(SyncHistoryOperation).filter(
            SyncHistoryOperation.profile_name == self.profile_name,
            SyncHistoryOperation.source_namespace == self.source_namespace,
            SyncHistoryOperation.source_ref == ref_to_json(source_ref),
            SyncHistoryOperation.target_namespace == self.target_namespace,
            SyncHistoryOperation.resource_kind == resource_kind,
            SyncHistoryOperation.outcome == outcome,
        )
        if target_ref is None:
            query = query.filter(SyncHistoryOperation.target_ref.is_(None))
        else:
            query = query.filter(
                SyncHistoryOperation.target_ref == ref_to_json(target_ref)
            )
        return query.order_by(SyncHistoryOperation.timestamp.desc()).first()

    def _refresh_group(self, ctx: Any, group_id: int) -> None:
        group = ctx.session.get(SyncHistoryGroup, group_id)
        if group is None:
            return
        operations = tuple(group.operations)
        if not operations:
            if group.outcome != SyncOutcome.NOT_FOUND:
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

    def flush_failure_history_cleanup(self) -> None:
        """Delete stale NOT_FOUND and FAILED operations for successfully synced refs."""
        if not self._failure_history_cleanup_queue:
            return

        targets = tuple(self._failure_history_cleanup_queue)

        with self._db_factory() as ctx:
            touched_group_ids: set[int] = set()
            for start in range(0, len(targets), FAILURE_HISTORY_CLEANUP_BATCH_SIZE):
                chunk = targets[start : start + FAILURE_HISTORY_CLEANUP_BATCH_SIZE]
                for source_key, target_key in chunk:
                    if target_key is None:
                        group_rows = (
                            ctx.session.query(SyncHistoryGroup)
                            .filter(
                                SyncHistoryGroup.profile_name == self.profile_name,
                                SyncHistoryGroup.source_namespace
                                == self.source_namespace,
                                SyncHistoryGroup.source_parent_ref
                                == RefKey(key=source_key.key).to_json(),
                                SyncHistoryGroup.target_namespace
                                == self.target_namespace,
                                SyncHistoryGroup.target_parent_ref.is_(None),
                                SyncHistoryGroup.outcome == SyncOutcome.NOT_FOUND,
                            )
                            .all()
                        )
                        for group in group_rows:
                            ctx.session.delete(group)

                    query = ctx.session.query(SyncHistoryOperation).filter(
                        SyncHistoryOperation.profile_name == self.profile_name,
                        SyncHistoryOperation.source_namespace == self.source_namespace,
                        SyncHistoryOperation.source_ref == source_key.to_json(),
                        SyncHistoryOperation.target_namespace == self.target_namespace,
                        SyncHistoryOperation.outcome.in_(
                            [SyncOutcome.NOT_FOUND, SyncOutcome.FAILED]
                        ),
                    )
                    if target_key is None:
                        query = query.filter(SyncHistoryOperation.target_ref.is_(None))
                    else:
                        query = query.filter(
                            SyncHistoryOperation.target_ref == target_key.to_json()
                        )
                    rows = query.all()
                    touched_group_ids.update(row.group_id for row in rows)
                    for row in rows:
                        ctx.session.delete(row)

            ctx.session.flush()
            for group_id in touched_group_ids:
                self._refresh_group(ctx, group_id)
            ctx.session.commit()

        self._failure_history_cleanup_queue.difference_update(targets)
        log.debug(
            "[%s] Cleaned up failure history for %s cached targets",
            self.profile_name,
            len(targets),
        )

    def queue_failure_history_cleanup(
        self,
        *,
        source_ref: Ref,
        target_ref: Ref | None = None,
    ) -> None:
        """Queue failure-history operations for deletion."""
        source_key = ref_to_key(source_ref)
        target_key = ref_to_key(target_ref) if target_ref is not None else None
        self._failure_history_cleanup_queue.add((source_key, target_key))
        if target_key is not None:
            self._failure_history_cleanup_queue.add((source_key, None))
        if (
            len(self._failure_history_cleanup_queue)
            >= FAILURE_HISTORY_CLEANUP_BATCH_SIZE
        ):
            self.flush_failure_history_cleanup()
