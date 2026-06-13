"""Sync history database model."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import ForeignKeyConstraint, Index
from sqlalchemy.sql.sqltypes import JSON, Boolean, DateTime, Enum, Integer, String

from anibridge.app.models.db.base import Base

__all__ = ["SyncHistory", "SyncOutcome"]


class SyncOutcome(StrEnum):
    """Enumeration of possible synchronization outcomes for media items."""

    SYNCED = "synced"
    SKIPPED = "skipped"
    FAILED = "failed"
    NOT_FOUND = "not_found"
    DELETED = "deleted"
    PENDING = "pending"
    UNDONE = "undone"


class SyncHistory(Base):
    """Model for tracking normalized sync operations."""

    __tablename__ = "sync_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_name: Mapped[str] = mapped_column(String, index=True)

    source_namespace: Mapped[str] = mapped_column(String, index=True)
    source_ref: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)

    target_namespace: Mapped[str] = mapped_column(String, index=True)
    target_ref: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True), default=None, nullable=True
    )

    animap_provider: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )
    animap_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    animap_scope: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    outcome: Mapped[SyncOutcome] = mapped_column(Enum(SyncOutcome), index=True)

    before_state: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=dict, nullable=True
    )
    after_state: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=dict, nullable=True
    )
    info: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=dict, nullable=True
    )

    error_message: Mapped[str | None] = mapped_column(
        String, default=None, nullable=True
    )
    ephemeral: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), index=True
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["animap_provider", "animap_id", "animap_scope"],
            [
                "animap_entry.provider",
                "animap_entry.entry_id",
                "animap_entry.entry_scope",
            ],
            ondelete="SET NULL",
            onupdate="CASCADE",
        ),
        Index("ix_sync_history_profile_timestamp", "profile_name", "timestamp"),
        Index(
            "ix_sync_history_profile_source_outcome",
            "profile_name",
            "source_namespace",
            "outcome",
        ),
    )
