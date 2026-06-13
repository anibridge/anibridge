"""Tests for provider record planning."""

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, cast

import pytest
from anibridge.provider.base import (
    Capabilities,
    Descriptor,
    FieldConstraint,
    FieldSpec,
    Match,
    Node,
    NumericConstraint,
    Page,
    Progress,
    ProgressConstraint,
    Provider,
    Rating,
    Record,
    RecordField,
    RecordKind,
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
    TemporalConstraint,
    TemporalPrecision,
    TextConstraint,
    UpsertRecord,
    WriteOp,
    WriteResult,
)
from anibridge.utils.mappings import AnibridgeMapping

from anibridge.app.core.sync.planner import (
    PreparedRecordUpdate,
    RecordPlanner,
    SyncLogContext,
)
from anibridge.app.core.sync.rules import SyncRuleEngine
from anibridge.app.logging import get_logger
from anibridge.app.models.db.sync_history import SyncOutcome
from anibridge.app.utils.terminal import ARROW

_KIND = "progress"


def _status_descriptors(
    statuses: tuple[Status, ...] = (
        Status.PLANNED,
        Status.ACTIVE,
        Status.COMPLETED,
        Status.REPEATING,
    ),
) -> tuple[Descriptor[Status], ...]:
    return tuple(Descriptor(status.value, status) for status in statuses)


def _fields(
    *,
    readable: bool,
    writable: bool,
    constraints: Mapping[RecordField, tuple[FieldConstraint, ...]] | None = None,
) -> dict[RecordField, FieldSpec]:
    constraints = constraints or {}
    return {
        RecordField.STATUS: FieldSpec(
            RecordField.STATUS,
            readable=readable,
            writable=writable,
            values=_status_descriptors(),
        ),
        **{
            field: FieldSpec(
                field,
                readable=readable,
                writable=writable,
                constraints=cast(
                    tuple[FieldConstraint, ...],
                    constraints.get(field, ()),
                ),
            )
            for field in RecordField
            if field != RecordField.STATUS
        },
    }


def _capabilities(
    *,
    role: Role,
    readable: bool,
    writable: bool,
    constraints: Mapping[RecordField, tuple[FieldConstraint, ...]] | None = None,
) -> Capabilities:
    return Capabilities(
        roles=frozenset({role}),
        external_authorities=(
            frozenset({"target"}) if role == Role.TARGET else frozenset()
        ),
        record_kinds=(Descriptor(_KIND, RecordKind.PROGRESS),),
        record_fields=_fields(
            readable=readable,
            writable=writable,
            constraints=constraints,
        ),
        write_ops=frozenset({WriteOp.UPSERT_RECORD}) if writable else frozenset(),
    )


class _Source(SupportsScan):
    NAMESPACE = "source"

    def capabilities(self) -> Capabilities:
        return _capabilities(role=Role.SOURCE, readable=True, writable=False)

    async def scan(self, query: ScanQuery) -> Page[ScanItem]:
        return Page(items=())


class _Target(SupportsMapping, SupportsRecordReads, SupportsRecordWrites):
    NAMESPACE = "target"

    def capabilities(self) -> Capabilities:
        return _capabilities(role=Role.TARGET, readable=True, writable=True)

    async def resolve(self, ids) -> tuple[Match, ...]:
        return tuple(Match(item, Ref.anchor(item.value), 1.0) for item in ids)

    async def fetch_records(self, query) -> Page[Record]:
        return Page(items=())

    async def write_records(self, writes) -> tuple[WriteResult, ...]:
        return tuple(WriteResult(ok=True, op=WriteOp.UPSERT_RECORD) for _ in writes)


class _PlainProvider(Provider):
    NAMESPACE = "plain"

    def account(self):
        return None


def _planner(
    *,
    source: Capabilities | None = None,
    target: Capabilities | None = None,
    destructive: bool = False,
) -> RecordPlanner:
    return RecordPlanner(
        source_capabilities=source
        or _capabilities(role=Role.SOURCE, readable=True, writable=False),
        target_capabilities=target
        or _capabilities(role=Role.TARGET, readable=True, writable=True),
        sync_rule_engine=SyncRuleEngine(),
        destructive_sync=destructive,
    )


