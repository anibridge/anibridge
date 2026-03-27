"""Provider-agnostic base class for library/list synchronization."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from copy import copy
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from typing import Any

from anibridge.library import LibraryEntry, LibraryProvider
from anibridge.list import ListEntry, ListProvider, ListStatus
from anibridge.utils.mappings import AnibridgeDescriptorMapping, descriptor_key
from anibridge.utils.types import Comparable, MappingDescriptor

from anibridge.app import log
from anibridge.app.config.database import db
from anibridge.app.config.settings import SyncField, SyncRulesConfig
from anibridge.app.core.animap import AnimapClient
from anibridge.app.core.sync.cache import SyncCacheManager
from anibridge.app.core.sync.history import SyncHistoryManager
from anibridge.app.core.sync.rules import SyncRuleEngine
from anibridge.app.core.sync.stats import (
    BatchUpdate,
    EntrySnapshot,
    ItemIdentifier,
    SyncStats,
)
from anibridge.app.core.sync.targeting import SyncTarget, diff_snapshots
from anibridge.app.models.db.sync_history import SyncOutcome
from anibridge.app.utils.terminal import ARROW

__all__ = ["BaseSyncClient"]


@dataclass(slots=True)
class _FieldApplicationState:
    """Track why individual sync fields were blocked during planning."""

    pinned_blocked_fields: set[str] = dataclass_field(default_factory=set)
    sync_rules_blocked: dict[str, str] = dataclass_field(default_factory=dict)
    status_gate_blocked: dict[str, str] = dataclass_field(default_factory=dict)
    destructive_blocked_fields: set[str] = dataclass_field(default_factory=set)
    unchanged_fields: set[str] = dataclass_field(default_factory=set)

    def mark_block(self, field_name: str, reason: str | None) -> None:
        """Record why a field was blocked during sync planning."""
        if reason is None:
            return
        if reason == "pinned":
            self.pinned_blocked_fields.add(field_name)
        elif reason == "unchanged":
            self.unchanged_fields.add(field_name)
        elif reason == "destructive_disabled":
            self.destructive_blocked_fields.add(field_name)
        elif reason.startswith("sync_rules"):
            self.sync_rules_blocked[field_name] = reason.partition(":")[2]


class BaseSyncClient[
    ParentMediaT: LibraryEntry,
    ChildMediaT: LibraryEntry,
    GrandchildMediaT: LibraryEntry,
](ABC):
    """Provider-agnostic base class for media synchronization."""

    def __init__(
        self,
        *,
        library_provider: LibraryProvider,
        list_provider: ListProvider,
        animap_client: AnimapClient,
        full_scan: bool,
        destructive_sync: bool,
        empty_sync: bool = False,
        search_fallback_threshold: int,
        batch_requests: bool,
        dry_run: bool,
        profile_name: str,
        sync_rules: SyncRulesConfig | None = None,
    ) -> None:
        """Initialize the base synchronization client.

        Args:
            library_provider (LibraryProvider): Source library provider.
            list_provider (ListProvider): Destination list provider.
            animap_client (AnimapClient): Animap client used for descriptor resolution.
            full_scan (bool): Whether to include unplayed library items.
            destructive_sync (bool): Whether sync may remove or decrease list state.
            empty_sync (bool): Whether empty activity should still produce
                planning entries.
            search_fallback_threshold (int): Minimum fuzzy score for search
                fallback matches.
            batch_requests (bool): Whether to queue updates for batch submission.
            dry_run (bool): Whether to log changes without applying them.
            profile_name (str): Active profile name.
            sync_rules (SyncRulesConfig | None): Declarative per-field sync rules.
        """
        self.library_provider = library_provider
        self.list_provider = list_provider
        self.animap_client = animap_client
        self._sync_rule_engine = SyncRuleEngine(
            variables=sync_rules.resolved_vars() if sync_rules else None,
            field_rules=sync_rules.field_rules() if sync_rules else None,
        )
        self.full_scan = full_scan
        self.destructive_sync = destructive_sync
        self.empty_sync = empty_sync
        self.search_fallback_threshold = search_fallback_threshold
        self.batch_requests = batch_requests
        self.dry_run = dry_run
        self.profile_name = profile_name
        self.sync_stats = SyncStats()
        self._pending_updates: list[BatchUpdate[ParentMediaT, ChildMediaT]] = []
        self._cache = SyncCacheManager(
            list_provider=self.list_provider,
            profile_name=self.profile_name,
            db_factory=lambda: db(),
        )
        self._history = SyncHistoryManager(
            profile_name=self.profile_name,
            library_namespace=self.library_provider.NAMESPACE,
            list_namespace=self.list_provider.NAMESPACE,
            db_factory=lambda: db(),
        )
        self._field_calculators: dict[SyncField, Callable[..., Any]] = {
            SyncField.STATUS: self._calculate_status,
            SyncField.PROGRESS: self._calculate_progress,
            SyncField.REPEATS: self._calculate_repeats,
            SyncField.REVIEW: self._calculate_review,
            SyncField.USER_RATING: self._calculate_user_rating,
            SyncField.STARTED_AT: self._calculate_started_at,
            SyncField.FINISHED_AT: self._calculate_finished_at,
        }

    async def clear_cache(self) -> None:
        """Clear all sync client caches.

        Returns:
            None: This method clears manager caches and decorated function caches.
        """
        self._cache.clear()

    async def prefetch_entries(self, items: Sequence[ParentMediaT]) -> None:
        """Prefetch list entries for a batch of library items.

        Args:
            items (Sequence[ParentMediaT]): Items whose list entries should be
                loaded in advance.

        Returns:
            None: This method warms cache state for later sync operations.
        """
        await self._cache.prefetch_entries(
            items=items, collect_keys=self._collect_prefetch_keys
        )

    def _get_pinned_fields(self, namespace: str, media_key: str | None) -> list[str]:
        """Return the set of pinned fields for the given list media identifier."""
        return self._cache.get_pinned_fields(namespace, media_key)

    async def process_media(self, item: ParentMediaT) -> None:
        """Process one library item through target resolution and sync.

        Args:
            item (ParentMediaT): Library item to process.

        Returns:
            None: This method updates sync stats and history as needed.
        """
        item_identifier = ItemIdentifier.from_item(item)
        debug_title = self._debug_log_title(item=item, child_item=None)
        ids_summary = self._debug_log_ids(
            item=item, child_item=None, entry=None, media_key=None
        )
        log.debug(
            "[%s] Processing %s %s %s",
            self.profile_name,
            item.media_kind.value,
            debug_title,
            ids_summary,
        )

        trackable = await self._get_all_trackable_items(item)
        if not trackable:
            log.debug(
                "[%s] Skipping %s %s because it has no trackable items %s",
                self.profile_name,
                item.media_kind.value,
                debug_title,
                ids_summary,
            )
            self.sync_stats.track_item(item_identifier, SyncOutcome.SKIPPED)
            return
        self.sync_stats.register_pending_items(trackable)
        self.sync_stats.track_item(item_identifier, SyncOutcome.PENDING)

        found_match = False
        async for child_item, grandchild_items, target in self.map_media(item):
            found_match = True
            grandchildren = (
                grandchild_items
                if isinstance(grandchild_items, tuple)
                else tuple(grandchild_items)
            )
            grandchild_ids = ItemIdentifier.from_items(grandchildren)
            entry = target.entry
            list_media_key = target.list_media_key
            debug_title = self._debug_log_title(item=item, child_item=child_item)
            debug_ids = self._debug_log_ids(
                item=item, child_item=child_item, entry=entry, media_key=list_media_key
            )
            log.debug(
                "[%s] %s list entry for %s %s %s",
                self.profile_name,
                "No existing" if entry is None else "Found",
                item.media_kind.value,
                debug_title,
                debug_ids,
            )
            try:
                outcome = await self.sync_media(
                    item=item,
                    child_item=child_item,
                    grandchild_items=grandchildren,
                    entry=entry,
                    list_media_key=list_media_key,
                    mapping_descriptors=target.mapping_descriptors,
                    mappings=target.mappings,
                )
                self.sync_stats.track_items(grandchild_ids, outcome)
                self.sync_stats.track_item(item_identifier, outcome)
            except Exception:
                log.error(
                    "[%s] Failed to process %s %s %s",
                    self.profile_name,
                    item.media_kind.value,
                    debug_title,
                    debug_ids,
                )
                log.exception("[%s] Sync processing error details", self.profile_name)
                self.sync_stats.track_items(grandchild_ids, SyncOutcome.FAILED)
                self.sync_stats.track_item(item_identifier, SyncOutcome.FAILED)

        if not found_match:
            remaining_trackable = self.sync_stats.filter_tracked_items(trackable)
            if not remaining_trackable:
                log.debug(
                    "[%s] Skipping %s %s because all eligible items were filtered %s",
                    self.profile_name,
                    item.media_kind.value,
                    self._debug_log_title(item=item, child_item=None),
                    ids_summary,
                )
                self.sync_stats.track_item(item_identifier, SyncOutcome.SKIPPED)
                return

            attempted_descriptors = tuple(
                sorted(item.mapping_descriptors(), key=descriptor_key)
            )
            log.warning(
                "[%s] No list entries found for %s %s %s",
                self.profile_name,
                item.media_kind.value,
                self._debug_log_title(item=item, child_item=None),
                ids_summary,
            )
            await self._create_sync_history(
                item=item,
                child_item=None,
                grandchild_items=None,
                snapshots=(None, None),
                list_media_key=None,
                outcome=SyncOutcome.NOT_FOUND,
                info={
                    "operation": "resolve_target",
                    "reason": "no_matching_list_entry",
                    "trackable_items": str(len(remaining_trackable)),
                    "mapping_descriptor_count": str(len(attempted_descriptors)),
                    "mapping_descriptors": ", ".join(
                        descriptor_key(d) for d in attempted_descriptors
                    ),
                },
            )
            self.sync_stats.track_items(remaining_trackable, SyncOutcome.NOT_FOUND)
            self.sync_stats.track_item(item_identifier, SyncOutcome.NOT_FOUND)

    @abstractmethod
    async def _get_all_trackable_items(
        self, item: ParentMediaT
    ) -> Sequence[ItemIdentifier]:
        """Return all identifiers that should be tracked for the given item."""
        ...

    @abstractmethod
    async def _collect_prefetch_keys(self, item: ParentMediaT) -> Sequence[str]:
        """Collect list provider keys to prefetch for the given item."""
        ...

    @abstractmethod
    def map_media(
        self, item: ParentMediaT
    ) -> AsyncIterator[tuple[ChildMediaT, Sequence[GrandchildMediaT], SyncTarget]]:
        """Yield potential list entries matching the supplied library item."""
        ...

    @abstractmethod
    async def search_media(
        self, item: ParentMediaT, child_item: ChildMediaT
    ) -> ListEntry | None:
        """Search the list provider for fallback matches.

        Args:
            item (ParentMediaT): Parent library item being synchronized.
            child_item (ChildMediaT): Child item used to scope the search.

        Returns:
            ListEntry | None: Matching list entry, if one can be found.
        """

    async def sync_media(
        self,
        item: ParentMediaT,
        child_item: ChildMediaT,
        grandchild_items: Sequence[GrandchildMediaT],
        entry: ListEntry,
        list_media_key: str | None,
        mapping_descriptors: Sequence[MappingDescriptor] | None = None,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> SyncOutcome:
        """Synchronize a mapped media item with the list provider.

        Args:
            item (ParentMediaT): Parent library item being synchronized.
            child_item (ChildMediaT): Child item mapped to the target entry.
            grandchild_items (Sequence[GrandchildMediaT]): Trackable descendants
                used for field calculation.
            entry (ListEntry): Current list entry to update.
            list_media_key (str | None): Resolved list media key for the target entry.
            mapping_descriptors (Sequence[MappingDescriptor] | None): Mapping
                descriptors used to find the target.
            mappings (Sequence[AnibridgeDescriptorMapping] | None): Source mapping
                metadata used to
                resolve the target.

        Returns:
            SyncOutcome: Final outcome for the sync operation.
        """
        original_entry = entry
        planned_entry = copy(entry)
        before_snapshot = EntrySnapshot.from_entry(original_entry)
        resolved_list_key = (
            list_media_key or original_entry.media().key or before_snapshot.media_key
        )
        debug_title = self._debug_log_title(item=item, child_item=child_item)
        debug_ids = self._debug_log_ids(
            item=item,
            child_item=child_item,
            entry=original_entry,
            media_key=resolved_list_key,
        )

        pinned_fields = self._get_pinned_fields(
            self.list_provider.NAMESPACE, resolved_list_key
        )
        skip_fields = set(pinned_fields)
        disabled_fields = {
            f.value for f in SyncField if self._sync_rule_engine.is_disabled(f.value)
        }
        field_state = _FieldApplicationState(pinned_blocked_fields=skip_fields.copy())

        calc_kwargs = {
            "item": item,
            "child_item": child_item,
            "grandchild_items": grandchild_items,
            "entry": planned_entry,
            "mappings": mappings,
        }
        computed_values = await self._calculate_computed_values(
            calc_kwargs=calc_kwargs, disabled_fields=disabled_fields
        )
        current_values = {f.value: getattr(planned_entry, f.value) for f in SyncField}

        status_rule = self._sync_rule_engine.evaluate_field(
            field_name=SyncField.STATUS.value,
            current_values=current_values,
            computed_values=computed_values,
            rule_context=self._build_rule_context(
                item=item,
                child_item=child_item,
                grandchild_items=grandchild_items,
                list_media_key=resolved_list_key,
                mapping_descriptors=mapping_descriptors,
                required_media_fields=self._sync_rule_engine.context_media_fields(
                    SyncField.STATUS.value
                ),
            ),
        )

        if status_rule.value is None:
            if (
                self.destructive_sync
                and before_snapshot.status is not None
                and SyncField.STATUS.value not in skip_fields
            ):
                log.success(
                    "[%s] Deleting list entry for %s %s %s",
                    self.profile_name,
                    item.media_kind.value,
                    debug_title,
                    debug_ids,
                )
                if self.dry_run:
                    log.debug(
                        "[%s] Dry run; skipping deletion of %s %s %s",
                        self.profile_name,
                        item.media_kind.value,
                        debug_title,
                        debug_ids,
                    )
                    await self._create_sync_history(
                        item=item,
                        child_item=child_item,
                        grandchild_items=grandchild_items,
                        snapshots=(before_snapshot, None),
                        list_media_key=resolved_list_key,
                        mapping_descriptors=mapping_descriptors,
                        outcome=SyncOutcome.DELETED,
                        info={
                            "operation": "delete_entry",
                            "reason": "status_resolved_to_none",
                            "destructive_sync": self.destructive_sync,
                            "disabled_fields": sorted(disabled_fields),
                            "pinned_fields": ", ".join(sorted(skip_fields)),
                            "mapping_descriptor_count": len(mapping_descriptors or ()),
                            "dry_run": True,
                        },
                    )
                    return SyncOutcome.DELETED
                await self.list_provider.delete_entry(before_snapshot.media_key)
                self._cache.remove_entry(before_snapshot.media_key)
                await self._create_sync_history(
                    item=item,
                    child_item=child_item,
                    grandchild_items=grandchild_items,
                    snapshots=(before_snapshot, None),
                    list_media_key=resolved_list_key,
                    mapping_descriptors=mapping_descriptors,
                    outcome=SyncOutcome.DELETED,
                    info={
                        "operation": "delete_entry",
                        "reason": "status_resolved_to_none",
                        "destructive_sync": self.destructive_sync,
                        "disabled_fields": sorted(disabled_fields),
                        "pinned_fields": ", ".join(sorted(skip_fields)),
                        "mapping_descriptor_count": len(mapping_descriptors or ()),
                    },
                )
                return SyncOutcome.DELETED

            if not status_rule.allowed:
                skip_reason = (
                    f"status blocked by sync rules ({status_rule.reason or 'blocked'})"
                )
            elif (
                before_snapshot.status is not None
                and SyncField.STATUS.value in skip_fields
            ):
                skip_reason = "status is pinned"
            elif before_snapshot.status is not None:
                skip_reason = "status would be cleared but destructive sync is disabled"
            else:
                skip_reason = "no syncable activity"
            log.info(
                "[%s] Skipping %s %s because %s %s",
                self.profile_name,
                item.media_kind.value,
                debug_title,
                skip_reason,
                debug_ids,
            )
            return SyncOutcome.SKIPPED

        considered_attrs: set[str] = set()
        status_should_apply, status_reason = (
            self._should_apply_field(
                SyncField.STATUS, status_rule.value, before_snapshot.status, skip_fields
            )
            if status_rule.allowed
            else (False, f"sync_rules:{status_rule.reason}")
        )
        if status_should_apply:
            setattr(planned_entry, SyncField.STATUS.value, status_rule.value)
        else:
            field_state.mark_block(SyncField.STATUS.value, status_reason)
        considered_attrs.add(SyncField.STATUS.value)

        await self._apply_secondary_fields(
            item=item,
            child_item=child_item,
            grandchild_items=grandchild_items,
            list_media_key=resolved_list_key,
            mapping_descriptors=mapping_descriptors,
            entry=planned_entry,
            final_status=planned_entry.status,
            current_values=current_values,
            computed_values=computed_values,
            skip_fields=skip_fields,
            disabled_fields=disabled_fields,
            considered_attrs=considered_attrs,
            field_state=field_state,
        )

        after_snapshot = EntrySnapshot.from_entry(planned_entry)
        diff = diff_snapshots(before_snapshot, after_snapshot, considered_attrs)
        sync_diagnostics = self._normalize_history_info(
            {
                "computed_status": status_rule.value,
                "final_status": planned_entry.status,
                "disabled_fields": sorted(disabled_fields),
                "pinned_blocked": sorted(field_state.pinned_blocked_fields),
                "sync_rules_blocked": [
                    f"{k}({v})" if v else k
                    for k, v in sorted(field_state.sync_rules_blocked.items())
                ],
                "status_gate_blocked": [
                    f"{k}({v})" if v else k
                    for k, v in sorted(field_state.status_gate_blocked.items())
                ],
                "destructive_blocked": sorted(field_state.destructive_blocked_fields),
                "unchanged_fields": sorted(field_state.unchanged_fields),
                "considered_fields": sorted(considered_attrs),
                "applied_fields": sorted(diff.keys()),
            }
        )

        if not diff:
            log.info(
                "[%s] Skipping %s %s because it is already up to date %s",
                self.profile_name,
                item.media_kind.value,
                debug_title,
                debug_ids,
            )
            log.debug(
                (
                    "[%s] Field state: pinned=%s rules=%s gates=%s destructive=%s "
                    "unchanged=%s %s"
                ),
                self.profile_name,
                sorted(field_state.pinned_blocked_fields),
                sorted(field_state.sync_rules_blocked),
                sorted(field_state.status_gate_blocked),
                sorted(field_state.destructive_blocked_fields),
                sorted(field_state.unchanged_fields),
                debug_ids,
            )
            self._history.queue_failure_history_cleanup(
                item=item, child_item=child_item, list_media_key=resolved_list_key
            )
            return SyncOutcome.SKIPPED

        return await self._apply_update(
            BatchUpdate(
                item=item,
                child=child_item,
                grandchildren=grandchild_items,
                before=before_snapshot,
                after=after_snapshot,
                entry=planned_entry,
                source_entry=original_entry,
                list_media_key=resolved_list_key,
                mapping_descriptors=tuple(mapping_descriptors or ()),
                diagnostics=sync_diagnostics,
            ),
            self._format_diff(diff),
            debug_title,
            debug_ids,
        )

    async def _apply_update(
        self,
        plan: BatchUpdate[ParentMediaT, ChildMediaT],
        diff_str: str,
        debug_title: str,
        debug_ids: str,
    ) -> SyncOutcome:
        """Queue or apply a list entry update."""
        if self.batch_requests:
            log.success(
                "[%s] Queuing %s %s %s for batch sync",
                self.profile_name,
                plan.item.media_kind.value,
                debug_title,
                debug_ids,
            )
            log.success("\tQUEUED UPDATE: %s", diff_str)
            self._pending_updates.append(plan)
            return SyncOutcome.SYNCED

        if self.dry_run:
            log.success(
                "[%s] Dry run; skipping sync of %s %s %s",
                self.profile_name,
                plan.item.media_kind.value,
                debug_title,
                debug_ids,
            )
            log.success("\tDRY RUN UPDATE: %s", diff_str)
            await self._create_sync_history(
                item=plan.item,
                child_item=plan.child,
                grandchild_items=plan.grandchildren,
                snapshots=(plan.before, plan.after),
                list_media_key=plan.list_media_key,
                mapping_descriptors=plan.mapping_descriptors,
                outcome=SyncOutcome.SYNCED,
                info={
                    **plan.diagnostics,
                    "operation": "update_entry",
                    "mode": "single",
                    "dry_run": True,
                },
            )
            return SyncOutcome.SYNCED

        try:
            await self.list_provider.update_entry(plan.after.media_key, plan.entry)
            self._cache.apply_planned_update(
                source_entry=plan.source_entry,
                planned_entry=plan.entry,
                fields=[f.value for f in SyncField],
            )
            log.success(
                "[%s] Synced %s %s %s",
                self.profile_name,
                plan.item.media_kind.value,
                debug_title,
                debug_ids,
            )
            log.success("\tUPDATE: %s", diff_str)
            await self._create_sync_history(
                item=plan.item,
                child_item=plan.child,
                grandchild_items=plan.grandchildren,
                snapshots=(plan.before, plan.after),
                list_media_key=plan.list_media_key,
                mapping_descriptors=plan.mapping_descriptors,
                outcome=SyncOutcome.SYNCED,
                info={
                    **plan.diagnostics,
                    "operation": "update_entry",
                    "mode": "single",
                },
            )
            return SyncOutcome.SYNCED
        except Exception as exc:
            log.error(
                "[%s] Failed to sync %s %s %s: %s",
                self.profile_name,
                plan.item.media_kind.value,
                debug_title,
                debug_ids,
                exc,
            )
            log.exception("[%s] Sync update error details", self.profile_name)
            await self._create_sync_history(
                item=plan.item,
                child_item=plan.child,
                grandchild_items=plan.grandchildren,
                snapshots=(plan.before, plan.after),
                list_media_key=plan.list_media_key,
                mapping_descriptors=plan.mapping_descriptors,
                outcome=SyncOutcome.FAILED,
                error_message=str(exc),
                info={
                    **plan.diagnostics,
                    "operation": "update_entry",
                    "mode": "single",
                    "error_type": type(exc).__name__,
                },
            )
            raise

    async def _apply_secondary_fields(
        self,
        *,
        item,
        child_item,
        grandchild_items,
        list_media_key,
        mapping_descriptors,
        entry: ListEntry,
        final_status: ListStatus | None,
        current_values: Mapping[str, Any],
        computed_values: Mapping[str, Any],
        skip_fields: set[str],
        disabled_fields: set[str],
        considered_attrs: set[str],
        field_state: _FieldApplicationState,
    ) -> None:
        """Apply non-status sync fields when gates and rules allow it."""
        for sync_field in SyncField:
            if sync_field == SyncField.STATUS:
                continue
            if sync_field.value in skip_fields:
                field_state.pinned_blocked_fields.add(sync_field.value)
                continue
            if (
                reason := self._status_gate_reason(sync_field, final_status)
            ) is not None:
                field_state.status_gate_blocked[sync_field.value] = reason
                continue
            if self._sync_rule_engine.is_disabled(sync_field.value):
                disabled_fields.add(sync_field.value)
                continue

            rule_context = self._build_rule_context(
                item=item,
                child_item=child_item,
                grandchild_items=grandchild_items,
                list_media_key=list_media_key,
                mapping_descriptors=mapping_descriptors,
                required_media_fields=self._sync_rule_engine.context_media_fields(
                    sync_field.value
                ),
            )

            rule_decision = self._sync_rule_engine.evaluate_field(
                field_name=sync_field.value,
                current_values=current_values,
                computed_values=computed_values,
                rule_context=rule_context,
            )
            if not rule_decision.allowed:
                field_state.mark_block(
                    sync_field.value, f"sync_rules:{rule_decision.reason}"
                )
                continue

            should_apply, apply_reason = self._should_apply_field(
                sync_field,
                rule_decision.value,
                current_values[sync_field.value],
                skip_fields,
            )
            if not should_apply:
                field_state.mark_block(sync_field.value, apply_reason)
                continue

            setattr(entry, sync_field.value, rule_decision.value)
            considered_attrs.add(sync_field.value)

    async def _calculate_computed_values(
        self, *, calc_kwargs: Mapping[str, Any], disabled_fields: set[str]
    ) -> dict[str, Comparable | None]:
        """Calculate raw field values before any declarative rules are applied."""
        computed: dict[str, Comparable | None] = {
            SyncField.STATUS.value: await self._field_calculators[SyncField.STATUS](
                **calc_kwargs
            )
        }

        for sync_field in SyncField:
            if sync_field == SyncField.STATUS:
                continue
            if self._sync_rule_engine.is_disabled(sync_field.value):
                disabled_fields.add(sync_field.value)
                continue
            computed[sync_field.value] = await self._field_calculators[sync_field](
                **calc_kwargs
            )

        return computed

    @staticmethod
    def _build_rule_context(
        *,
        item,
        child_item,
        grandchild_items,
        list_media_key,
        mapping_descriptors,
        required_media_fields: frozenset[str],
    ) -> dict[str, Any]:
        """Build the shimmed `ctx` object exposed to sync rule expressions."""
        return {
            "list_media_key": list_media_key,
            "item": BaseSyncClient._shim_rule_media(item, required_media_fields),
            "child": BaseSyncClient._shim_rule_media(child_item, required_media_fields),
            "grandchildren": [
                BaseSyncClient._shim_rule_media(g, required_media_fields)
                for g in grandchild_items
            ],
        }

    @staticmethod
    def _shim_rule_media(
        media: Any, required_media_fields: frozenset[str]
    ) -> dict[str, Any]:
        """Build a stable rule-facing view of a library media object."""
        payload = {
            "key": getattr(media, "key", None),
            "title": getattr(media, "title", None),
            "media_kind": getattr(getattr(media, "media_kind", None), "value", None),
        }

        if "on_watching" in required_media_fields:
            payload["on_watching"] = getattr(media, "on_watching", None)
        if "on_watchlist" in required_media_fields:
            payload["on_watchlist"] = getattr(media, "on_watchlist", None)
        if "user_rating" in required_media_fields:
            payload["user_rating"] = getattr(media, "user_rating", None)
        if "view_count" in required_media_fields:
            payload["view_count"] = getattr(media, "view_count", None)
        if "index" in required_media_fields and hasattr(media, "index"):
            payload["index"] = getattr(media, "index", None)
        if "season_index" in required_media_fields and hasattr(media, "season_index"):
            payload["season_index"] = getattr(media, "season_index", None)

        return payload

    def _status_gate_reason(
        self, field: SyncField, final_status: ListStatus | None
    ) -> str | None:
        """Return the status-based reason a field cannot be updated."""
        if final_status is None:
            return "status_unset"
        if (
            field in (SyncField.USER_RATING, SyncField.REPEATS, SyncField.FINISHED_AT)
            and final_status < ListStatus.COMPLETED
        ):
            return "requires_completed"
        if field == SyncField.STARTED_AT and final_status <= ListStatus.PLANNING:
            return "requires_active_status"
        return None

    def _render_diff(self, plan: BatchUpdate[ParentMediaT, ChildMediaT]) -> str:
        """Render a diff string for a planned update."""
        after_fields = plan.after.to_dict()
        return self._format_diff(diff_snapshots(plan.before, plan.after, after_fields))

    async def batch_sync(self) -> None:
        """Flush queued updates to the list provider.

        Returns:
            None: This method submits pending updates and records history results.
        """
        if not self._pending_updates:
            return

        log.success(
            "[%s] Syncing %s items to list provider in batch mode",
            self.profile_name,
            len(self._pending_updates),
        )

        if self.dry_run:
            log.debug(
                "[%s] Dry run; skipping batch sync of %s items",
                self.profile_name,
                len(self._pending_updates),
            )
            for update in self._pending_updates:
                log.success(
                    "[%s] Dry run batch update for %s %s",
                    self.profile_name,
                    update.item.media_kind.value,
                    self._debug_log_title(item=update.item, child_item=update.child),
                )
                log.success("\tDRY RUN BATCH UPDATE: %s", self._render_diff(update))
                await self._create_sync_history(
                    item=update.item,
                    child_item=update.child,
                    grandchild_items=update.grandchildren,
                    snapshots=(update.before, update.after),
                    list_media_key=update.list_media_key,
                    mapping_descriptors=update.mapping_descriptors,
                    outcome=SyncOutcome.SYNCED,
                    info={
                        **update.diagnostics,
                        "operation": "update_entry",
                        "mode": "batch",
                        "dry_run": True,
                    },
                )
            self._pending_updates.clear()
            return

        try:
            updated = await self.list_provider.update_entries_batch(
                [u.entry for u in self._pending_updates]
            )
            updated_keys = {e.media().key for e in updated if e is not None}
            for update in self._pending_updates:
                outcome = (
                    SyncOutcome.SYNCED
                    if update.after.media_key in updated_keys
                    else SyncOutcome.FAILED
                )
                if outcome == SyncOutcome.SYNCED:
                    self._cache.apply_planned_update(
                        source_entry=update.source_entry,
                        planned_entry=update.entry,
                        fields=[f.value for f in SyncField],
                    )
                await self._create_sync_history(
                    item=update.item,
                    child_item=update.child,
                    grandchild_items=update.grandchildren,
                    snapshots=(update.before, update.after),
                    list_media_key=update.list_media_key,
                    mapping_descriptors=update.mapping_descriptors,
                    outcome=outcome,
                    info={
                        **update.diagnostics,
                        "operation": "update_entry",
                        "mode": "batch",
                    },
                )

            log.success(
                "[%s] Batch sync completed for %s items with %s failures",
                self.profile_name,
                len(self._pending_updates),
                len(self._pending_updates) - len(updated_keys),
            )
        except Exception as exc:
            log.error("Batch sync failed: %s", exc)
            log.exception("Batch sync error details")
            for update in self._pending_updates:
                await self._create_sync_history(
                    item=update.item,
                    child_item=update.child,
                    grandchild_items=update.grandchildren,
                    snapshots=(update.before, update.after),
                    list_media_key=update.list_media_key,
                    mapping_descriptors=update.mapping_descriptors,
                    outcome=SyncOutcome.FAILED,
                    error_message=str(exc),
                    info={
                        **update.diagnostics,
                        "operation": "update_entry",
                        "mode": "batch",
                        "error_type": type(exc).__name__,
                    },
                )
            raise
        finally:
            self._pending_updates.clear()

    def _normalize_history_info(
        self, payload: Mapping[str, Any] | None
    ) -> dict[str, str]:
        """Normalize history metadata to a flat string dictionary."""
        return self._history.normalize_info(payload)

    async def _create_sync_history(
        self,
        *,
        item,
        child_item,
        grandchild_items,
        snapshots,
        list_media_key,
        mapping_descriptors=None,
        outcome,
        error_message=None,
        info=None,
    ) -> None:
        """Record the outcome of a sync attempt."""
        await self._history.create_sync_history(
            item=item,
            child_item=child_item,
            grandchild_items=grandchild_items,
            snapshots=snapshots,
            list_media_key=list_media_key,
            mapping_descriptors=mapping_descriptors,
            outcome=outcome,
            error_message=error_message,
            info=info,
            ephemeral=self.dry_run,
        )

    def flush_failure_history_cleanup(self) -> None:
        """Flush queued failure-history cleanup operations.

        Returns:
            None: This method delegates cleanup to the history manager.
        """
        self._history.flush_failure_history_cleanup()

    def _should_apply_field(
        self, field: SyncField, new_value, current_value, skip_fields: set[str]
    ) -> tuple[bool, str | None]:
        """Return whether field should be applied and a diagnostic reason."""
        if field.value in skip_fields:
            return False, "pinned"
        if current_value == new_value:
            return False, "unchanged"
        if (
            not self.destructive_sync
            and current_value is not None
            and new_value is None
        ):
            return False, "destructive_disabled"
        return True, "applied"

    def _format_diff(self, diff: dict[str, tuple[Any, Any]]) -> str:
        """Format a diff dictionary for logging."""
        return " | ".join(
            f"{f}: {self._format_value(b)} {ARROW} {self._format_value(a)}"
            for f, (b, a) in sorted(diff.items())
        )

    @staticmethod
    def _format_value(value: Any) -> str:
        """Format individual values for diff logging."""
        if isinstance(value, ListStatus):
            return value.value
        if isinstance(value, datetime):
            dt = (
                value.replace(tzinfo=UTC)
                if value.tzinfo is None
                else value.astimezone(UTC)
            )
            return dt.isoformat()
        return "None" if value is None else repr(value)

    @abstractmethod
    async def _calculate_status(
        self,
        *,
        item: ParentMediaT,
        child_item: ChildMediaT,
        grandchild_items: Sequence[GrandchildMediaT],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> ListStatus | None:
        """Calculate the desired status for the list entry."""
        ...

    @abstractmethod
    async def _calculate_user_rating(
        self,
        *,
        item: ParentMediaT,
        child_item: ChildMediaT,
        grandchild_items: Sequence[GrandchildMediaT],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> int | None:
        """Calculate the desired score for the list entry."""
        ...

    @abstractmethod
    async def _calculate_progress(
        self,
        *,
        item: ParentMediaT,
        child_item: ChildMediaT,
        grandchild_items: Sequence[GrandchildMediaT],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> int | None:
        """Calculate the desired progress for the list entry."""
        ...

    @abstractmethod
    async def _calculate_repeats(
        self,
        *,
        item: ParentMediaT,
        child_item: ChildMediaT,
        grandchild_items: Sequence[GrandchildMediaT],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> int | None:
        """Calculate the desired repeat count for the list entry."""
        ...

    @abstractmethod
    async def _calculate_started_at(
        self,
        *,
        item: ParentMediaT,
        child_item: ChildMediaT,
        grandchild_items: Sequence[GrandchildMediaT],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> datetime | None:
        """Calculate the desired start date for the list entry."""
        ...

    @abstractmethod
    async def _calculate_finished_at(
        self,
        *,
        item: ParentMediaT,
        child_item: ChildMediaT,
        grandchild_items: Sequence[GrandchildMediaT],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> datetime | None:
        """Calculate the desired completion date for the list entry."""
        ...

    @abstractmethod
    async def _calculate_review(
        self,
        *,
        item: ParentMediaT,
        child_item: ChildMediaT,
        grandchild_items: Sequence[GrandchildMediaT],
        entry: ListEntry,
        mappings: Sequence[AnibridgeDescriptorMapping] | None = None,
    ) -> str | None:
        """Calculate the desired review/notes for the list entry."""
        ...

    @abstractmethod
    def _debug_log_title(
        self, *, item: ParentMediaT, child_item: ChildMediaT | None = None
    ) -> str:
        """Return a debug-friendly title representation."""
        ...

    @abstractmethod
    def _debug_log_ids(
        self,
        *,
        item: ParentMediaT,
        child_item: ChildMediaT | None,
        entry: ListEntry | None,
        media_key: str | None,
    ) -> str:
        """Return a debug-friendly identifier representation."""
        ...
