"""Tests for sync client event ref helpers."""

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from anibridge.provider.base import (
    AppendEvent,
    Capabilities,
    Descriptor,
    Event,
    EventKind,
    EventQuery,
    EventSpec,
    ExternalId,
    FieldSpec,
    Match,
    Node,
    Page,
    Progress,
    Provider,
    Record,
    RecordField,
    RecordKind,
    RecordQuery,
    RecordSpec,
    RecordWrite,
    Ref,
    Role,
    ScanItem,
    ScanQuery,
    State,
    Status,
    SupportsEventReads,
    SupportsEventWrites,
    SupportsMapping,
    SupportsRecordReads,
    SupportsRecordWrites,
    SupportsScan,
    UpsertRecord,
    WriteError,
    WriteOp,
    WriteResult,
)
from anibridge.utils.mappings import AnibridgeMapping

from anibridge.app.core.animap import AnimapClient
from anibridge.app.core.sync import ScanPlan, SyncTrigger, ref_to_json, ref_to_key
from anibridge.app.core.sync.base import SyncClient, _TargetWork
from anibridge.app.core.sync.planner import PreparedUpdate, SyncLabel
from anibridge.app.core.sync.stats import RecordPlan, RecordSnapshot, SyncItem
from anibridge.app.core.sync.targeting import ResolvedTarget
from anibridge.app.models.db.pin import Pin
from anibridge.app.models.db.sync_history import SyncOutcome

_RECORD_KIND = "progress"
_SOURCE_EVENT = "played"
_TARGET_EVENT = "scrobbled"


def _record_fields(*, readable: bool, writable: bool) -> dict[RecordField, FieldSpec]:
    return {
        RecordField.STATUS: FieldSpec(
            RecordField.STATUS,
            readable=readable,
            writable=writable,
            values=(
                Descriptor(Status.ACTIVE.value, Status.ACTIVE),
                Descriptor(Status.COMPLETED.value, Status.COMPLETED),
            ),
        ),
        RecordField.PROGRESS: FieldSpec(
            RecordField.PROGRESS,
            readable=readable,
            writable=writable,
        ),
    }


def _record_spec(*, readable: bool, writable: bool, delete: bool = False) -> RecordSpec:
    write_ops = {WriteOp.UPSERT_RECORD} if writable else set()
    if delete:
        write_ops.add(WriteOp.DELETE_RECORD)
    return RecordSpec(
        kind=Descriptor(_RECORD_KIND, RecordKind.PROGRESS),
        fields=_record_fields(readable=readable, writable=writable),
        write_ops=frozenset(write_ops),
    )


def _capabilities(
    *,
    role: Role,
    events: tuple[EventSpec, ...] = (),
    delete: bool = False,
) -> Capabilities:
    return Capabilities(
        roles=frozenset({role}),
        records=(
            _record_spec(
                readable=role == Role.SOURCE,
                writable=role == Role.TARGET,
                delete=delete,
            ),
        ),
        events=events,
        external_authorities=(
            frozenset({"target"}) if role == Role.TARGET else frozenset()
        ),
    )


class _History:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.cleanup: list[tuple[Ref, Ref | None]] = []
        self.flushed = False

    async def create_sync_history(self, **kwargs) -> None:
        self.created.append(kwargs)

    def queue_failure_history_cleanup(
        self,
        *,
        source_ref: Ref,
        target_ref: Ref | None,
    ) -> None:
        self.cleanup.append((source_ref, target_ref))

    def flush_failure_history_cleanup(self) -> None:
        self.flushed = True


class _Animap:
    def resolve_edges(self, *args, **kwargs):
        return ()


