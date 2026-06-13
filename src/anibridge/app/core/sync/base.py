"""Synchronization engine for the AniBridge provider contract."""

import asyncio
import contextlib
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from typing import cast

import msgspec
from anibridge.provider.base import (
    DeleteRecord,
    ExternalId,
    FacetName,
    NodeFlag,
    Page,
    Provider,
    Record,
    RecordField,
    RecordQuery,
    RecordWrite,
    Ref,
    ScanItem,
    ScanQuery,
    SupportsRecordReads,
    SupportsRecordWrites,
    SupportsScan,
    WriteOp,
    WriteResult,
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
from anibridge.app.core.sync.rules import SyncRuleEngine
from anibridge.app.core.sync.stats import (
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


class _TargetWork(msgspec.Struct, frozen=True):
    """One source record resolved to one target record location."""

    item: ScanItem
    sync_items: tuple[SyncItem, ...]
    projected_record: Record
    target_ref: Ref
    target_kind: str
    mappings: Sequence[AnibridgeMapping]
    label: SyncLabel

    @property
    def key(self) -> _TargetRecordKey:
        return _TargetRecordKey(ref_to_key(self.target_ref), self.target_kind)


class _TargetRecordKey(msgspec.Struct, frozen=True):
    ref: RefKey
    kind: str


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
        native_record_kinds = frozenset(self._planner.source_record_kinds())
        source_provider = cast(SupportsScan, self.source_provider)

        async def fetch_page(cursor: str | None):
            return await source_provider.scan(
                ScanQuery(
                    sources=source_refs,
                    flags=frozenset({NodeFlag.TRACKABLE}),
                    facets=facets,
                    native_record_kinds=native_record_kinds,
                    fields=frozenset(self._sync_fields),
                    with_records=True,
                    require_activity=scan.require_activity,
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

    async def process_item(self, item: ScanItem) -> None:
        """Process one scanned source item through target resolution and sync."""
        await self.process_page((item,))

    async def process_page(self, items: Sequence[ScanItem]) -> None:
        """Process one source scan page with batched target reads and writes."""
        if not items:
            return

        outcomes, work_items = await self._resolve_work_items(items)
        target_keys = [work.key for work in work_items]
        target_key_counts = Counter(target_keys)
        target_records = await self._fetch_target_records_batch(
            (work.target_ref, work.target_kind)
            for work, target_key in zip(work_items, target_keys, strict=True)
            if target_key_counts[target_key] == 1
        )
        pinned_fields = self._fetch_pinned_fields_batch(
            (work.target_ref, work.target_kind) for work in work_items
        )

        updates: list[tuple[_TargetWork, PreparedUpdate]] = []
        for work, target_key in zip(work_items, target_keys, strict=True):
            try:
                if target_key_counts[target_key] > 1:
                    target_record = await self._fetch_target_record(
                        work.target_ref,
                        work.target_kind,
                    )
                    outcome = await self.sync_record(
                        item=work.item,
                        source_record=work.projected_record,
                        target_record=target_record,
                        target_ref=work.target_ref,
                        target_kind=work.target_kind,
                        pinned_fields=pinned_fields.get(target_key, ()),
                        mappings=work.mappings,
                        label=work.label,
                    )
                    self._record_best_outcome(
                        outcomes,
                        work.sync_items,
                        outcome,
                    )
                    continue

                target_record = target_records.get(work.key)
                planned = await self._prepare_record_update(
                    item=work.item,
                    source_record=work.projected_record,
                    target_record=target_record,
                    target_ref=work.target_ref,
                    target_kind=work.target_kind,
                    pinned_fields=pinned_fields.get(target_key, ()),
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

        if updates:
            batch_results = await self._apply_updates_batch(
                [planned for _, planned in updates]
            )
            for (work, _), outcome in zip(
                updates,
                batch_results,
                strict=True,
            ):
                self._record_best_outcome(
                    outcomes,
                    work.sync_items,
                    outcome,
                )

        for sync_item, outcome in outcomes.items():
            self.sync_stats.track_item(sync_item, outcome)

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
                        info={"trackable_count": len(sync_items)},
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
        target_kind = self._planner.target_record_kind_for(source_record.kind)
        if target_kind is None:
            log.debug(
                "[%s] Skipping record kind $$'%s'$$ for %s %s because target "
                "provider has no matching record kind",
                self.profile_name,
                source_record.kind,
                label.node_kind,
                label.source,
            )
            return ()

        work_items: list[_TargetWork] = []
        for match in matches:
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
                    target_kind=target_kind,
                    mappings=match.mappings,
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

    async def sync_record(
        self,
        *,
        item: ScanItem,
        source_record: Record,
        target_record: Record | None,
        target_ref: Ref,
        target_kind: str,
        pinned_fields: Sequence[RecordField] = (),
        mappings: Sequence[AnibridgeMapping] = (),
        label: SyncLabel | None = None,
    ) -> SyncOutcome:
        """Synchronize one source record to one target record."""
        label = label or self._sync_label(
            item=item,
            target_ref=target_ref,
            mappings=mappings,
        )
        planned = await self._prepare_record_update(
            item=item,
            source_record=source_record,
            target_record=target_record,
            target_ref=target_ref,
            target_kind=target_kind,
            label=label,
            pinned_fields=pinned_fields,
            mappings=mappings,
        )
        if isinstance(planned, SyncOutcome):
            return planned
        return await self._apply_update(
            planned.plan,
            source_record=planned.source_record,
            diff_str=planned.diff_str,
            label=planned.label,
        )

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
        mappings: Sequence[AnibridgeMapping] = (),
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
                        "write_reconciled_after_error": True,
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

    async def _apply_updates_batch(
        self,
        updates: Sequence[PreparedUpdate],
    ) -> tuple[SyncOutcome, ...]:
        """Apply planned updates independently to avoid ambiguous partial batches."""
        if not updates:
            return ()

        if self.dry_run:
            outcomes: list[SyncOutcome] = []
            for update in updates:
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
                    ephemeral=self.dry_run,
                )
                outcomes.append(SyncOutcome.SYNCED)
            return tuple(outcomes)

        try:
            outcomes: list[SyncOutcome] = []
            for update in updates:
                try:
                    outcomes.append(
                        await self._apply_update(
                            update.plan,
                            source_record=update.source_record,
                            diff_str=update.diff_str,
                            label=update.label,
                        )
                    )
                except Exception:
                    outcomes.append(SyncOutcome.FAILED)
            return tuple(outcomes)
        except Exception:
            log.exception("[%s] Unexpected sequential write failure", self.profile_name)
            return tuple(SyncOutcome.FAILED for _ in updates)

    async def _target_matches_after(self, plan: RecordPlan) -> bool:
        """Return whether the current target state matches a planned upsert."""
        if plan.target_ref is None or plan.after is None:
            return False
        if not isinstance(self.target_provider, SupportsRecordReads):
            return False
        try:
            actual = await self._fetch_target_record(
                plan.target_ref,
                plan.after.kind,
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
        if WriteOp.DELETE_RECORD not in self._target_capabilities.write_ops:
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
            kind=target_kind,
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

    async def _write_records(self, writes: Sequence[RecordWrite]):
        """Write records and raise when any write fails."""
        results = await self._submit_record_writes(writes)
        for result in results:
            if not result.ok:
                raise RuntimeError(result.error or result.code or "record write failed")
        return results

    async def _submit_record_writes(
        self,
        writes: Sequence[RecordWrite],
    ) -> Sequence[WriteResult]:
        """Write records and validate only positional result shape."""
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
        return results

    async def _fetch_target_record(self, target_ref, target_kind: str) -> Record | None:
        """Fetch the existing target record for planning."""
        if not isinstance(self.target_provider, SupportsRecordReads):
            return None
        page = await self.target_provider.fetch_records(
            RecordQuery(
                refs=(target_ref,),
                native_record_kinds=(target_kind,),
                fields=frozenset(self._sync_fields),
                limit=1,
            )
        )
        return page.items[0] if page.items else None

    async def _fetch_target_records_batch(
        self,
        requests: Iterable[tuple[Ref, str]],
    ) -> dict[_TargetRecordKey, Record]:
        """Fetch existing target records grouped by native record kind."""
        if not isinstance(self.target_provider, SupportsRecordReads):
            return {}

        grouped: dict[str, dict[RefKey, Ref]] = {}
        for target_ref, target_kind in requests:
            grouped.setdefault(target_kind, {}).setdefault(
                ref_to_key(target_ref),
                target_ref,
            )

        records: dict[_TargetRecordKey, Record] = {}
        for target_kind, refs_by_key in grouped.items():
            refs = tuple(refs_by_key.values())
            if not refs:
                continue
            page = await self.target_provider.fetch_records(
                RecordQuery(
                    refs=refs,
                    native_record_kinds=(target_kind,),
                    fields=frozenset(self._sync_fields),
                    limit=len(refs),
                )
            )
            for record in page.items:
                record_key = ref_to_key(record.ref)
                records[_TargetRecordKey(record_key, record.kind)] = record
                records.setdefault(_TargetRecordKey(record_key, target_kind), record)
        return records

    def _fetch_pinned_fields_batch(
        self,
        requests: Iterable[tuple[Ref, str]],
    ) -> dict[_TargetRecordKey, list[RecordField]]:
        """Fetch pinned fields for target records in one page-level query."""
        wanted = [
            _TargetRecordKey(ref_to_key(target_ref), record_kind)
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

        scored_fields: dict[_TargetRecordKey, tuple[int, list[RecordField]]] = {}
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
                if pin_ref_key == target_key.ref:
                    ref_score = 2
                elif pin_ref_key.covers(target_key.ref):
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
