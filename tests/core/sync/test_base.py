"""Focused tests for page-level sync batching helpers."""

from collections.abc import Mapping
from typing import cast

import pytest
from anibridge.provider.base import (
    Capabilities,
    Descriptor,
    ExternalId,
    FieldSpec,
    Match,
    Node,
    Page,
    Progress,
    ProgressConstraint,
    Provider,
    Rating,
    Record,
    RecordField,
    RecordKind,
    RecordQuery,
    RecordWrite,
    Ref,
    Role,
    ScanItem,
    ScanQuery,
    State,
    Status,
    SupportsMapping,
    SupportsRecordReads,
    SupportsRecordWrites,
    SupportsScan,
    UpsertRecord,
    Value,
    WriteOp,
    WriteResult,
)
from anibridge.utils.mappings import AnibridgeMapping

import anibridge.app.core.sync.base as base_module
from anibridge.app.config.sync_rules import SyncRulesConfig
from anibridge.app.core.animap import AnimapClient
from anibridge.app.core.sync.base import SyncClient
from anibridge.app.core.sync.history import SyncHistoryManager
from anibridge.app.core.sync.planner import (
    PreparedRecordUpdate,
    RecordPlanner,
    SyncLogContext,
)
from anibridge.app.core.sync.refs import ref_to_json
from anibridge.app.core.sync.request import SourceScan, SyncTrigger
from anibridge.app.core.sync.rules import SyncRuleEngine
from anibridge.app.core.sync.stats import PlannedWrite, RecordSnapshot
from anibridge.app.core.sync.targeting import ResolvedTarget
from anibridge.app.models.db.pin import Pin
from anibridge.app.models.db.sync_history import SyncHistory, SyncOutcome

_PROGRESS_KIND = "progress"


def _capabilities(
    *,
    role: Role,
    readable: bool,
    writable: bool,
    statuses: tuple[Status, ...] = (Status.ACTIVE, Status.COMPLETED),
    progress_constraints: tuple[ProgressConstraint, ...] = (),
) -> Capabilities:
    status_values = tuple(Descriptor(status.value, status) for status in statuses)
    return Capabilities(
        roles=frozenset({role}),
        external_authorities=frozenset({"target"}),
        record_kinds=(Descriptor(_PROGRESS_KIND, RecordKind.PROGRESS),),
        record_fields={
            RecordField.STATUS: FieldSpec(
                RecordField.STATUS,
                readable=readable,
                writable=writable,
                values=status_values,
            ),
            RecordField.PROGRESS: FieldSpec(
                RecordField.PROGRESS,
                readable=readable,
                writable=writable,
                constraints=progress_constraints,
            ),
            RecordField.RATING: FieldSpec(
                RecordField.RATING,
                readable=readable,
                writable=writable,
            ),
            RecordField.NOTES: FieldSpec(
                RecordField.NOTES,
                readable=readable,
                writable=writable,
            ),
        },
        write_ops=frozenset({WriteOp.UPSERT_RECORD}) if writable else frozenset(),
    )


class FakeScanProvider(SupportsScan):
    """Source provider double that records scan field projections."""

    NAMESPACE = "source"

    def __init__(
        self,
        statuses: tuple[Status, ...] = (Status.ACTIVE, Status.COMPLETED),
    ) -> None:
        self.queries: list[ScanQuery] = []
        self.statuses = statuses
        self.pages: list[Page[ScanItem]] = [Page(items=())]
        self.cleared = False

    def capabilities(self) -> Capabilities:
        return _capabilities(
            role=Role.SOURCE,
            readable=True,
            writable=False,
            statuses=self.statuses,
        )

    async def scan(self, query: ScanQuery) -> Page[ScanItem]:
        self.queries.append(query)
        if self.pages:
            return self.pages.pop(0)
        return Page(items=())

    async def clear_cache(self) -> None:
        self.cleared = True


class FakeTargetProvider(
    SupportsMapping,
    SupportsRecordReads,
    SupportsRecordWrites,
):
    """Target provider double that records read and write projections."""

    NAMESPACE = "target"

    def __init__(
        self,
        statuses: tuple[Status, ...] = (Status.ACTIVE, Status.COMPLETED),
    ) -> None:
        self.queries: list[RecordQuery] = []
        self.statuses = statuses
        self.records: dict[tuple[str, str], Record] = {}
        self.writes: list[RecordWrite] = []
        self.write_results: tuple[WriteResult, ...] | None = None
        self.write_error: Exception | None = None
        self.cleared = False

    def capabilities(self) -> Capabilities:
        return _capabilities(
            role=Role.TARGET,
            readable=True,
            writable=True,
            statuses=self.statuses,
        )

    async def fetch_records(self, query: RecordQuery) -> Page[Record]:
        self.queries.append(query)
        return Page(
            items=tuple(
                record
                for ref in query.refs
                for kind in query.native_record_kinds
                if (record := self.records.get((ref.key, kind))) is not None
            )
        )

    async def resolve(self, ids) -> tuple[Match, ...]:
        return tuple(
            Match(
                external_id=item,
                ref=Ref.anchor(item.value),
                confidence=1.0,
            )
            for item in ids
            if isinstance(item, ExternalId) and item.authority == "target"
        )

    async def write_records(self, writes) -> tuple[WriteResult, ...]:
        self.writes.extend(writes)
        if self.write_error is not None:
            raise self.write_error
        if self.write_results is not None:
            return self.write_results
        return tuple(WriteResult(ok=True, op=WriteOp.UPSERT_RECORD) for _ in writes)

    async def clear_cache(self) -> None:
        self.cleared = True


