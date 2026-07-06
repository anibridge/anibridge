"""Models for authority-range mapping graph."""

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import ForeignKey, Index, UniqueConstraint
from sqlalchemy.sql.sqltypes import Integer, String

from anibridge.app.models.db.base import Base

__all__ = ["AnimapEntry", "AnimapMapping", "AnimapProvenance"]


class AnimapEntry(Base):
    """Model representing a unique entry from a authority."""

    __tablename__ = "animap_entry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    authority: Mapped[str] = mapped_column(String, nullable=False, index=True)
    value: Mapped[str] = mapped_column(String, nullable=False, index=True)
    scope: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    __table_args__ = (
        UniqueConstraint("authority", "value", "scope"),
        Index("ix_animap_entry_authority_value", "authority", "value"),
    )


class AnimapMapping(Base):
    """Model representing a mapping between two authority entries."""

    __tablename__ = "animap_mapping"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    source_entry_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("animap_entry.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    destination_entry_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("animap_entry.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    source_range: Mapped[str] = mapped_column(String, nullable=False)
    destination_range: Mapped[str] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "source_entry_id",
            "destination_entry_id",
            "source_range",
            "destination_range",
        ),
        Index(
            "ix_animap_mapping_destination_source",
            "destination_entry_id",
            "source_entry_id",
        ),
    )


class AnimapProvenance(Base):
    """Tracks the provenance (source paths/URLs) for each Animap row.

    Stores one row per source with an order column `n` to preserve the
    original order of sources for a given entry.
    """

    __tablename__ = "animap_provenance"

    mapping_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("animap_mapping.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    n: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
