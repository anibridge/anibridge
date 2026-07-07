"""Synchronization engine for the AniBridge provider contract."""

import asyncio
import contextlib
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime
from typing import cast

import msgspec
from anibridge.provider.base import (
    Capabilities,
    Delete,
    Event,
    EventQuery,
    EventSpec,
    ExternalId,
    FacetName,
    Node,
    NodeFlag,
    Page,
    Progress,
    Provider,
    Record,
    RecordField,
    RecordQuery,
    Ref,
    ResourceKind,
    Role,
    ScanItem,
    ScanQuery,
    Status,
    Step,
    SupportsReads,
    SupportsScan,
    SupportsWrites,
    UpsertEvent,
    UpsertRecord,
    Write,
    WriteAction,
    WriteResult,
)
from anibridge.utils.mappings import AnibridgeMapping

from anibridge.app.config.database import db
from anibridge.app.config.settings import SyncRulesConfig
from anibridge.app.core.animap import AnimapClient
from anibridge.app.core.sync import (
    RecordUndoRequest,
    RefKey,
    ScanPlan,
    ref_from_payload,
    ref_to_json,
    ref_to_key,
)
from anibridge.app.core.sync.history import SyncHistoryManager
from anibridge.app.core.sync.planner import (
    PreparedUpdate,
    RecordPlanner,
    SyncLabel,
)
from anibridge.app.core.sync.projection import MappingProjector
from anibridge.app.core.sync.rules import SyncRuleEngine
from anibridge.app.core.sync.stats import (
    MappingRange,
    RecordPlan,
    RecordSnapshot,
    SyncItem,
    SyncStats,
)
from anibridge.app.core.sync.targeting import ResolvedTarget, TargetResolver
from anibridge.app.logging import get_logger
from anibridge.app.models.db.pin import Pin
from anibridge.app.models.db.sync_history import SyncOperationAction, SyncOutcome

__all__ = ["SyncClient"]

log = get_logger(__name__)

_OUTCOME_PRIORITY: Mapping[SyncOutcome, int] = {
    SyncOutcome.FAILED: 5,
    SyncOutcome.SYNCED: 4,
    SyncOutcome.DELETED: 4,
    SyncOutcome.NOT_FOUND: 3,
    SyncOutcome.SKIPPED: 2,
    SyncOutcome.PENDING: 1,
}


def _event_specs(capabilities: Capabilities) -> tuple[EventSpec, ...]:
    """Return event resource specs from unified provider capabilities."""
    return tuple(spec for spec in capabilities.specs if isinstance(spec, EventSpec))


def _status_supersedes(candidate: Status, current: Status) -> bool:
    replacements = {
        Status.PLANNED: {
            Status.ACTIVE,
            Status.PAUSED,
            Status.COMPLETED,
            Status.DROPPED,
            Status.REPEATING,
        },
        Status.ACTIVE: {
            Status.PAUSED,
            Status.COMPLETED,
            Status.DROPPED,
            Status.REPEATING,
        },
        Status.PAUSED: {
            Status.ACTIVE,
            Status.COMPLETED,
            Status.DROPPED,
            Status.REPEATING,
        },
        Status.DROPPED: {
            Status.ACTIVE,
            Status.PAUSED,
            Status.COMPLETED,
            Status.REPEATING,
        },
        Status.COMPLETED: {Status.REPEATING},
        Status.REPEATING: set(),
    }[current]
    return candidate in replacements


class _TargetWork(msgspec.Struct, frozen=True):
    """One source record resolved to one target record location."""

    item: ScanItem
    sync_items: tuple[SyncItem, ...]
    projected_record: Record
    target_ref: Ref
    target_surface: str
    mappings: Sequence[MappingRange]
    label: SyncLabel
    source_descriptor: ExternalId | None = None

    @property
    def key(self) -> TargetRecordKey:
        return (ref_to_key(self.target_ref), self.target_surface)


type TargetRecordKey = tuple[RefKey, str]
type EventIdentity = tuple[RefKey, str, str, str | datetime]


