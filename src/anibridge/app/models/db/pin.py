"""Pin model for per-profile AniList field pinning."""

from datetime import UTC, datetime

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import Index, UniqueConstraint
from sqlalchemy.sql.sqltypes import JSON, DateTime, Integer, String

from anibridge.app.models.db.base import Base

__all__ = ["Pin"]


class Pin(Base):
    """Model representing pinned AniList fields for a profile entry."""

    __tablename__ = "pin"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_name: Mapped[str] = mapped_column(String, index=True)

    list_namespace: Mapped[str] = mapped_column(String, index=True)
    list_media_key: Mapped[str] = mapped_column(String, index=True)

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
        UniqueConstraint("profile_name", "list_namespace", "list_media_key"),
        Index("ix_pin_profile_updated_at", "profile_name", "updated_at"),
    )
