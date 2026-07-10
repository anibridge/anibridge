"""Replace animap_entry_id with descriptor columns in sync_history.

The reason for this change is to allow sync_history records to retain
their association with animap entries even if the animap entry is
deleted. Currently, if an animap entry is deleted and recreated with
a new ID, the sync_history record would lose its reference.

Revision ID: a1b2c3d4e5f6
Revises: 48fc289f8e59
Create Date: 2026-04-05 02:53:00.000000

"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "48fc289f8e59"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    existing_columns = {c["name"] for c in inspect(conn).get_columns("sync_history")}

    # Add the three descriptor columns if not already present (guard against partial re-runs)
    if "animap_provider" not in existing_columns:
        op.add_column(
            "sync_history", sa.Column("animap_provider", sa.String(), nullable=True)
        )
    if "animap_id" not in existing_columns:
        op.add_column(
            "sync_history", sa.Column("animap_id", sa.String(), nullable=True)
        )
    if "animap_scope" not in existing_columns:
        op.add_column(
            "sync_history", sa.Column("animap_scope", sa.String(), nullable=True)
        )

    # Populate the descriptor columns from the animap_entry table based on existing animap_entry_id
    # (Only meaningful if animap_entry_id still exists; safe to run even if already populated.)
    if "animap_entry_id" in existing_columns:
        op.execute("""
            UPDATE sync_history
            SET animap_provider = ae.provider,
                animap_id = ae.entry_id,
                animap_scope = ae.entry_scope
            FROM animap_entry ae
            WHERE sync_history.animap_entry_id = ae.id
        """)

    with op.batch_alter_table("sync_history", schema=None) as batch_op:
        if "animap_entry_id" in existing_columns:
            batch_op.drop_index(batch_op.f("ix_sync_history_animap_entry_id"))
            batch_op.drop_column("animap_entry_id")

        batch_op.create_index(
            batch_op.f("ix_sync_history_animap_provider"),
            ["animap_provider"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_animap_id"), ["animap_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_animap_scope"), ["animap_scope"], unique=False
        )

        batch_op.create_foreign_key(
            "fk_sync_history_animap_descriptor",
            "animap_entry",
            ["animap_provider", "animap_id", "animap_scope"],
            ["provider", "entry_id", "entry_scope"],
            ondelete="SET NULL",
            onupdate="CASCADE",
        )


def downgrade() -> None:
    # Re-add the animap_entry_id column as nullable first
    op.add_column(
        "sync_history", sa.Column("animap_entry_id", sa.Integer(), nullable=True)
    )

    # Populate it from the descriptor columns by looking up in animap_entry
    op.execute("""
        UPDATE sync_history
        SET animap_entry_id = ae.id
        FROM animap_entry ae
        WHERE sync_history.animap_provider = ae.provider
          AND sync_history.animap_id = ae.entry_id
          AND sync_history.animap_scope = ae.entry_scope
    """)

    with op.batch_alter_table("sync_history", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_sync_history_animap_descriptor", type_="foreignkey"
        )

        batch_op.drop_index(batch_op.f("ix_sync_history_animap_scope"))
        batch_op.drop_index(batch_op.f("ix_sync_history_animap_id"))
        batch_op.drop_index(batch_op.f("ix_sync_history_animap_provider"))

        batch_op.create_index(
            batch_op.f("ix_sync_history_animap_entry_id"),
            ["animap_entry_id"],
            unique=False,
        )

        batch_op.create_foreign_key(
            "fk_sync_history_animap_entry_id",
            "animap_entry",
            ["animap_entry_id"],
            ["id"],
            ondelete="SET NULL",
            onupdate="CASCADE",
        )

        batch_op.drop_column("animap_scope")
        batch_op.drop_column("animap_id")
        batch_op.drop_column("animap_provider")