class FakeRecordReader(SupportsRecordReads):
    """Target provider double that records batched read queries."""

    NAMESPACE = "target"

    def __init__(self) -> None:
        self.queries: list[RecordQuery] = []

    async def fetch_records(self, query: RecordQuery) -> Page[Record]:
        self.queries.append(query)
        return Page(
            items=tuple(
                Record(ref=ref, kind="", values={RecordField.NOTES: "existing"})
                for ref in query.refs
            )
        )


class FakeWriteOnlyTarget(SupportsRecordWrites):
    """Target provider double missing mapping support."""

    NAMESPACE = "target"

    def capabilities(self) -> Capabilities:
        return _capabilities(role=Role.TARGET, readable=True, writable=True)

    async def write_records(self, writes) -> tuple[WriteResult, ...]:
        return tuple(WriteResult(ok=True, op=WriteOp.UPSERT_RECORD) for _ in writes)


class FakeHistory:
    """History manager double that records calls without touching the database."""

    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.cleanup: list[tuple[Ref, Ref]] = []
        self.flushed = False

    async def create_sync_history(self, **kwargs) -> None:
        self.created.append(kwargs)

    def queue_failure_history_cleanup(
        self,
        *,
        source_ref: Ref,
        target_ref: Ref,
    ) -> None:
        self.cleanup.append((source_ref, target_ref))

    def flush_failure_history_cleanup(self) -> None:
        self.flushed = True


def _sync_client(
    *,
    source: FakeScanProvider | None = None,
    target: FakeTargetProvider | None = None,
    destructive_sync: bool = False,
    dry_run: bool = False,
) -> SyncClient:
    client = SyncClient(
        source_provider=cast(Provider, source or FakeScanProvider()),
        target_provider=cast(Provider, target or FakeTargetProvider()),
        animap_client=cast(AnimapClient, object()),
        full_scan=False,
        destructive_sync=destructive_sync,
        dry_run=dry_run,
        profile_name="profile",
        sync_rules=SyncRulesConfig(),
    )
    client._history = cast(SyncHistoryManager, FakeHistory())
    return client


def _scan_item(
    key: str,
    *,
    records: tuple[Record, ...] | None = None,
) -> ScanItem:
    return ScanItem(
        node=Node(ref=Ref.anchor(key), title=key.title(), kind="anime"),
        records=records or (),
    )


def _record(
    key: str,
    *,
    values: Mapping[RecordField, Value] | None = None,
    kind: str = _PROGRESS_KIND,
) -> Record:
    return Record(
        ref=Ref.anchor(key),
        kind=kind,
        values=values or {},
    )


def _planned_write(
    *,
    target_ref: Ref | None = None,
    after: RecordSnapshot | None = None,
) -> tuple[PlannedWrite, Record]:
    source_record = _record(
        "source",
        values={RecordField.PROGRESS: Progress(current=1, total=12)},
    )
    write = UpsertRecord(
        ref=target_ref or Ref.anchor("target"),
        kind=_PROGRESS_KIND,
        set={RecordField.PROGRESS: Progress(current=2, total=12)},
    )
    return (
        PlannedWrite(
            item=_scan_item("source"),
            source_record=source_record,
            before=None,
            after=after
            or RecordSnapshot(
                ref=target_ref or Ref.anchor("target"),
                kind=_PROGRESS_KIND,
                values={"progress": 2},
            ),
            write=write,
            target_ref=target_ref or Ref.anchor("target"),
            diagnostics={"reason": "test"},
        ),
        source_record,
    )


def test_status_field_specs_must_declare_status_values() -> None:
    """STATUS fields must advertise the native values they can read or write."""
    with pytest.raises(ValueError, match="STATUS fields must declare"):
        FieldSpec(RecordField.STATUS, readable=True)


def test_status_field_values_are_status_only() -> None:
    """Non-status fields cannot use status value descriptors."""
    with pytest.raises(ValueError, match=r"only valid for RecordField\.STATUS"):
        FieldSpec(
            RecordField.NOTES,
            values=(Descriptor("active", Status.ACTIVE),),
        )


