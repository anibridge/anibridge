"""Provider-agnostic base class for library/list synchronization."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from anibridge.library import LibraryEntry, LibraryProvider
from anibridge.list import ListEntry, ListProvider, ListStatus, MappingDescriptor
from rapidfuzz import fuzz
from sqlalchemy import tuple_

from src import log
from src.config.database import db
from src.config.settings import SyncField
from src.core.animap import AnimapClient, descriptor_key
from src.core.sync.stats import (
    BatchUpdate,
    EntrySnapshot,
    ItemIdentifier,
    SyncStats,
)
from src.models.db.animap import AnimapEntry
from src.models.db.pin import Pin
from src.models.db.sync_history import SyncHistory, SyncOutcome
from src.utils.mapping_ranges import SourceRange, parse_source_range
from src.utils.terminal import ARROW
from src.utils.types import Comparable

__all__ = ["BaseSyncClient"]

FAILURE_HISTORY_CLEANUP_BATCH_SIZE = 256


def diff_snapshots(
    before: EntrySnapshot | None,
    after: EntrySnapshot | None,
    fields: set[str],
) -> dict[str, tuple[Any, Any]]:
    """Compute differences between two snapshots for the specified fields."""
    diff: dict[str, tuple[Any, Any]] = {}
    before_map = before.to_dict() if before else {}
    after_map = after.to_dict() if after else {}
    for field in fields:
        if before_map.get(field) != after_map.get(field):
            diff[field] = (before_map.get(field), after_map.get(field))
    return diff


@dataclass(frozen=True)
class SyncTarget:
    """Resolved list target for a library media item."""

    list_media_key: str
    entry: ListEntry
    mapping_descriptors: tuple[MappingDescriptor, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceRangeMapping:
    """Source descriptor with one or more source ranges."""

    descriptor: MappingDescriptor
    ranges: tuple[SourceRange, ...]


@dataclass(frozen=True, slots=True)
class ResolvedListTarget:
    """Resolved list key with mapping descriptors and ranges."""

    list_media_key: str
    mapping_descriptors: tuple[MappingDescriptor, ...]
    source_mappings: tuple[SourceRangeMapping, ...]


@dataclass(slots=True)
class _GroupedTargets:
    descriptors: set[MappingDescriptor]
    mappings: dict[MappingDescriptor, list[SourceRange]]


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
        search_fallback_threshold: int,
        batch_requests: bool,
        dry_run: bool,
        profile_name: str,
        sync_fields: Mapping[str, bool | Mapping[str, bool]] | None = None,
    ) -> None:
        """Initialize the base synchronisation client."""
        self.library_provider: LibraryProvider = library_provider
        self.list_provider: ListProvider = list_provider
        self.animap_client: AnimapClient = animap_client
        self.sync_fields: dict[str, bool | dict[str, bool]] = {
            str(field): (
                bool(rules)
                if isinstance(rules, bool)
                else {str(rule): bool(enabled) for rule, enabled in rules.items()}
            )
            for field, rules in (sync_fields or {}).items()
        }
        self.full_scan: bool = full_scan
        self.destructive_sync: bool = destructive_sync
        self.search_fallback_threshold: int = search_fallback_threshold
        self.batch_requests: bool = batch_requests
        self.dry_run: bool = dry_run
        self.profile_name: str = profile_name

        self.sync_stats: SyncStats = SyncStats()
        self._pin_cache: dict[tuple[str, str], list[str]] = {}
        self._prefetched_entries: dict[str, ListEntry] = {}
        self._pending_updates: list[BatchUpdate[ParentMediaT, ChildMediaT]] = []
        self._failure_history_cleanup_queue: set[tuple[str, str, str | None]] = set()

        self._field_calculators: dict[
            SyncField,
            Callable[..., Any],
        ] = {
            SyncField.STATUS: self._calculate_status,
            SyncField.PROGRESS: self._calculate_progress,
            SyncField.REPEATS: self._calculate_repeats,
            SyncField.REVIEW: self._calculate_review,
            SyncField.USER_RATING: self._calculate_user_rating,
            SyncField.STARTED_AT: self._calculate_started_at,
            SyncField.FINISHED_AT: self._calculate_finished_at,
        }

    async def clear_cache(self) -> None:
        """Clear any LRU/TTL caches defined on the client."""
        self._pin_cache.clear()
        self._prefetched_entries.clear()
        for v in dir(self):
            attr = getattr(self, v)
            if callable(attr) and hasattr(attr, "cache_clear"):
                attr.cache_clear()

    def _cache_list_entry(self, entry: ListEntry) -> None:
        """Store a list entry in the local prefetch cache."""
        self._prefetched_entries[entry.media().key] = entry
        self._prefetched_entries[entry.key] = entry

    async def _get_entry_cached(self, key: str) -> ListEntry | None:
        """Return a list entry from cache or fall back to the provider."""
        cached = self._prefetched_entries.get(str(key))
        if cached is not None:
            return cached
        entry = await self.list_provider.get_entry(key)
        if entry is not None:
            self._cache_list_entry(entry)
        return entry

    async def prefetch_entries(self, items: Sequence[ParentMediaT]) -> None:
        """Prefetch list entries for the supplied library items."""
        if not items:
            return

        collected: set[str] = set()
        for item in items:
            try:
                keys = await self._collect_prefetch_keys(item)
            except Exception:
                log.error(
                    "[%s] Failed to collect prefetch keys",
                    self.profile_name,
                )
                log.exception(
                    "[%s] Prefetch key collection error details",
                    self.profile_name,
                )
                continue
            for key in keys:
                if key is None:
                    continue
                collected.add(str(key))

        if not collected:
            return

        log.debug(
            "[%s] Prefetching %s list entries",
            self.profile_name,
            len(collected),
        )

        try:
            entries = await self.list_provider.get_entries_batch(list(collected))
        except Exception:
            log.error(
                "[%s] Failed to prefetch list entries",
                self.profile_name,
            )
            log.exception(
                "[%s] Prefetch batch error details",
                self.profile_name,
            )
            return

        for entry in entries:
            if entry is None:
                continue
            self._cache_list_entry(entry)

        log.debug(
            "[%s] Prefetched %s list entries",
            self.profile_name,
            len(entries),
        )

    def _get_pinned_fields(self, namespace: str, media_key: str | None) -> list[str]:
        """Return the set of pinned fields for the given list media identifier."""
        if not media_key:
            return []

        cache_key = (namespace, media_key)
        cached = self._pin_cache.get(cache_key)
        if cached is not None:
            return cached

        with db() as ctx:
            pin: Pin | None = (
                ctx.session.query(Pin)
                .filter(
                    Pin.profile_name == self.profile_name,
                    Pin.list_namespace == namespace,
                    Pin.list_media_key == media_key,
                )
                .first()
            )

        fields = list(pin.fields) if pin and pin.fields else []
        self._pin_cache[cache_key] = fields
        return fields

    async def _resolve_list_targets_batch(
        self,
        descriptor_sets: Sequence[Sequence[MappingDescriptor]],
    ) -> list[tuple[ResolvedListTarget, ...]]:
        """Resolve mapping descriptors into list targets in a single batch."""
        normalized: list[tuple[MappingDescriptor, ...]] = []
        all_descriptors: set[MappingDescriptor] = set()
        for descriptor_set in descriptor_sets:
            ordered = tuple(dict.fromkeys(descriptor_set))
            normalized.append(ordered)
            all_descriptors.update(ordered)

        if not all_descriptors:
            return [tuple() for _ in normalized]

        grouped_edges = self.animap_client.resolve_edges_grouped(
            list(all_descriptors),
            target_providers=self.list_provider.MAPPING_PROVIDERS,
        )
        ranges_by_target: dict[
            MappingDescriptor, dict[MappingDescriptor, list[SourceRange]]
        ] = {
            target_descriptor: {
                source_descriptor: [
                    parse_source_range(source_range) for source_range in source_ranges
                ]
                for source_descriptor, source_ranges in sources.items()
            }
            for target_descriptor, sources in grouped_edges.items()
        }

        direct_targets = [
            descriptor
            for descriptor in all_descriptors
            if descriptor[0] in self.list_provider.MAPPING_PROVIDERS
        ]
        target_descriptors = {
            *ranges_by_target.keys(),
            *direct_targets,
        }
        if not target_descriptors:
            return [tuple() for _ in normalized]

        resolved_targets = await self.list_provider.resolve_mapping_descriptors(
            list(target_descriptors)
        )

        grouped: dict[str, _GroupedTargets] = {}
        for target in resolved_targets:
            group = grouped.setdefault(
                target.media_key,
                _GroupedTargets(descriptors=set(), mappings={}),
            )
            group.descriptors.add(target.descriptor)
            if target.descriptor in ranges_by_target:
                for source_descriptor, ranges in ranges_by_target[
                    target.descriptor
                ].items():
                    mapping_ranges = group.mappings.setdefault(source_descriptor, [])
                    mapping_ranges.extend(ranges)

        def _build_target(key: str, payload: _GroupedTargets) -> ResolvedListTarget:
            descriptors = tuple(sorted(payload.descriptors, key=descriptor_key))
            source_mappings = tuple(
                SourceRangeMapping(
                    descriptor=source_descriptor,
                    ranges=tuple(ranges),
                )
                for source_descriptor, ranges in sorted(
                    payload.mappings.items(),
                    key=lambda item: descriptor_key(item[0]),
                )
            )
            return ResolvedListTarget(
                list_media_key=key,
                mapping_descriptors=descriptors,
                source_mappings=source_mappings,
            )

        by_key = {
            key: _build_target(key, grouped[key]) for key in sorted(grouped.keys())
        }

        results: list[tuple[ResolvedListTarget, ...]] = []
        for descriptor_set in normalized:
            source_order = list(descriptor_set)
            filtered: list[ResolvedListTarget] = []
            for target in by_key.values():
                ordered_descriptors = tuple(
                    descriptor
                    for descriptor in source_order
                    if descriptor in target.mapping_descriptors
                )
                mapping_lookup = {
                    mapping.descriptor: mapping.ranges
                    for mapping in target.source_mappings
                }
                ordered_mappings = tuple(
                    SourceRangeMapping(
                        descriptor=descriptor,
                        ranges=mapping_lookup[descriptor],
                    )
                    for descriptor in source_order
                    if descriptor in mapping_lookup
                )
                if not ordered_descriptors and not ordered_mappings:
                    continue
                filtered.append(
                    ResolvedListTarget(
                        list_media_key=target.list_media_key,
                        mapping_descriptors=ordered_descriptors
                        or target.mapping_descriptors,
                        source_mappings=ordered_mappings,
                    )
                )
            results.append(tuple(filtered))
        return results

    async def _resolve_list_targets(
        self, *media_items: LibraryEntry
    ) -> tuple[ResolvedListTarget, ...]:
        """Resolve mapping descriptors into list targets with range metadata."""
        descriptors: list[MappingDescriptor] = []
        for media in media_items:
            descriptors.extend(media.mapping_descriptors())
        resolved = await self._resolve_list_targets_batch([descriptors])
        return resolved[0] if resolved else tuple()

    async def process_media(self, item: ParentMediaT) -> None:
        """Process a single library item."""
        ids_summary = self._debug_log_ids(
            item=item,
            child_item=None,
            entry=None,
            media_key=None,
        )
        debug_title = self._debug_log_title(item=item, child_item=None)
        log.debug(
            "[%s] Processing %s %s %s",
            self.profile_name,
            item.media_kind.value,
            debug_title,
            ids_summary,
        )

        item_identifier = ItemIdentifier.from_item(item)
        trackable = await self._get_all_trackable_items(item)
        if trackable:
            self.sync_stats.register_pending_items(trackable)
            self.sync_stats.track_item(item_identifier, SyncOutcome.PENDING)
        else:
            log.debug(
                "[%s] Skipping %s %s because it has no eligible items %s",
                self.profile_name,
                item.media_kind.value,
                debug_title,
                ids_summary,
            )
            self.sync_stats.track_item(item_identifier, SyncOutcome.SKIPPED)
            return

        found_match = False
        async for (
            child_item,
            grandchild_items,
            target,
        ) in self.map_media(item):
            found_match = True
            grandchildren = tuple(grandchild_items)
            grandchild_ids = ItemIdentifier.from_items(grandchildren)

            entry = target.entry
            list_media_key = target.list_media_key

            debug_title = self._debug_log_title(item=item, child_item=child_item)
            debug_ids = self._debug_log_ids(
                item=item,
                child_item=child_item,
                entry=entry,
                media_key=list_media_key,
            )
            if entry is None:
                log.debug(
                    "[%s] No existing list entry for %s; preparing new entry %s %s",
                    self.profile_name,
                    item.media_kind.value,
                    debug_title,
                    debug_ids,
                )
            else:
                log.debug(
                    "[%s] Found list entry for %s %s %s",
                    self.profile_name,
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
                log.exception(
                    "[%s] Sync processing error details",
                    self.profile_name,
                )
                self.sync_stats.track_items(grandchild_ids, SyncOutcome.FAILED)
                self.sync_stats.track_item(item_identifier, SyncOutcome.FAILED)

        if not found_match:
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
            )
            if trackable:
                self.sync_stats.track_items(trackable, SyncOutcome.NOT_FOUND)
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
    ) -> AsyncIterator[
        tuple[
            ChildMediaT,
            Sequence[GrandchildMediaT],
            SyncTarget,
        ]
    ]:
        """Yield potential list entries matching the supplied library item."""
        ...

    @abstractmethod
    async def search_media(
        self, item: ParentMediaT, child_item: ChildMediaT
    ) -> ListEntry | None:
        """Search the list provider for fallback matches."""

    def _best_search_result(
        self, title: str, results: Sequence[ListEntry]
    ) -> ListEntry | None:
        """Return the best fuzzy match for the given title."""
        best_entry: ListEntry | None = None
        best_ratio = 0
        for entry in results:
            candidates = {entry.title}
            media_title = entry.media().title
            if media_title:
                candidates.add(media_title)
            for candidate in candidates:
                if not candidate:
                    continue
                ratio = fuzz.ratio(title, candidate)
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_entry = entry
        if best_ratio < self.search_fallback_threshold:
            return None
        return best_entry

    async def sync_media(
        self,
        item: ParentMediaT,
        child_item: ChildMediaT,
        grandchild_items: Sequence[GrandchildMediaT],
        entry: ListEntry,
        list_media_key: str | None,
        mapping_descriptors: Sequence[MappingDescriptor] | None = None,
    ) -> SyncOutcome:
        """Synchronize a mapped media item with the list provider."""
        resolved_list_key = list_media_key or entry.media().key

        debug_title = self._debug_log_title(item=item, child_item=child_item)
        debug_ids = self._debug_log_ids(
            item=item,
            child_item=child_item,
            entry=entry,
            media_key=resolved_list_key,
        )

        before_snapshot = EntrySnapshot.from_entry(entry)

        if resolved_list_key is None:
            resolved_list_key = before_snapshot.media_key
        pinned_fields = self._get_pinned_fields(
            self.list_provider.NAMESPACE, resolved_list_key
        )
        skip_fields = set(pinned_fields)

        calc_kwargs = {
            "item": item,
            "child_item": child_item,
            "grandchild_items": grandchild_items,
            "entry": entry,
        }

        status_value: ListStatus | None = await self._field_calculators[
            SyncField.STATUS
        ](**calc_kwargs)

        if status_value is None:
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
                    log.info(
                        "[%s] Dry run enabled; skipping deletion of %s %s %s",
                        self.profile_name,
                        item.media_kind.value,
                        debug_title,
                        debug_ids,
                    )
                    return SyncOutcome.SKIPPED
                else:
                    await self.list_provider.delete_entry(before_snapshot.media_key)

                await self._create_sync_history(
                    item=item,
                    child_item=child_item,
                    grandchild_items=grandchild_items,
                    snapshots=(before_snapshot, None),
                    list_media_key=resolved_list_key,
                    mapping_descriptors=mapping_descriptors,
                    outcome=SyncOutcome.DELETED,
                )
                return SyncOutcome.DELETED

            log.info(
                "[%s] Skipping %s due to no activity %s %s",
                self.profile_name,
                item.media_kind.value,
                debug_title,
                debug_ids,
            )
            return SyncOutcome.SKIPPED

        considered_attrs: set[str] = set()

        if self._should_apply_field(
            SyncField.STATUS,
            status_value,
            before_snapshot.status,
            skip_fields,
        ):
            entry.status = status_value
        considered_attrs.add(SyncField.STATUS.value)
        final_status = entry.status

        for field in SyncField:
            if field == SyncField.STATUS:
                continue
            if field.value in skip_fields:
                continue
            if self.sync_fields.get(field.value) is False:
                continue

            if final_status is None:
                continue
            if (
                field
                in (SyncField.USER_RATING, SyncField.REPEATS, SyncField.FINISHED_AT)
                and final_status < ListStatus.COMPLETED
            ):
                continue
            if field == SyncField.STARTED_AT and final_status <= ListStatus.PLANNING:
                continue

            value = await self._field_calculators[field](**calc_kwargs)
            current_value = getattr(entry, field.value)
            if not self._should_apply_field(field, value, current_value, skip_fields):
                continue

            setattr(entry, field.value, value)
            considered_attrs.add(field.value)

        after_snapshot = EntrySnapshot.from_entry(entry)
        diff = diff_snapshots(before_snapshot, after_snapshot, considered_attrs)

        if not diff:
            log.info(
                "[%s] Skipping %s because it is already up to date %s %s",
                self.profile_name,
                item.media_kind.value,
                debug_title,
                debug_ids,
            )
            self._cleanup_failure_history(
                item=item,
                child_item=child_item,
                list_media_key=resolved_list_key,
            )
            return SyncOutcome.SKIPPED

        plan = BatchUpdate(
            item=item,
            child=child_item,
            grandchildren=grandchild_items,
            before=before_snapshot,
            after=after_snapshot,
            entry=entry,
            list_media_key=resolved_list_key,
            mapping_descriptors=tuple(mapping_descriptors or ()),
        )

        diff_str = self._format_diff(diff)
        return await self._apply_update(plan, diff_str, debug_title, debug_ids)

    async def _apply_update(
        self,
        plan: BatchUpdate[ParentMediaT, ChildMediaT],
        diff_str: str,
        debug_title: str,
        debug_ids: str,
    ) -> SyncOutcome:
        """Queue or apply a list entry update."""
        if self.batch_requests:
            log.info(
                "[%s] Queuing %s for batch sync %s %s",
                self.profile_name,
                plan.item.media_kind.value,
                debug_title,
                debug_ids,
            )
            log.success("\t\tQUEUED UPDATE: %s", diff_str)
            self._pending_updates.append(plan)
            return SyncOutcome.SYNCED

        if self.dry_run:
            log.info(
                "[%s] Dry run enabled; skipping sync of %s %s %s",
                self.profile_name,
                plan.item.media_kind.value,
                debug_title,
                debug_ids,
            )
            log.success("\t\tDRY RUN UPDATE: %s", diff_str)
            return SyncOutcome.SKIPPED

        try:
            await self.list_provider.update_entry(plan.after.media_key, plan.entry)
            log.success(
                "[%s] Synced %s %s %s",
                self.profile_name,
                plan.item.media_kind.value,
                debug_title,
                debug_ids,
            )
            log.success("\t\tUPDATE: %s", diff_str)
            await self._create_sync_history(
                item=plan.item,
                child_item=plan.child,
                grandchild_items=plan.grandchildren,
                snapshots=(plan.before, plan.after),
                list_media_key=plan.list_media_key,
                mapping_descriptors=plan.mapping_descriptors,
                outcome=SyncOutcome.SYNCED,
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
            log.exception(
                "[%s] Sync update error details",
                self.profile_name,
            )
            await self._create_sync_history(
                item=plan.item,
                child_item=plan.child,
                grandchild_items=plan.grandchildren,
                snapshots=(plan.before, plan.after),
                list_media_key=plan.list_media_key,
                mapping_descriptors=plan.mapping_descriptors,
                outcome=SyncOutcome.FAILED,
                error_message=str(exc),
            )
            raise

    def _render_diff(self, plan: BatchUpdate[ParentMediaT, ChildMediaT]) -> str:
        """Render a diff string for a planned update."""
        diff = diff_snapshots(
            plan.before,
            plan.after,
            set(plan.after.to_dict().keys()),
        )
        return self._format_diff(diff)

    async def batch_sync(self) -> None:
        """Flush any queued batch updates to the list provider."""
        if not self._pending_updates:
            return

        log.success(
            "[%s] Syncing %s items to list provider in batch mode",
            self.profile_name,
            len(self._pending_updates),
        )

        if self.dry_run:
            log.info(
                "[%s] Dry run enabled; skipping batch sync of %s items",
                self.profile_name,
                len(self._pending_updates),
            )
            for update in self._pending_updates:
                diff_str = self._render_diff(update)
                debug_title = self._debug_log_title(
                    item=update.item, child_item=update.child
                )
                debug_ids = self._debug_log_ids(
                    item=update.item,
                    child_item=update.child,
                    entry=update.entry,
                    media_key=update.after.media_key,
                )
                log.success(
                    "[%s] Dry run update for %s %s %s",
                    self.profile_name,
                    update.item.media_kind.value,
                    debug_title,
                    debug_ids,
                )
                log.success("\t\tDRY RUN BATCH UPDATE: %s", diff_str)
            self._pending_updates.clear()
            return

        try:
            updated = await self.list_provider.update_entries_batch(
                [update.entry for update in self._pending_updates]
            )
            updated_list_keys = {
                entry.media().key for entry in updated if entry is not None
            }
            for update in self._pending_updates:
                await self._create_sync_history(
                    item=update.item,
                    child_item=update.child,
                    grandchild_items=update.grandchildren,
                    snapshots=(update.before, update.after),
                    list_media_key=update.list_media_key,
                    mapping_descriptors=update.mapping_descriptors,
                    outcome=SyncOutcome.SYNCED
                    if update.after.media_key in updated_list_keys
                    else SyncOutcome.FAILED,
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
                )
            raise
        finally:
            self._pending_updates.clear()

    async def _create_sync_history(
        self,
        *,
        item: ParentMediaT,
        child_item: ChildMediaT | None,
        grandchild_items: Sequence[LibraryEntry] | None,
        snapshots: tuple[EntrySnapshot | None, EntrySnapshot | None],
        list_media_key: str | None,
        mapping_descriptors: Sequence[MappingDescriptor] | None = None,
        outcome: SyncOutcome,
        error_message: str | None = None,
    ) -> None:
        """Record the outcome of a sync attempt."""
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

        library_target: LibraryEntry = child_item if child_item is not None else item
        library_namespace = self.library_provider.NAMESPACE
        library_section_key = library_target.section().key
        library_media_key = str(library_target.key)
        list_namespace = self.list_provider.NAMESPACE
        media_kind = library_target.media_kind

        with db() as ctx:
            if outcome == SyncOutcome.SYNCED:
                # Remove any previous NOT_FOUND/FAILED records on successful sync
                self._cleanup_failure_history(
                    item=item,
                    child_item=child_item,
                    list_media_key=resolved_list_media_key,
                )

            if outcome == SyncOutcome.SKIPPED:
                return

            async def get_mapping_entry_id() -> int | None:
                if not mapping_descriptors:
                    return None
                descriptor = sorted(mapping_descriptors, key=descriptor_key)[0]
                provider, entry_id, scope = descriptor
                entry = (
                    ctx.session.query(AnimapEntry)
                    .filter(
                        AnimapEntry.provider == provider,
                        AnimapEntry.entry_id == entry_id,
                        AnimapEntry.entry_scope == scope,
                    )
                    .first()
                )
                return entry.id if entry else None

            mapping_entry_id = await get_mapping_entry_id()

            if outcome in (SyncOutcome.NOT_FOUND, SyncOutcome.FAILED):
                # If a not found/failed record already exists, update it
                filters = [
                    SyncHistory.profile_name == self.profile_name,
                    SyncHistory.library_namespace == library_namespace,
                    SyncHistory.library_section_key == library_section_key,
                    SyncHistory.library_media_key == library_media_key,
                    SyncHistory.outcome == outcome,
                ]
                if resolved_list_media_key is None:
                    filters.append(SyncHistory.list_media_key.is_(None))
                else:
                    filters.extend(
                        [
                            SyncHistory.list_namespace == list_namespace,
                            SyncHistory.list_media_key == resolved_list_media_key,
                        ]
                    )
                existing = ctx.session.query(SyncHistory).filter(*filters).first()
                if existing:
                    if existing.error_message == error_message:
                        # If we're just seeing the same error, don't bring the record
                        # forward by updating the timestamp
                        return
                    existing.before_state = before_state
                    existing.after_state = after_state
                    existing.error_message = error_message
                    existing.timestamp = datetime.now(UTC)
                    existing.animap_entry_id = mapping_entry_id
                    ctx.session.commit()
                    return

            history_record = SyncHistory(
                profile_name=self.profile_name,
                library_namespace=library_namespace,
                library_section_key=library_section_key,
                library_media_key=library_media_key,
                list_namespace=list_namespace,
                list_media_key=resolved_list_media_key,
                animap_entry_id=mapping_entry_id,
                media_kind=media_kind,
                outcome=outcome,
                before_state=before_state,
                after_state=after_state,
                error_message=error_message,
            )
            ctx.session.add(history_record)
            ctx.session.commit()

    def flush_failure_history_cleanup(self) -> None:
        """Remove cached failure history rows in a single delete statement."""
        if not self._failure_history_cleanup_queue:
            return

        targets = set(self._failure_history_cleanup_queue)
        target_pairs = list(targets)
        profile_name = self.profile_name
        library_namespace = self.library_provider.NAMESPACE

        with db() as ctx:
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
                        SyncHistory.profile_name == profile_name,
                        SyncHistory.library_namespace == library_namespace,
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
                        SyncHistory.profile_name == profile_name,
                        SyncHistory.library_namespace == library_namespace,
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
            f"[{profile_name}] Cleaned up failure history for "
            f"{len(targets)} cached targets"
        )

    def _cleanup_failure_history(
        self,
        item: ParentMediaT,
        child_item: ChildMediaT | None = None,
        list_media_key: str | None = None,
    ) -> None:
        """Delete NOT_FOUND/FAILED history rows."""
        library_target: LibraryEntry = child_item if child_item is not None else item
        library_section_key = library_target.section().key
        library_media_key = str(library_target.key)

        if not library_media_key:
            return

        cleanup_key = (library_section_key, library_media_key, list_media_key)
        self._failure_history_cleanup_queue.add(cleanup_key)
        if list_media_key is not None:
            self._failure_history_cleanup_queue.add(
                (library_section_key, library_media_key, None)
            )
        if len(self._failure_history_cleanup_queue) >= (
            FAILURE_HISTORY_CLEANUP_BATCH_SIZE
        ):
            self.flush_failure_history_cleanup()

    def _should_apply_field(
        self,
        field: SyncField,
        new_value: Comparable | None,
        current_value: Comparable | None,
        skip_fields: set[str],
    ) -> bool:
        """Determine whether a field should be updated based on its rule."""
        if field.value in skip_fields or current_value == new_value:
            return False
        if (
            not self.destructive_sync
            and current_value is not None
            and new_value is None
        ):
            return False
        return self._is_sync_field_allowed(field, current_value, new_value)

    def _is_sync_field_allowed(
        self,
        field: SyncField,
        current_value: Comparable | None,
        new_value: Comparable | None,
    ) -> bool:
        """Check if syncing a field is allowed based on the sync_fields rules."""
        rules = self.sync_fields.get(field.value)
        if rules is None:
            return True
        if isinstance(rules, bool):
            return rules

        if isinstance(new_value, ListStatus) and rules.get(new_value.value) is False:
            return False

        if current_value is not None and new_value is not None:
            for key, blocked in (
                ("_lt", new_value < current_value),
                ("_lte", new_value <= current_value),
                ("_gt", new_value > current_value),
                ("_gte", new_value >= current_value),
            ):
                if rules.get(key, True) is False and blocked:
                    return False

        if rules.get("_eq", True) is False and new_value == current_value:
            return False
        return not (rules.get("_ne", True) is False and new_value != current_value)

    def _format_diff(self, diff: dict[str, tuple[Any, Any]]) -> str:
        """Format a diff dictionary for logging."""
        parts = [
            f"{field}: {self._format_value(before)} {ARROW} {self._format_value(after)}"
            for field, (before, after) in sorted(diff.items(), key=lambda item: item[0])
        ]
        return " | ".join(parts)

    @staticmethod
    def _format_value(value: Any) -> str:
        """Format individual values for diff logging."""
        if isinstance(value, ListStatus):
            return value.value
        if isinstance(value, datetime):
            dt = value
            dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
            return dt.isoformat()
        if value is None:
            return "None"
        return repr(value)

    @abstractmethod
    async def _calculate_status(
        self,
        *,
        item: ParentMediaT,
        child_item: ChildMediaT,
        grandchild_items: Sequence[GrandchildMediaT],
        entry: ListEntry,
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
    ) -> str | None:
        """Calculate the desired review/notes for the list entry."""
        ...

    @abstractmethod
    def _debug_log_title(
        self,
        *,
        item: ParentMediaT,
        child_item: ChildMediaT | None = None,
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
