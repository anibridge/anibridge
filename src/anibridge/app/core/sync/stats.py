"""Synchronization statistics and planning value objects."""

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Literal

import msgspec
from anibridge.provider.base import (
    FacetName,
    Node,
    Part,
    Progress,
    Rating,
    Record,
    RecordField,
    Ref,
    Scalar,
    ScanItem,
    State,
    Structure,
    Value,
    Write,
)
from anibridge.utils.mappings import AnibridgeMapping

from anibridge.app.models.db.sync_history import SyncOutcome
from anibridge.app.utils.terminal import ARROW

__all__ = [
    "AppliedRule",
    "FieldBlock",
    "FieldChange",
    "MappingRange",
    "PlanDiagnostics",
    "RecordPlan",
    "RecordSnapshot",
    "RecordSnapshotValue",
    "SyncItem",
    "SyncProgress",
    "SyncStats",
]


class RecordSnapshotValue(msgspec.Struct, frozen=True, omit_defaults=True):
    """Restorable record value captured in history."""

    state: State | None = None
    progress: Progress | None = None
    rating: Rating | None = None
    scalar: Scalar | None = None
    date_value: date | None = None
    datetime_value: datetime | None = None

    @classmethod
    def from_value(cls, value: Value) -> RecordSnapshotValue:
        """Wrap a provider value in a typed snapshot payload."""
        if isinstance(value, State):
            return cls(state=value)
        if isinstance(value, Progress):
            return cls(progress=value)
        if isinstance(value, Rating):
            return cls(rating=value)
        if isinstance(value, datetime):
            return cls(datetime_value=value)
        if isinstance(value, date):
            return cls(date_value=value)
        return cls(scalar=value)

    def to_record_value(self) -> Value:
        """Return the provider value represented by this snapshot value."""
        for value in (
            self.state,
            self.progress,
            self.rating,
            self.scalar,
            self.date_value,
            self.datetime_value,
        ):
            if value is not None:
                return value
        raise ValueError("Record snapshot value is empty")

    def as_display_value(self) -> object | None:
        """Return the compact history display value."""
        value = self.to_record_value()
        if isinstance(value, State):
            return value.status.value if value.status is not None else None
        if isinstance(value, Progress):
            return value.current
        if isinstance(value, Rating):
            return value.value
        return value


class SyncItem(msgspec.Struct, frozen=True):
    """Stable identifier for a provider ref participating in sync."""

    namespace: str
    ref: Ref
    repr: str

    @classmethod
    def from_record_parts(
        cls,
        *,
        namespace: str,
        node: Node,
        record: Record,
    ) -> tuple[SyncItem, ...]:
        """Create coverage identifiers for a scanned record."""
        parts: tuple[Part, ...] = ()
        progress = record.values.get(RecordField.PROGRESS)
        structure = node.facets.get(FacetName.STRUCTURE)
        if isinstance(progress, Progress) and isinstance(structure, Structure):
            if record.ref.path:
                parent = record.ref.path
                parts = tuple(
                    part
                    for part in structure.parts
                    if len(parent) < len(child := part.position)
                    and child[: len(parent)] == parent
                )
            else:
                parts = structure.parts
        refs = (
            ((record.ref, None),)
            if not parts
            else tuple((Ref(record.ref.key, part.position), part) for part in parts)
        )
        items: list[SyncItem] = []
        for ref, part in refs:
            label = node.title or record.ref.key
            if part is not None and part.title:
                label = f"{label} - {part.title}"
            label = label.replace("\\", "\\\\").replace('"', '\\"')
            label = label[:35] + "…" if len(label) > 35 else label
            path = "/".join(
                f"{axis}={value}"
                for axis, value in (
                    (node.kind, ref.key),
                    *((step.axis, step.value) for step in ref.path),
                )
            )
            items.append(
                cls(
                    namespace=namespace,
                    ref=ref,
                    repr=f'<{namespace}:{path} "{label}">',
                )
            )
        return tuple(items)

    def __hash__(self) -> int:
        """Hash by provider namespace and normalized ref."""
        return hash((self.namespace, self.ref))

    def __eq__(self, other: object) -> bool:
        """Compare by provider namespace and normalized ref only."""
        if not isinstance(other, SyncItem):
            return NotImplemented
        return (self.namespace, self.ref) == (other.namespace, other.ref)

    def __repr__(self) -> str:
        """Return a readable item label for logs."""
        return self.repr or super().__repr__()


class SyncStats(msgspec.Struct):
    """Outcome tracker for a sync cycle."""

    _item_outcomes: dict[SyncItem, SyncOutcome] = msgspec.field(default_factory=dict)

    def track_item(self, item_id: SyncItem, outcome: SyncOutcome) -> None:
        """Track the outcome for one item."""
        self._item_outcomes[item_id] = outcome

    def register_pending_items(self, item_ids: Iterable[SyncItem]) -> None:
        """Mark unprocessed items as pending."""
        for item_id in item_ids:
            self._item_outcomes.setdefault(item_id, SyncOutcome.PENDING)

    def items(self, *outcomes: SyncOutcome) -> list[SyncItem]:
        """Return tracked items matching optional outcomes."""
        allowed = set(outcomes)
        return [
            item
            for item, outcome in self._item_outcomes.items()
            if not allowed or outcome in allowed
        ]

    def count(self, *outcomes: SyncOutcome) -> int:
        """Count tracked items matching optional outcomes."""
        return len(self.items(*outcomes))

    @property
    def synced(self) -> int:
        """Number of refs successfully synced."""
        return self.count(SyncOutcome.SYNCED)

    @property
    def deleted(self) -> int:
        """Number of target records deleted."""
        return self.count(SyncOutcome.DELETED)

    @property
    def skipped(self) -> int:
        """Number of refs skipped."""
        return self.count(SyncOutcome.SKIPPED)

    @property
    def not_found(self) -> int:
        """Number of refs without target matches."""
        return self.count(SyncOutcome.NOT_FOUND)

    @property
    def failed(self) -> int:
        """Number of refs that failed."""
        return self.count(SyncOutcome.FAILED)

    @property
    def coverage(self) -> float:
        """Percentage of refs with a covered outcome."""
        total = self.count()
        if not total:
            return 1.0
        processed = self.count(
            SyncOutcome.SYNCED,
            SyncOutcome.SKIPPED,
            SyncOutcome.DELETED,
        )
        return processed / total


