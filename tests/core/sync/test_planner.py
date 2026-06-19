"""Tests for provider record planning."""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from logging import Logger
from typing import cast

import pytest
from anibridge.provider.base import (
    Capabilities,
    Descriptor,
    ExternalId,
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
    RecordSpec,
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

from anibridge.app.core.sync.planner import PreparedUpdate, RecordPlanner, SyncLabel
from anibridge.app.core.sync.rules import SyncRuleEngine
from anibridge.app.core.sync.stats import FieldChange
from anibridge.app.models.db.sync_history import SyncOutcome
from anibridge.app.utils.terminal import ARROW

_KIND = "progress"


def _status_values(
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
    statuses: tuple[Status, ...] = (
        Status.PLANNED,
        Status.ACTIVE,
        Status.COMPLETED,
        Status.REPEATING,
    ),
) -> dict[RecordField, FieldSpec]:
    constraints = constraints or {}
    return {
        RecordField.STATUS: FieldSpec(
            RecordField.STATUS,
            readable=readable,
            writable=writable,
            values=_status_values(statuses),
        ),
        **{
            field: FieldSpec(
                field,
                readable=readable,
                writable=writable,
                constraints=constraints.get(field, ()),
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
    statuses: tuple[Status, ...] = (
        Status.PLANNED,
        Status.ACTIVE,
        Status.COMPLETED,
        Status.REPEATING,
    ),
    delete: bool = False,
) -> Capabilities:
    write_ops = {WriteOp.UPSERT_RECORD} if writable else set()
    if delete:
        write_ops.add(WriteOp.DELETE_RECORD)
    return Capabilities(
        roles=frozenset({role}),
        external_authorities=(
            frozenset({"target"}) if role == Role.TARGET else frozenset()
        ),
        records=(
            RecordSpec(
                kind=Descriptor(_KIND, RecordKind.PROGRESS),
                fields=_fields(
                    readable=readable,
                    writable=writable,
                    constraints=constraints,
                    statuses=statuses,
                ),
                write_ops=frozenset(write_ops),
            ),
        ),
    )


class _Source(SupportsScan):
    NAMESPACE = "source"

    async def scan(self, query: ScanQuery) -> Page[ScanItem]:
        return Page(items=())


class _Target(SupportsMapping, SupportsRecordReads, SupportsRecordWrites):
    NAMESPACE = "target"

    async def resolve(self, ids: Sequence[ExternalId]) -> Sequence[Match]:
        return tuple(Match(item, Ref.anchor(item.value), 1.0) for item in ids)

    async def fetch_records(self, query) -> Page[Record]:
        return Page(items=())

    async def write_records(self, writes) -> Sequence[WriteResult]:
        return tuple(WriteResult(ok=True, op=WriteOp.UPSERT_RECORD) for _ in writes)


class _PlainProvider(Provider):
    NAMESPACE = "plain"
    DISPLAY_NAME = "Plain"

    def account(self):
        return None


def _planner(
    *,
    source: Capabilities | None = None,
    target: Capabilities | None = None,
    destructive: bool = False,
    rules: SyncRuleEngine | None = None,
) -> RecordPlanner:
    return RecordPlanner(
        source_capabilities=source
        or _capabilities(role=Role.SOURCE, readable=True, writable=False),
        target_capabilities=target
        or _capabilities(role=Role.TARGET, readable=True, writable=True),
        sync_rule_engine=rules or SyncRuleEngine(),
        destructive_sync=destructive,
    )


def _item() -> ScanItem:
    return ScanItem(node=Node(ref=Ref.anchor("node"), title="Title", kind="item"))


def _label() -> SyncLabel:
    return SyncLabel(node_kind="item", source="Title", target="target")


def test_validate_provider_contracts_reports_missing_capabilities() -> None:
    planner = RecordPlanner(
        source_capabilities=Capabilities(),
        target_capabilities=Capabilities(),
        sync_rule_engine=SyncRuleEngine(),
        destructive_sync=False,
    )

    with pytest.raises(TypeError, match="source role"):
        logger = cast(Logger, object())
        planner.validate_provider_contracts(
            source_provider=cast(Provider, _PlainProvider(logger=logger)),
            target_provider=cast(Provider, _PlainProvider(logger=logger)),
        )

    _planner().validate_provider_contracts(
        source_provider=cast(Provider, _Source()),
        target_provider=cast(Provider, _Target()),
    )


def test_record_kind_projection_sync_fields_and_deletion() -> None:
    planner = _planner(
        target=_capabilities(
            role=Role.TARGET, readable=True, writable=True, delete=True
        )
    )
    item = ScanItem(
        node=Node(ref=Ref.anchor("node"), kind="item"),
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
    assert planner.can_delete_record(_KIND) is True
    assert planner.can_delete_record("unknown") is False
    assert [record.ref.key for record in planner.syncable_source_records(item)] == [
        "progress"
    ]
    assert [
        record.ref.key
        for record in _planner(destructive=True).syncable_source_records(item)
    ] == ["empty", "progress"]


def test_sync_fields_require_readable_source_writable_target_and_status_overlap() -> (
    None
):
    target = _capabilities(
        role=Role.TARGET,
        readable=True,
        writable=True,
        statuses=(Status.DROPPED,),
    )
    planner = _planner(
        target=target, rules=SyncRuleEngine(field_rules={"notes": False})
    )

    assert RecordField.STATUS not in planner.sync_fields_for(_KIND, _KIND)
    assert RecordField.NOTES not in planner.sync_fields_for(_KIND, _KIND)
    assert RecordField.PROGRESS in planner.sync_fields_for(_KIND, _KIND)


def test_project_source_record_maps_progress_and_status() -> None:
    planner = _planner()
    source_record = Record(
        ref=Ref.anchor("source"),
        kind=_KIND,
        values={
            RecordField.STATUS: State(status=Status.ACTIVE),
            RecordField.PROGRESS: Progress(current=12, total=12, unit="unit"),
        },
    )
    mapping = AnibridgeMapping.parse("1-12", "1-6|2")

    assert planner.project_source_record(source_record, mappings=()) is source_record
    projected = planner.project_source_record(source_record, mappings=(mapping,))

    assert projected.values[RecordField.PROGRESS] == Progress(
        current=6,
        total=6,
        unit="unit",
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


def test_prepare_upsert_sets_clears_blocks_and_formats_diff() -> None:
    planner = _planner(destructive=True)
    target = Record(
        ref=Ref.anchor("target"),
        kind=_KIND,
        key="entry",
        revision="rev",
        values={
            RecordField.STATUS: State(status=Status.ACTIVE),
            RecordField.PROGRESS: Progress(current=1, total=10, unit="unit"),
            RecordField.NOTES: "old",
            RecordField.REPEAT_COUNT: 1,
        },
    )

    planned = planner.prepare_upsert(
        _item(),
        source_record=Record(
            ref=Ref.anchor("source"),
            kind=_KIND,
            values={
                RecordField.STATUS: State(status=Status.ACTIVE),
                RecordField.PROGRESS: Progress(current=2, total=10, unit="unit"),
            },
        ),
        target_record=target,
        target_ref=Ref.anchor("target"),
        target_kind=_KIND,
        pinned_fields=(RecordField.REPEAT_COUNT,),
        label=_label(),
        mappings=(AnibridgeMapping.parse("1-2", "1-2"),),
    )

    assert isinstance(planned, PreparedUpdate)
    assert isinstance(planned.plan.write, UpsertRecord)
    assert planned.plan.write.expected_revision == "rev"
    assert planned.plan.write.set[RecordField.PROGRESS] == Progress(
        current=2, total=10, unit="unit"
    )
    assert planned.plan.write.clear == frozenset({RecordField.NOTES})
    assert RecordField.REPEAT_COUNT not in planned.plan.write.clear
    assert any(
        block.reason == "pinned" for block in planned.plan.diagnostics.blocked_fields
    )
    assert planned.plan.diagnostics.mapping_ranges[0].source == "1-2"
    assert "progress" in planned.diff_str

    assert (
        planner.prepare_upsert(
            _item(),
            source_record=target,
            target_record=target,
            target_ref=Ref.anchor("target"),
            target_kind=_KIND,
            pinned_fields=(),
            label=_label(),
        )
        == SyncOutcome.SKIPPED
    )


def test_prepare_upsert_applies_rules_and_status_support() -> None:
    planner = _planner(
        rules=SyncRuleEngine(
            field_rules={
                "status": [{"name": "complete", "set": "Status.COMPLETED"}],
                "notes": False,
            }
        )
    )
    planned = planner.prepare_upsert(
        _item(),
        source_record=Record(
            ref=Ref.anchor("source"),
            kind=_KIND,
            values={
                RecordField.STATUS: State(status=Status.ACTIVE),
                RecordField.NOTES: "new",
            },
        ),
        target_record=Record(ref=Ref.anchor("target"), kind=_KIND, values={}),
        target_ref=Ref.anchor("target"),
        target_kind=_KIND,
        pinned_fields=(),
        label=_label(),
    )

    assert isinstance(planned, PreparedUpdate)
    assert isinstance(planned.plan.write, UpsertRecord)
    assert planned.plan.write.set[RecordField.STATUS] == State(
        native="completed", status=Status.COMPLETED
    )
    assert RecordField.NOTES not in planned.plan.write.set
    assert any(
        rule.name == "complete" for rule in planned.plan.diagnostics.applied_rules
    )

    unsupported = _planner(
        target=_capabilities(
            role=Role.TARGET,
            readable=True,
            writable=True,
            statuses=(Status.ACTIVE,),
        )
    )
    blocked = unsupported.prepare_upsert(
        _item(),
        source_record=Record(
            ref=Ref.anchor("source"),
            kind=_KIND,
            values={RecordField.STATUS: State(status=Status.COMPLETED)},
        ),
        target_record=Record(ref=Ref.anchor("target"), kind=_KIND, values={}),
        target_ref=Ref.anchor("target"),
        target_kind=_KIND,
        pinned_fields=(),
        label=_label(),
    )
    assert blocked == SyncOutcome.SKIPPED


def test_status_helpers_and_value_coercion_constraints() -> None:
    constraints = {
        RecordField.PROGRESS: (
            ProgressConstraint(
                current=NumericConstraint(0, None, 1), total=False, unit=False
            ),
        ),
        RecordField.RATING: (NumericConstraint(0, 100, 10),),
        RecordField.NOTES: (TextConstraint(3),),
        RecordField.REPEAT_COUNT: (NumericConstraint(0, 10, 2),),
        RecordField.STARTED_AT: (TemporalConstraint(TemporalPrecision.DATE),),
    }
    planner = _planner(
        target=_capabilities(
            role=Role.TARGET, readable=True, writable=True, constraints=constraints
        )
    )

    assert planner.status_of(Status.ACTIVE) is Status.ACTIVE
    assert planner.status_of(State(status=Status.COMPLETED)) is Status.COMPLETED
    assert planner.status_of("active") is None
    with pytest.raises(ValueError, match="empty status"):
        planner.target_state_for_status(None, _KIND)
    assert planner.target_state_for_status(Status.ACTIVE, _KIND) == State(
        native="active", status=Status.ACTIVE
    )
    assert planner._coerce_value(
        RecordField.STATUS, Status.ACTIVE, target_kind=_KIND
    ) == State(native="active", status=Status.ACTIVE)
    with pytest.raises(ValueError, match="timezone-aware"):
        planner._coerce_value(
            RecordField.STARTED_AT, datetime(2026, 1, 1), target_kind=_KIND
        )

    assert planner._coerce_value(
        RecordField.STARTED_AT,
        datetime(2026, 1, 1, 5, 30, tzinfo=UTC),
        target_kind=_KIND,
    ) == date(2026, 1, 1)
    assert planner._coerce_value(
        RecordField.STARTED_AT, date(2026, 1, 2), target_kind=_KIND
    ) == date(2026, 1, 2)
    assert _planner()._coerce_value(
        RecordField.STARTED_AT, date(2026, 1, 3), target_kind=_KIND
    ) == datetime(2026, 1, 3, tzinfo=UTC)
    assert planner._coerce_value(
        RecordField.PROGRESS,
        Progress(current=2.8, total=12, unit="unit"),
        Progress(current=1, total=10, unit="old"),
        target_kind=_KIND,
    ) == Progress(current=2, total=10, unit="old")
    assert planner._coerce_value(
        RecordField.RATING, Rating(5, (0, 10, 1)), target_kind=_KIND
    ) == Rating(50, (0, 100, 10))
    assert planner._coerce_value(
        RecordField.RATING, Rating(1, (1, 1, 1)), target_kind=_KIND
    ) == Rating(0, (0, 100, 10))
    assert (
        planner._coerce_value(RecordField.NOTES, "abcdef", target_kind=_KIND) == "abc"
    )
    assert planner._coerce_value(RecordField.REPEAT_COUNT, 9.1, target_kind=_KIND) == 10


def test_progress_equality_status_gates_mapping_edges_and_diff_formatting() -> None:
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
        Progress(current=1, total=12, unit="source"),
        Progress(current=1, total=24, unit="target"),
        _KIND,
    )
    assert not _planner()._values_equal(
        RecordField.PROGRESS,
        Progress(current=1, total=12, unit="source"),
        Progress(current=1, total=24, unit="target"),
        _KIND,
    )
    assert constrained._empty_progress(None)
    assert constrained._empty_progress(Progress(current=0))
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
    assert constrained._project_progress(
        Progress(current=2.5, total=12), (mapping,)
    ) == Progress(current=0.5, total=2)
    assert constrained._best_mapping_for_index(2, (mapping,)) is None

    diff = constrained.format_diff(
        [
            FieldChange(
                RecordField.STARTED_AT,
                datetime(2026, 1, 1, 12, 30),
                datetime(2026, 1, 2, 12, 30, tzinfo=UTC),
            )
        ]
    )
    assert (
        f"started_at: 2026-01-01T12:30:00+00:00 {ARROW} 2026-01-02T12:30:00+00:00"
        == diff
    )


def test_missing_status_spec_rejects_status_translation() -> None:
    target = _capabilities(role=Role.TARGET, readable=True, writable=True)
    target = target.__replace__(
        records=(
            target.records[0].__replace__(
                fields={
                    key: value
                    for key, value in target.records[0].fields.items()
                    if key != RecordField.STATUS
                }
            ),
        )
    )
    planner = _planner(target=target)

    with pytest.raises(ValueError, match="writable status"):
        planner.target_state_for_status(Status.ACTIVE, _KIND)


def test_integer_target_progress_floors_fractional_mapping_before_diff() -> None:
    planner = _planner(
        target=_capabilities(
            role=Role.TARGET,
            readable=True,
            writable=True,
            constraints={
                RecordField.PROGRESS: (
                    ProgressConstraint(
                        current=NumericConstraint(0, None, 1), total=False, unit=False
                    ),
                )
            },
        )
    )
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
    assert (
        planner.prepare_upsert(
            _item(),
            source_record=fractional,
            target_record=target,
            target_ref=Ref.anchor("target"),
            target_kind=_KIND,
            pinned_fields=(),
            label=_label(),
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
        _item(),
        source_record=boundary,
        target_record=target,
        target_ref=Ref.anchor("target"),
        target_kind=_KIND,
        pinned_fields=(),
        label=_label(),
        mappings=mappings,
    )

    assert isinstance(planned, PreparedUpdate)
    assert isinstance(planned.plan.write, UpsertRecord)
    assert planned.plan.write.set[RecordField.PROGRESS] == Progress(current=9)