def test_writable_status_specs_reject_ambiguous_semantics() -> None:
    """Writable status mappings must be deterministic."""
    with pytest.raises(ValueError, match="duplicate writable STATUS semantic"):
        FieldSpec(
            RecordField.STATUS,
            writable=True,
            values=(
                Descriptor("watching", Status.ACTIVE),
                Descriptor("current", Status.ACTIVE),
            ),
        )


def test_sync_client_requires_target_mapping_support() -> None:
    """Target providers must explicitly resolve mapping authorities."""
    with pytest.raises(TypeError, match="mapping resolution"):
        SyncClient(
            source_provider=cast(Provider, FakeScanProvider()),
            target_provider=cast(Provider, FakeWriteOnlyTarget()),
            animap_client=cast(AnimapClient, object()),
            full_scan=False,
            destructive_sync=False,
            dry_run=False,
            profile_name="profile",
            sync_rules=SyncRulesConfig(),
        )


def test_record_snapshot_stores_only_history_display_values() -> None:
    """Timeline snapshots should not expose provider value-object internals."""
    snapshot = RecordSnapshot.from_record(
        Record(
            ref=Ref.anchor("a"),
            kind=_PROGRESS_KIND,
            values={
                RecordField.STATUS: State(
                    native="watching",
                    status=Status.ACTIVE,
                ),
                RecordField.PROGRESS: Progress(
                    current=3,
                    total=12,
                    unit="episode",
                ),
                RecordField.RATING: Rating(8, (0, 10, 1)),
                RecordField.NOTES: "solid",
            },
        )
    )

    assert snapshot.values == {
        "status": "active",
        "progress": 3,
        "rating": 8,
        "notes": "solid",
    }


@pytest.mark.asyncio
async def test_fetch_target_records_batch_indexes_by_requested_kind() -> None:
    """Batch reads should find records even when providers echo an empty kind."""
    reader = FakeRecordReader()
    sync_client = SyncClient.__new__(SyncClient)
    sync_client.target_provider = reader
    sync_client._sync_fields = (RecordField.NOTES,)

    records = await sync_client._fetch_target_records_batch(
        [
            (Ref.anchor("a"), "progress"),
            (Ref.anchor("b"), "progress"),
        ]
    )

    assert len(reader.queries) == 1
    assert reader.queries[0].refs == (Ref.anchor("a"), Ref.anchor("b"))
    assert records[(("a", ()), "progress")].values[RecordField.NOTES] == "existing"
    assert records[(("b", ()), "progress")].values[RecordField.NOTES] == "existing"


def test_prepare_upsert_uses_positional_item_and_pinned_fields() -> None:
    """The base-client call shape should plan updates without touching pinned fields."""
    sync_client = SyncClient(
        source_provider=cast(Provider, FakeScanProvider()),
        target_provider=cast(
            Provider,
            FakeTargetProvider(
                statuses=(Status.PLANNED, Status.ACTIVE, Status.COMPLETED),
            ),
        ),
        animap_client=cast(AnimapClient, object()),
        full_scan=False,
        destructive_sync=False,
        dry_run=False,
        profile_name="profile",
        sync_rules=SyncRulesConfig(templates=[], notes=True),
    )
    item = ScanItem(node=Node(ref=Ref.anchor("source"), kind="anime"))
    source_record = Record(
        ref=Ref.anchor("source"),
        kind=_PROGRESS_KIND,
        values={
            RecordField.STATUS: State(status=Status.ACTIVE),
            RecordField.PROGRESS: Progress(current=2, total=12),
            RecordField.NOTES: "new",
        },
    )
    target_record = Record(
        ref=Ref.anchor("target"),
        kind=_PROGRESS_KIND,
        key="entry-1",
        revision="rev-1",
        values={
            RecordField.STATUS: State(native="planned", status=Status.PLANNED),
            RecordField.PROGRESS: Progress(current=1, total=12),
            RecordField.NOTES: "old",
        },
    )

    planned = sync_client._planner.prepare_upsert(
        item,
        source_record=source_record,
        target_record=target_record,
        target_ref=Ref.anchor("target"),
        target_kind=_PROGRESS_KIND,
        pinned_fields=("notes",),
        log_context=SyncLogContext(node_kind="anime", source="Title", target="{ids}"),
    )

    assert isinstance(planned, PreparedRecordUpdate)
    write = planned.plan.write
    assert isinstance(write, UpsertRecord)
    assert write.expected_revision == "rev-1"
    assert write.set[RecordField.STATUS] == State(
        native="active",
        status=Status.ACTIVE,
    )
    assert write.set[RecordField.PROGRESS] == Progress(
        current=2,
        total=12,
    )
    assert RecordField.NOTES not in write.set
    assert "notes(pinned)" in planned.plan.diagnostics["field_blocks"]


