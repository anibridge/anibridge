"""Record planning for the sync engine."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
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
    RecordSpec,
    RecordUnit,
    Ref,
    Role,
    ScanItem,
    State,
    Status,
    SupportsMapping,
    SupportsReads,
    SupportsScan,
    SupportsWrites,
    TemporalConstraint,
    TemporalPrecision,
    TextConstraint,
    UpsertRecord,
    Value,
    WriteAction,
)
from anibridge.utils.mappings import AnibridgeMapping

from anibridge.app.core.sync.history import to_builtins
from anibridge.app.core.sync.projection import MappingProjector
from anibridge.app.core.sync.rules import SyncRuleEngine, build_rule_context
from anibridge.app.core.sync.stats import (
    AppliedRule,
    FieldBlock,
    FieldChange,
    MappingRange,
    PlanDiagnostics,
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
_TEMPORAL_RECORD_FIELDS = (
    RecordField.STARTED_AT,
    RecordField.FINISHED_AT,
    RecordField.LAST_ACTIVITY_AT,
)


def _record_specs(capabilities: Capabilities) -> tuple[RecordSpec, ...]:
    """Return record resource specs from unified provider capabilities."""
    return tuple(spec for spec in capabilities.specs if isinstance(spec, RecordSpec))


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

        source_record_specs = _record_specs(source_capabilities)
        target_record_specs = _record_specs(target_capabilities)
        self._source_record_specs = {spec.name: spec for spec in source_record_specs}
        self._target_record_specs = {spec.name: spec for spec in target_record_specs}
        self._surface_fields: dict[tuple[str, str], tuple[RecordField, ...]] = {}

        self._target_surface_for_source: dict[str, str] = {}
        for source_kind, source_spec in self._source_record_specs.items():
            candidates: list[tuple[int, int, str]] = []
            for index, target_spec in enumerate(target_record_specs):
                fields = self._sync_fields_for_specs(source_spec, target_spec)
                self._surface_fields[(source_kind, target_spec.name)] = fields
                if fields and WriteAction.UPSERT in target_spec.write_actions:
                    candidates.append((len(fields), -index, target_spec.name))
            if candidates:
                self._target_surface_for_source[source_kind] = max(candidates)[2]
        self.sync_fields = tuple(
            field
            for field in RecordField
            if any(
                field in self._surface_fields[(source_kind, target_kind)]
                for source_kind, target_kind in self._target_surface_for_source.items()
            )
        )

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
            (isinstance(target_provider, SupportsReads), "record reads"),
            (isinstance(target_provider, SupportsWrites), "record writes"),
            (bool(self._source_record_specs), "source record surfaces"),
            (bool(self._target_record_specs), "target record surfaces"),
            (
                any(
                    WriteAction.UPSERT in spec.write_actions
                    for spec in self._target_record_specs.values()
                ),
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
        """Return source record kinds that the target can represent."""
        return tuple(self._target_surface_for_source)

    def can_delete_record(self, target_kind: str) -> bool:
        """Return whether a target record surface supports deletion."""
        spec = self._target_record_specs.get(target_kind)
        return spec is not None and WriteAction.DELETE in spec.write_actions

    def target_record_surface_for(self, source_kind: str) -> str | None:
        """Return the best target surface for one source record surface."""
        return self._target_surface_for_source.get(source_kind)

    def syncable_source_records(self, item: ScanItem) -> tuple[Record, ...]:
        """Return scanned records that should drive sync."""
        return tuple(
            record
            for record in item.records
            if self.target_record_surface_for(record.surface)
            and (record.values or self.destructive_sync)
        )

    def project_source_record(
        self,
        source_record: Record,
        *,
        mappings: Sequence[AnibridgeMapping],
    ) -> Record:
        """Project source record values into the resolved target's mapped unit space."""
        if not mappings:
            return source_record

        values = dict(source_record.values)
        projector = MappingProjector(mappings)
        progress = source_record.values.get(RecordField.PROGRESS)
        if isinstance(progress, Progress):
            projected = Progress(
                current=projector.target_progress(progress.current),
                total=projector.target_total(progress.total),
                unit=progress.unit,
            )
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

        self._project_temporal_fields(values, source_record.units, projector)

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
        mappings: Sequence[MappingRange] = (),
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
        sync_fields = self.sync_fields_for(source_record.surface, target_kind)
        current_values = self._rule_values(target_values, sync_fields)
        computed_values = self._rule_values(source_record.values, sync_fields)
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

        for field in sync_fields:
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
                and self.status_of(rule.value)
                not in self._status_semantics(
                    self._field_spec(
                        self._target_record_specs.get(target_kind), RecordField.STATUS
                    )
                )
            ):
                blocked_fields.append(FieldBlock(field, "unsupported_status"))
                continue

            new_value = self._coerce_value(
                field,
                rule.value,
                current_values.get(field),
                target_kind,
            )

            current_value = current_values.get(field)
            if self._values_equal(field, current_value, new_value, target_kind):
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
            surface=target_kind,
            key=target_record.key if target_record else None,
            values=planned_values,
        )
        after = RecordSnapshot.from_record(after_record)
        diff = RecordSnapshot.diff_optional(before, after, changed_fields)
        if not diff:
            return SyncOutcome.SKIPPED

        write = UpsertRecord(
            ref=target_ref,
            surface=target_kind,
            key=target_record.key if target_record else None,
            expected_revision=target_record.revision if target_record else None,
            set={
                field: planned_values[field]
                for field in sync_fields
                if field in changed_fields and field in planned_values
            },
            clear=frozenset(clear_fields),
        )
        diagnostics = PlanDiagnostics(
            blocked_fields=tuple(blocked_fields),
            applied_rules=tuple(applied_rules),
            mappings=tuple(mappings),
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

    def target_state_for_status(self, status: Status | None, target_kind: str) -> State:
        """Return the target-native state for a normalized status."""
        if status is None:
            raise ValueError("Cannot build target state for an empty status")

        spec = self._field_spec(
            self._target_record_specs.get(target_kind), RecordField.STATUS
        )
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

    def format_diff(self, diff: Sequence[FieldChange]) -> str:
        """Format a diff dictionary for logging."""
        parts: list[str] = []
        for change in sorted(diff, key=lambda item: item.field.value):
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

    def sync_fields_for(
        self,
        source_kind: str,
        target_kind: str,
    ) -> tuple[RecordField, ...]:
        """Return fields syncable between two record surfaces."""
        return self._surface_fields.get((source_kind, target_kind), ())

    def _sync_fields_for_specs(
        self,
        source_spec: RecordSpec,
        target_spec: RecordSpec,
    ) -> tuple[RecordField, ...]:
        """Return fields syncable between two record specs."""
        fields: list[RecordField] = []
        for field in RecordField:
            if self.sync_rule_engine.is_disabled(field):
                continue
            source_field = self._field_spec(source_spec, field)
            if not isinstance(source_field, FieldSpec) or not source_field.readable:
                continue
            target_field = self._field_spec(target_spec, field)
            if not isinstance(target_field, FieldSpec) or not target_field.writable:
                continue
            if field == RecordField.STATUS and not (
                self._status_semantics(source_field)
                & self._status_semantics(target_field)
            ):
                continue
            fields.append(field)
        return tuple(fields)

    def _rule_values(
        self,
        values: Mapping[RecordField, Value],
        fields: Sequence[RecordField],
    ) -> dict[RecordField, Any]:
        rule_values: dict[RecordField, Any] = {}
        for field in fields:
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
        target_kind: str = "",
    ) -> Value | None:
        if value is None:
            return None
        if field == RecordField.STATUS:
            if isinstance(value, State):
                value = self.target_state_for_status(value.status, target_kind)
            elif isinstance(value, Status):
                value = self.target_state_for_status(value, target_kind)
        if isinstance(value, datetime | date):
            return self._coerce_temporal(field, value, target_kind)
        if field == RecordField.PROGRESS and isinstance(value, Progress):
            constraint = self._constraint(field, ProgressConstraint, target_kind)
            if constraint is None:
                return value
            current = current_value if isinstance(current_value, Progress) else None
            coerced_current = (
                None
                if value.current is None
                else self._coerce_number(
                    value.current,
                    constraint.current,
                    floor_step=True,
                )
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
            return self._coerce_rating(value, target_kind)
        if field == RecordField.NOTES and isinstance(value, str):
            limit = self._constraint(field, TextConstraint, target_kind)
            return value[: limit.max_length] if limit and limit.max_length else value
        if (
            field == RecordField.REPEAT_COUNT
            and isinstance(value, int | float)
            and not isinstance(value, bool)
        ):
            constraint = self._constraint(field, NumericConstraint, target_kind)
            coerced = self._coerce_number(value, constraint) if constraint else value
            return round(coerced)
        return value

    @staticmethod
    def _coerce_number(
        value: int | float,
        constraint: NumericConstraint,
        *,
        floor_step: bool = False,
    ) -> int | float:
        coerced = float(value)
        if constraint.minimum is not None:
            coerced = max(coerced, constraint.minimum)
        if constraint.maximum is not None:
            coerced = min(coerced, constraint.maximum)
        if constraint.step is not None:
            origin = constraint.minimum or 0
            steps = (coerced - origin) / constraint.step
            stepped = floor(steps + 1e-9) if floor_step else round(steps)
            coerced = stepped * constraint.step + origin
            coerced = max(
                coerced,
                constraint.minimum if constraint.minimum is not None else coerced,
            )
            coerced = min(
                coerced,
                constraint.maximum if constraint.maximum is not None else coerced,
            )
        return int(coerced) if coerced.is_integer() else coerced

    def _coerce_temporal(
        self,
        field: RecordField,
        value: date | datetime,
        target_kind: str,
    ) -> date | datetime:
        if isinstance(value, datetime) and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ValueError(f"{field.value} must be timezone-aware")
        constraint = self._constraint(field, TemporalConstraint, target_kind)
        if constraint is not None and constraint.precision == TemporalPrecision.DATE:
            return value.date() if isinstance(value, datetime) else value
        if not isinstance(value, datetime):
            return datetime(value.year, value.month, value.day, tzinfo=UTC)
        value = value.astimezone(UTC)
        return value

    def _coerce_rating(self, value: Rating, target_kind: str) -> Rating:
        constraint = self._constraint(
            RecordField.RATING, NumericConstraint, target_kind
        )
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
            translated,
            NumericConstraint(target_min, target_max, target_step),
        )
        return Rating(
            float(coerced),
            (float(target_min), float(target_max), target_step),
        )

    def _constraint(
        self,
        field: RecordField,
        constraint_type: type[_ConstraintT],
        target_kind: str = "",
    ) -> _ConstraintT | None:
        record_spec = self._target_record_specs.get(target_kind)
        field_spec = self._field_spec(record_spec, field)
        if not isinstance(field_spec, FieldSpec):
            return None
        for constraint in field_spec.constraints:
            if isinstance(constraint, constraint_type):
                return constraint
        return None

    @staticmethod
    def _field_spec(
        record_spec: RecordSpec | None, field: RecordField
    ) -> FieldSpec | None:
        if record_spec is None:
            return None
        spec = record_spec.fields.get(field)
        return spec if isinstance(spec, FieldSpec) else None

    def _values_equal(
        self,
        field: RecordField,
        current: Any,
        new: Any,
        target_kind: str,
    ) -> bool:
        if field == RecordField.PROGRESS:
            if (
                current is None
                or (
                    isinstance(current, Progress)
                    and (current.current is None or current.current == 0)
                )
            ) and (
                new is None
                or (
                    isinstance(new, Progress)
                    and (new.current is None or new.current == 0)
                )
            ):
                return True
            if not isinstance(current, Progress) or not isinstance(new, Progress):
                return current == new

            constraint = self._constraint(field, ProgressConstraint, target_kind)
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

    def _project_temporal_fields(
        self,
        values: dict[RecordField, Value],
        units: Sequence[RecordUnit],
        projector: MappingProjector,
    ) -> None:
        """Replace aggregate temporal fields with mapped-unit temporal values."""
        if not units:
            return

        scoped_units = tuple(
            unit
            for unit in units
            if any(
                mapping.source_range.contains(unit.index)
                for mapping in projector.mappings
            )
        )
        for field in _TEMPORAL_RECORD_FIELDS:
            temporal_values = tuple(
                value
                for unit in scoped_units
                if isinstance(value := unit.values.get(field), date)
            )
            if not temporal_values:
                values.pop(field, None)
                continue
            values[field] = (
                min(temporal_values)
                if field == RecordField.STARTED_AT
                else max(temporal_values)
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