class SyncProgress(msgspec.Struct):
    """Live sync progress snapshot exposed to the web UI."""

    state: Literal["running", "idle"]
    started_at: datetime
    stage: str
    source_namespace: str
    target_namespace: str
    trigger: str
    scanned_items: int
    processed_items: int
    total_items: int | None = None


class FieldChange(msgspec.Struct, frozen=True):
    """One changed record field."""

    field: RecordField
    before: object | None
    after: object | None


class FieldBlock(msgspec.Struct, frozen=True):
    """Reason a field was not updated."""

    field: RecordField
    reason: str


class AppliedRule(msgspec.Struct, frozen=True):
    """Rule that changed a planned field value."""

    field: RecordField
    name: str


class MappingRange(msgspec.Struct, frozen=True):
    """Mapped source and target progress range."""

    source_mapping_descriptor: str
    target_mapping_descriptor: str
    mapping: AnibridgeMapping

    @property
    def source(self) -> str:
        """Return the serialized source range."""
        return self.mapping.source_key

    @property
    def target(self) -> str:
        """Return the serialized target range."""
        return self.mapping.target_value


class PlanDiagnostics(msgspec.Struct, frozen=True):
    """Typed planning diagnostics for history metadata."""

    blocked_fields: tuple[FieldBlock, ...] = ()
    applied_rules: tuple[AppliedRule, ...] = ()
    mappings: tuple[MappingRange, ...] = ()

    def as_info(self) -> dict[str, str]:
        """Return the JSON-friendly history metadata shape."""
        return {
            "field_blocks": ", ".join(
                f"{item.field.value}({item.reason})"
                for item in sorted(
                    self.blocked_fields,
                    key=lambda item: item.field.value,
                )
            ),
            "applied_rules": ", ".join(
                f"{item.field.value}({item.name})"
                for item in sorted(
                    self.applied_rules,
                    key=lambda item: item.field.value,
                )
            ),
            "mapping_ranges": ", ".join(
                f"{item.source_mapping_descriptor}@{item.source} {ARROW} "
                f"{item.target_mapping_descriptor}@{item.target}"
                for item in self.mappings
            ),
        }


class RecordSnapshot(msgspec.Struct, frozen=True):
    """Planner/history snapshot of one normalized record."""

    ref: Ref
    surface: str = ""
    key: str | None = None
    ids: tuple[str, ...] = ()
    values: Mapping[RecordField, RecordSnapshotValue] = msgspec.field(
        default_factory=dict
    )

    @classmethod
    def from_record(cls, record: Record) -> RecordSnapshot:
        """Create a snapshot from a provider record."""
        return cls(
            ref=record.ref,
            surface=record.surface,
            key=record.key,
            ids=tuple(item.descriptor for item in record.ids),
            values={
                field: RecordSnapshotValue.from_value(value)
                for field, value in record.values.items()
            },
        )

    def values_for_restore(self) -> dict[RecordField, Value]:
        """Return provider values that can restore the target state."""
        return {field: value.to_record_value() for field, value in self.values.items()}

    def values_for_display(self) -> dict[str, object]:
        """Return compact values for diffs and UI display."""
        values: dict[str, object] = {}
        for field, value in self.values.items():
            display_value = value.as_display_value()
            if display_value is not None:
                values[field.value] = display_value
        return values

    def diff(
        self,
        after: RecordSnapshot,
        fields: Iterable[RecordField],
    ) -> tuple[FieldChange, ...]:
        """Compare this snapshot to another snapshot."""
        changes: list[FieldChange] = []
        before_values = self.values_for_display()
        after_values = after.values_for_display()
        for field in fields:
            before_value = before_values.get(field.value)
            after_value = after_values.get(field.value)
            if before_value != after_value:
                changes.append(FieldChange(field, before_value, after_value))
        return tuple(changes)

    @classmethod
    def diff_optional(
        cls,
        before: RecordSnapshot | None,
        after: RecordSnapshot,
        fields: Iterable[RecordField],
    ) -> tuple[FieldChange, ...]:
        """Compare optional before state to a required after state."""
        if before is None:
            before = cls(ref=after.ref, surface=after.surface, key=after.key)
        return before.diff(after, fields)


class RecordPlan(msgspec.Struct):
    """Normalized record write with history metadata."""

    item: ScanItem
    source_record: Record | None
    before: RecordSnapshot | None
    after: RecordSnapshot | None
    write: Write
    target_ref: Ref | None
    diagnostics: PlanDiagnostics = msgspec.field(default_factory=PlanDiagnostics)