def test_prepare_upsert_blocks_unsupported_status_values() -> None:
    """Unsupported source statuses should not abort otherwise valid field updates."""
    planner = RecordPlanner(
        source_capabilities=FakeScanProvider(
            statuses=(Status.ACTIVE, Status.COMPLETED),
        ).capabilities(),
        target_capabilities=FakeTargetProvider(
            statuses=(Status.COMPLETED,),
        ).capabilities(),
        sync_rule_engine=SyncRuleEngine(),
        destructive_sync=False,
    )
    item = ScanItem(node=Node(ref=Ref.anchor("source"), kind="anime"))

    planned = planner.prepare_upsert(
        item,
        source_record=Record(
            ref=Ref.anchor("source"),
            kind=_PROGRESS_KIND,
            values={
                RecordField.STATUS: State(status=Status.ACTIVE),
                RecordField.PROGRESS: Progress(current=3, total=12),
            },
        ),
        target_record=Record(
            ref=Ref.anchor("target"),
            kind=_PROGRESS_KIND,
            values={
                RecordField.STATUS: State(native="completed", status=Status.COMPLETED),
                RecordField.PROGRESS: Progress(current=1, total=12),
            },
        ),
        target_ref=Ref.anchor("target"),
        target_kind=_PROGRESS_KIND,
        pinned_fields=(),
        log_context=SyncLogContext(node_kind="anime", source="Title", target="{ids}"),
    )

    assert isinstance(planned, PreparedRecordUpdate)
    write = planned.plan.write
    assert isinstance(write, UpsertRecord)
    assert RecordField.STATUS not in write.set
    assert write.set[RecordField.PROGRESS] == Progress(current=3, total=12)
    assert "status(unsupported_status)" in planned.plan.diagnostics["field_blocks"]


def test_fetch_pinned_fields_batch_matches_wildcards_and_specificity(sync_db) -> None:
    """Page-level pin reads should preserve existing broad pin behavior."""
    sync_client = SyncClient.__new__(SyncClient)
    sync_client.profile_name = "profile"
    sync_client.target_provider = FakeTargetProvider()

    target_ref = Ref.anchor("show").child("episode", 1)
    with sync_db as ctx:
        ctx.session.add_all(
            [
                Pin(
                    profile_name="profile",
                    target_namespace="target",
                    target_ref=ref_to_json(Ref.anchor("show")),
                    fields=["status"],
                ),
                Pin(
                    profile_name="profile",
                    target_namespace="target",
                    target_ref=ref_to_json(target_ref),
                    fields=["progress"],
                ),
                Pin(
                    profile_name="profile",
                    target_namespace="other",
                    target_ref=ref_to_json(target_ref),
                    fields=["rating"],
                ),
            ]
        )
        ctx.session.commit()

    fields = sync_client._fetch_pinned_fields_batch(
        [
            (target_ref, "progress"),
            (Ref.anchor("show").child("episode", 2), "progress"),
        ]
    )

    assert fields[(("show", (("episode", 1),)), "progress")] == ["progress"]
    assert fields[(("show", (("episode", 2),)), "progress")] == ["status"]


def test_failure_history_cleanup_is_scoped_to_target_namespace(sqlite_db_factory):
    """Cleanup must not delete same-ref failures for another target provider."""
    manager = SyncHistoryManager(
        profile_name="profile",
        source_namespace="source",
        target_namespace="target",
        db_factory=sqlite_db_factory,
    )
    source_ref = Ref.anchor("source-1")
    target_ref = Ref.anchor("target-1")

    with sqlite_db_factory() as ctx:
        ctx.session.add_all(
            [
                SyncHistory(
                    profile_name="profile",
                    source_namespace="source",
                    source_ref=ref_to_json(source_ref),
                    target_namespace="target",
                    target_ref=ref_to_json(target_ref),
                    outcome=SyncOutcome.FAILED,
                ),
                SyncHistory(
                    profile_name="profile",
                    source_namespace="source",
                    source_ref=ref_to_json(source_ref),
                    target_namespace="other",
                    target_ref=ref_to_json(target_ref),
                    outcome=SyncOutcome.FAILED,
                ),
            ]
        )
        ctx.session.commit()

    manager.queue_failure_history_cleanup(
        source_ref=source_ref,
        target_ref=target_ref,
    )
    manager.flush_failure_history_cleanup()

    with sqlite_db_factory() as ctx:
        rows = ctx.session.query(SyncHistory).all()
        assert len(rows) == 1
        assert rows[0].target_namespace == "other"