class _SourceProvider(SupportsScan, SupportsEventReads):
    NAMESPACE = "source"
    DISPLAY_NAME = "Source"

    def __init__(self) -> None:
        self.pages: list[Page[ScanItem]] = []
        self.scan_queries: list[ScanQuery] = []
        self.events: dict[tuple[Ref, str, str | None], Page[Event]] = {}
        self.cleared = False

    def capabilities(self) -> Capabilities:
        return _capabilities(
            role=Role.SOURCE,
            events=(EventSpec(Descriptor(_SOURCE_EVENT, EventKind.SCROBBLE)),),
        )

    async def clear_cache(self) -> None:
        self.cleared = True

    async def scan(self, query: ScanQuery) -> Page[ScanItem]:
        self.scan_queries.append(query)
        return self.pages.pop(0) if self.pages else Page(items=())

    async def fetch_events(self, query: EventQuery) -> Page[Event]:
        key = (query.refs[0], query.native_event_kinds[0], query.cursor)
        return self.events.get(key, Page(items=()))


class _TargetProvider(
    SupportsMapping,
    SupportsRecordReads,
    SupportsRecordWrites,
    SupportsEventReads,
    SupportsEventWrites,
):
    NAMESPACE = "target"
    DISPLAY_NAME = "Target"

    def __init__(self, *, delete: bool = False, resolve_matches: bool = True) -> None:
        self.delete = delete
        self.resolve_matches = resolve_matches
        self.records: dict[tuple[object, str], Record] = {}
        self.record_queries: list[RecordQuery] = []
        self.record_writes: list[RecordWrite] = []
        self.events: dict[tuple[Ref, str, str | None], Page[Event]] = {}
        self.event_queries: list[EventQuery] = []
        self.event_writes: list[AppendEvent] = []
        self.cleared = False

    def capabilities(self) -> Capabilities:
        return _capabilities(
            role=Role.TARGET,
            delete=self.delete,
            events=(
                EventSpec(
                    Descriptor(_TARGET_EVENT, EventKind.SCROBBLE),
                    write_ops=frozenset({WriteOp.APPEND_EVENT}),
                ),
            ),
        )

    async def clear_cache(self) -> None:
        self.cleared = True

    async def resolve(self, ids) -> tuple[Match, ...]:
        if not self.resolve_matches:
            return ()
        return tuple(Match(item, Ref.anchor(item.value), 1.0) for item in ids)

    async def fetch_records(self, query: RecordQuery) -> Page[Record]:
        self.record_queries.append(query)
        records = []
        for ref in query.refs:
            for kind in query.native_record_kinds:
                record = self.records.get((ref_to_key(ref), kind))
                if record is not None:
                    records.append(record)
        return Page(items=tuple(records))

    async def write_records(self, writes) -> tuple[WriteResult, ...]:
        self.record_writes.extend(writes)
        return tuple(WriteResult(ok=True, op=WriteOp.UPSERT_RECORD) for _ in writes)

    async def fetch_events(self, query: EventQuery) -> Page[Event]:
        self.event_queries.append(query)
        items: list[Event] = []
        for ref in query.refs:
            for kind in query.native_event_kinds:
                page = self.events.get((ref, kind, query.cursor), Page(items=()))
                items.extend(page.items)
        return Page(items=tuple(items))

    async def write_events(self, writes) -> tuple[WriteResult, ...]:
        self.event_writes.extend(writes)
        return tuple(WriteResult(ok=True, op=WriteOp.APPEND_EVENT) for _ in writes)


def _client(
    *,
    source: _SourceProvider | None = None,
    target: _TargetProvider | None = None,
    destructive: bool = False,
    dry_run: bool = False,
    patch_pins: bool = True,
) -> SyncClient:
    client = SyncClient(
        source_provider=cast(Provider, source or _SourceProvider()),
        target_provider=cast(Provider, target or _TargetProvider()),
        animap_client=cast(AnimapClient, _Animap()),
        destructive_sync=destructive,
        dry_run=dry_run,
        profile_name="profile",
    )
    client._history = cast(Any, _History())
    if patch_pins:
        client._fetch_pinned_fields_batch = cast(Any, lambda requests: {})
    return client


