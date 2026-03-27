"""Sync History Database Model."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from anibridge.library import MediaKind
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import ForeignKey, Index
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
    """Model for tracking individual item sync operations."""

    __tablename__ = "sync_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_name: Mapped[str] = mapped_column(String, index=True)

    library_namespace: Mapped[str] = mapped_column(String, index=True)
    library_section_key: Mapped[str] = mapped_column(String, index=True)
    library_media_key: Mapped[str] = mapped_column(String, index=True)

    list_namespace: Mapped[str] = mapped_column(String, index=True)
    list_media_key: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )

    animap_entry_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("animap_entry.id", ondelete="SET NULL", onupdate="CASCADE"),
        nullable=True,
        index=True,
    )

    media_kind: Mapped[MediaKind] = mapped_column(Enum(MediaKind), index=True)
    outcome: Mapped[SyncOutcome] = mapped_column(Enum(SyncOutcome), index=True)

    before_state: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=dict, nullable=True
    )
    after_state: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=dict, nullable=True
    )
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

    __table_args__ = (
        Index("ix_sync_history_profile_timestamp", "profile_name", "timestamp"),
        Index(
            "ix_sync_history_profile_library_media_outcome",
            "profile_name",
            "library_namespace",
            "library_section_key",
            "library_media_key",
            "outcome",
        ),
    )