@pytest.mark.asyncio
async def test_apply_updates_batch_applies_writes_independently(monkeypatch) -> None:
    """Planned updates should not be submitted as one ambiguous provider batch."""
    sync_client = SyncClient.__new__(SyncClient)
    sync_client.dry_run = False
    calls: list[str] = []

    async def fake_apply_update(plan, **kwargs):
        calls.append(plan.target_ref.key)
        if plan.target_ref.key == "b":
            raise RuntimeError("failed")
        return SyncOutcome.SYNCED

    monkeypatch.setattr(sync_client, "_apply_update", fake_apply_update)
    updates = [
        PreparedRecordUpdate(
            plan=PlannedWrite(
                item=ScanItem(node=Node(ref=Ref.anchor(key), kind="anime")),
                source_record=None,
                before=None,
                after=RecordSnapshot(ref=Ref.anchor(key), kind=_PROGRESS_KIND),
                write=cast(RecordWrite, object()),
                target_ref=Ref.anchor(key),
            ),
            source_record=Record(ref=Ref.anchor(key), kind=_PROGRESS_KIND),
            diff_str="",
            log_context=SyncLogContext(node_kind="anime", source=key, target=key),
        )
        for key in ("a", "b", "c")
    ]

    outcomes = await sync_client._apply_updates_batch(updates)

    assert calls == ["a", "b", "c"]
    assert outcomes == (
        SyncOutcome.SYNCED,
        SyncOutcome.FAILED,
        SyncOutcome.SYNCED,
    )


def _sync_client_with_disabled_expensive_fields() -> SyncClient:
    return SyncClient(
        source_provider=cast(Provider, FakeScanProvider()),
        target_provider=cast(Provider, FakeTargetProvider()),
        animap_client=cast(AnimapClient, object()),
        full_scan=False,
        destructive_sync=False,
        dry_run=False,
        profile_name="profile",
        sync_rules=SyncRulesConfig(rating=False, notes=False),
    )


def test_status_field_is_excluded_when_provider_values_do_not_overlap() -> None:
    """The planner should not request STATUS when providers cannot translate it."""
    sync_client = SyncClient(
        source_provider=cast(Provider, FakeScanProvider(statuses=(Status.ACTIVE,))),
        target_provider=cast(
            Provider,
            FakeTargetProvider(statuses=(Status.COMPLETED,)),
        ),
        animap_client=cast(AnimapClient, object()),
        full_scan=False,
        destructive_sync=False,
        dry_run=False,
        profile_name="profile",
        sync_rules=SyncRulesConfig(),
    )

    assert RecordField.STATUS not in sync_client._sync_fields
    assert RecordField.PROGRESS in sync_client._sync_fields


def test_target_state_for_status_rejects_unsupported_status() -> None:
    """Unsupported normalized statuses should fail before reaching providers."""
    planner = RecordPlanner(
        source_capabilities=FakeScanProvider(
            statuses=(Status.ACTIVE, Status.COMPLETED),
        ).capabilities(),
        target_capabilities=FakeTargetProvider(
            statuses=(Status.COMPLETED,),
        ).capabilities(),
        sync_rule_engine=SyncRuleEngine(),
        destructive_sync=False,
    )

    with pytest.raises(ValueError, match="cannot represent status"):
        planner.target_state_for_status(Status.ACTIVE)

    assert planner.target_state_for_status(Status.COMPLETED).native == "completed"


def test_project_progress_handles_large_fractional_ranges() -> None:
    """Progress projection should avoid per-unit loops and keep fractional progress."""
    planner = RecordPlanner(
        source_capabilities=FakeScanProvider().capabilities(),
        target_capabilities=FakeTargetProvider().capabilities(),
        sync_rule_engine=SyncRuleEngine(),
        destructive_sync=False,
    )

    projected = planner._project_progress(
        Progress(current=1000.5, total=2000, unit="episode"),
        (AnibridgeMapping.parse("1-2000", "1-1000|2"),),
    )

    assert projected == Progress(current=500.25, total=1000, unit="episode")


@pytest.mark.asyncio
async def test_disabled_sync_rule_fields_are_excluded_from_source_scan() -> None:
    """Disabled fields should never be requested from expensive source providers."""
    sync_client = _sync_client_with_disabled_expensive_fields()

    pages = [
        page
        async for page in sync_client.scan_source_pages(
            scan=SourceScan(
                trigger=SyncTrigger.MANUAL,
                source_refs=None,
                require_activity=False,
            )
        )
    ]

    assert pages == []
    source = sync_client.source_provider
    assert isinstance(source, FakeScanProvider)
    assert source.queries[0].fields == frozenset(
        {RecordField.STATUS, RecordField.PROGRESS}
    )


