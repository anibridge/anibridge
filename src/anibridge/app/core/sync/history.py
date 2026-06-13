"""Sync history persistence helpers."""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import msgspec
from anibridge.library import LibraryEntry
from anibridge.utils.mappings import AnibridgeDescriptorMapping, descriptor_key
from sqlalchemy.sql import tuple_

from anibridge.app.core.sync.stats import EntrySnapshot
from anibridge.app.logging import get_logger
from anibridge.app.models.db.animap import AnimapEntry
from anibridge.app.models.db.sync_history import SyncHistory, SyncOutcome

__all__ = ["FAILURE_HISTORY_CLEANUP_BATCH_SIZE", "SyncHistoryManager"]

FAILURE_HISTORY_CLEANUP_BATCH_SIZE = 256
log = get_logger(__name__)


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
        """Initialize history persistence helpers."""
        self.profile_name = profile_name
        self.library_namespace = library_namespace
        self.list_namespace = list_namespace
        self._db_factory = db_factory
        self._failure_history_cleanup_queue: set[tuple[str, str, str | None]] = set()

    def clear_cache(self) -> None:
        """Clear all history manager caches."""
        pass

    async def create_sync_history(
        self,
        *,
        item: LibraryEntry,
        child_item: LibraryEntry | None,
        grandchild_items: Sequence[LibraryEntry] | None,
        snapshots: tuple[EntrySnapshot | None, EntrySnapshot | None],
        list_media_key: str | None,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
        outcome: SyncOutcome,
        error_message: str | None = None,
        info: Mapping[str, Any] | None = None,
        ephemeral: bool = False,
    ) -> None:
        """Persist a sync history record."""
        before_snapshot, after_snapshot = snapshots
        before_state = msgspec.to_builtins(before_snapshot) if before_snapshot else None
        after_state = msgspec.to_builtins(after_snapshot) if after_snapshot else None

        section = item.section()

        library_section = f"{section.title} ({section.key})"
        library_item = f"{item.title} ({item.key})"
        library_child_item = (
            f"{child_item.title} ({child_item.key})" if child_item else None
        )

        resolved_list_media_key = list_media_key
        if resolved_list_media_key is None:
            resolved_list_media_key = (
                after_snapshot.media_key
                if after_snapshot
                else before_snapshot.media_key
                if before_snapshot
                else None
            )

        mapping_source_descriptors = tuple(
            descriptor_key(mapping.source) for mapping in mappings or ()
        )
        mapping_target_descriptors = tuple(
            descriptor_key(mapping.target) for mapping in mappings or ()
        )
        unused_mapping_sources = sorted(
            set(descriptor_key(d) for d in item.mapping_descriptors())
            - set(mapping_source_descriptors)
        )

        history_info = {
            str(key): str(value)
            for key, value in {
                "library_section": library_section,
                "library_item": library_item,
                "library_child_item": library_child_item,
                "list_media_key": resolved_list_media_key,
                "library_grandchild_items": (
                    len(grandchild_items) if grandchild_items is not None else None
                ),
                "mapping_sources": ", ".join(mapping_source_descriptors),
                "mapping_targets": ", ".join(mapping_target_descriptors),
                "unused_mapping_sources": ", ".join(unused_mapping_sources),
                **(info or {}),
            }.items()
            if key and value
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

            mapping_entry_info = self._get_mapping_entry_info(
                mappings=mappings,
                session=ctx.session,
            )

            if outcome in (SyncOutcome.NOT_FOUND, SyncOutcome.FAILED):
                updated = self._update_existing_failure_record(
                    session=ctx.session,
                    library_section_key=section.key,
                    library_media_key=item.key,
                    list_media_key=resolved_list_media_key,
                    outcome=outcome,
                    before_state=before_state,
                    after_state=after_state,
                    history_info=history_info,
                    error_message=error_message,
                    mapping_entry_info=mapping_entry_info,
                )
                if updated:
                    ctx.session.commit()
                    return

            history_record = SyncHistory(
                profile_name=self.profile_name,
                library_namespace=self.library_namespace,
                library_section_key=section.key,
                library_media_key=item.key,
                list_namespace=self.list_namespace,
                list_media_key=resolved_list_media_key,
                animap_provider=mapping_entry_info[1] if mapping_entry_info else None,
                animap_id=mapping_entry_info[2] if mapping_entry_info else None,
                animap_scope=mapping_entry_info[3] if mapping_entry_info else None,
                media_kind=item.media_kind,
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
        """Flush queued failure-history deletions.

        Returns:
            None: This method deletes queued NOT_FOUND and FAILED rows.
        """
        if not self._failure_history_cleanup_queue:
            return

        target_pairs = tuple(self._failure_history_cleanup_queue)

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

        self._failure_history_cleanup_queue.difference_update(target_pairs)
        log.debug(
            "[%s] Cleaned up failure history for %s cached targets",
            self.profile_name,
            len(target_pairs),
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

        library_section_key = str(item.section().key)
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
        mappings: Sequence[AnibridgeDescriptorMapping] | None,
        session: Any,
    ) -> int | None:
        """Return the Animap entry id for the first ordered source descriptor."""
        result = self._get_mapping_entry_info(mappings=mappings, session=session)
        return result[0] if result else None

    def _get_mapping_entry_info(
        self,
        *,
        mappings: Sequence[AnibridgeDescriptorMapping] | None,
        session: Any,
    ) -> tuple[int, str, str, str | None] | None:
        """Return the Animap entry ID and descriptor info for the first descriptor."""
        if not mappings:
            return None
        descriptor = mappings[0].source

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
        if entry:
            return (entry.id, entry.provider, entry.entry_id, entry.entry_scope)
        return None

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
        mapping_entry_info: tuple[int, str, str, str | None] | None,
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

        if mapping_entry_info:
            existing.animap_provider = mapping_entry_info[1]
            existing.animap_id = mapping_entry_info[2]
            existing.animap_scope = mapping_entry_info[3]
        else:
            existing.animap_provider = None
            existing.animap_id = None
            existing.animap_scope = None
        return True