def test_validate_provider_contracts_reports_missing_capabilities() -> None:
    planner = RecordPlanner(
        source_capabilities=Capabilities(),
        target_capabilities=Capabilities(),
        sync_rule_engine=SyncRuleEngine(),
        destructive_sync=False,
    )

    with pytest.raises(TypeError, match="source role"):
        planner.validate_provider_contracts(
            source_provider=_PlainProvider(logger=get_logger(__name__), config={}),
            target_provider=_PlainProvider(logger=get_logger(__name__), config={}),
        )

    _planner().validate_provider_contracts(
        source_provider=cast(Provider, _Source()),
        target_provider=cast(Provider, _Target()),
    )


def test_record_kind_projection_and_syncable_records() -> None:
    planner = _planner()
    item = ScanItem(
        node=Node(ref=Ref.anchor("node"), kind="anime"),
        records=(
            Record(ref=Ref.anchor("empty"), kind=_KIND),
            Record(
                ref=Ref.anchor("unknown"),
                kind="unknown",
                values={RecordField.PROGRESS: Progress(current=1)},
            ),
            Record(
                ref=Ref.anchor("progress"),
                kind=_KIND,
                values={RecordField.PROGRESS: Progress(current=1)},
            ),
        ),
    )

    assert planner.source_record_kinds() == (_KIND,)
    assert planner.target_record_kind_for(_KIND) == _KIND
    assert planner.target_record_kind_for("unknown") is None
    assert [record.ref.key for record in planner.syncable_source_records(item)] == [
        "progress"
    ]
    assert [
        record.ref.key
        for record in _planner(destructive=True).syncable_source_records(item)
    ] == ["empty", "progress"]


def test_project_source_record_maps_progress_and_status() -> None:
    planner = _planner()
    source_record = Record(
        ref=Ref.anchor("source"),
        kind=_KIND,
        values={
            RecordField.STATUS: State(status=Status.ACTIVE),
            RecordField.PROGRESS: Progress(current=12, total=12, unit="episode"),
        },
    )
    mapping = AnibridgeMapping.parse("1-12", "1-6|2")

    assert planner.project_source_record(source_record, mappings=()) is source_record
    projected = planner.project_source_record(source_record, mappings=(mapping,))

    assert projected.values[RecordField.PROGRESS] == Progress(
        current=6,
        total=6,
        unit="episode",
    )
    assert projected.values[RecordField.STATUS] == State(status=Status.COMPLETED)

    completed = Record(
        ref=Ref.anchor("source"),
        kind=_KIND,
        values={
            RecordField.STATUS: State(status=Status.COMPLETED),
            RecordField.PROGRESS: Progress(current=1, total=12),
        },
    )
    projected = planner.project_source_record(completed, mappings=(mapping,))
    assert projected.values[RecordField.STATUS] == State(status=Status.ACTIVE)