@pytest.mark.asyncio
async def test_disabled_sync_rule_fields_are_excluded_from_target_reads() -> None:
    """Disabled fields should not be fetched while planning target updates."""
    sync_client = _sync_client_with_disabled_expensive_fields()
    target = sync_client.target_provider
    assert isinstance(target, FakeTargetProvider)

    await sync_client._fetch_target_records_batch([(Ref.anchor("a"), _PROGRESS_KIND)])

    assert target.queries[0].fields == frozenset(
        {RecordField.STATUS, RecordField.PROGRESS}
    )


@pytest.mark.asyncio
async def test_scan_source_pages_collects_pages_and_clears_provider_caches() -> None:
    source = FakeScanProvider()
    target = FakeTargetProvider()
    first_item = _scan_item("first")
    second_item = _scan_item("second")
    source.pages = [
        Page(items=(), cursor="c1"),
        Page(items=(first_item,), cursor="c2", total=2),
        Page(items=(second_item,), cursor=None, total=2),
    ]
    client = _sync_client(source=source, target=target)

    items = await client.scan_source(
        scan=SourceScan(
            trigger=SyncTrigger.MANUAL,
            source_refs=(Ref.anchor("source"),),
            require_activity=False,
        )
    )
    await client.clear_cache()

    assert items == (first_item, second_item)
    assert [query.cursor for query in source.queries] == [None, "c1", "c2"]
    assert source.queries[0].sources == (Ref.anchor("source"),)
    assert source.queries[0].limit is None
    assert source.cleared is True
    assert target.cleared is True


@pytest.mark.asyncio
async def test_process_page_handles_skip_not_found_and_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = FakeTargetProvider()
    client = _sync_client(target=target)
    history = cast(FakeHistory, client._history)

    async def resolve_target_refs(**kwargs):
        record = kwargs["record"]
        if record.ref.key == "missing":
            return ()
        return (
            ResolvedTarget(
                Match(
                    ExternalId("target", record.ref.key),
                    Ref.anchor(f"target-{record.ref.key}"),
                    1.0,
                )
            ),
        )

    monkeypatch.setattr(base_module, "resolve_target_refs", resolve_target_refs)
    monkeypatch.setattr(client, "_fetch_pinned_fields_batch", lambda _requests: {})

    await client.process_page(())
    await client.process_item(_scan_item("empty"))
    await client.process_page(
        (
            _scan_item(
                "missing",
                records=(
                    _record(
                        "missing",
                        values={RecordField.PROGRESS: Progress(current=1, total=12)},
                    ),
                ),
            ),
            _scan_item(
                "updated",
                records=(
                    _record(
                        "updated",
                        values={RecordField.PROGRESS: Progress(current=2, total=12)},
                    ),
                ),
            ),
        )
    )

    assert len(history.created) == 2
    assert history.created[0]["outcome"] is SyncOutcome.NOT_FOUND
    assert history.created[1]["outcome"] is SyncOutcome.SYNCED
    assert len(target.writes) == 1
    assert client.sync_stats.not_found == 1
    assert client.sync_stats.synced == 1


@pytest.mark.asyncio
async def test_process_page_duplicate_targets_and_processing_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _sync_client()
    monkeypatch.setattr(client, "_fetch_pinned_fields_batch", lambda _requests: {})

    async def resolve_same_target(**_kwargs):
        return (
            ResolvedTarget(
                Match(ExternalId("target", "same"), Ref.anchor("same"), 1.0)
            ),
        )

    sync_calls: list[str] = []

    async def sync_record(**kwargs):
        sync_calls.append(kwargs["source_record"].ref.key)
        return SyncOutcome.SYNCED

    monkeypatch.setattr(base_module, "resolve_target_refs", resolve_same_target)
    monkeypatch.setattr(client, "sync_record", sync_record)

    await client.process_page(
        (
            _scan_item(
                "first",
                records=(
                    _record(
                        "first",
                        values={RecordField.PROGRESS: Progress(current=1, total=12)},
                    ),
                ),
            ),
            _scan_item(
                "second",
                records=(
                    _record(
                        "second",
                        values={RecordField.PROGRESS: Progress(current=2, total=12)},
                    ),
                ),
            ),
        )
    )
    assert sync_calls == ["first", "second"]

    client = _sync_client()
    monkeypatch.setattr(client, "_fetch_pinned_fields_batch", lambda _requests: {})
    monkeypatch.setattr(base_module, "resolve_target_refs", resolve_same_target)

    async def boom(**_kwargs):
        raise RuntimeError("planning failed")

    monkeypatch.setattr(client, "_prepare_record_update", boom)
    await client.process_page(
        (
            _scan_item(
                "bad",
                records=(
                    _record(
                        "bad",
                        values={RecordField.PROGRESS: Progress(current=1, total=12)},
                    ),
                ),
            ),
        )
    )
    assert client.sync_stats.failed == 1


