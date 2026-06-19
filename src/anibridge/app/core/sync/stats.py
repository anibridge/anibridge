"""Synchronization statistics and planning value objects."""

from collections.abc import Iterable, Mapping
from datetime import datetime
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
    RecordWrite,
    Ref,
    ScanItem,
    State,
    Structure,
    Value,
)

from anibridge.app.models.db.sync_history import SyncOutcome

__all__ = [
    "AppliedRule",
    "FieldBlock",
    "FieldChange",
    "MappingRange",
    "PlanDiagnostics",
    "RecordPlan",
    "RecordSnapshot",
    "SyncItem",
    "SyncProgress",
    "SyncStats",
]


class SyncItem(msgspec.Struct, frozen=True):
    """Stable identifier for a provider ref participating in sync."""

    namespace: str
    ref: Ref
    repr: str
    trackable: bool = True

    @classmethod
    def from_record_parts(
        cls,
        *,
        namespace: str,
        node: Node,
        record: Record,
    ) -> tuple[SyncItem, ...]:
        """Create coverage identifiers for a scanned record."""
        parts = cls.coverage_parts(node, record)
        if not parts:
            return (cls.from_record(namespace=namespace, node=node, record=record),)
        return tuple(
            cls.from_ref(
                namespace=namespace,
                node=node,
                record=record,
                ref=Ref(record.ref.key, part.position),
                part=part,
            )
            for part in parts
        )

    @classmethod
    def from_record(
        cls,
        *,
        namespace: str,
        node: Node,
        record: Record,
    ) -> SyncItem:
        """Create a coverage identifier for the record's own ref."""
        return cls.from_ref(
            namespace=namespace,
            node=node,
            record=record,
            ref=record.ref,
        )

    @classmethod
    def from_ref(
        cls,
        *,
        namespace: str,
        node: Node,
        record: Record,
        ref: Ref,
        part: Part | None = None,
    ) -> SyncItem:
        """Create an identifier from a concrete coverage ref."""
        label = cls.label(namespace, node, record, ref, part)
        return cls(
            namespace=namespace,
            ref=ref,
            repr=label,
        )

    @staticmethod
    def label(
        namespace: str,
        node: Node,
        record: Record,
        ref: Ref,
        part: Part | None,
    ) -> str:
        """Generate a label for a provider ref coordinate."""
        # Quote and truncate label
        label = node.title or record.ref.key
        label = label.replace("\\", "\\\\").replace('"', '\\"')
        label = label[:35] + "…" if len(label) > 35 else label

        segments = [
            (node.kind, ref.key),
            *((step.axis, step.value) for step in ref.path),
        ]
        path = "/".join(f"{axis}={value}" for axis, value in segments)
        return f'<{namespace}:{path} "{label}">'

    @staticmethod
    def coverage_parts(node: Node, record: Record) -> tuple[Part, ...]:
        """Return concrete parts represented by an aggregate source record."""
        progress = record.values.get(RecordField.PROGRESS)
        if not isinstance(progress, Progress):
            return ()
        structure = node.facets.get(FacetName.STRUCTURE)
        if not isinstance(structure, Structure) or not structure.parts:
            return ()
        if not record.ref.path:
            return structure.parts

        parent = record.ref.path
        return tuple(
            part
            for part in structure.parts
            # Keep parts whose position is a descendant of the record path.
            if len(parent) < len(child := part.position)
            and child[: len(parent)] == parent
        )

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

    def items(
        self,
        *outcomes: SyncOutcome,
        trackable: bool = False,
    ) -> list[SyncItem]:
        """Return tracked items matching optional outcomes."""
        allowed = set(outcomes)
        return [
            item
            for item, outcome in self._item_outcomes.items()
            if (not trackable or item.trackable) and (not allowed or outcome in allowed)
        ]

    def count(self, *outcomes: SyncOutcome, trackable: bool = False) -> int:
        """Count tracked items matching optional outcomes."""
        return len(self.items(*outcomes, trackable=trackable))

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
        """Percentage of trackable refs with a covered outcome."""
        total = self.count(trackable=True)
        if not total:
            return 1.0
        processed = self.count(
            SyncOutcome.SYNCED,
            SyncOutcome.SKIPPED,
            SyncOutcome.DELETED,
            trackable=True,
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

    source: str
    target: str


class PlanDiagnostics(msgspec.Struct, frozen=True):
    """Typed planning diagnostics for history metadata."""

    blocked_fields: tuple[FieldBlock, ...] = ()
    applied_rules: tuple[AppliedRule, ...] = ()
    mapping_ranges: tuple[MappingRange, ...] = ()

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
                f"{item.source}->{item.target}" for item in self.mapping_ranges
            ),
        }


class RecordSnapshot(msgspec.Struct, frozen=True):
    """Planner/history snapshot of one normalized record."""

    ref: Ref
    kind: str = ""
    key: str | None = None
    ids: tuple[str, ...] = ()
    values: Mapping[str, object] = msgspec.field(default_factory=dict)

    @classmethod
    def from_record(cls, record: Record) -> RecordSnapshot:
        """Create a snapshot from a provider record."""
        values: dict[str, object] = {}
        for field, value in record.values.items():
            snapshot_value = cls.snapshot_value(value)
            if snapshot_value is not None:
                values[field.value] = snapshot_value
        return cls(
            ref=record.ref,
            kind=record.kind,
            key=record.key,
            ids=tuple(item.descriptor for item in record.ids),
            values=values,
        )

    def diff(
        self,
        after: RecordSnapshot,
        fields: Iterable[RecordField],
    ) -> tuple[FieldChange, ...]:
        """Compare this snapshot to another snapshot."""
        changes: list[FieldChange] = []
        for field in fields:
            before_value = self.values.get(field.value)
            after_value = after.values.get(field.value)
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
            before = cls(ref=after.ref, kind=after.kind, key=after.key)
        return before.diff(after, fields)

    @staticmethod
    def snapshot_value(value: Value) -> object | None:
        """Reduce provider value objects to the history field value AniBridge owns."""
        if isinstance(value, State):
            return value.status.value if value.status is not None else None
        if isinstance(value, Progress):
            return value.current
        if isinstance(value, Rating):
            return value.value
        return value


class RecordPlan(msgspec.Struct):
    """Normalized record write with history metadata."""

    item: ScanItem
    source_record: Record | None
    before: RecordSnapshot | None
    after: RecordSnapshot | None
    write: RecordWrite
    target_ref: Ref | None
    diagnostics: PlanDiagnostics = msgspec.field(default_factory=PlanDiagnostics)
