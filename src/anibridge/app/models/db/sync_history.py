"""Sync history database model."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.schema import ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.sql.sqltypes import JSON, Boolean, DateTime, Enum, Integer, String

from anibridge.app.models.db.base import Base

__all__ = [
    "SyncHistoryGroup",
    "SyncHistoryOperation",
    "SyncHistoryRun",
    "SyncOperationAction",
    "SyncOutcome",
    "SyncResourceKind",
]


class SyncOutcome(StrEnum):
    """Enumeration of possible synchronization outcomes for media items."""

    SYNCED = "synced"
    SKIPPED = "skipped"
    FAILED = "failed"
    NOT_FOUND = "not_found"
    DELETED = "deleted"
    PENDING = "pending"
    UNDONE = "undone"


class SyncResourceKind(StrEnum):
    """Resource kind represented by one history operation."""

    RECORD = "record"
    EVENT = "event"
    NODE = "node"


class SyncOperationAction(StrEnum):
    """Provider mutation represented by one history operation."""

    UPSERT = "upsert"
    DELETE = "delete"
    UNDO = "undo"


class SyncHistoryRun(Base):
    """A single sync attempt for one profile."""

    __tablename__ = "sync_history_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_name: Mapped[str] = mapped_column(String, index=True)
    source_namespace: Mapped[str] = mapped_column(String, index=True)
    target_namespace: Mapped[str] = mapped_column(String, index=True)
    trigger: Mapped[str | None] = mapped_column(String, default=None, nullable=True)
    source: Mapped[str | None] = mapped_column(String, default=None, nullable=True)
    outcome: Mapped[SyncOutcome | None] = mapped_column(
        Enum(SyncOutcome), default=None, nullable=True, index=True
    )
    info: Mapped[dict[str, str] | None] = mapped_column(
        JSON, default=dict, nullable=True
    )
    ephemeral: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, nullable=True, index=True
    )

    groups: Mapped[list[SyncHistoryGroup]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_sync_history_run_profile_started", "profile_name", "started_at"),
    )


class SyncHistoryGroup(Base):
    """Run-scoped history group for one source parent item."""

    __tablename__ = "sync_history_group"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("sync_history_run.id", ondelete="CASCADE"), index=True
    )
    profile_name: Mapped[str] = mapped_column(String, index=True)

    source_namespace: Mapped[str] = mapped_column(String, index=True)
    source_parent_ref: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)

    target_namespace: Mapped[str] = mapped_column(String, index=True)
    target_parent_ref: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True), default=None, nullable=True
    )

    animap_authority: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )
    animap_value: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    animap_scope: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    outcome: Mapped[SyncOutcome] = mapped_column(Enum(SyncOutcome), index=True)
    operation_count: Mapped[int] = mapped_column(Integer, default=0)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)

    info: Mapped[dict[str, str] | None] = mapped_column(
        JSON, default=dict, nullable=True
    )
    ephemeral: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), index=True
    )

    run: Mapped[SyncHistoryRun] = relationship(back_populates="groups")
    operations: Mapped[list[SyncHistoryOperation]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SyncHistoryOperation.timestamp",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["animap_authority", "animap_value", "animap_scope"],
            [
                "animap_entry.authority",
                "animap_entry.value",
                "animap_entry.scope",
            ],
            ondelete="SET NULL",
            onupdate="CASCADE",
        ),
        UniqueConstraint(
            "run_id",
            "source_namespace",
            "source_parent_ref",
            "target_namespace",
            "target_parent_ref",
            "animap_authority",
            "animap_value",
            "animap_scope",
            name="uq_sync_history_group_run_parent",
        ),
        Index("ix_sync_history_group_profile_timestamp", "profile_name", "timestamp"),
        Index(
            "ix_sync_history_group_profile_scope_outcome",
            "profile_name",
            "source_namespace",
            "target_namespace",
            "outcome",
        ),
    )


class SyncHistoryOperation(Base):
    """One record, event, or node mutation within a history group."""

    __tablename__ = "sync_history_operation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("sync_history_group.id", ondelete="CASCADE"), index=True
    )
    profile_name: Mapped[str] = mapped_column(String, index=True)

    resource_kind: Mapped[SyncResourceKind] = mapped_column(
        Enum(SyncResourceKind), index=True
    )
    action: Mapped[SyncOperationAction] = mapped_column(
        Enum(SyncOperationAction), index=True
    )

    source_namespace: Mapped[str] = mapped_column(String, index=True)
    source_ref: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)

    target_namespace: Mapped[str] = mapped_column(String, index=True)
    target_ref: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True), default=None, nullable=True
    )

    source_surface: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )
    target_surface: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )
    resource_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    outcome: Mapped[SyncOutcome] = mapped_column(Enum(SyncOutcome), index=True)

    before_state: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=dict, nullable=True
    )
    after_state: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=dict, nullable=True
    )
    # Human-readable debug metadata only. Runtime behavior does not depend on it.
    info: Mapped[dict[str, str] | None] = mapped_column(
        JSON, default=dict, nullable=True
    )

    error_message: Mapped[str | None] = mapped_column(
        String, default=None, nullable=True
    )
    ephemeral: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), index=True
    )

    group: Mapped[SyncHistoryGroup] = relationship(back_populates="operations")

    __table_args__ = (
        Index(
            "ix_sync_history_operation_group_resource_outcome",
            "group_id",
            "resource_kind",
            "outcome",
        ),
        Index(
            "ix_sync_history_operation_profile_resource_timestamp",
            "profile_name",
            "resource_kind",
            "timestamp",
        ),
    )