@pytest.mark.asyncio
async def test_prepare_update_delete_paths_and_failure_cleanup() -> None:
    item = _scan_item("source")
    source_record = _record("source")
    target_record = _record(
        "target",
        values={RecordField.PROGRESS: Progress(current=3, total=12)},
    )

    client = _sync_client()
    assert (
        await client._prepare_record_update(
            item=item,
            source_record=source_record,
            target_record=target_record,
            target_ref=Ref.anchor("target"),
            target_kind=_PROGRESS_KIND,
            log_context=SyncLogContext(
                node_kind="anime", source="Title", target="{ids}"
            ),
        )
        is SyncOutcome.SKIPPED
    )

    delete_client = _sync_client(destructive_sync=True, dry_run=True)
    delete_client._target_capabilities = delete_client._target_capabilities.__replace__(
        write_ops=frozenset({WriteOp.UPSERT_RECORD, WriteOp.DELETE_RECORD})
    )
    assert (
        await delete_client._prepare_record_update(
            item=item,
            source_record=source_record,
            target_record=target_record,
            target_ref=Ref.anchor("target"),
            target_kind=_PROGRESS_KIND,
            log_context=SyncLogContext(
                node_kind="anime", source="Title", target="{ids}"
            ),
        )
        is SyncOutcome.DELETED
    )

    no_delete_client = _sync_client(destructive_sync=True)
    assert (
        await no_delete_client._delete_record(
            item=item,
            source_record=source_record,
            target_record=target_record,
            target_ref=Ref.anchor("target"),
            target_kind=_PROGRESS_KIND,
            before_snapshot=RecordSnapshot.from_record(target_record),
            log_context=SyncLogContext(
                node_kind="anime", source="Title", target="{ids}"
            ),
        )
        is SyncOutcome.SKIPPED
    )

    cleanup_client = _sync_client()
    skipped = await cleanup_client._prepare_record_update(
        item=item,
        source_record=target_record,
        target_record=target_record,
        target_ref=Ref.anchor("target"),
        target_kind=_PROGRESS_KIND,
        log_context=SyncLogContext(node_kind="anime", source="Title", target="{ids}"),
    )
    assert skipped is SyncOutcome.SKIPPED
    assert cast(FakeHistory, cleanup_client._history).cleanup == [
        (target_record.ref, Ref.anchor("target"))
    ]


@pytest.mark.asyncio
async def test_apply_update_success_dry_run_failure_and_reconciliation() -> None:
    plan, source_record = _planned_write()

    dry_run = _sync_client(dry_run=True)
    assert (
        await dry_run._apply_update(
            plan,
            source_record=source_record,
            diff_str="diff",
            log_context=SyncLogContext(
                node_kind="anime", source="Title", target="{ids}"
            ),
        )
        is SyncOutcome.SYNCED
    )
    assert cast(FakeHistory, dry_run._history).created[0]["ephemeral"] is True

    success = _sync_client()
    assert (
        await success._apply_update(
            plan,
            source_record=source_record,
            diff_str="diff",
            log_context=SyncLogContext(
                node_kind="anime", source="Title", target="{ids}"
            ),
        )
        is SyncOutcome.SYNCED
    )

    target = FakeTargetProvider()
    target.write_error = RuntimeError("write failed")
    target.records[("target", _PROGRESS_KIND)] = Record(
        ref=Ref.anchor("target"),
        kind=_PROGRESS_KIND,
        values={RecordField.PROGRESS: Progress(current=2, total=12)},
    )
    reconciled = _sync_client(target=target)
    assert (
        await reconciled._apply_update(
            plan,
            source_record=source_record,
            diff_str="diff",
            log_context=SyncLogContext(
                node_kind="anime", source="Title", target="{ids}"
            ),
        )
        is SyncOutcome.SYNCED
    )

    failing = _sync_client()
    failing_target = cast(FakeTargetProvider, failing.target_provider)
    failing_target.write_error = RuntimeError("write failed")
    with pytest.raises(RuntimeError, match="write failed"):
        await failing._apply_update(
            plan,
            source_record=source_record,
            diff_str="diff",
            log_context=SyncLogContext(
                node_kind="anime",
                source="Title",
                target="{ids}",
            ),
        )
    assert (
        cast(FakeHistory, failing._history).created[0]["outcome"] is SyncOutcome.FAILED
    )


@pytest.mark.asyncio
async def test_apply_updates_batch_dry_run_empty_and_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, source_record = _planned_write()
    update = PreparedRecordUpdate(
        plan=plan,
        source_record=source_record,
        diff_str="diff",
        log_context=SyncLogContext(node_kind="anime", source="Title", target="{ids}"),
    )
    dry_run = _sync_client(dry_run=True)
    assert await dry_run._apply_updates_batch(()) == ()
    assert await dry_run._apply_updates_batch((update,)) == (SyncOutcome.SYNCED,)

    client = _sync_client()

    async def explode(*_args, **_kwargs):
        raise ValueError("outer")

    monkeypatch.setattr(client, "_apply_update", explode)
    assert await client._apply_updates_batch((update,)) == (SyncOutcome.FAILED,)