def test_integer_target_progress_floors_fractional_mapping_before_diff() -> None:
    """Integer progress targets should not plan no-op fractional updates."""
    planner = _planner(
        target=_capabilities(
            role=Role.TARGET,
            readable=True,
            writable=True,
            constraints={
                RecordField.PROGRESS: (
                    ProgressConstraint(
                        current=NumericConstraint(0, None, 1),
                        total=False,
                        unit=False,
                    ),
                )
            },
        )
    )
    item = ScanItem(node=Node(ref=Ref.anchor("node"), title="Title", kind="anime"))
    target = Record(
        ref=Ref.anchor("target"),
        kind=_KIND,
        values={RecordField.PROGRESS: Progress(current=8)},
    )
    mappings = tuple(
        AnibridgeMapping.parse(source, target)
        for source, target in (
            ("1", "1"),
            ("2-3", "2|2"),
            ("4-12", "3-5|3"),
            ("13", "6"),
            ("14-15", "7|2"),
            ("16-21", "8-9|3"),
            ("22-23", "10|2"),
            ("24-26", "11|3"),
            ("27-30", "12-13|2"),
        )
    )

    fractional = planner.project_source_record(
        Record(
            ref=Ref.anchor("source"),
            kind=_KIND,
            values={RecordField.PROGRESS: Progress(current=20, total=30)},
        ),
        mappings=mappings,
    )
    assert fractional.values[RecordField.PROGRESS] == Progress(
        current=8.666666666666666, total=13
    )
    assert (
        planner.prepare_upsert(
            item,
            source_record=fractional,
            target_record=target,
            target_ref=Ref.anchor("target"),
            target_kind=_KIND,
            pinned_fields=(),
            log_context=SyncLogContext(
                node_kind="anime",
                source="Title",
                target="{anilist:151799}",
            ),
            mappings=mappings,
        )
        == SyncOutcome.SKIPPED
    )

    boundary = planner.project_source_record(
        Record(
            ref=Ref.anchor("source"),
            kind=_KIND,
            values={RecordField.PROGRESS: Progress(current=21, total=30)},
        ),
        mappings=mappings,
    )
    planned = planner.prepare_upsert(
        item,
        source_record=boundary,
        target_record=target,
        target_ref=Ref.anchor("target"),
        target_kind=_KIND,
        pinned_fields=(),
        log_context=SyncLogContext(
            node_kind="anime",
            source="Title",
            target="{anilist:151799}",
        ),
        mappings=mappings,
    )

    assert isinstance(planned, PreparedRecordUpdate)
    assert isinstance(planned.plan.write, UpsertRecord)
    assert planned.plan.write.set[RecordField.PROGRESS] == Progress(current=9)
    assert planned.diff_str == f"progress: 8 {ARROW} 9"


def test_prepare_upsert_clear_fields_rules_and_diff_formatting() -> None:
    planner = _planner(destructive=True)
    item = ScanItem(node=Node(ref=Ref.anchor("node"), title="Title", kind="anime"))
    target = Record(
        ref=Ref.anchor("target"),
        kind=_KIND,
        key="entry",
        revision="rev",
        values={
            RecordField.STATUS: State(status=Status.ACTIVE),
            RecordField.NOTES: "old",
        },
    )

    planned = planner.prepare_upsert(
        item,
        source_record=Record(
            ref=Ref.anchor("source"),
            kind=_KIND,
            values=cast(
                Any,
                {
                    RecordField.STATUS: State(status=Status.ACTIVE),
                    RecordField.NOTES: None,
                },
            ),
        ),
        target_record=target,
        target_ref=Ref.anchor("target"),
        target_kind=_KIND,
        pinned_fields=(),
        log_context=SyncLogContext(node_kind="anime", source="Title", target="{ids}"),
    )

    assert isinstance(planned, PreparedRecordUpdate)
    assert isinstance(planned.plan.write, UpsertRecord)
    assert planned.plan.write.clear == frozenset({RecordField.NOTES})
    assert "notes" in planned.diff_str

    assert (
        planner.prepare_upsert(
            item,
            source_record=target,
            target_record=target,
            target_ref=Ref.anchor("target"),
            target_kind=_KIND,
            pinned_fields=(),
            log_context=SyncLogContext(
                node_kind="anime", source="Title", target="{ids}"
            ),
        )
        == SyncOutcome.SKIPPED
    )

    diff = planner.format_diff(
        {
            "started_at": (
                datetime(2026, 1, 1, 12, 30),
                datetime(2026, 1, 2, 12, 30, tzinfo=UTC),
            )
        }
    )
    assert "2026-01-01T12:30:00+00:00" in diff
    assert "2026-01-02T12:30:00+00:00" in diff


