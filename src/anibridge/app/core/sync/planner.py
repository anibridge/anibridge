"""Record planning for the sync engine."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from itertools import pairwise
from math import floor
from typing import Any, TypeVar

import msgspec
from anibridge.provider.base import (
    Capabilities,
    FieldSpec,
    NumericConstraint,
    Progress,
    ProgressConstraint,
    Provider,
    Rating,
    Record,
    RecordField,
    Ref,
    Role,
    ScanItem,
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
    Value,
    WriteOp,
)
from anibridge.utils.mappings import AnibridgeMapping

from anibridge.app.core.sync.history import to_builtins
from anibridge.app.core.sync.rules import SyncRuleEngine, build_rule_context
from anibridge.app.core.sync.stats import (
    AppliedRule,
    FieldBlock,
    MappingRange,
    PlanDiagnostics,
    RecordDiff,
    RecordPlan,
    RecordSnapshot,
)
from anibridge.app.models.db.sync_history import SyncOutcome
from anibridge.app.utils.terminal import ARROW

__all__ = ["PreparedUpdate", "RecordPlanner"]

_ConstraintT = TypeVar("_ConstraintT")

_STATUS_ORDER: Mapping[Status | None, int] = {
    None: 0,
    Status.PLANNED: 1,
    Status.DROPPED: 2,
    Status.PAUSED: 3,
    Status.ACTIVE: 4,
    Status.COMPLETED: 5,
    Status.REPEATING: 6,
}


class SyncLabel(msgspec.Struct, frozen=True):
    """Formatted source/target labels for one sync log subject."""

    node_kind: str
    source: str
    target: str | None = None


class PreparedUpdate(msgspec.Struct, frozen=True):
    """A planned update plus logging context for batched application."""

    plan: RecordPlan
    source_record: Record
    diff_str: str
    label: SyncLabel


class RecordPlanner:
    """Translate normalized source records into target write plans."""

    def __init__(
        self,
        *,
        source_capabilities: Capabilities,
        target_capabilities: Capabilities,
        sync_rule_engine: SyncRuleEngine,
        destructive_sync: bool,
    ) -> None:
        """Initialize capability indexes and the field projection."""
        self.source_capabilities = source_capabilities
        self.target_capabilities = target_capabilities
        self.sync_rule_engine = sync_rule_engine
        self.destructive_sync = destructive_sync

        self._source_kind_semantics = {
            descriptor.native: descriptor.semantic
            for descriptor in source_capabilities.record_kinds
        }
        self._target_kinds_by_semantic = {
            descriptor.semantic: descriptor.native
            for descriptor in target_capabilities.record_kinds
            if descriptor.semantic is not None
        }
        self._target_writable_fields = {
            field
            for field, spec in target_capabilities.record_fields.items()
            if isinstance(spec, FieldSpec) and spec.writable
        }
        self._source_statuses = self._status_semantics(
            source_capabilities.record_fields.get(RecordField.STATUS)
        )
        self._target_statuses = self._status_semantics(
            target_capabilities.record_fields.get(RecordField.STATUS)
        )
        self.sync_fields = self._sync_fields()

    def validate_provider_contracts(
        self,
        *,
        source_provider: Provider,
        target_provider: Provider,
    ) -> None:
        """Validate the minimum provider capabilities needed by sync."""
        checks = (
            (Role.SOURCE in self.source_capabilities.roles, "source role"),
            (Role.TARGET in self.target_capabilities.roles, "target role"),
            (isinstance(source_provider, SupportsScan), "scan"),
            (isinstance(target_provider, SupportsMapping), "mapping resolution"),
            (isinstance(target_provider, SupportsRecordReads), "record reads"),
            (isinstance(target_provider, SupportsRecordWrites), "record writes"),
            (bool(self.source_capabilities.record_kinds), "source record kinds"),
            (bool(self.target_capabilities.record_kinds), "target record kinds"),
            (bool(self.source_capabilities.record_fields), "source record fields"),
            (bool(self.target_capabilities.record_fields), "target record fields"),
            (
                WriteOp.UPSERT_RECORD in self.target_capabilities.write_ops,
                "upsert_record",
            ),
            (
                bool(self.target_capabilities.external_authorities),
                "target external authorities",
            ),
        )
        failures = [name for ok, name in checks if not ok]
        if failures:
            raise TypeError(
                "Provider contract is incomplete for sync: "
                + ", ".join(failures)
                + f" (source={source_provider.NAMESPACE!r}, "
                + f"target={target_provider.NAMESPACE!r})"
            )

    def source_record_kinds(self) -> tuple[str, ...]:
        """Return source-native record kinds that the target can represent."""
        return tuple(
            native
            for native, semantic in self._source_kind_semantics.items()
            if semantic in self._target_kinds_by_semantic
        )

    def target_record_kind_for(self, source_kind: str) -> str | None:
        """Translate a source-native record kind to a target-native kind."""
        semantic = self._source_kind_semantics.get(source_kind)
        if semantic is None:
            return None
        return self._target_kinds_by_semantic.get(semantic)

    def syncable_source_records(self, item: ScanItem) -> tuple[Record, ...]:
        """Return scanned records that should drive sync."""
        return tuple(
            record
            for record in item.records
            if self.target_record_kind_for(record.kind)
            and (record.values or self.destructive_sync)
        )

    def project_source_record(
        self,
        source_record: Record,
        *,
        mappings: Sequence[AnibridgeMapping],
    ) -> Record:
        """Project source progress into the resolved target's mapped unit space."""
        progress = source_record.values.get(RecordField.PROGRESS)
        if not mappings or not isinstance(progress, Progress):
            return source_record

        values = dict(source_record.values)
        projected = self._project_progress(progress, mappings)
        values[RecordField.PROGRESS] = projected

        status = self.status_of(values.get(RecordField.STATUS))
        if (
            status in {Status.ACTIVE, Status.COMPLETED, Status.REPEATING}
            and projected.total
        ):
            current = projected.current or 0
            if current >= projected.total:
                values[RecordField.STATUS] = State(status=Status.COMPLETED)
            elif status in {Status.COMPLETED, Status.REPEATING}:
                values[RecordField.STATUS] = State(status=Status.ACTIVE)

        return replace(source_record, values=values)

    def prepare_upsert(
        self,
        item: ScanItem,
        *,
        source_record: Record,
        target_record: Record | None,
        target_ref: Ref,
        target_kind: str,
        pinned_fields: Sequence[RecordField],
        label: SyncLabel,
        mappings: Sequence[AnibridgeMapping] = (),
    ) -> PreparedUpdate | SyncOutcome:
        """Plan one source-to-target record upsert."""
        before = RecordSnapshot.from_record(target_record) if target_record else None
        planned_values: dict[RecordField, Value] = dict(
            target_record.values if target_record else {}
        )
        clear_fields: set[RecordField] = set()
        changed_fields: set[RecordField] = set()
        blocked_fields: list[FieldBlock] = []
        applied_rules: list[AppliedRule] = []
        pinned = set(pinned_fields)

        target_values = target_record.values if target_record else {}
        current_values = self._rule_values(target_values)
        computed_values = self._rule_values(source_record.values)
        source_status = self.status_of(source_record.values.get(RecordField.STATUS))
        final_status = (
            source_status
            if source_status is not None or target_record is None
            else self.status_of(target_record.values.get(RecordField.STATUS))
        )
        rule_context = build_rule_context(
            node=item.node,
            source_record=source_record,
            target_record=target_record,
            target_ref=target_ref,
        )

        for field in self.sync_fields:
            if field in pinned:
                blocked_fields.append(FieldBlock(field, "pinned"))
                continue
            if gate_reason := self._status_gate(field, final_status):
                blocked_fields.append(FieldBlock(field, gate_reason))
                continue

            rule = self.sync_rule_engine.evaluate_field(
                field=field,
                current_values=current_values,
                computed_values=computed_values,
                rule_context=rule_context,
            )
            if not rule.allowed:
                blocked_fields.append(FieldBlock(field, rule.reason or "blocked"))
                continue
            if (
                field == RecordField.STATUS
                and rule.value is not None
                and self.status_of(rule.value) not in self._target_statuses
            ):
                blocked_fields.append(FieldBlock(field, "unsupported_status"))
                continue

            new_value = self._coerce_value(
                field,
                rule.value,
                current_values.get(field),
            )

            current_value = current_values.get(field)
            if self._values_equal(field, current_value, new_value):
                continue
            if (
                not self.destructive_sync
                and current_value is not None
                and new_value is None
            ):
                blocked_fields.append(FieldBlock(field, "destructive_disabled"))
                continue

            if new_value is None:
                planned_values.pop(field, None)
                clear_fields.add(field)
            else:
                planned_values[field] = new_value
                clear_fields.discard(field)
            changed_fields.add(field)
            if rule.reason and rule.reason != "default":
                applied_rules.append(AppliedRule(field, rule.reason))

        after_record = Record(
            ref=target_ref,
            kind=target_kind,
            key=target_record.key if target_record else None,
            values=planned_values,
        )
        after = RecordSnapshot.from_record(after_record)
        diff = RecordSnapshot.diff_optional(before, after, changed_fields)
        if not diff:
            return SyncOutcome.SKIPPED

        write = UpsertRecord(
            ref=target_ref,
            kind=target_kind,
            key=target_record.key if target_record else None,
            expected_revision=target_record.revision if target_record else None,
            set={
                field: planned_values[field]
                for field in self.sync_fields
                if field in changed_fields and field in planned_values
            },
            clear=frozenset(clear_fields),
        )
        diagnostics = PlanDiagnostics(
            blocked_fields=tuple(blocked_fields),
            applied_rules=tuple(applied_rules),
            mapping_ranges=tuple(
                MappingRange(mapping.source_key, mapping.target_value)
                for mapping in mappings
            ),
        )
        return PreparedUpdate(
            plan=RecordPlan(
                item=item,
                source_record=source_record,
                before=before,
                after=after,
                write=write,
                target_ref=target_ref,
                diagnostics=diagnostics,
            ),
            source_record=source_record,
            diff_str=self.format_diff(diff),
            label=label,
        )

    def target_state_for_status(self, status: Status | None) -> State:
        """Return the target-native state for a normalized status."""
        if status is None:
            return State(native=None, status=None)

        spec = self.target_capabilities.record_fields.get(RecordField.STATUS)
        if not isinstance(spec, FieldSpec):
            raise ValueError("Target provider does not advertise writable status")

        for descriptor in spec.values:
            if descriptor.semantic == status:
                return State(native=descriptor.native, status=status)
        raise ValueError(f"Target provider cannot represent status {status.value!r}")

    @staticmethod
    def status_of(value: Any) -> Status | None:
        """Extract a normalized status from a State or Status value."""
        if isinstance(value, Status):
            return value
        if isinstance(value, State):
            return value.status
        return None

    def format_diff(self, diff: RecordDiff) -> str:
        """Format a diff dictionary for logging."""
        parts: list[str] = []
        for change in sorted(diff.changes, key=lambda item: item.field.value):
            rendered = []
            for value in (change.before, change.after):
                if isinstance(value, datetime):
                    value = (
                        value.replace(tzinfo=UTC)
                        if value.tzinfo is None
                        else value.astimezone(UTC)
                    )
                    rendered.append(value.isoformat())
                elif isinstance(value, date):
                    rendered.append(value.isoformat())
                else:
                    rendered.append(
                        "None" if value is None else repr(to_builtins(value))
                    )
            parts.append(f"{change.field.value}: {rendered[0]} {ARROW} {rendered[1]}")
        return " | ".join(parts)

    def _sync_fields(self) -> tuple[RecordField, ...]:
        fields: list[RecordField] = []
        for field in RecordField:
            source_spec = self.source_capabilities.record_fields.get(field)
            if self.sync_rule_engine.is_disabled(field):
                continue
            if not isinstance(source_spec, FieldSpec) or not source_spec.readable:
                continue
            if field not in self._target_writable_fields:
                continue
            if field == RecordField.STATUS and not (
                self._source_statuses & self._target_statuses
            ):
                continue
            fields.append(field)
        return tuple(fields)

    def _rule_values(
        self,
        values: Mapping[RecordField, Value],
    ) -> dict[RecordField, Any]:
        rule_values: dict[RecordField, Any] = {}
        for field in self.sync_fields:
            value = values.get(field)
            rule_values[field] = (
                self.status_of(value) if field == RecordField.STATUS else value
            )
        return rule_values

    def _coerce_value(
        self,
        field: RecordField,
        value: Any,
        current_value: Any = None,
    ) -> Value | None:
        if value is None:
            return None
        if field == RecordField.STATUS:
            if isinstance(value, State):
                value = self.target_state_for_status(value.status)
            elif isinstance(value, Status):
                value = self.target_state_for_status(value)
        if isinstance(value, datetime | date):
            return self._coerce_temporal(field, value)
        if field == RecordField.PROGRESS and isinstance(value, Progress):
            constraint = self._constraint(field, ProgressConstraint)
            if constraint is None:
                return value
            current = current_value if isinstance(current_value, Progress) else None
            coerced_current = (
                self._coerce_progress_current(value.current, constraint.current)
                if constraint.current is not None
                else value.current
            )
            return Progress(
                current=coerced_current,
                total=value.total
                if constraint.total
                else (current.total if current else None),
                unit=(
                    value.unit
                    if constraint.unit
                    else (current.unit if current else None)
                ),
            )
        if field == RecordField.RATING and isinstance(value, Rating):
            return self._coerce_rating(value)
        if field == RecordField.NOTES and isinstance(value, str):
            limit = self._constraint(field, TextConstraint)
            return value[: limit.max_length] if limit and limit.max_length else value
        if (
            field == RecordField.REPEAT_COUNT
            and isinstance(value, int | float)
            and not isinstance(value, bool)
        ):
            return round(self._coerce_number(field, float(value)))
        return value

    def _coerce_progress_current(
        self,
        value: int | float | None,
        constraint: NumericConstraint,
    ) -> int | float | None:
        """Coerce progress counts without marking partial target units complete."""
        if value is None:
            return None
        coerced = float(value)
        if constraint.minimum is not None:
            coerced = max(coerced, constraint.minimum)
        if constraint.maximum is not None:
            coerced = min(coerced, constraint.maximum)
        if constraint.step is not None:
            origin = constraint.minimum or 0
            coerced = (
                floor((coerced - origin) / constraint.step + 1e-9) * constraint.step
                + origin
            )
            if constraint.minimum is not None:
                coerced = max(coerced, constraint.minimum)
            if constraint.maximum is not None:
                coerced = min(coerced, constraint.maximum)
        return int(coerced) if coerced.is_integer() else coerced

    def _coerce_temporal(
        self,
        field: RecordField,
        value: date | datetime,
    ) -> date | datetime:
        if isinstance(value, datetime) and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ValueError(f"{field.value} must be timezone-aware")
        constraint = self._constraint(field, TemporalConstraint)
        if constraint is not None and constraint.precision == TemporalPrecision.DATE:
            return value.date() if isinstance(value, datetime) else value
        if not isinstance(value, datetime):
            return datetime(value.year, value.month, value.day, tzinfo=UTC)
        value = value.astimezone(UTC)
        return value

    def _coerce_rating(self, value: Rating) -> Rating:
        constraint = self._constraint(RecordField.RATING, NumericConstraint)
        if constraint is None:
            return value

        source_min, source_max, source_step = value.scale
        target_min = (
            constraint.minimum if constraint.minimum is not None else source_min
        )
        target_max = (
            constraint.maximum if constraint.maximum is not None else source_max
        )
        target_step = constraint.step if constraint.step is not None else source_step
        translated = (
            target_min
            if source_max == source_min
            else target_min
            + ((value.value - source_min) / (source_max - source_min))
            * (target_max - target_min)
        )
        coerced = self._coerce_number(
            RecordField.RATING,
            translated,
            NumericConstraint(target_min, target_max, target_step),
        )
        return Rating(
            float(coerced),
            (float(target_min), float(target_max), target_step),
        )

    def _coerce_number(
        self,
        field: RecordField,
        value: float,
        constraint: NumericConstraint | None = None,
    ) -> float:
        constraint = constraint or self._constraint(field, NumericConstraint)
        if constraint is None:
            return value
        if constraint.minimum is not None:
            value = max(value, constraint.minimum)
        if constraint.maximum is not None:
            value = min(value, constraint.maximum)
        if constraint.step is not None:
            origin = constraint.minimum or 0
            value = round((value - origin) / constraint.step) * constraint.step + origin
            if constraint.minimum is not None:
                value = max(value, constraint.minimum)
            if constraint.maximum is not None:
                value = min(value, constraint.maximum)
        return value

    def _constraint(
        self,
        field: RecordField,
        constraint_type: type[_ConstraintT],
    ) -> _ConstraintT | None:
        spec = self.target_capabilities.record_fields.get(field)
        if not isinstance(spec, FieldSpec):
            return None
        for constraint in spec.constraints:
            if isinstance(constraint, constraint_type):
                return constraint
        return None

    def _values_equal(self, field: RecordField, current: Any, new: Any) -> bool:
        if (
            field == RecordField.PROGRESS
            and isinstance(current, Progress)
            and isinstance(new, Progress)
        ):
            constraint = self._constraint(field, ProgressConstraint)
            current_tuple = [current.current]
            new_tuple = [new.current]
            if constraint is None or constraint.total:
                current_tuple.append(current.total)
                new_tuple.append(new.total)
            if constraint is None or constraint.unit:
                current_tuple.append(current.unit)
                new_tuple.append(new.unit)
            return current_tuple == new_tuple
        return current == new

    def _status_gate(self, field: RecordField, status: Status | None) -> str | None:
        if (
            field in (RecordField.REPEAT_COUNT, RecordField.FINISHED_AT)
            and status is not None
            and _STATUS_ORDER[status] < _STATUS_ORDER[Status.COMPLETED]
        ):
            return "requires_completed"
        if (
            field == RecordField.STARTED_AT
            and status is not None
            and _STATUS_ORDER[status] <= _STATUS_ORDER[Status.PLANNED]
        ):
            return "requires_active_status"
        return None

    def _project_progress(
        self,
        progress: Progress,
        mappings: Sequence[AnibridgeMapping],
    ) -> Progress:
        total: float | None = 0.0
        for mapping in mappings:
            for target_range in mapping.target_ranges:
                if target_range.length is None:
                    total = None
                    break
                total += target_range.length
            if total is None:
                break

        current = 0.0
        source_current = max(float(progress.current or 0), 0.0)
        watched = floor(source_current)
        if watched:
            current += self._project_whole_progress(watched, mappings)

        fractional = source_current - watched
        if fractional:
            # Fractional progress belongs to the next source unit, so weight it by
            # that unit's active mapping instead of the last completed range.
            mapping = self._best_mapping_for_index(watched + 1, mappings)
            if mapping is not None:
                current += mapping.source_weight * fractional

        if total is not None:
            current = min(current, total)
        projected_total = (
            int(total) if total is not None and total.is_integer() else total
        )
        return Progress(
            current=int(current) if current.is_integer() else current,
            total=projected_total if total is not None else progress.total,
            unit=progress.unit,
        )

    def _project_whole_progress(
        self,
        watched: int,
        mappings: Sequence[AnibridgeMapping],
    ) -> float:
        """Project complete source units without iterating one unit at a time."""
        boundaries = {1, watched + 1}
        for mapping in mappings:
            start = max(mapping.source_range.start, 1)
            if start > watched:
                continue
            end = mapping.source_range.end
            bounded_end = watched if end is None else min(end, watched)
            if bounded_end < start:
                continue
            boundaries.add(start)
            boundaries.add(bounded_end + 1)

        current = 0.0
        ordered = sorted(boundaries)
        for start, next_start in pairwise(ordered):
            if start > watched:
                break
            end = min(next_start - 1, watched)
            if end < start:
                continue
            mapping = self._best_mapping_for_index(start, mappings)
            if mapping is not None:
                current += (end - start + 1) * mapping.source_weight
        return current

    @staticmethod
    def _best_mapping_for_index(
        index: int,
        mappings: Sequence[AnibridgeMapping],
    ) -> AnibridgeMapping | None:
        """Return the most specific mapping covering one source index."""
        return min(
            (
                candidate
                for candidate in mappings
                if candidate.source_range.contains(index)
            ),
            key=lambda candidate: candidate.source_range.length or float("inf"),
            default=None,
        )

    @staticmethod
    def _status_semantics(spec: FieldSpec | None) -> frozenset[Status]:
        if not isinstance(spec, FieldSpec):
            return frozenset()
        return frozenset(
            descriptor.semantic
            for descriptor in spec.values
            if descriptor.semantic is not None
        )
