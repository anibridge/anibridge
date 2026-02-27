"""Housekeeping Model Module."""

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.sqltypes import String

from anibridge.app.models.db.base import Base

__all__ = ["Housekeeping"]


class Housekeeping(Base):
    """Model for the Housekeeping table.

    This table is used to store miscellaneous data such as timestamps and hashes.
    """

    __tablename__ = "house_keeping"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str | None] = mapped_column(String, nullable=True)