@pytest.mark.asyncio
async def test_target_matching_and_record_io_error_paths() -> None:
    plan, _source_record = _planned_write(target_ref=None, after=None)
    client = _sync_client()

    assert await client._target_matches_after(plan) is False
    plan, _source_record = _planned_write()
    client.target_provider = cast(Provider, FakeWriteOnlyTarget())
    assert await client._target_matches_after(plan) is False

    client = _sync_client()
    assert await client._target_matches_after(plan) is False
    target = cast(FakeTargetProvider, client.target_provider)
    target.records[("target", _PROGRESS_KIND)] = Record(
        ref=Ref.anchor("target"),
        kind=_PROGRESS_KIND,
        values={RecordField.PROGRESS: Progress(current=2, total=12)},
    )
    assert await client._target_matches_after(plan) is True
    target.records[("target", _PROGRESS_KIND)] = Record(
        ref=Ref.anchor("target"),
        kind=_PROGRESS_KIND,
        values={RecordField.PROGRESS: Progress(current=1, total=12)},
    )
    assert await client._target_matches_after(plan) is False

    target.write_results = (WriteResult(ok=True, op=WriteOp.UPSERT_RECORD),) * 2
    with pytest.raises(ValueError, match="2 write results for 1 writes"):
        await client._submit_record_writes([plan.write])
    target.write_results = (
        WriteResult(ok=False, op=WriteOp.UPSERT_RECORD, error="bad write"),
    )
    with pytest.raises(RuntimeError, match="bad write"):
        await client._write_records([plan.write])

    client.target_provider = cast(Provider, FakeRecordReader())
    with pytest.raises(TypeError, match="record writes"):
        await client._submit_record_writes([plan.write])
    client.target_provider = cast(Provider, FakeWriteOnlyTarget())
    fetched = await client._fetch_target_record(Ref.anchor("target"), _PROGRESS_KIND)
    assert fetched is None
    assert (
        await client._fetch_target_records_batch(
            [(Ref.anchor("target"), _PROGRESS_KIND)]
        )
        == {}
    )


def test_flush_cleanup_pins_empty_and_debug_helpers() -> None:
    client = _sync_client()
    history = cast(FakeHistory, client._history)
    client.flush_failure_history_cleanup()
    assert history.flushed is True
    assert client._fetch_pinned_fields_batch(()) == {}

    item = _scan_item("source")
    assert (
        client._node_log_label(
            namespace="source",
            ref=item.node.ref,
            title=item.node.title,
            descriptor=None,
            mappings=(),
            side="source",
        )
        == "$$'Source (source)'$$"
    )
    assert (
        client._node_log_label(
            namespace="target",
            ref=Ref.anchor("target"),
            title=None,
            descriptor=None,
            mappings=(),
            side="target",
        )
        == "$$'(target)'$$"
    )
    assert (
        client._node_log_label(
            namespace="target",
            ref=Ref.at("target", ("episode", 3)),
            title=None,
            descriptor=None,
            mappings=(),
            side="target",
        )
        == "$$'(target/episode=3)'$$"
    )
    assert (
        client._node_log_label(
            namespace="source",
            ref=item.node.ref,
            title=item.node.title,
            descriptor=ExternalId("tmdb_show", "10", "s1"),
            mappings=(AnibridgeMapping.parse("1-12", "1-12"),),
            side="source",
        )
        == "$$'Source (source)'$$ $${tmdb_show:10:s1/1-12}$$"
    )
    assert (
        client._node_log_label(
            namespace="target",
            ref=Ref.anchor("target"),
            title=None,
            descriptor=ExternalId("anilist", "456"),
            mappings=(AnibridgeMapping.parse("1-12", "1-12"),),
            side="target",
        )
        == "$$'(target)'$$ $${anilist:456/1-12}$$"
    )


def test_sync_client_rejects_no_shared_writable_fields() -> None:
    class SourceWithoutReadableFields(FakeScanProvider):
        def capabilities(self) -> Capabilities:
            return _capabilities(role=Role.SOURCE, readable=False, writable=False)

    with pytest.raises(TypeError, match="no common readable/writable"):
        SyncClient(
            source_provider=cast(Provider, SourceWithoutReadableFields()),
            target_provider=cast(Provider, FakeTargetProvider()),
            animap_client=cast(AnimapClient, object()),
            full_scan=False,
            destructive_sync=False,
            dry_run=False,
            profile_name="profile",
            sync_rules=SyncRulesConfig(),
        )
