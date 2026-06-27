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
    AppendEvent,
    Capabilities,
    DeleteRecord,
    Event,
    EventQuery,
    EventWrite,
    ExternalId,
    FacetName,
    NodeFlag,
    Page,
    Progress,
    Provider,
    Record,
    RecordField,
    RecordQuery,
    RecordWrite,
    Ref,
    Role,
    ScanItem,
    ScanQuery,
    Step,
    SupportsEventReads,
    SupportsEventWrites,
    SupportsRecordReads,
    SupportsRecordWrites,
    SupportsScan,
    WriteOp,
)
from anibridge.utils.mappings import AnibridgeMapping

from anibridge.app.config.database import db
from anibridge.app.config.settings import SyncRulesConfig
from anibridge.app.core.animap import AnimapClient
from anibridge.app.core.sync import RefKey, ScanPlan, ref_from_payload, ref_to_key
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
from anibridge.app.models.db.sync_history import SyncOutcome

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

_STATUS_ORDER: Mapping[object, int] = {
    None: 0,
    "planned": 1,
    "dropped": 2,
    "paused": 3,
    "active": 4,
    "completed": 5,
    "repeating": 6,
}


class _TargetWork(msgspec.Struct, frozen=True):
    """One source record resolved to one target record location."""

    item: ScanItem
    sync_items: tuple[SyncItem, ...]
    projected_record: Record
    target_ref: Ref
    target_surface: str
    mappings: Sequence[MappingRange]
    label: SyncLabel

    @property
    def key(self) -> TargetRecordKey:
        return (ref_to_key(self.target_ref), self.target_surface)


