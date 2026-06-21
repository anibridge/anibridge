"""Sync history persistence helpers for normalized provider records."""

from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

import msgspec
from anibridge.provider.base import ExternalId, Node, Record, Ref

from anibridge.app.core.sync import RefKey, ref_to_json, ref_to_key
from anibridge.app.core.sync.stats import RecordSnapshot
from anibridge.app.logging import get_logger
from anibridge.app.models.db.sync_history import SyncHistory, SyncOutcome

__all__ = ["FAILURE_HISTORY_CLEANUP_BATCH_SIZE", "SyncHistoryManager", "to_builtins"]

FAILURE_HISTORY_CLEANUP_BATCH_SIZE = 256
log = get_logger(__name__)


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
        return to_builtins(msgspec.structs.asdict(value))
    if is_dataclass(value):
        return to_builtins(asdict(value))
    if isinstance(value, Mapping):
        return {str(to_builtins(key)): to_builtins(item) for key, item in value.items()}
    if isinstance(value, tuple | list | frozenset | set):
        return [to_builtins(item) for item in value]
    return str(value)


class SyncHistoryManager:
    """Persist and clean up synchronization history records."""

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
        self._failure_history_cleanup_queue: set[tuple[RefKey, RefKey | None]] = set()

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
    ) -> None:
        """Persist a sync history record."""
        before_snapshot, after_snapshot = snapshots
        before_state = to_builtins(before_snapshot) if before_snapshot else None
        after_state = to_builtins(after_snapshot) if after_snapshot else None
        source_ref = source_record.ref if source_record else source_node.ref

        history_info: dict[str, str] = {}
        if source_node.title:
            history_info["source_title"] = source_node.title
        if source_record is not None:
            history_info["source_record_kind"] = source_record.kind
            if source_record.key:
                history_info["source_record_key"] = source_record.key
        if after_snapshot is not None:
            history_info["target_record_kind"] = after_snapshot.kind
        elif before_snapshot is not None:
            history_info["target_record_kind"] = before_snapshot.kind
        if info:
            history_info.update(info)

        with self._db_factory() as ctx:
            if outcome == SyncOutcome.SYNCED:
                self.queue_failure_history_cleanup(
                    source_ref=source_ref,
                    target_ref=target_ref,
                )

            if outcome == SyncOutcome.SKIPPED:
                return

            if outcome in (SyncOutcome.NOT_FOUND, SyncOutcome.FAILED):
                updated = self._update_existing_failure_record(
                    session=ctx.session,
                    source_ref=source_ref,
                    target_ref=target_ref,
                    outcome=outcome,
                    before_state=before_state,
                    after_state=after_state,
                    history_info=history_info,
                    error_message=error_message,
                )
                if updated:
                    ctx.session.commit()
                    return

            history_record = SyncHistory(
                profile_name=self.profile_name,
                source_namespace=self.source_namespace,
                source_ref=ref_to_json(source_ref),
                target_namespace=self.target_namespace,
                target_ref=ref_to_json(target_ref) if target_ref else None,
                animap_provider=external_id.authority if external_id else None,
                animap_id=external_id.value if external_id else None,
                animap_scope=external_id.scope if external_id else None,
                outcome=outcome,
                before_state=before_state,
                after_state=after_state,
                info=history_info,
                error_message=error_message,
                ephemeral=ephemeral,
            )
            ctx.session.add(history_record)
            ctx.session.commit()

    def flush_failure_history_cleanup(self) -> None:
        """Delete stale NOT_FOUND and FAILED rows for successfully synced refs."""
        if not self._failure_history_cleanup_queue:
            return

        targets = tuple(self._failure_history_cleanup_queue)

        with self._db_factory() as ctx:
            for start in range(0, len(targets), FAILURE_HISTORY_CLEANUP_BATCH_SIZE):
                chunk = targets[start : start + FAILURE_HISTORY_CLEANUP_BATCH_SIZE]
                for source_key, target_key in chunk:
                    query = ctx.session.query(SyncHistory).filter(
                        SyncHistory.profile_name == self.profile_name,
                        SyncHistory.source_namespace == self.source_namespace,
                        SyncHistory.source_ref == source_key.to_json(),
                        SyncHistory.target_namespace == self.target_namespace,
                        SyncHistory.outcome.in_(
                            [SyncOutcome.NOT_FOUND, SyncOutcome.FAILED]
                        ),
                    )
                    if target_key is None:
                        query = query.filter(SyncHistory.target_ref.is_(None))
                    else:
                        query = query.filter(
                            SyncHistory.target_ref == target_key.to_json()
                        )
                    query.delete(synchronize_session=False)
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
        """Queue failure-history records for deletion."""
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

    def _update_existing_failure_record(
        self,
        *,
        session: Any,
        source_ref: Ref,
        target_ref: Ref | None,
        outcome: SyncOutcome,
        before_state: dict[str, Any] | None,
        after_state: dict[str, Any] | None,
        history_info: dict[str, str],
        error_message: str | None,
    ) -> bool:
        """Update an existing failure row when one already represents this ref."""
        query = session.query(SyncHistory).filter(
            SyncHistory.profile_name == self.profile_name,
            SyncHistory.source_namespace == self.source_namespace,
            SyncHistory.source_ref == ref_to_json(source_ref),
            SyncHistory.target_namespace == self.target_namespace,
            SyncHistory.outcome == outcome,
        )
        if target_ref is None:
            query = query.filter(SyncHistory.target_ref.is_(None))
        else:
            query = query.filter(SyncHistory.target_ref == ref_to_json(target_ref))

        existing = query.order_by(SyncHistory.timestamp.desc()).first()
        if existing is None:
            return False

        existing.before_state = before_state
        existing.after_state = after_state
        existing.info = history_info
        existing.error_message = error_message
        existing.timestamp = datetime.now(UTC)
        return True