def _sync_item(ref: Ref | None = None) -> SyncItem:
    ref = ref or Ref.anchor("source")
    return SyncItem(namespace="source", ref=ref, repr="source")


def _work(
    *,
    source_ref: Ref,
    target_ref: Ref,
    mappings: tuple[AnibridgeMapping, ...] = (),
    sync_items: tuple[SyncItem, ...] = (),
) -> _TargetWork:
    return _TargetWork(
        item=ScanItem(node=Node(ref=source_ref, kind="item")),
        sync_items=sync_items,
        projected_record=Record(ref=source_ref, kind="state"),
        target_ref=target_ref,
        target_kind="state",
        mappings=mappings,
        label=SyncLabel(node_kind="item", source="source", target="target"),
    )


def test_target_event_refs_preserve_generic_relative_path() -> None:
    """Event paths should stay relative to the resolved target ref."""
    client = object.__new__(SyncClient)
    work = _work(
        source_ref=Ref.at("source", ("group", 2)),
        target_ref=Ref.at("target", ("bucket", "a")),
    )

    refs = client._target_event_refs(
        Ref.at("source", ("group", 2), ("part", 5)),
        work,
    )

    assert refs == (Ref.at("target", ("bucket", "a"), ("part", 5)),)


def test_target_event_refs_map_generic_final_path_index() -> None:
    """Mappings should transform the final numeric path coordinate only."""
    client = object.__new__(SyncClient)
    work = _work(
        source_ref=Ref.anchor("source"),
        target_ref=Ref.at("target", ("bucket", "a")),
        mappings=(AnibridgeMapping.parse("2-4", "10-12"),),
    )

    refs = client._target_event_refs(Ref.at("source", ("part", 3)), work)

    assert refs == (Ref.at("target", ("bucket", "a"), ("part", 11)),)