class SyncClient:
    """Synchronize records from one provider to another."""

    def __init__(
        self,
        *,
        source_provider: Provider,
        target_provider: Provider,
        animap_client: AnimapClient,
        destructive_sync: bool,
        dry_run: bool,
        profile_name: str,
        sync_rules: SyncRulesConfig | None = None,
    ) -> None:
        """Initialize the normalized sync client."""
        self.source_provider = source_provider
        self.target_provider = target_provider
        self.destructive_sync = destructive_sync
        self.dry_run = dry_run
        self.profile_name = profile_name
        self.sync_stats = SyncStats()
        self._target_resolver = TargetResolver(
            target_provider=target_provider,
            animap_client=animap_client,
        )

        self._source_capabilities = source_provider.capabilities()
        self._target_capabilities = target_provider.capabilities()
        self._target_event_specs = {
            spec.kind.native: spec for spec in _event_specs(self._target_capabilities)
        }
        self._validate_events(
            provider=self.source_provider, capabilities=self._source_capabilities
        )
        self._validate_events(
            provider=self.target_provider, capabilities=self._target_capabilities
        )
        self._sync_rule_engine = SyncRuleEngine(sync_rules)
        self._planner = RecordPlanner(
            source_capabilities=self._source_capabilities,
            target_capabilities=self._target_capabilities,
            sync_rule_engine=self._sync_rule_engine,
            destructive_sync=self.destructive_sync,
        )
        self._planner.validate_provider_contracts(
            source_provider=self.source_provider,
            target_provider=self.target_provider,
        )
        self._sync_fields = self._planner.sync_fields
        self._event_pairs = self._event_sync_pairs()
        if not self._sync_fields:
            raise TypeError(
                f"Providers '{self.source_provider.NAMESPACE}' and "
                f"'{self.target_provider.NAMESPACE}' have no common readable/writable "
                "record fields"
            )
        self._history = SyncHistoryManager(
            profile_name=self.profile_name,
            source_namespace=self.source_provider.NAMESPACE,
            target_namespace=self.target_provider.NAMESPACE,
            db_factory=lambda: db(),
        )

    @staticmethod
    def _validate_events(
        *,
        provider: Provider,
        capabilities: Capabilities,
    ) -> None:
        """Fail fast when event specs and event protocols disagree."""
        event_specs = _event_specs(capabilities)
        if not event_specs:
            return

        if Role.SOURCE in capabilities.roles and not isinstance(
            provider,
            SupportsReads,
        ):
            raise TypeError(
                f"Provider {provider.NAMESPACE!r} advertises source event channels "
                "but does not implement event reads"
            )

        writable_events = tuple(
            spec
            for spec in event_specs
            if spec.write_actions.intersection({WriteAction.UPSERT, WriteAction.DELETE})
        )
        if writable_events and not isinstance(provider, SupportsWrites):
            raise TypeError(
                f"Provider {provider.NAMESPACE!r} advertises writable event "
                "channels but does not implement event writes"
            )

        deletable_events = tuple(
            spec for spec in event_specs if WriteAction.DELETE in spec.write_actions
        )
        if deletable_events and not isinstance(provider, SupportsReads):
            raise TypeError(
                f"Provider {provider.NAMESPACE!r} advertises deletable event "
                "channels but does not implement event reads"
            )

        readable_events = tuple(
            spec
            for spec in event_specs
            if WriteAction.UPSERT not in spec.write_actions
            or WriteAction.DELETE in spec.write_actions
        )
        if readable_events and not isinstance(provider, SupportsReads):
            raise TypeError(
                f"Provider {provider.NAMESPACE!r} advertises readable event channels "
                "but does not implement event reads"
            )

    async def clear_cache(self) -> None:
        """Clear sync and provider caches."""
        await self.source_provider.clear_cache()
        await self.target_provider.clear_cache()

    async def scan_source_pages(
        self,
        *,
        scan: ScanPlan,
        page_size: int | None = None,
    ) -> AsyncIterator[Page[ScanItem]]:
        """Stream source scan pages using the provider contract."""
        cursor: str | None = None
        source_refs = tuple(scan.source_refs or ())
        facets = frozenset(
            facet
            for facet in (FacetName.IDS, FacetName.STRUCTURE)
            if facet in self._source_capabilities.facets
        )
        record_kinds = frozenset(self._planner.source_record_kinds())
        source_provider = cast(SupportsScan, self.source_provider)

        async def fetch_page(cursor: str | None):
            return await source_provider.scan(
                ScanQuery(
                    sources=source_refs,
                    flags=frozenset({NodeFlag.TRACKABLE}),
                    facets=facets,
                    record_kinds=record_kinds,
                    record_fields=frozenset(self._sync_fields),
                    include_records=True,
                    require_user_data=scan.require_user_data,
                    cursor=cursor,
                    limit=page_size,
                )
            )

        page_task = asyncio.create_task(fetch_page(cursor))
        try:
            while True:
                page = await page_task
                if page.cursor is None:
                    if page.items:
                        yield page
                    break

                cursor = page.cursor
                page_task = asyncio.create_task(fetch_page(cursor))
                if page.items:
                    yield page
        finally:
            if not page_task.done():
                page_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await page_task
            else:
                with contextlib.suppress(asyncio.CancelledError):
                    page_task.exception()

    async def process_page(self, items: Sequence[ScanItem]) -> None:
        """Process one source scan page with batched target reads and writes."""
        if not items:
            return

        outcomes, work_items = await self._resolve_work_items(items)
        record_work_items = self._merge_record_work_items(work_items)
        pinned_target_parents = self._fetch_pinned_target_parents(
            work.target_ref for work in work_items
        )
        target_key_counts = Counter(work.key for work in record_work_items)
        target_records = await self._fetch_target_records_batch(
            (work.target_ref, work.target_surface)
            for work in record_work_items
            if target_key_counts[work.key] == 1
        )

        updates: list[tuple[_TargetWork, PreparedUpdate]] = []
        for work in record_work_items:
            try:
                if self._target_parent_key(work.target_ref) in pinned_target_parents:
                    log.info(
                        "[%s] Skipping %s %s because target parent %s is pinned",
                        self.profile_name,
                        work.label.node_kind,
                        work.label.source,
                        work.target_ref.key,
                    )
                    continue
                target_record = (
                    await self._fetch_target_record(
                        work.target_ref,
                        work.target_surface,
                    )
                    if target_key_counts[work.key] > 1
                    else target_records.get(work.key)
                )
                planned = await self._prepare_record_update(
                    item=work.item,
                    source_record=work.projected_record,
                    target_record=target_record,
                    target_ref=work.target_ref,
                    target_kind=work.target_surface,
                    mappings=work.mappings,
                    label=work.label,
                    source_descriptor=work.source_descriptor,
                )
                if isinstance(planned, SyncOutcome):
                    self._record_best_outcome(
                        outcomes,
                        work.sync_items,
                        planned,
                    )
                else:
                    updates.append((work, planned))
            except Exception:
                log.error(
                    "[%s] Failed to process %s %s with target %s",
                    self.profile_name,
                    work.label.node_kind,
                    work.label.source,
                    work.label.target,
                )
                log.exception(
                    "[%s] Sync processing error details",
                    self.profile_name,
                )
                self._record_best_outcome(
                    outcomes,
                    work.sync_items,
                    SyncOutcome.FAILED,
                )

        update_outcomes = await self._apply_updates(
            tuple(update for _work, update in updates)
        )
        for (work, _update), outcome in zip(updates, update_outcomes, strict=True):
            self._record_best_outcome(outcomes, work.sync_items, outcome)

        for work in work_items:
            try:
                outcome = await self._sync_events_for_work(
                    work,
                    pinned_target_parents=pinned_target_parents,
                )
            except Exception:
                log.error(
                    "[%s] Failed to sync events for %s %s with target %s",
                    self.profile_name,
                    work.label.node_kind,
                    work.label.source,
                    work.label.target,
                )
                log.exception("[%s] Event sync error details", self.profile_name)
                outcome = SyncOutcome.FAILED
            if outcome != SyncOutcome.SKIPPED:
                self._record_best_outcome(outcomes, work.sync_items, outcome)

        for sync_item, outcome in outcomes.items():
            self.sync_stats.track_item(sync_item, outcome)

    def _merge_record_work_items(
        self,
        work_items: Iterable[_TargetWork],
    ) -> list[_TargetWork]:
        """Merge mapped records that resolve to the same target record."""
        grouped: dict[tuple[RefKey, TargetRecordKey], list[_TargetWork]] = {}
        order: list[tuple[RefKey, TargetRecordKey]] = []
        for work in work_items:
            group_key = (ref_to_key(work.item.node.ref), work.key)
            if group_key not in grouped:
                grouped[group_key] = []
                order.append(group_key)
            grouped[group_key].append(work)

        merged_work: list[_TargetWork] = []
        for group_key in order:
            works = grouped[group_key]
            if len(works) == 1:
                merged_work.append(works[0])
                continue

            first = works[0]
            values = dict(first.projected_record.values)
            mappings: list[MappingRange] = []
            sync_items: list[SyncItem] = []
            labels: list[SyncLabel] = []
            for work in works:
                mappings.extend(work.mappings)
                sync_items.extend(work.sync_items)
                labels.append(work.label)

                for field, value in work.projected_record.values.items():
                    existing = values.get(field)
                    if field == RecordField.PROGRESS:
                        if not isinstance(value, Progress):
                            continue
                        if not isinstance(existing, Progress):
                            values[field] = value
                            continue
                        totals = tuple(
                            item.total
                            for item in (existing, value)
                            if item.total is not None
                        )
                        values[field] = Progress(
                            current=max(existing.current or 0, value.current or 0),
                            total=max(totals) if totals else None,
                            unit=existing.unit or value.unit,
                        )
                    elif isinstance(value, date) and field == RecordField.STARTED_AT:
                        if not isinstance(existing, date) or self._date_key(
                            value
                        ) < self._date_key(existing):
                            values[field] = value
                    elif isinstance(value, date) and field in (
                        RecordField.FINISHED_AT,
                        RecordField.LAST_ACTIVITY_AT,
                    ):
                        if not isinstance(existing, date) or self._date_key(
                            value
                        ) > self._date_key(existing):
                            values[field] = value
                    elif field == RecordField.STATUS:
                        status = RecordPlanner.status_of(value)
                        existing_status = RecordPlanner.status_of(existing)
                        if existing is None or (
                            status is not None
                            and (
                                existing_status is None
                                or _status_supersedes(status, existing_status)
                            )
                        ):
                            values[field] = value
                    elif existing is None:
                        values[field] = value

            merged_work.append(
                _TargetWork(
                    item=first.item,
                    sync_items=tuple(dict.fromkeys(sync_items)),
                    projected_record=replace(first.projected_record, values=values),
                    target_ref=first.target_ref,
                    target_surface=first.target_surface,
                    source_descriptor=first.source_descriptor,
                    mappings=tuple(dict.fromkeys(mappings)),
                    label=self._merged_label(labels),
                )
            )
        return merged_work

    @staticmethod
    def _date_key(value: date | datetime) -> tuple[int, int, int, int, int, int, int]:
        """Return a comparable key for date and datetime values."""
        if isinstance(value, datetime):
            return (
                value.year,
                value.month,
                value.day,
                value.hour,
                value.minute,
                value.second,
                value.microsecond,
            )
        return (value.year, value.month, value.day, 0, 0, 0, 0)

    @staticmethod
    def _merged_label(labels: Sequence[SyncLabel]) -> SyncLabel:
        """Combine labels for a merged target record write."""
        first = labels[0]
        sources = tuple(dict.fromkeys(label.source for label in labels))
        targets = tuple(
            dict.fromkeys(label.target for label in labels if label.target is not None)
        )
        return SyncLabel(
            node_kind=first.node_kind,
            source="; ".join(sources),
            target="; ".join(targets) if targets else None,
        )

    async def _resolve_work_items(
        self,
        items: Sequence[ScanItem],
    ) -> tuple[dict[SyncItem, SyncOutcome], list[_TargetWork]]:
        outcomes: dict[SyncItem, SyncOutcome] = {}
        work_items: list[_TargetWork] = []

        for item in items:
            label = self._sync_label(item=item)
            log.debug(
                "[%s] Processing %s %s",
                self.profile_name,
                label.node_kind,
                label.source,
            )

            if not self._sync_rule_engine.allows_node(node=item.node):
                log.info(
                    "[%s] Skipping %s %s because sync_rules blocked the node",
                    self.profile_name,
                    label.node_kind,
                    label.source,
                )
                continue

            records = self._planner.syncable_source_records(item)
            if not records:
                log.debug(
                    "[%s] Skipping %s %s because it has no trackable records",
                    self.profile_name,
                    label.node_kind,
                    label.source,
                )
                continue

            sync_items_by_record = [
                SyncItem.from_record_parts(
                    namespace=self.source_provider.NAMESPACE,
                    node=item.node,
                    record=record,
                )
                for record in records
            ]
            for sync_items in sync_items_by_record:
                self.sync_stats.register_pending_items(sync_items)

            for record, sync_items in zip(records, sync_items_by_record, strict=True):
                for sync_item in sync_items:
                    outcomes.setdefault(sync_item, SyncOutcome.SKIPPED)
                matches = await self._target_resolver.resolve(
                    node=item.node,
                    record=record,
                )
                if not matches:
                    log.warning(
                        "[%s] No target refs found for %s %s record %s",
                        self.profile_name,
                        label.node_kind,
                        label.source,
                        record.ref,
                    )
                    await self._history.create_sync_history(
                        source_node=item.node,
                        source_record=record,
                        target_ref=None,
                        snapshots=(None, None),
                        outcome=SyncOutcome.NOT_FOUND,
                        info={"trackable_count": str(len(sync_items))},
                        ephemeral=self.dry_run,
                    )
                    for sync_item in sync_items:
                        outcomes[sync_item] = SyncOutcome.NOT_FOUND
                    continue

                work_items.extend(
                    self._work_items_for_matches(
                        item=item,
                        source_record=record,
                        sync_items=sync_items,
                        matches=matches,
                        label=label,
                    )
                )

        return outcomes, work_items

    def _work_items_for_matches(
        self,
        *,
        item: ScanItem,
        source_record: Record,
        sync_items: tuple[SyncItem, ...],
        matches: Sequence[ResolvedTarget],
        label: SyncLabel,
    ) -> tuple[_TargetWork, ...]:
        target_surface = self._planner.target_record_surface_for(source_record.surface)
        if target_surface is None:
            log.debug(
                "[%s] Skipping record surface $$'%s'$$ for %s %s because target "
                "provider has no matching record surface",
                self.profile_name,
                source_record.surface,
                label.node_kind,
                label.source,
            )
            return ()

        scoped_matches = tuple(match for match in matches if match.mappings) or tuple(
            matches
        )
        work_items: list[_TargetWork] = []
        for match in scoped_matches:
            target_ref = match.ref
            work_label = self._sync_label(
                item=item,
                target_ref=target_ref,
                source_descriptor=match.source_id,
                target_descriptor=match.target_id,
                mappings=match.mappings,
            )
            work_items.append(
                _TargetWork(
                    item=item,
                    sync_items=sync_items,
                    projected_record=self._planner.project_source_record(
                        source_record,
                        mappings=match.mappings,
                    ),
                    target_ref=target_ref,
                    target_surface=target_surface,
                    source_descriptor=match.source_id,
                    mappings=self._mappings_for_match(match),
                    label=work_label,
                )
            )
            log.debug(
                "[%s] Resolved target record for %s %s with target %s",
                self.profile_name,
                work_label.node_kind,
                work_label.source,
                work_label.target,
            )

        return tuple(work_items)

    @staticmethod
    def _record_best_outcome(
        outcomes: dict[SyncItem, SyncOutcome],
        sync_items: Iterable[SyncItem],
        outcome: SyncOutcome,
    ) -> None:
        """Keep the highest-priority outcome for a source record."""
        for sync_item in sync_items:
            current = outcomes.get(sync_item, SyncOutcome.SKIPPED)
            if _OUTCOME_PRIORITY[outcome] > _OUTCOME_PRIORITY[current]:
                outcomes[sync_item] = outcome

    async def _prepare_record_update(
        self,
        *,
        item: ScanItem,
        source_record: Record,
        target_record: Record | None,
        target_ref: Ref,
        target_kind: str,
        label: SyncLabel,
        source_descriptor: ExternalId | None = None,
        mappings: Sequence[MappingRange] = (),
    ) -> PreparedUpdate | SyncOutcome:
        """Plan one source-to-target record mutation without applying updates."""
        before_snapshot = (
            RecordSnapshot.from_record(target_record) if target_record else None
        )

        if not source_record.values and target_record is not None:
            if self.destructive_sync:
                return await self._delete_record(
                    item=item,
                    source_record=source_record,
                    target_record=target_record,
                    target_ref=target_ref,
                    target_kind=target_kind,
                    before_snapshot=before_snapshot,
                    label=label,
                )
            log.info(
                "[%s] Skipping %s %s because source record is empty and "
                "destructive sync is disabled%s",
                self.profile_name,
                label.node_kind,
                label.source,
                self._target_suffix(label),
            )
            return SyncOutcome.SKIPPED

        planned = self._planner.prepare_upsert(
            item,
            source_record=source_record,
            target_record=target_record,
            target_ref=target_ref,
            target_kind=target_kind,
            label=label,
            mappings=mappings,
        )
        if isinstance(planned, SyncOutcome):
            if planned == SyncOutcome.SKIPPED:
                log.info(
                    "[%s] Skipping %s %s because it is already up to date%s",
                    self.profile_name,
                    label.node_kind,
                    label.source,
                    self._target_suffix(label),
                )
                self._history.queue_failure_history_cleanup(
                    source_ref=source_record.ref,
                    target_ref=target_ref,
                )
            return planned
        return PreparedUpdate(
            plan=planned.plan,
            source_record=planned.source_record,
            diff_str=planned.diff_str,
            label=planned.label,
            source_descriptor=source_descriptor,
        )

    async def _apply_updates(
        self,
        updates: Sequence[PreparedUpdate],
    ) -> tuple[SyncOutcome, ...]:
        """Apply planned record updates in one provider write batch."""
        if not updates:
            return ()
        if self.dry_run:
            return tuple(
                [await self._record_update_dry_run(update) for update in updates]
            )

        try:
            results = await self._write_batch(
                tuple(update.plan.write for update in updates),
                resource_label="record",
            )
        except Exception as exc:
            return tuple(
                [
                    await self._record_update_write_error(update, exc)
                    for update in updates
                ]
            )

        outcomes: list[SyncOutcome] = []
        for update, result in zip(updates, results, strict=True):
            if result.ok:
                outcomes.append(await self._record_update_success(update))
                continue
            outcomes.append(
                await self._record_update_write_error(
                    update,
                    self._write_result_exception(result),
                )
            )
        return tuple(outcomes)

    async def undo_records(
        self,
        requests: Sequence[RecordUndoRequest],
    ) -> tuple[SyncOutcome, ...]:
        """Restore target record states captured in history."""
        outcomes: list[SyncOutcome] = [await self._undo_record(r) for r in requests]
        return tuple(outcomes)

    async def _undo_record(self, request: RecordUndoRequest) -> SyncOutcome:
        """Apply one target record undo request."""
        before = request.before
        after = request.after
        try:
            write = self._undo_record_write(request)
            if self.dry_run:
                log.debug(
                    "[%s] Dry run; skipping undo of target record %s",
                    self.profile_name,
                    request.target_ref,
                )
            else:
                await self._write([write], resource_label="record")
            await self._history.create_sync_history(
                source_node=Node(ref=request.source_ref, kind="history_undo"),
                source_record=None,
                target_ref=request.target_ref,
                snapshots=(after, before),
                outcome=SyncOutcome.UNDONE,
                info={"source": "history:undo_item"},
                ephemeral=self.dry_run,
                dedupe_failures=False,
            )
            return SyncOutcome.UNDONE
        except Exception as exc:
            log.error(
                "[%s] Failed to undo target record %s: %s",
                self.profile_name,
                request.target_ref,
                exc,
            )
            await self._history.create_sync_history(
                source_node=Node(ref=request.source_ref, kind="history_undo"),
                source_record=None,
                target_ref=request.target_ref,
                snapshots=(after, before),
                outcome=SyncOutcome.FAILED,
                error_message=str(exc),
                info={
                    "source": "history:undo_item",
                    "error_type": type(exc).__name__,
                },
                ephemeral=self.dry_run,
                dedupe_failures=False,
            )
            return SyncOutcome.FAILED

    def _undo_record_write(self, request: RecordUndoRequest) -> Write:
        """Build the provider write that restores a history snapshot."""
        before = request.before
        after = request.after
        if before is None:
            if after is None:
                raise ValueError("Cannot undo history item without record state")
            if not self._planner.can_delete_record(after.surface):
                raise TypeError(
                    "Target provider does not support deleting created records"
                )
            return Delete(
                resource=ResourceKind.RECORD,
                ref=request.target_ref,
                name=after.surface,
            )

        set_values = before.values_for_restore()
        before_fields = set(set_values)
        after_fields = set(after.values) if after is not None else set()
        return UpsertRecord(
            ref=request.target_ref,
            surface=before.surface,
            key=before.key,
            set=set_values,
            clear=frozenset(after_fields - before_fields),
        )

    async def _record_update_dry_run(self, update: PreparedUpdate) -> SyncOutcome:
        """Record a dry-run update outcome without writing to the target."""
        log.success(
            "[%s] Dry run; skipping sync of %s %s",
            self.profile_name,
            update.label.node_kind,
            self._source_with_target(update.label),
        )
        log.success("\tDRY RUN UPDATE: %s", update.diff_str)
        await self._history.create_sync_history(
            source_node=update.plan.item.node,
            source_record=update.source_record,
            target_ref=update.plan.target_ref,
            snapshots=(update.plan.before, update.plan.after),
            outcome=SyncOutcome.SYNCED,
            info=update.plan.diagnostics.as_info(),
            external_id=update.source_descriptor,
            ephemeral=self.dry_run,
        )
        return SyncOutcome.SYNCED

    async def _record_update_success(self, update: PreparedUpdate) -> SyncOutcome:
        """Record a successful target record update."""
        log.success(
            "[%s] Synced %s %s",
            self.profile_name,
            update.label.node_kind,
            self._source_with_target(update.label),
        )
        log.success("\tUPDATE: %s", update.diff_str)
        await self._history.create_sync_history(
            source_node=update.plan.item.node,
            source_record=update.source_record,
            target_ref=update.plan.target_ref,
            snapshots=(update.plan.before, update.plan.after),
            outcome=SyncOutcome.SYNCED,
            info=update.plan.diagnostics.as_info(),
            external_id=update.source_descriptor,
            ephemeral=self.dry_run,
        )
        return SyncOutcome.SYNCED

    async def _record_update_write_error(
        self,
        update: PreparedUpdate,
        exc: Exception,
    ) -> SyncOutcome:
        """Record or reconcile a failed target record update."""
        if await self._target_matches_after(update.plan):
            log.warning(
                "[%s] Provider reported failure after writing %s %s; target "
                "state matches the planned update, so marking it synced: %s",
                self.profile_name,
                update.label.node_kind,
                self._source_with_target(update.label),
                exc,
            )
            await self._history.create_sync_history(
                source_node=update.plan.item.node,
                source_record=update.source_record,
                target_ref=update.plan.target_ref,
                snapshots=(update.plan.before, update.plan.after),
                outcome=SyncOutcome.SYNCED,
                info={
                    **update.plan.diagnostics.as_info(),
                    "write_reconciled_after_error": "true",
                    "write_error_type": type(exc).__name__,
                    "write_error": str(exc),
                },
                external_id=update.source_descriptor,
                ephemeral=self.dry_run,
            )
            return SyncOutcome.SYNCED

        log.error(
            "[%s] Failed to sync %s %s: %s",
            self.profile_name,
            update.label.node_kind,
            self._source_with_target(update.label),
            exc,
        )
        await self._history.create_sync_history(
            source_node=update.plan.item.node,
            source_record=update.source_record,
            target_ref=update.plan.target_ref,
            snapshots=(update.plan.before, update.plan.after),
            outcome=SyncOutcome.FAILED,
            error_message=str(exc),
            info={
                **update.plan.diagnostics.as_info(),
                "error_type": type(exc).__name__,
            },
            external_id=update.source_descriptor,
            ephemeral=self.dry_run,
        )
        return SyncOutcome.FAILED

    async def _target_matches_after(self, plan: RecordPlan) -> bool:
        """Return whether the current target state matches a planned upsert."""
        if plan.target_ref is None or plan.after is None:
            return False
        if not isinstance(self.target_provider, SupportsReads):
            return False
        try:
            actual = await self._fetch_target_record(
                plan.target_ref,
                plan.after.surface,
            )
        except Exception:
            log.exception("[%s] Failed to reconcile target state", self.profile_name)
            return False
        if actual is None:
            return False

        actual_snapshot = RecordSnapshot.from_record(actual)
        for field, expected_value in plan.after.values.items():
            if actual_snapshot.values.get(field) != expected_value:
                return False
        return True

    def flush_failure_history_cleanup(self) -> None:
        """Flush queued failure-history cleanup operations."""
        self._history.flush_failure_history_cleanup()
        self._history.complete_run()

    async def _delete_record(
        self,
        *,
        item: ScanItem,
        source_record: Record,
        target_record: Record | None,
        target_ref,
        target_kind: str,
        before_snapshot: RecordSnapshot | None,
        label: SyncLabel,
    ) -> SyncOutcome:
        """Delete a target record when destructive sync permits it."""
        if not self._planner.can_delete_record(target_kind):
            log.warning(
                "[%s] Skipping deletion for %s %s because target provider "
                "does not advertise delete_record",
                self.profile_name,
                label.node_kind,
                self._source_with_target(label),
            )
            return SyncOutcome.SKIPPED
        log.success(
            "[%s] Deleting target record for %s %s",
            self.profile_name,
            label.node_kind,
            self._source_with_target(label),
        )
        if self.dry_run:
            log.debug(
                "[%s] Dry run; skipping deletion of %s %s",
                self.profile_name,
                label.node_kind,
                self._source_with_target(label),
            )
            await self._history.create_sync_history(
                source_node=item.node,
                source_record=source_record,
                target_ref=target_ref,
                snapshots=(before_snapshot, None),
                outcome=SyncOutcome.DELETED,
                ephemeral=self.dry_run,
            )
            return SyncOutcome.DELETED

        write = Delete(
            resource=ResourceKind.RECORD,
            ref=target_ref,
            name=target_kind,
            key=target_record.key if target_record else None,
        )
        await self._write([write], resource_label="record")
        await self._history.create_sync_history(
            source_node=item.node,
            source_record=source_record,
            target_ref=target_ref,
            snapshots=(before_snapshot, None),
            outcome=SyncOutcome.DELETED,
            ephemeral=self.dry_run,
        )
        return SyncOutcome.DELETED

    async def _sync_events_for_work(
        self,
        work: _TargetWork,
        *,
        pinned_target_parents: frozenset[RefKey] = frozenset(),
    ) -> SyncOutcome:
        """Append missing source events to the resolved target ref."""
        if not self._event_pairs:
            return SyncOutcome.SKIPPED
        if not isinstance(self.source_provider, SupportsReads):
            return SyncOutcome.SKIPPED
        if not isinstance(self.target_provider, SupportsWrites):
            return SyncOutcome.SKIPPED
        if self._target_parent_key(work.target_ref) in pinned_target_parents:
            log.info(
                "[%s] Skipping events for %s %s because target parent %s is pinned",
                self.profile_name,
                work.label.node_kind,
                work.label.source,
                work.target_ref.key,
            )
            return SyncOutcome.SKIPPED

        writes: list[Write] = []
        operations: list[tuple[Event, Ref, SyncOperationAction, str, str | None]] = []
        append_count = 0
        delete_count = 0
        for source_kind, target_kind in self._event_pairs:
            can_append = self._can_append_event(target_kind)
            can_delete = self.destructive_sync and self._can_delete_event(target_kind)
            if not can_append and not can_delete:
                continue

            source_events = await self._fetch_source_events(
                work.projected_record.ref,
                source_kind,
            )
            projected_events = tuple(
                (event, target_ref)
                for event in source_events
                if self._source_event_in_scope(event, work)
                for target_ref in self._target_event_refs(event.ref, work)
            )
            target_refs = tuple(target_ref for _event, target_ref in projected_events)
            if can_delete and not target_refs:
                target_refs = (work.target_ref,)
            target_events = await self._fetch_target_events(
                target_refs,
                target_kind,
            )
            existing_events = {
                self._event_identity(
                    event.ref,
                    event.kind,
                    event.at,
                    event.dedupe_key,
                )
                for event in target_events
            }
            projected_event_identities: set[EventIdentity] = set()
            for event, target_ref in projected_events:
                dedupe_key = self._event_dedupe_key(
                    event,
                    target_ref,
                    scoped=len(projected_events) > 1,
                )
                identity = self._event_identity(
                    target_ref,
                    target_kind,
                    event.at,
                    dedupe_key,
                )
                projected_event_identities.add(identity)
                if not can_append or identity in existing_events:
                    continue
                if not self._sync_rule_engine.allows_event(
                    action="upsert",
                    kind=target_kind,
                    destructive_sync=self.destructive_sync,
                ):
                    continue
                writes.append(
                    UpsertEvent(
                        ref=target_ref,
                        kind=target_kind,
                        at=event.at,
                        dedupe_key=dedupe_key,
                        metadata=event.metadata,
                    )
                )
                operations.append(
                    (
                        event,
                        target_ref,
                        SyncOperationAction.UPSERT,
                        target_kind,
                        dedupe_key,
                    )
                )
                append_count += 1

            if can_delete:
                for event in target_events:
                    identity = self._event_identity(
                        event.ref,
                        event.kind,
                        event.at,
                        event.dedupe_key,
                    )
                    if identity in projected_event_identities:
                        continue
                    if not self._sync_rule_engine.allows_event(
                        action="delete",
                        kind=event.kind,
                        destructive_sync=self.destructive_sync,
                    ):
                        continue
                    writes.append(self._delete_event_for(event))
                    operations.append(
                        (
                            event,
                            event.ref,
                            SyncOperationAction.DELETE,
                            event.kind,
                            event.dedupe_key,
                        )
                    )
                    delete_count += 1

        if not writes:
            return SyncOutcome.SKIPPED
        if self.dry_run:
            log.success(
                "[%s] Dry run; skipping sync of %s activity event changes "
                "(%s append, %s delete) for %s %s",
                self.profile_name,
                len(writes),
                append_count,
                delete_count,
                work.label.node_kind,
                self._source_with_target(work.label),
            )
            for event, target_ref, action, target_kind, dedupe_key in operations:
                await self._history.record_event_operation(
                    source_ref=event.ref
                    if action == SyncOperationAction.UPSERT
                    else work.projected_record.ref,
                    target_ref=target_ref,
                    action=action,
                    outcome=SyncOutcome.SYNCED,
                    event_kind=target_kind,
                    event_at=event.at,
                    dedupe_key=dedupe_key,
                    resource_key=event.key,
                    info={
                        "dry_run": "true",
                        "source_event_kind": event.kind,
                        "operation": "event_sync",
                    },
                    ephemeral=self.dry_run,
                )
            return SyncOutcome.SYNCED

        results = await self._write_batch(writes, resource_label="event")
        failed_result: WriteResult | None = None
        for (event, target_ref, action, target_kind, dedupe_key), result in zip(
            operations,
            results,
            strict=True,
        ):
            outcome = SyncOutcome.SYNCED if result.ok else SyncOutcome.FAILED
            if not result.ok and failed_result is None:
                failed_result = result
            await self._history.record_event_operation(
                source_ref=event.ref
                if action == SyncOperationAction.UPSERT
                else work.projected_record.ref,
                target_ref=result.ref or target_ref,
                action=action,
                outcome=outcome,
                event_kind=target_kind,
                event_at=event.at,
                dedupe_key=dedupe_key,
                resource_key=result.key or event.key,
                error_message=result.error,
                info={
                    "source_event_kind": event.kind,
                    "operation": "event_sync",
                    "write_result_action": result.action,
                    "write_result_resource": result.resource,
                    "write_result_code": result.code,
                    "write_result_error": result.error,
                    "write_result_revision": result.revision,
                },
                ephemeral=self.dry_run,
            )
        if failed_result is not None:
            raise self._write_result_exception(failed_result, "event")
        log.success(
            "[%s] Synced %s activity event changes (%s appended, %s deleted) for %s %s",
            self.profile_name,
            len(writes),
            append_count,
            delete_count,
            work.label.node_kind,
            self._source_with_target(work.label),
        )
        return SyncOutcome.SYNCED

    async def _fetch_source_events(self, ref: Ref, kind: str) -> tuple[Event, ...]:
        """Fetch source events for one source ref and event channel."""
        if not isinstance(self.source_provider, SupportsReads):
            return ()
        events: list[Event] = []
        cursor: str | None = None
        while True:
            page = cast(
                Page[Event],
                await self.source_provider.fetch(
                    EventQuery(
                        refs=(ref,),
                        native_kinds=(kind,),
                        cursor=cursor,
                    )
                ),
            )
            events.extend(page.items)
            if page.cursor is None:
                return tuple(events)
            cursor = page.cursor

    async def _fetch_target_events(
        self,
        refs: Sequence[Ref],
        kind: str,
    ) -> tuple[Event, ...]:
        """Fetch target events when the target can report existing activity."""
        if not refs or not isinstance(self.target_provider, SupportsReads):
            return ()
        deduped = tuple({ref_to_key(ref): ref for ref in refs}.values())
        events: list[Event] = []
        cursor: str | None = None
        while True:
            page = cast(
                Page[Event],
                await self.target_provider.fetch(
                    EventQuery(
                        refs=deduped,
                        native_kinds=(kind,),
                        cursor=cursor,
                    )
                ),
            )
            events.extend(page.items)
            if page.cursor is None:
                return tuple(events)
            cursor = page.cursor

    def _target_event_refs(self, source_ref: Ref, work: _TargetWork) -> tuple[Ref, ...]:
        """Project a source event ref onto target event refs."""
        if not source_ref.path:
            return (work.target_ref,)

        path = self._relative_event_path(source_ref, work.projected_record.ref)
        if not path:
            return (work.target_ref,)
        target_path = (*work.target_ref.path, *path)
        if not work.mappings:
            return (Ref(work.target_ref.key, target_path),)

        source_index = self._path_tail_int(source_ref, work.projected_record.ref)
        if source_index is None:
            return ()
        target_indices = MappingProjector(
            self._raw_mappings(work.mappings)
        ).target_indices(source_index)
        if not target_indices:
            log.debug(
                "[%s] Skipping activity event for %s because no target path "
                "mapping exists",
                self.profile_name,
                self._node_ref_key(self.source_provider.NAMESPACE, source_ref),
            )
            return ()

        prefix = target_path[:-1]
        tail = target_path[-1]
        return tuple(
            Ref(work.target_ref.key, (*prefix, Step(tail.axis, index)))
            for index in target_indices
        )

    def _source_event_in_scope(self, event: Event, work: _TargetWork) -> bool:
        """Return whether an event belongs to this mapped work item."""
        if not work.mappings or not event.ref.path:
            return True
        if not self._relative_event_path(event.ref, work.projected_record.ref):
            return True
        source_index = self._path_tail_int(event.ref, work.projected_record.ref)
        return source_index is not None and any(
            mapping.mapping.source_range.contains(source_index)
            for mapping in work.mappings
        )

    @staticmethod
    def _event_dedupe_key(
        event: Event,
        target_ref: Ref,
        *,
        scoped: bool,
    ) -> str | None:
        """Return a stable dedupe key for projected event writes."""
        if event.dedupe_key is None:
            return None
        if not scoped:
            return event.dedupe_key
        target_key = ref_to_key(target_ref)
        path = "/".join(f"{step.axis}={step.value}" for step in target_key.path)
        suffix = f"{target_key.key}/{path}" if path else target_key.key
        return f"{event.dedupe_key}@{suffix}"

    def _event_sync_pairs(self) -> tuple[tuple[str, str], ...]:
        """Return source event channel to target event channel mappings."""
        target_can_read_events = isinstance(self.target_provider, SupportsReads)
        target_events = tuple(
            spec
            for spec in _event_specs(self._target_capabilities)
            if spec.kind.semantic is not None
            and (
                (
                    WriteAction.UPSERT in spec.write_actions
                    and (target_can_read_events or spec.idempotent_upserts)
                )
                or (WriteAction.DELETE in spec.write_actions and target_can_read_events)
            )
        )
        return tuple(
            dict.fromkeys(
                (source.kind.native, target.kind.native)
                for source in _event_specs(self._source_capabilities)
                if source.kind.semantic is not None
                for target in target_events
                if source.kind.semantic == target.kind.semantic
            )
        )

    def _can_append_event(self, target_kind: str) -> bool:
        """Return whether a target event channel supports appending events."""
        spec = self._target_event_spec(target_kind)
        return spec is not None and WriteAction.UPSERT in spec.write_actions

    def _can_delete_event(self, target_kind: str) -> bool:
        """Return whether a target event channel supports deleting events."""
        spec = self._target_event_spec(target_kind)
        return spec is not None and WriteAction.DELETE in spec.write_actions

    def _target_event_spec(self, target_kind: str) -> EventSpec | None:
        """Return a target event channel spec by native kind."""
        return self._target_event_specs.get(target_kind)

    @staticmethod
    def _event_identity(
        ref: Ref,
        kind: str,
        at: datetime,
        dedupe_key: str | None,
    ) -> EventIdentity:
        """Return the comparison identity used for event append/delete planning."""
        if dedupe_key is not None:
            return (ref_to_key(ref), kind, "dedupe", dedupe_key)
        return (ref_to_key(ref), kind, "at", at)

    @staticmethod
    def _delete_event_for(event: Event) -> Delete:
        """Build a delete write for an existing target event."""
        return Delete(
            resource=ResourceKind.EVENT,
            ref=event.ref,
            name=event.kind,
            at=event.at,
            key=event.key,
            dedupe_key=event.dedupe_key,
        )

    @staticmethod
    def _relative_event_path(ref: Ref, root_ref: Ref) -> tuple[Step, ...]:
        """Return the event path relative to the source record ref when possible."""
        if ref.path[: len(root_ref.path)] == root_ref.path:
            return ref.path[len(root_ref.path) :]
        return ref.path

    @classmethod
    def _path_tail_int(cls, ref: Ref, root_ref: Ref) -> int | None:
        """Return the final integer coordinate from an event path."""
        path = cls._relative_event_path(ref, root_ref)
        if not path:
            return None
        try:
            return int(path[-1].value)
        except TypeError, ValueError:
            return None

    async def _write(
        self,
        writes: Sequence[Write],
        *,
        resource_label: str,
    ) -> Sequence[WriteResult]:
        """Write resources and raise when any write fails."""
        results = await self._write_batch(writes, resource_label=resource_label)
        for result in results:
            if not result.ok:
                raise self._write_result_exception(result, resource_label)
        return results

    async def _write_batch(
        self,
        writes: Sequence[Write],
        *,
        resource_label: str,
    ) -> Sequence[WriteResult]:
        """Write resources and return positional provider results."""
        if not writes:
            return ()
        if not isinstance(self.target_provider, SupportsWrites):
            raise TypeError(
                f"Target provider '{self.target_provider.NAMESPACE}' must support "
                f"{resource_label} writes"
            )
        results = await self.target_provider.write(writes)
        if len(results) != len(writes):
            raise ValueError(
                f"Target provider '{self.target_provider.NAMESPACE}' returned "
                f"{len(results)} write results for {len(writes)} writes"
            )
        return results

    @staticmethod
    def _write_result_exception(
        result: WriteResult,
        resource_label: str = "write",
    ) -> RuntimeError:
        """Build an exception from a failed write result."""
        return RuntimeError(
            result.error or result.code or f"{resource_label} write failed"
        )

    async def _fetch_target_record(self, target_ref, target_kind: str) -> Record | None:
        """Fetch the existing target record for planning."""
        if not isinstance(self.target_provider, SupportsReads):
            return None
        fields = self._target_fields_for(target_kind)
        page = cast(
            Page[Record],
            await self.target_provider.fetch(
                RecordQuery(
                    refs=(target_ref,),
                    native_kinds=(target_kind,),
                    fields=fields,
                    limit=1,
                )
            ),
        )
        return page.items[0] if page.items else None

    async def _fetch_target_records_batch(
        self,
        requests: Iterable[tuple[Ref, str]],
    ) -> dict[TargetRecordKey, Record]:
        """Fetch existing target records grouped by record surface."""
        if not isinstance(self.target_provider, SupportsReads):
            return {}

        grouped: dict[str, dict[RefKey, Ref]] = {}
        for target_ref, target_kind in requests:
            grouped.setdefault(target_kind, {}).setdefault(
                ref_to_key(target_ref),
                target_ref,
            )

        records: dict[TargetRecordKey, Record] = {}
        for target_kind, refs_by_key in grouped.items():
            refs = tuple(refs_by_key.values())
            if not refs:
                continue
            fields = self._target_fields_for(target_kind)
            page = cast(
                Page[Record],
                await self.target_provider.fetch(
                    RecordQuery(
                        refs=refs,
                        native_kinds=(target_kind,),
                        fields=fields,
                        limit=len(refs),
                    )
                ),
            )
            for record in page.items:
                record_key = ref_to_key(record.ref)
                records[(record_key, record.surface)] = record
                records.setdefault((record_key, target_kind), record)
        return records

    def _target_fields_for(self, target_kind: str) -> frozenset[RecordField]:
        """Return the source fields that can be synced into one target surface."""
        fields: set[RecordField] = set()
        for source_kind in self._planner.source_record_kinds():
            if self._planner.target_record_surface_for(source_kind) == target_kind:
                fields.update(self._planner.sync_fields_for(source_kind, target_kind))
        return frozenset(fields)

    @staticmethod
    def _target_parent_key(ref: Ref) -> RefKey:
        """Return the target parent key covered by a pin."""
        return RefKey(key=ref.key)

    def _fetch_pinned_target_parents(
        self,
        refs: Iterable[Ref],
    ) -> frozenset[RefKey]:
        """Fetch pinned target parent refs in one page-level query."""
        wanted = tuple({self._target_parent_key(ref) for ref in refs})
        if not wanted:
            return frozenset()

        ref_json = [ref_to_json(Ref.anchor(ref.key)) for ref in wanted]

        with db() as ctx:
            pins = (
                ctx.session.query(Pin)
                .filter(
                    Pin.profile_name == self.profile_name,
                    Pin.target_namespace == self.target_provider.NAMESPACE,
                    Pin.target_parent_ref.in_(ref_json),
                )
                .all()
            )

        pinned: set[RefKey] = set()
        for pin in pins:
            pin_ref = ref_from_payload(pin.target_parent_ref)
            if pin_ref is None:
                continue
            pinned.add(self._target_parent_key(pin_ref))
        return frozenset(pinned)

    def _sync_label(
        self,
        *,
        item: ScanItem,
        target_ref: Ref | None = None,
        source_descriptor: ExternalId | None = None,
        target_descriptor: ExternalId | None = None,
        mappings: Sequence[AnibridgeMapping] = (),
    ) -> SyncLabel:
        """Return formatted source/target context for sync logs."""
        source = self._node_log_label(
            namespace=self.source_provider.NAMESPACE,
            ref=item.node.ref,
            title=item.node.title,
            descriptor=source_descriptor,
            mappings=mappings,
            side="source",
        )
        target = (
            self._node_log_label(
                namespace=self.target_provider.NAMESPACE,
                ref=target_ref,
                title=None,
                descriptor=target_descriptor,
                mappings=mappings,
                side="target",
            )
            if target_ref is not None
            else None
        )
        return SyncLabel(node_kind=item.node.kind, source=source, target=target)

    @staticmethod
    def _mappings_for_match(match: ResolvedTarget) -> tuple[MappingRange, ...]:
        """Return descriptor-qualified mappings for history diagnostics."""
        if match.source_id is None or match.target_id is None:
            return ()
        return tuple(
            MappingRange(
                source_mapping_descriptor=match.source_id.descriptor,
                target_mapping_descriptor=match.target_id.descriptor,
                mapping=mapping,
            )
            for mapping in match.mappings
        )

    @staticmethod
    def _raw_mappings(mappings: Sequence[MappingRange]) -> tuple[AnibridgeMapping, ...]:
        """Return raw mapping values from descriptor-qualified mappings."""
        return tuple(item.mapping for item in mappings)

    @staticmethod
    def _source_with_target(context: SyncLabel) -> str:
        """Return source label plus target phrase when a target exists."""
        if context.target is None:
            return context.source
        return f"{context.source} with target {context.target}"

    @staticmethod
    def _target_suffix(context: SyncLabel) -> str:
        """Return target phrase suffix when a target exists."""
        if context.target is None:
            return ""
        return f" with target {context.target}"

    def _node_log_label(
        self,
        *,
        namespace: str,
        ref: Ref,
        title: str | None,
        descriptor: ExternalId | None,
        mappings: Sequence[AnibridgeMapping],
        side: str,
    ) -> str:
        """Return a title/ref/mapping label for sync logs."""
        ref_key = self._node_ref_key(namespace, ref)
        mapping = self._mapping_log_label(
            descriptor=descriptor,
            mappings=mappings,
            side=side,
        )
        if title:
            return f"$$'{title} ({ref_key})'$${mapping}"
        return f"$$'({ref_key})'$${mapping}"

    @staticmethod
    def _mapping_log_label(
        *,
        descriptor: ExternalId | None,
        mappings: Sequence[AnibridgeMapping],
        side: str,
    ) -> str:
        """Return optional mapping descriptor/range context for sync logs."""
        if descriptor is None or not mappings:
            return ""
        ranges = (
            mapping.source_key if side == "source" else mapping.target_value
            for mapping in mappings
        )
        descriptors = ", ".join(
            f"{descriptor.descriptor}/{range_value}" for range_value in ranges
        )
        return f" $${{{descriptors}}}$$"

    @staticmethod
    def _node_ref_key(namespace: str, ref: Ref) -> str:
        """Return a provider ref key for sync logs."""
        del namespace
        if not ref.path:
            return ref.key
        path = "/".join(f"{step.axis}={step.value}" for step in ref.path)
        return f"{ref.key}/{path}"