type TargetRecordKey = tuple[RefKey, str]


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
        self._validate_events(
            provider=self.source_provider, capabilities=self._source_capabilities
        )
        self._validate_events(
            provider=self.target_provider, capabilities=self._target_capabilities
        )
        sync_rule_engine = SyncRuleEngine(
            variables=sync_rules.resolved_vars() if sync_rules else None,
            field_rules=sync_rules.field_rules() if sync_rules else None,
        )
        self._planner = RecordPlanner(
            source_capabilities=self._source_capabilities,
            target_capabilities=self._target_capabilities,
            sync_rule_engine=sync_rule_engine,
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
        if not capabilities.events:
            return

        if Role.SOURCE in capabilities.roles and not isinstance(
            provider,
            SupportsEventReads,
        ):
            raise TypeError(
                f"Provider {provider.NAMESPACE!r} advertises source event channels "
                "but does not implement event reads"
            )

        writable_events = tuple(
            spec
            for spec in capabilities.events
            if WriteOp.APPEND_EVENT in spec.write_ops
        )
        if writable_events and not isinstance(provider, SupportsEventWrites):
            raise TypeError(
                f"Provider {provider.NAMESPACE!r} advertises appendable event "
                "channels but does not implement event writes"
            )

        readable_events = tuple(
            spec
            for spec in capabilities.events
            if WriteOp.APPEND_EVENT not in spec.write_ops
        )
        if readable_events and not isinstance(provider, SupportsEventReads):
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
        record_surfaces = frozenset(self._planner.source_record_surfaces())
        source_provider = cast(SupportsScan, self.source_provider)

        async def fetch_page(cursor: str | None):
            return await source_provider.scan(
                ScanQuery(
                    sources=source_refs,
                    flags=frozenset({NodeFlag.TRACKABLE}),
                    facets=facets,
                    record_surfaces=record_surfaces,
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

    async def scan_source(
        self,
        *,
        scan: ScanPlan,
    ) -> tuple[ScanItem, ...]:
        """Collect all source items for explicit batch-mode processing."""
        items: list[ScanItem] = []
        async for page in self.scan_source_pages(scan=scan):
            items.extend(page.items)
        return tuple(items)

    async def process_page(self, items: Sequence[ScanItem]) -> None:
        """Process one source scan page with batched target reads and writes."""
        if not items:
            return

        outcomes, work_items = await self._resolve_work_items(items)
        record_work_items = self._merge_record_work_items(work_items)
        target_key_counts = Counter(work.key for work in record_work_items)
        target_records = await self._fetch_target_records_batch(
            (work.target_ref, work.target_surface)
            for work in record_work_items
            if target_key_counts[work.key] == 1
        )
        pinned_fields = self._fetch_pinned_fields_batch(
            (work.target_ref, work.target_surface) for work in record_work_items
        )

        updates: list[tuple[_TargetWork, PreparedUpdate]] = []
        for work in record_work_items:
            try:
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
                    pinned_fields=pinned_fields.get(work.key, ()),
                    mappings=work.mappings,
                    label=work.label,
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

        for work, update in updates:
            try:
                outcome = await self._apply_update(
                    update.plan,
                    source_record=update.source_record,
                    diff_str=update.diff_str,
                    label=update.label,
                )
            except Exception:
                outcome = SyncOutcome.FAILED
            self._record_best_outcome(outcomes, work.sync_items, outcome)

        for work in work_items:
            try:
                outcome = await self._sync_events_for_work(work)
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
                        if existing is None or _STATUS_ORDER.get(
                            status.value if status is not None else None,
                            0,
                        ) > _STATUS_ORDER.get(
                            (
                                existing_status.value
                                if existing_status is not None
                                else None
                            ),
                            0,
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
        pinned_fields: Sequence[RecordField] = (),
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
            pinned_fields=pinned_fields,
            label=label,
            mappings=mappings,
        )
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

    async def _apply_update(
        self,
        plan: RecordPlan,
        *,
        source_record: Record,
        diff_str: str,
        label: SyncLabel,
    ) -> SyncOutcome:
        """Queue or apply a record update."""
        if self.dry_run:
            log.success(
                "[%s] Dry run; skipping sync of %s %s",
                self.profile_name,
                label.node_kind,
                self._source_with_target(label),
            )
            log.success("\tDRY RUN UPDATE: %s", diff_str)
            await self._history.create_sync_history(
                source_node=plan.item.node,
                source_record=source_record,
                target_ref=plan.target_ref,
                snapshots=(plan.before, plan.after),
                outcome=SyncOutcome.SYNCED,
                info=plan.diagnostics.as_info(),
                ephemeral=self.dry_run,
            )
            return SyncOutcome.SYNCED

        try:
            await self._write_records([plan.write])
            log.success(
                "[%s] Synced %s %s",
                self.profile_name,
                label.node_kind,
                self._source_with_target(label),
            )
            log.success("\tUPDATE: %s", diff_str)
            await self._history.create_sync_history(
                source_node=plan.item.node,
                source_record=source_record,
                target_ref=plan.target_ref,
                snapshots=(plan.before, plan.after),
                outcome=SyncOutcome.SYNCED,
                info=plan.diagnostics.as_info(),
                ephemeral=self.dry_run,
            )
            return SyncOutcome.SYNCED
        except Exception as exc:
            if await self._target_matches_after(plan):
                log.warning(
                    "[%s] Provider raised after writing %s %s; target "
                    "state matches the planned update, so marking it synced: %s",
                    self.profile_name,
                    label.node_kind,
                    self._source_with_target(label),
                    exc,
                )
                await self._history.create_sync_history(
                    source_node=plan.item.node,
                    source_record=source_record,
                    target_ref=plan.target_ref,
                    snapshots=(plan.before, plan.after),
                    outcome=SyncOutcome.SYNCED,
                    info={
                        **plan.diagnostics.as_info(),
                        "write_reconciled_after_error": "true",
                        "write_error_type": type(exc).__name__,
                        "write_error": str(exc),
                    },
                    ephemeral=self.dry_run,
                )
                return SyncOutcome.SYNCED

            log.error(
                "[%s] Failed to sync %s %s: %s",
                self.profile_name,
                label.node_kind,
                self._source_with_target(label),
                exc,
            )
            log.exception("[%s] Sync update error details", self.profile_name)
            await self._history.create_sync_history(
                source_node=plan.item.node,
                source_record=source_record,
                target_ref=plan.target_ref,
                snapshots=(plan.before, plan.after),
                outcome=SyncOutcome.FAILED,
                error_message=str(exc),
                info={
                    **plan.diagnostics.as_info(),
                    "error_type": type(exc).__name__,
                },
                ephemeral=self.dry_run,
            )
            raise

    async def _target_matches_after(self, plan: RecordPlan) -> bool:
        """Return whether the current target state matches a planned upsert."""
        if plan.target_ref is None or plan.after is None:
            return False
        if not isinstance(self.target_provider, SupportsRecordReads):
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

        write = DeleteRecord(
            ref=target_ref,
            surface=target_kind,
            key=target_record.key if target_record else None,
        )
        await self._write_records([write])
        await self._history.create_sync_history(
            source_node=item.node,
            source_record=source_record,
            target_ref=target_ref,
            snapshots=(before_snapshot, None),
            outcome=SyncOutcome.DELETED,
            ephemeral=self.dry_run,
        )
        return SyncOutcome.DELETED

    async def _sync_events_for_work(self, work: _TargetWork) -> SyncOutcome:
        """Append missing source events to the resolved target ref."""
        if not self._event_pairs:
            return SyncOutcome.SKIPPED
        if not isinstance(self.source_provider, SupportsEventReads):
            return SyncOutcome.SKIPPED
        if not isinstance(self.target_provider, SupportsEventWrites):
            return SyncOutcome.SKIPPED

        writes: list[AppendEvent] = []
        for source_kind, target_kind in self._event_pairs:
            source_events = await self._fetch_source_events(
                work.projected_record.ref,
                source_kind,
            )
            if not source_events:
                continue
            projected_events = tuple(
                (event, target_ref)
                for event in source_events
                if self._source_event_in_scope(event, work)
                for target_ref in self._target_event_refs(event.ref, work)
            )
            if not projected_events:
                continue
            target_events = await self._fetch_target_events(
                tuple(target_ref for _event, target_ref in projected_events),
                target_kind,
            )
            existing_dedupe_keys = {
                (ref_to_key(event.ref), event.kind, event.dedupe_key)
                for event in target_events
                if event.dedupe_key is not None
            }
            existing_unkeyed_times = {
                (ref_to_key(event.ref), event.kind, event.at)
                for event in target_events
                if event.dedupe_key is None
            }
            for event, target_ref in projected_events:
                dedupe_key = self._event_dedupe_key(
                    event,
                    target_ref,
                    scoped=len(projected_events) > 1,
                )
                target_key = ref_to_key(target_ref)
                if dedupe_key is not None:
                    if (target_key, target_kind, dedupe_key) in existing_dedupe_keys:
                        continue
                elif (target_key, target_kind, event.at) in existing_unkeyed_times:
                    continue
                writes.append(
                    AppendEvent(
                        ref=target_ref,
                        kind=target_kind,
                        at=event.at,
                        dedupe_key=dedupe_key,
                        metadata=event.metadata,
                    )
                )

        if not writes:
            return SyncOutcome.SKIPPED
        if self.dry_run:
            log.success(
                "[%s] Dry run; skipping sync of %s activity events for %s %s",
                self.profile_name,
                len(writes),
                work.label.node_kind,
                self._source_with_target(work.label),
            )
            return SyncOutcome.SYNCED

        await self._write_events(writes)
        log.success(
            "[%s] Synced %s activity events for %s %s",
            self.profile_name,
            len(writes),
            work.label.node_kind,
            self._source_with_target(work.label),
        )
        return SyncOutcome.SYNCED

    async def _fetch_source_events(self, ref: Ref, kind: str) -> tuple[Event, ...]:
        """Fetch source events for one source ref and event channel."""
        if not isinstance(self.source_provider, SupportsEventReads):
            return ()
        events: list[Event] = []
        cursor: str | None = None
        while True:
            page = await self.source_provider.fetch_events(
                EventQuery(
                    refs=(ref,),
                    native_event_kinds=(kind,),
                    cursor=cursor,
                )
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
        if not refs or not isinstance(self.target_provider, SupportsEventReads):
            return ()
        deduped = tuple({ref_to_key(ref): ref for ref in refs}.values())
        events: list[Event] = []
        cursor: str | None = None
        while True:
            page = await self.target_provider.fetch_events(
                EventQuery(
                    refs=deduped,
                    native_event_kinds=(kind,),
                    cursor=cursor,
                )
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
        target_events = tuple(
            spec
            for spec in self._target_capabilities.events
            if spec.kind.semantic is not None
            and WriteOp.APPEND_EVENT in spec.write_ops
            and (
                isinstance(self.target_provider, SupportsEventReads)
                or spec.idempotent_appends
            )
        )
        return tuple(
            dict.fromkeys(
                (source.kind.native, target.kind.native)
                for source in self._source_capabilities.events
                if source.kind.semantic is not None
                for target in target_events
                if source.kind.semantic == target.kind.semantic
            )
        )

    async def _write_events(self, writes: Sequence[EventWrite]):
        """Write events and raise when any write fails."""
        if not isinstance(self.target_provider, SupportsEventWrites):
            raise TypeError(
                f"Target provider '{self.target_provider.NAMESPACE}' must support "
                "event writes"
            )
        results = await self.target_provider.write_events(writes)
        if len(results) != len(writes):
            raise ValueError(
                f"Target provider '{self.target_provider.NAMESPACE}' returned "
                f"{len(results)} write results for {len(writes)} writes"
            )
        for result in results:
            if not result.ok:
                raise RuntimeError(result.error or result.code or "event write failed")
        return results

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

    async def _write_records(self, writes: Sequence[RecordWrite]):
        """Write records and raise when any write fails."""
        if not isinstance(self.target_provider, SupportsRecordWrites):
            raise TypeError(
                f"Target provider '{self.target_provider.NAMESPACE}' must support "
                "record writes"
            )
        results = await self.target_provider.write_records(writes)
        if len(results) != len(writes):
            raise ValueError(
                f"Target provider '{self.target_provider.NAMESPACE}' returned "
                f"{len(results)} write results for {len(writes)} writes"
            )
        for result in results:
            if not result.ok:
                raise RuntimeError(result.error or result.code or "record write failed")
        return results

    async def _fetch_target_record(self, target_ref, target_kind: str) -> Record | None:
        """Fetch the existing target record for planning."""
        if not isinstance(self.target_provider, SupportsRecordReads):
            return None
        fields = self._target_fields_for(target_kind)
        page = await self.target_provider.fetch_records(
            RecordQuery(
                refs=(target_ref,),
                record_surfaces=(target_kind,),
                fields=fields,
                limit=1,
            )
        )
        return page.items[0] if page.items else None

    async def _fetch_target_records_batch(
        self,
        requests: Iterable[tuple[Ref, str]],
    ) -> dict[TargetRecordKey, Record]:
        """Fetch existing target records grouped by record surface."""
        if not isinstance(self.target_provider, SupportsRecordReads):
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
            page = await self.target_provider.fetch_records(
                RecordQuery(
                    refs=refs,
                    record_surfaces=(target_kind,),
                    fields=fields,
                    limit=len(refs),
                )
            )
            for record in page.items:
                record_key = ref_to_key(record.ref)
                records[(record_key, record.surface)] = record
                records.setdefault((record_key, target_kind), record)
        return records

    def _target_fields_for(self, target_kind: str) -> frozenset[RecordField]:
        """Return the source fields that can be synced into one target surface."""
        fields: set[RecordField] = set()
        for source_kind in self._planner.source_record_surfaces():
            if self._planner.target_record_surface_for(source_kind) == target_kind:
                fields.update(self._planner.sync_fields_for(source_kind, target_kind))
        return frozenset(fields)

    def _fetch_pinned_fields_batch(
        self,
        requests: Iterable[tuple[Ref, str]],
    ) -> dict[TargetRecordKey, list[RecordField]]:
        """Fetch pinned fields for target records in one page-level query."""
        wanted = [
            (ref_to_key(target_ref), record_kind)
            for target_ref, record_kind in requests
        ]
        if not wanted:
            return {}

        with db() as ctx:
            pins = (
                ctx.session.query(Pin)
                .filter(
                    Pin.profile_name == self.profile_name,
                    Pin.target_namespace == self.target_provider.NAMESPACE,
                )
                .all()
            )

        scored_fields: dict[TargetRecordKey, tuple[int, list[RecordField]]] = {}
        for pin in pins:
            pin_ref = ref_from_payload(pin.target_ref)
            if pin_ref is None:
                continue
            pin_ref_key = ref_to_key(pin_ref)
            pin_fields: list[RecordField] = []
            for field in pin.fields or []:
                try:
                    pin_fields.append(RecordField(field))
                except ValueError:
                    continue
            for target_key in wanted:
                target_ref_key, _target_kind = target_key
                if pin_ref_key == target_ref_key:
                    ref_score = 2
                elif pin_ref_key.covers(target_ref_key):
                    ref_score = 1
                else:
                    continue
                existing = scored_fields.get(target_key)
                if existing is None or ref_score > existing[0]:
                    scored_fields[target_key] = (ref_score, pin_fields)
        return {key: fields for key, (_, fields) in scored_fields.items()}

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