def test_source_event_in_scope_keeps_exact_record_ref() -> None:
    """Mapped events at the record ref itself should stay in scope."""
    client = object.__new__(SyncClient)
    work = _work(
        source_ref=Ref.at("source", ("group", 2)),
        target_ref=Ref.at("target", ("bucket", "a")),
        mappings=(AnibridgeMapping.parse("2-4", "10-12"),),
    )
    event = Event(
        ref=Ref.at("source", ("group", 2)),
        kind="activity",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert client._source_event_in_scope(event, work)


def test_validate_events_rejects_mismatched_protocols() -> None:
    class Plain:
        NAMESPACE = "plain"

    with pytest.raises(TypeError, match="event reads"):
        SyncClient._validate_events(
            provider=cast(Provider, Plain()),
            capabilities=_capabilities(
                role=Role.SOURCE,
                events=(EventSpec(Descriptor(_SOURCE_EVENT, EventKind.SCROBBLE)),),
            ),
        )

    with pytest.raises(TypeError, match="event writes"):
        SyncClient._validate_events(
            provider=cast(Provider, Plain()),
            capabilities=_capabilities(
                role=Role.TARGET,
                events=(
                    EventSpec(
                        Descriptor(_TARGET_EVENT, EventKind.SCROBBLE),
                        write_ops=frozenset({WriteOp.APPEND_EVENT}),
                    ),
                ),
            ),
        )


@pytest.mark.asyncio
async def test_scan_source_pages_builds_contract_query_and_clears_cache() -> None:
    source = _SourceProvider()
    target = _TargetProvider()
    item = ScanItem(node=Node(ref=Ref.anchor("source"), kind="item"))
    source.pages = [Page(items=(item,), cursor="next"), Page(items=())]
    client = _client(source=source, target=target)

    pages = [
        page
        async for page in client.scan_source_pages(
            scan=ScanPlan(
                trigger=SyncTrigger.MANUAL,
                source_refs=(Ref.anchor("source"),),
                require_user_data=True,
            ),
            page_size=5,
        )
    ]
    await client.clear_cache()

    assert pages == [Page(items=(item,), cursor="next")]
    assert source.scan_queries[0].include_records is True
    assert source.scan_queries[0].record_fields == frozenset(
        {RecordField.STATUS, RecordField.PROGRESS}
    )
    assert source.scan_queries[0].require_user_data is True
    assert source.scan_queries[0].limit == 5
    assert source.scan_queries[1].cursor == "next"
    assert source.cleared is True
    assert target.cleared is True


@pytest.mark.asyncio
async def test_process_page_writes_records_events_and_tracks_outcomes() -> None:
    source = _SourceProvider()
    target = _TargetProvider()
    event_at = datetime(2026, 1, 1, tzinfo=UTC)
    source.events[(Ref.anchor("source"), _SOURCE_EVENT, None)] = Page(
        items=(
            Event(
                ref=Ref.at("source", ("part", 1)),
                kind=_SOURCE_EVENT,
                at=event_at,
                dedupe_key="event-1",
                metadata={"origin": "source"},
            ),
        )
    )
    client = _client(source=source, target=target)
    item = ScanItem(
        node=Node(ref=Ref.anchor("source"), kind="item", title="Source"),
        records=(
            Record(
                ref=Ref.anchor("source"),
                kind=_RECORD_KIND,
                ids=(ExternalId("target", "target"),),
                values={
                    RecordField.STATUS: State(status=Status.ACTIVE),
                    RecordField.PROGRESS: Progress(current=1),
                },
            ),
        ),
    )

    await client.process_page((item,))

    assert isinstance(target.record_writes[0], UpsertRecord)
    assert target.record_writes[0].set[RecordField.PROGRESS] == Progress(current=1)
    assert target.event_writes == [
        AppendEvent(
            ref=Ref.at("target", ("part", 1)),
            kind=_TARGET_EVENT,
            at=event_at,
            dedupe_key="event-1",
            metadata={"origin": "source"},
        )
    ]
    assert client.sync_stats.synced == 1
    history = cast(_History, client._history)
    assert history.created[-1]["outcome"] == SyncOutcome.SYNCED


@pytest.mark.asyncio
async def test_resolve_work_items_records_not_found_history() -> None:
    target = _TargetProvider(resolve_matches=False)
    client = _client(target=target)
    item = ScanItem(
        node=Node(ref=Ref.anchor("source"), kind="item"),
        records=(
            Record(
                ref=Ref.anchor("source"),
                kind=_RECORD_KIND,
                ids=(ExternalId("target", "missing"),),
                values={RecordField.PROGRESS: Progress(current=1)},
            ),
        ),
    )

    outcomes, work_items = await client._resolve_work_items((item,))

    assert work_items == []
    assert tuple(outcomes.values()) == (SyncOutcome.NOT_FOUND,)
    history = cast(_History, client._history)
    assert history.created[0]["outcome"] == SyncOutcome.NOT_FOUND
    assert client.sync_stats.count(SyncOutcome.PENDING) == 1


@pytest.mark.asyncio
async def test_prepare_record_update_delete_and_dry_run_apply() -> None:
    target = _TargetProvider(delete=True)
    client = _client(target=target, destructive=True, dry_run=True)
    item = ScanItem(node=Node(ref=Ref.anchor("source"), kind="item"))
    target_record = Record(
        ref=Ref.anchor("target"),
        kind=_RECORD_KIND,
        values={RecordField.PROGRESS: Progress(current=1)},
    )

    deleted = await client._prepare_record_update(
        item=item,
        source_record=Record(ref=Ref.anchor("source"), kind=_RECORD_KIND),
        target_record=target_record,
        target_ref=Ref.anchor("target"),
        target_kind=_RECORD_KIND,
        label=SyncLabel("item", "source", "target"),
    )

    assert deleted == SyncOutcome.DELETED
    history = cast(_History, client._history)
    assert history.created[-1]["outcome"] == SyncOutcome.DELETED

    plan = RecordSnapshot.from_record(
        Record(
            ref=Ref.anchor("target"),
            kind=_RECORD_KIND,
            values={RecordField.PROGRESS: Progress(current=2)},
        )
    )
    outcome = await client._apply_update(
        __import__("anibridge.app.core.sync.stats", fromlist=["RecordPlan"]).RecordPlan(
            item=item,
            source_record=target_record,
            before=None,
            after=plan,
            write=UpsertRecord(
                ref=Ref.anchor("target"),
                kind=_RECORD_KIND,
                set={RecordField.PROGRESS: Progress(current=2)},
            ),
            target_ref=Ref.anchor("target"),
        ),
        source_record=target_record,
        diff_str="progress: 1 -> 2",
        label=SyncLabel("item", "source", "target"),
    )

    assert outcome == SyncOutcome.SYNCED
    assert history.created[-1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_event_sync_skips_existing_and_validates_write_results() -> None:
    source = _SourceProvider()
    target = _TargetProvider()
    event_at = datetime(2026, 1, 1, tzinfo=UTC)
    source.events[(Ref.anchor("source"), _SOURCE_EVENT, None)] = Page(
        items=(Event(ref=Ref.anchor("source"), kind=_SOURCE_EVENT, at=event_at),)
    )
    target.events[(Ref.anchor("target"), _TARGET_EVENT, None)] = Page(
        items=(Event(ref=Ref.anchor("target"), kind=_TARGET_EVENT, at=event_at),)
    )
    client = _client(source=source, target=target)

    assert (
        await client._sync_events_for_work(
            _work(source_ref=Ref.anchor("source"), target_ref=Ref.anchor("target"))
        )
        == SyncOutcome.SKIPPED
    )
    assert target.event_writes == []

    class BadTarget(_TargetProvider):
        async def write_events(self, writes):
            return ()

    with pytest.raises(ValueError, match="write results"):
        await _client(target=BadTarget())._write_events(
            [AppendEvent(ref=Ref.anchor("target"), kind=_TARGET_EVENT, at=event_at)]
        )


@pytest.mark.asyncio
async def test_fetch_target_records_batch_and_write_record_errors() -> None:
    target = _TargetProvider()
    target.records[(ref_to_key(Ref.anchor("target")), _RECORD_KIND)] = Record(
        ref=Ref.anchor("target"),
        kind=_RECORD_KIND,
        values={RecordField.PROGRESS: Progress(current=1)},
    )
    client = _client(target=target)

    records = await client._fetch_target_records_batch(
        ((Ref.anchor("target"), _RECORD_KIND), (Ref.anchor("target"), _RECORD_KIND))
    )

    assert records[(ref_to_key(Ref.anchor("target")), _RECORD_KIND)].values[
        RecordField.PROGRESS
    ] == Progress(current=1)
    assert target.record_queries[0].fields == frozenset(
        {RecordField.STATUS, RecordField.PROGRESS}
    )

    class FailingTarget(_TargetProvider):
        async def write_records(self, writes):
            return (
                WriteResult(
                    ok=False,
                    op=WriteOp.UPSERT_RECORD,
                    code=WriteError.INVALID,
                    error="nope",
                ),
            )

    with pytest.raises(RuntimeError, match="nope"):
        await _client(target=FailingTarget())._write_records(
            [UpsertRecord(ref=Ref.anchor("target"), kind=_RECORD_KIND)]
        )


def test_labels_and_best_outcome_helpers() -> None:
    client = _client()
    item = ScanItem(
        node=Node(ref=Ref.at("source", ("part", 1)), kind="item", title="Title")
    )
    label = client._sync_label(
        item=item,
        target_ref=Ref.anchor("target"),
        source_descriptor=ExternalId("source-auth", "1"),
        target_descriptor=ExternalId("target-auth", "2"),
        mappings=(AnibridgeMapping.parse("1", "2"),),
    )
    outcomes = {_sync_item(): SyncOutcome.SKIPPED}

    SyncClient._record_best_outcome(outcomes, outcomes.keys(), SyncOutcome.FAILED)

    assert label.node_kind == "item"
    assert "source-auth:1/1" in label.source
    assert label.target is not None
    assert "target-auth:2/2" in label.target
    assert SyncClient._source_with_target(label).endswith(f"with target {label.target}")
    assert SyncClient._target_suffix(label) == f" with target {label.target}"
    assert tuple(outcomes.values()) == (SyncOutcome.FAILED,)
    client.flush_failure_history_cleanup()
    assert cast(_History, client._history).flushed is True


@pytest.mark.asyncio
async def test_apply_update_reconciles_write_error_and_records_failure() -> None:
    class RaisingTarget(_TargetProvider):
        async def write_records(self, writes):
            raise RuntimeError("after-write failure")

    item = ScanItem(node=Node(ref=Ref.anchor("source"), kind="item"))
    after_record = Record(
        ref=Ref.anchor("target"),
        kind=_RECORD_KIND,
        values={RecordField.PROGRESS: Progress(current=2)},
    )
    target = RaisingTarget()
    target.records[(ref_to_key(after_record.ref), _RECORD_KIND)] = after_record
    client = _client(target=target)
    plan = RecordPlan(
        item=item,
        source_record=after_record,
        before=None,
        after=RecordSnapshot.from_record(after_record),
        write=UpsertRecord(
            ref=after_record.ref,
            kind=_RECORD_KIND,
            set={RecordField.PROGRESS: Progress(current=2)},
        ),
        target_ref=after_record.ref,
    )

    assert (
        await client._apply_update(
            plan,
            source_record=after_record,
            diff_str="progress",
            label=SyncLabel("item", "source", "target"),
        )
        == SyncOutcome.SYNCED
    )
    history = cast(_History, client._history)
    assert history.created[-1]["info"]["write_reconciled_after_error"] is True

    target.records.clear()
    with pytest.raises(RuntimeError, match="after-write failure"):
        await client._apply_update(
            plan,
            source_record=after_record,
            diff_str="progress",
            label=SyncLabel("item", "source", "target"),
        )
    assert history.created[-1]["outcome"] == SyncOutcome.FAILED


@pytest.mark.asyncio
async def test_delete_record_skips_without_capability_and_writes_delete() -> None:
    item = ScanItem(node=Node(ref=Ref.anchor("source"), kind="item"))
    source_record = Record(ref=Ref.anchor("source"), kind=_RECORD_KIND)
    target_record = Record(ref=Ref.anchor("target"), kind=_RECORD_KIND, key="entry")
    label = SyncLabel("item", "source", "target")

    skipped = await _client(destructive=True)._delete_record(
        item=item,
        source_record=source_record,
        target_record=target_record,
        target_ref=Ref.anchor("target"),
        target_kind=_RECORD_KIND,
        before_snapshot=RecordSnapshot.from_record(target_record),
        label=label,
    )
    assert skipped == SyncOutcome.SKIPPED

    target = _TargetProvider(delete=True)
    client = _client(target=target, destructive=True)
    deleted = await client._delete_record(
        item=item,
        source_record=source_record,
        target_record=target_record,
        target_ref=Ref.anchor("target"),
        target_kind=_RECORD_KIND,
        before_snapshot=RecordSnapshot.from_record(target_record),
        label=label,
    )
    assert deleted == SyncOutcome.DELETED
    assert target.record_writes[0].key == "entry"
    history = cast(_History, client._history)
    assert history.created[-1]["outcome"] == SyncOutcome.DELETED


@pytest.mark.asyncio
async def test_sync_events_dry_run_and_fetch_event_pagination() -> None:
    source = _SourceProvider()
    target = _TargetProvider()
    event_at = datetime(2026, 1, 1, tzinfo=UTC)
    source.events[(Ref.anchor("source"), _SOURCE_EVENT, None)] = Page(
        items=(Event(ref=Ref.anchor("source"), kind=_SOURCE_EVENT, at=event_at),),
        cursor="more",
    )
    source.events[(Ref.anchor("source"), _SOURCE_EVENT, "more")] = Page(
        items=(
            Event(
                ref=Ref.at("source", ("part", 2)),
                kind=_SOURCE_EVENT,
                at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        )
    )
    target.events[(Ref.anchor("target"), _TARGET_EVENT, None)] = Page(
        items=(), cursor="done"
    )
    target.events[(Ref.anchor("target"), _TARGET_EVENT, "done")] = Page(items=())
    client = _client(source=source, target=target, dry_run=True)

    events = await client._fetch_source_events(Ref.anchor("source"), _SOURCE_EVENT)
    target_events = await client._fetch_target_events(
        (Ref.anchor("target"),), _TARGET_EVENT
    )
    outcome = await client._sync_events_for_work(
        _work(source_ref=Ref.anchor("source"), target_ref=Ref.anchor("target"))
    )

    assert len(events) == 2
    assert target_events == ()
    assert outcome == SyncOutcome.SYNCED
    assert target.event_writes == []


@pytest.mark.asyncio
async def test_write_event_failure_and_non_supporting_fetches() -> None:
    event_at = datetime(2026, 1, 1, tzinfo=UTC)

    class FailingEventTarget(_TargetProvider):
        async def write_events(self, writes):
            return (
                WriteResult(
                    ok=False,
                    op=WriteOp.APPEND_EVENT,
                    code=WriteError.INVALID,
                    error="bad event",
                ),
            )

    client = _client(target=FailingEventTarget())
    with pytest.raises(RuntimeError, match="bad event"):
        await client._write_events(
            [AppendEvent(ref=Ref.anchor("target"), kind=_TARGET_EVENT, at=event_at)]
        )

    class NoReadTarget(SupportsMapping, SupportsRecordReads, SupportsRecordWrites):
        NAMESPACE = "target"

        def capabilities(self) -> Capabilities:
            return _capabilities(role=Role.TARGET)

        async def resolve(self, ids):
            return ()

        async def fetch_records(self, query):
            return Page(items=())

        async def write_records(self, writes):
            return ()

    no_read = object.__new__(SyncClient)
    no_read.target_provider = NoReadTarget()
    assert (
        await no_read._fetch_target_events((Ref.anchor("target"),), _TARGET_EVENT) == ()
    )


def test_fetch_pinned_fields_batch_scores_specific_refs(sync_db) -> None:
    client = _client(patch_pins=False)
    with sync_db as ctx:
        ctx.session.add_all(
            [
                Pin(
                    profile_name="profile",
                    target_namespace="target",
                    target_ref=ref_to_json(Ref.anchor("target")),
                    fields=[RecordField.STATUS.value, "unknown"],
                ),
                Pin(
                    profile_name="profile",
                    target_namespace="target",
                    target_ref=ref_to_json(Ref.at("target", ("part", 1))),
                    fields=[RecordField.PROGRESS.value],
                ),
            ]
        )
        ctx.session.commit()

    pinned = client._fetch_pinned_fields_batch(
        (
            (Ref.at("target", ("part", 1)), _RECORD_KIND),
            (Ref.at("target", ("part", 2)), _RECORD_KIND),
        )
    )

    assert pinned[(ref_to_key(Ref.at("target", ("part", 1))), _RECORD_KIND)] == [
        RecordField.PROGRESS
    ]
    assert pinned[(ref_to_key(Ref.at("target", ("part", 2))), _RECORD_KIND)] == [
        RecordField.STATUS
    ]


@pytest.mark.asyncio
async def test_process_page_records_prepare_apply_and_event_failures() -> None:
    item_id = _sync_item()
    work = _work(
        source_ref=Ref.anchor("source"),
        target_ref=Ref.anchor("target"),
        sync_items=(item_id,),
    )
    client = _client()

    async def resolve_items(items):
        return {item_id: SyncOutcome.SKIPPED}, [work]

    async def fail_prepare(**kwargs):
        raise RuntimeError("prepare failed")

    client._resolve_work_items = cast(Any, resolve_items)
    client._prepare_record_update = cast(Any, fail_prepare)
    await client.process_page((work.item,))
    assert client.sync_stats.failed == 1

    client = _client()
    after = RecordSnapshot.from_record(
        Record(ref=Ref.anchor("target"), kind=_RECORD_KIND, values={})
    )
    update = PreparedUpdate(
        plan=RecordPlan(
            item=work.item,
            source_record=work.projected_record,
            before=None,
            after=after,
            write=UpsertRecord(ref=Ref.anchor("target"), kind=_RECORD_KIND),
            target_ref=Ref.anchor("target"),
        ),
        source_record=work.projected_record,
        diff_str="diff",
        label=work.label,
    )

    async def prepare_update(**kwargs):
        return update

    async def fail_apply(*args, **kwargs):
        raise RuntimeError("apply failed")

    client._resolve_work_items = cast(Any, resolve_items)
    client._prepare_record_update = cast(Any, prepare_update)
    client._apply_update = cast(Any, fail_apply)
    await client.process_page((work.item,))
    assert client.sync_stats.failed == 1

    client = _client()
    mapped_work = _work(
        source_ref=Ref.anchor("source"),
        target_ref=Ref.anchor("target"),
        mappings=(AnibridgeMapping.parse("1", "1"),),
        sync_items=(item_id,),
    )

    async def resolve_mapped(items):
        return {item_id: SyncOutcome.SKIPPED}, [mapped_work]

    async def fail_events(work):
        raise RuntimeError("events failed")

    client._resolve_work_items = cast(Any, resolve_mapped)
    client._sync_events_for_work = cast(Any, fail_events)
    await client.process_page((mapped_work.item,))
    assert client.sync_stats.failed == 1


@pytest.mark.asyncio
async def test_base_helper_edge_branches() -> None:
    client = _client()
    item = ScanItem(node=Node(ref=Ref.anchor("source"), kind="item"))
    outcomes, work_items = await client._resolve_work_items((item,))
    assert outcomes == {}
    assert work_items == []

    assert (
        client._work_items_for_matches(
            item=item,
            source_record=Record(
                ref=Ref.anchor("source"),
                kind="unknown",
                values={RecordField.PROGRESS: Progress(current=1)},
            ),
            sync_items=(_sync_item(),),
            matches=(
                ResolvedTarget(
                    Match(ExternalId("target", "target"), Ref.anchor("target"))
                ),
            ),
            label=SyncLabel("item", "source"),
        )
        == ()
    )

    mapped_work = _work(
        source_ref=Ref.anchor("source"),
        target_ref=Ref.anchor("target"),
        mappings=(AnibridgeMapping.parse("2-3", "10-11"),),
    )
    assert client._target_event_refs(Ref.at("source", ("part", "x")), mapped_work) == ()
    assert client._target_event_refs(Ref.at("source", ("part", 5)), mapped_work) == ()
    assert SyncClient._mapped_path_indices(
        1, (AnibridgeMapping.parse("1", "10-11"),)
    ) == (10, 11)
    assert (
        SyncClient._mapped_path_indices(2, (AnibridgeMapping.parse("1-2", "10|2"),))
        == ()
    )
    assert SyncClient._path_tail_int(Ref.anchor("source"), Ref.anchor("source")) is None
    assert (
        SyncClient._relative_event_path(
            Ref.at("source", ("outer", 1)),
            Ref.at("source", ("other", 1)),
        )
        == Ref.at("source", ("outer", 1)).path
    )
