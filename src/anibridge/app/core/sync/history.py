"""Sync history persistence helpers."""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from anibridge.library import LibraryEntry
from anibridge.list import ListStatus
from anibridge.utils.mappings import descriptor_key
from anibridge.utils.types import MappingDescriptor
from sqlalchemy import tuple_

from anibridge.app import log
from anibridge.app.core.sync.stats import EntrySnapshot
from anibridge.app.models.db.animap import AnimapEntry
from anibridge.app.models.db.sync_history import SyncHistory, SyncOutcome

__all__ = ["FAILURE_HISTORY_CLEANUP_BATCH_SIZE", "SyncHistoryManager"]

FAILURE_HISTORY_CLEANUP_BATCH_SIZE = 256


class SyncHistoryManager:
    """Persist and clean up synchronization history records."""

    def __init__(
        self,
        *,
        profile_name: str,
        library_namespace: str,
        list_namespace: str,
        db_factory: Callable[[], Any],
    ) -> None:
        """Initialize history persistence helpers.

        Args:
            profile_name (str): Sync profile name.
            library_namespace (str): Library provider namespace.
            list_namespace (str): List provider namespace.
            db_factory (Callable[[], Any]): Factory returning a database context
                manager.
        """
        self.profile_name = profile_name
        self.library_namespace = library_namespace
        self.list_namespace = list_namespace
        self._db_factory = db_factory
        self._failure_history_cleanup_queue: set[tuple[str, str, str | None]] = set()

    @staticmethod
    def stringify_info_value(value: Any) -> str | None:
        """Serialize a history metadata value to a string.

        Args:
            value (Any): Value to serialize.

        Returns:
            str | None: Serialized value, or None when the input is empty.
        """
        if value is None:
            return None
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, datetime):
            dt = (
                value.replace(tzinfo=UTC)
                if value.tzinfo is None
                else value.astimezone(UTC)
            )
            return dt.isoformat()
        if isinstance(value, ListStatus):
            return value.value
        if isinstance(value, (list, tuple, set)):
            values = [
                serialized
                for serialized in (
                    SyncHistoryManager.stringify_info_value(item) for item in value
                )
                if serialized
            ]
            return ", ".join(values) if values else None
        text = str(value).strip()
        return text or None

    def normalize_info(self, payload: Mapping[str, Any] | None) -> dict[str, str]:
        """Normalize history metadata to a flat string mapping.

        Args:
            payload (Mapping[str, Any] | None): Raw metadata payload.

        Returns:
            dict[str, str]: Normalized metadata with empty keys and values removed.
        """
        if not payload:
            return {}
        normalized: dict[str, str] = {}
        for key, value in payload.items():
            normalized_key = str(key).strip()
            if not normalized_key:
                continue
            normalized_value = self.stringify_info_value(value)
            if normalized_value is not None:
                normalized[normalized_key] = normalized_value
        return normalized

    async def create_sync_history(
        self,
        *,
        item: LibraryEntry,
        child_item: LibraryEntry | None,
        grandchild_items: Sequence[LibraryEntry] | None,
        snapshots: tuple[EntrySnapshot | None, EntrySnapshot | None],
        list_media_key: str | None,
        mapping_descriptors: Sequence[MappingDescriptor] | None = None,
        outcome: SyncOutcome,
        error_message: str | None = None,
        info: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist a sync history record.

        Args:
            item (LibraryEntry): Parent library item being synchronized.
            child_item (LibraryEntry | None): Child item associated with the sync
                attempt.
            grandchild_items (Sequence[LibraryEntry] | None): Grandchild items
                included in the sync attempt.
            snapshots (tuple[EntrySnapshot | None, EntrySnapshot | None]): Before
                and after entry snapshots.
            list_media_key (str | None): Resolved list media key.
            mapping_descriptors (Sequence[MappingDescriptor] | None): Mapping
                descriptors used to resolve the item.
            outcome (SyncOutcome): Final synchronization outcome.
            error_message (str | None): Optional failure message.
            info (Mapping[str, Any] | None): Additional diagnostic metadata.

        Returns:
            None: This method writes history rows and updates failure records.
        """
        before_snapshot, after_snapshot = snapshots
        before_state = before_snapshot.serialize() if before_snapshot else None
        after_state = after_snapshot.serialize() if after_snapshot else None

        resolved_list_media_key = list_media_key
        if resolved_list_media_key is None:
            resolved_list_media_key = (
                after_snapshot.media_key
                if after_snapshot
                else before_snapshot.media_key
                if before_snapshot
                else None
            )

        library_section_key = item.section().key
        library_media_key = str(item.key)
        mapping_target_descriptors = sorted(
            set(mapping_descriptors or ()),
            key=descriptor_key,
        )

        base_info = self.normalize_info(
            {
                "outcome": outcome.value,
                "library_section_key": library_section_key,
                "library_media_key": library_media_key,
                "list_media_key": resolved_list_media_key,
                "mapping_targets": [
                    descriptor_key(descriptor)
                    for descriptor in mapping_target_descriptors
                ],
                "mapping_count": len(mapping_target_descriptors),
                "grandchild_count": (
                    len(grandchild_items) if grandchild_items is not None else None
                ),
            }
        )
        history_info = {
            **base_info,
            **self.normalize_info(info),
        }

        with self._db_factory() as ctx:
            if outcome == SyncOutcome.SYNCED:
                self.queue_failure_history_cleanup(
                    item=item,
                    child_item=child_item,
                    list_media_key=resolved_list_media_key,
                )

            if outcome == SyncOutcome.SKIPPED:
                return

            mapping_entry_id = self._get_mapping_entry_id(
                mapping_descriptors=mapping_descriptors,
                session=ctx.session,
            )

            if outcome in (SyncOutcome.NOT_FOUND, SyncOutcome.FAILED):
                updated = self._update_existing_failure_record(
                    session=ctx.session,
                    library_section_key=library_section_key,
                    library_media_key=library_media_key,
                    list_media_key=resolved_list_media_key,
                    outcome=outcome,
                    before_state=before_state,
                    after_state=after_state,
                    history_info=history_info,
                    error_message=error_message,
                    mapping_entry_id=mapping_entry_id,
                )
                if updated:
                    ctx.session.commit()
                    return

            history_record = SyncHistory(
                profile_name=self.profile_name,
                library_namespace=self.library_namespace,
                library_section_key=library_section_key,
                library_media_key=library_media_key,
                list_namespace=self.list_namespace,
                list_media_key=resolved_list_media_key,
                animap_entry_id=mapping_entry_id,
                media_kind=item.media_kind,
                outcome=outcome,
                before_state=before_state,
                after_state=after_state,
                info=history_info,
                error_message=error_message,
            )
            ctx.session.add(history_record)
            ctx.session.commit()

    def flush_failure_history_cleanup(self) -> None:
        """Flush queued failure-history deletions.

        Returns:
            None: This method deletes queued NOT_FOUND and FAILED rows.
        """
        if not self._failure_history_cleanup_queue:
            return

        targets = set(self._failure_history_cleanup_queue)
        target_pairs = list(targets)

        with self._db_factory() as ctx:
            for start in range(
                0,
                len(target_pairs),
                FAILURE_HISTORY_CLEANUP_BATCH_SIZE,
            ):
                chunk = target_pairs[start : start + FAILURE_HISTORY_CLEANUP_BATCH_SIZE]
                with_list_key = [
                    (section_key, media_key, list_media_key)
                    for section_key, media_key, list_media_key in chunk
                    if list_media_key is not None
                ]
                without_list_key = [
                    (section_key, media_key)
                    for section_key, media_key, list_media_key in chunk
                    if list_media_key is None
                ]
                if with_list_key:
                    ctx.session.query(SyncHistory).filter(
                        SyncHistory.profile_name == self.profile_name,
                        SyncHistory.library_namespace == self.library_namespace,
                        tuple_(
                            SyncHistory.library_section_key,
                            SyncHistory.library_media_key,
                            SyncHistory.list_media_key,
                        ).in_(with_list_key),
                        SyncHistory.outcome.in_(
                            [SyncOutcome.NOT_FOUND, SyncOutcome.FAILED]
                        ),
                    ).delete(synchronize_session=False)
                if without_list_key:
                    ctx.session.query(SyncHistory).filter(
                        SyncHistory.profile_name == self.profile_name,
                        SyncHistory.library_namespace == self.library_namespace,
                        tuple_(
                            SyncHistory.library_section_key,
                            SyncHistory.library_media_key,
                        ).in_(without_list_key),
                        SyncHistory.list_media_key.is_(None),
                        SyncHistory.outcome.in_(
                            [SyncOutcome.NOT_FOUND, SyncOutcome.FAILED]
                        ),
                    ).delete(synchronize_session=False)
            ctx.session.commit()

        self._failure_history_cleanup_queue -= targets
        log.debug(
            "[%s] Cleaned up failure history for %s cached targets",
            self.profile_name,
            len(targets),
        )

    def queue_failure_history_cleanup(
        self,
        *,
        item: LibraryEntry,
        child_item: LibraryEntry | None = None,
        list_media_key: str | None = None,
    ) -> None:
        """Queue failure-history records for deletion.

        Args:
            item (LibraryEntry): Parent library item whose failure history should
                be cleaned.
            child_item (LibraryEntry | None): Child item associated with the
                cleanup target.
            list_media_key (str | None): Optional list media key used to scope deletion.

        Returns:
            None: This method updates the in-memory cleanup queue.
        """
        del child_item

        library_section_key = item.section().key
        library_media_key = str(item.key)
        if not library_media_key:
            return

        cleanup_key = (library_section_key, library_media_key, list_media_key)
        self._failure_history_cleanup_queue.add(cleanup_key)
        if list_media_key is not None:
            self._failure_history_cleanup_queue.add(
                (library_section_key, library_media_key, None)
            )
        if (
            len(self._failure_history_cleanup_queue)
            >= FAILURE_HISTORY_CLEANUP_BATCH_SIZE
        ):
            self.flush_failure_history_cleanup()

    def _get_mapping_entry_id(
        self,
        *,
        mapping_descriptors: Sequence[MappingDescriptor] | None,
        session: Any,
    ) -> int | None:
        """Return the Animap entry id for the first mapping descriptor."""
        if not mapping_descriptors:
            return None
        descriptor = sorted(mapping_descriptors, key=descriptor_key)[0]
        provider, entry_id, scope = descriptor
        entry = (
            session.query(AnimapEntry)
            .filter(
                AnimapEntry.provider == provider,
                AnimapEntry.entry_id == entry_id,
                AnimapEntry.entry_scope == scope,
            )
            .first()
        )
        return entry.id if entry else None

    def _update_existing_failure_record(
        self,
        *,
        session: Any,
        library_section_key: str,
        library_media_key: str,
        list_media_key: str | None,
        outcome: SyncOutcome,
        before_state: Mapping[str, Any] | None,
        after_state: Mapping[str, Any] | None,
        history_info: Mapping[str, str],
        error_message: str | None,
        mapping_entry_id: int | None,
    ) -> bool:
        """Update an existing NOT_FOUND or FAILED history record if one exists."""
        filters = [
            SyncHistory.profile_name == self.profile_name,
            SyncHistory.library_namespace == self.library_namespace,
            SyncHistory.library_section_key == library_section_key,
            SyncHistory.library_media_key == library_media_key,
            SyncHistory.outcome == outcome,
        ]
        if list_media_key is None:
            filters.append(SyncHistory.list_media_key.is_(None))
        else:
            filters.extend(
                [
                    SyncHistory.list_namespace == self.list_namespace,
                    SyncHistory.list_media_key == list_media_key,
                ]
            )

        existing = session.query(SyncHistory).filter(*filters).first()
        if existing is None:
            return False
        if (
            existing.error_message == error_message
            and (existing.info or {}) == history_info
        ):
            return True

        existing.before_state = before_state
        existing.after_state = after_state
        existing.info = dict(history_info)
        existing.error_message = error_message
        existing.timestamp = datetime.now(UTC)
        existing.animap_entry_id = mapping_entry_id
        return True
