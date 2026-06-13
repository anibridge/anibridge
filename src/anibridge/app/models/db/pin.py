"""Pin model for per-profile normalized record-field pinning."""

from datetime import UTC, datetime

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import Index, UniqueConstraint
from sqlalchemy.sql.sqltypes import JSON, DateTime, Integer, String

from anibridge.app.models.db.base import Base

__all__ = ["Pin"]


class Pin(Base):
    """Model representing pinned normalized record fields for a target ref."""

    __tablename__ = "pin"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_name: Mapped[str] = mapped_column(String, index=True)

    target_namespace: Mapped[str] = mapped_column(String, index=True)
    target_ref: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)

    fields: Mapped[list[str]] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        UniqueConstraint(
            "profile_name",
            "target_namespace",
            "target_ref",
            name="uq_pin_profile_target_ref",
        ),
        Index("ix_pin_profile_updated_at", "profile_name", "updated_at"),
    )