def test_status_helpers_and_value_coercion_constraints() -> None:
    constraints = {
        RecordField.PROGRESS: (ProgressConstraint(total=False, unit=False),),
        RecordField.RATING: (NumericConstraint(0, 100, 10),),
        RecordField.NOTES: (TextConstraint(3),),
        RecordField.REPEAT_COUNT: (NumericConstraint(0, 10, 2),),
        RecordField.STARTED_AT: (TemporalConstraint(TemporalPrecision.DATE),),
    }
    planner = _planner(
        target=_capabilities(
            role=Role.TARGET,
            readable=True,
            writable=True,
            constraints=constraints,
        )
    )

    assert planner.status_of(Status.ACTIVE) is Status.ACTIVE
    assert planner.status_of(State(status=Status.COMPLETED)) is Status.COMPLETED
    assert planner.status_of("active") is None
    assert planner.target_state_for_status(None) == State(native=None, status=None)
    assert planner._coerce_value(RecordField.STATUS, Status.ACTIVE) == State(
        native="active",
        status=Status.ACTIVE,
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        planner._coerce_value(RecordField.STARTED_AT, datetime(2026, 1, 1))

    assert planner._coerce_value(
        RecordField.STARTED_AT,
        datetime(2026, 1, 1, 5, 30, tzinfo=UTC),
    ) == date(2026, 1, 1)
    assert planner._coerce_value(
        RecordField.STARTED_AT,
        date(2026, 1, 2),
    ) == date(2026, 1, 2)

    datetime_planner = _planner()
    assert datetime_planner._coerce_value(
        RecordField.STARTED_AT,
        date(2026, 1, 3),
    ) == datetime(2026, 1, 3, tzinfo=UTC)
    assert planner._coerce_value(
        RecordField.PROGRESS,
        Progress(current=2, total=12, unit="episode"),
        Progress(current=1, total=10, unit="chapter"),
    ) == Progress(current=2, total=10, unit="chapter")
    assert planner._coerce_value(RecordField.RATING, Rating(5, (0, 10, 1))) == Rating(
        50,
        (0, 100, 10),
    )
    assert planner._coerce_value(RecordField.RATING, Rating(1, (1, 1, 1))) == Rating(
        0,
        (0, 100, 10),
    )
    assert planner._coerce_value(RecordField.NOTES, "abcdef") == "abc"
    assert planner._coerce_value(RecordField.REPEAT_COUNT, 9.1) == 10


def test_progress_equality_status_gates_and_mapping_edges() -> None:
    constrained = _planner(
        target=_capabilities(
            role=Role.TARGET,
            readable=True,
            writable=True,
            constraints={
                RecordField.PROGRESS: (ProgressConstraint(total=False, unit=False),)
            },
        )
    )

    assert constrained._values_equal(
        RecordField.PROGRESS,
        Progress(current=1, total=12, unit="episode"),
        Progress(current=1, total=24, unit="chapter"),
    )
    assert not _planner()._values_equal(
        RecordField.PROGRESS,
        Progress(current=1, total=12, unit="episode"),
        Progress(current=1, total=24, unit="chapter"),
    )
    assert (
        constrained._status_gate(RecordField.REPEAT_COUNT, Status.ACTIVE)
        == "requires_completed"
    )
    assert (
        constrained._status_gate(RecordField.STARTED_AT, Status.PLANNED)
        == "requires_active_status"
    )
    assert constrained._status_gate(RecordField.NOTES, Status.PLANNED) is None

    mapping = AnibridgeMapping.parse("3-4", "10-11")
    projected = constrained._project_progress(
        Progress(current=2.5, total=12),
        (mapping,),
    )
    assert projected == Progress(current=0.5, total=2)
    assert constrained._best_mapping_for_index(2, (mapping,)) is None


def test_missing_status_spec_rejects_status_translation() -> None:
    target = _capabilities(role=Role.TARGET, readable=True, writable=True)
    target = target.__replace__(
        record_fields={
            key: value
            for key, value in target.record_fields.items()
            if key != RecordField.STATUS
        }
    )
    planner = _planner(target=target)

    with pytest.raises(ValueError, match="writable status"):
        planner.target_state_for_status(Status.ACTIVE)
