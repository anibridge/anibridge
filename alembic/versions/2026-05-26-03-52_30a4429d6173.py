"""v3 database migration: backup and recreate pin + sync_history.

Revision ID: 30a4429d6173
Revises: a1b2c3d4e5f6
Create Date: 2026-05-26 03:52:30.619075
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "30a4429d6173"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PIN_BACKUP_TABLE = f"pin_{revision}"
SYNC_HISTORY_BACKUP_TABLE = f"sync_history_{revision}"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table(PIN_BACKUP_TABLE):
        raise RuntimeError(
            f"Backup table {PIN_BACKUP_TABLE!r} already exists. "
            "Refusing to overwrite downgrade backup."
        )

    if inspector.has_table(SYNC_HISTORY_BACKUP_TABLE):
        raise RuntimeError(
            f"Backup table {SYNC_HISTORY_BACKUP_TABLE!r} already exists. "
            "Refusing to overwrite downgrade backup."
        )

    op.execute(f'CREATE TABLE "{PIN_BACKUP_TABLE}" AS SELECT * FROM "pin"')
    op.drop_table("pin")

    op.create_table(
        "pin",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_name", sa.String(), nullable=False),
        sa.Column("target_namespace", sa.String(), nullable=False),
        sa.Column("target_ref", sa.JSON(), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_name",
            "target_namespace",
            "target_ref",
            name="uq_pin_profile_target_ref",
        ),
    )

    with op.batch_alter_table("pin") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_pin_profile_name"),
            ["profile_name"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_pin_target_namespace"),
            ["target_namespace"],
            unique=False,
        )
        batch_op.create_index(
            "ix_pin_profile_updated_at",
            ["profile_name", "updated_at"],
            unique=False,
        )

    op.execute(
        f'CREATE TABLE "{SYNC_HISTORY_BACKUP_TABLE}" '
        'AS SELECT * FROM "sync_history"'
    )
    op.drop_table("sync_history")

    op.create_table(
        "sync_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_name", sa.String(), nullable=False),
        sa.Column("source_namespace", sa.String(), nullable=False),
        sa.Column("source_ref", sa.JSON(), nullable=False),
        sa.Column("target_namespace", sa.String(), nullable=False),
        sa.Column("target_ref", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("animap_provider", sa.String(), nullable=True),
        sa.Column("animap_id", sa.String(), nullable=True),
        sa.Column("animap_scope", sa.String(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("before_state", sa.JSON(), nullable=True),
        sa.Column("after_state", sa.JSON(), nullable=True),
        sa.Column("info", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("ephemeral", sa.Boolean(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table("sync_history") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_sync_history_profile_name"),
            ["profile_name"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_source_namespace"),
            ["source_namespace"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_target_namespace"),
            ["target_namespace"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_animap_provider"),
            ["animap_provider"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_animap_id"),
            ["animap_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_animap_scope"),
            ["animap_scope"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_outcome"),
            ["outcome"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_ephemeral"),
            ["ephemeral"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_timestamp"),
            ["timestamp"],
            unique=False,
        )
        batch_op.create_index(
            "ix_sync_history_profile_timestamp",
            ["profile_name", "timestamp"],
            unique=False,
        )
        batch_op.create_index(
            "ix_sync_history_profile_source_outcome",
            ["profile_name", "source_namespace", "outcome"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    with op.batch_alter_table("sync_history") as batch_op:
        for idx_name in (
            "ix_sync_history_profile_timestamp",
            "ix_sync_history_profile_source_outcome",
        ):
            batch_op.drop_index(idx_name, if_exists=True)

        for idx_name in (
            "ix_sync_history_ephemeral",
            "ix_sync_history_timestamp",
            "ix_sync_history_outcome",
            "ix_sync_history_animap_scope",
            "ix_sync_history_animap_id",
            "ix_sync_history_animap_provider",
            "ix_sync_history_target_namespace",
            "ix_sync_history_source_namespace",
            "ix_sync_history_profile_name",
        ):
            batch_op.drop_index(batch_op.f(idx_name), if_exists=True)

    op.drop_table("sync_history")

    op.create_table(
        "sync_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_name", sa.String(), nullable=False),
        sa.Column("library_namespace", sa.String(), nullable=False),
        sa.Column("library_section_key", sa.String(), nullable=False),
        sa.Column("library_media_key", sa.String(), nullable=False),
        sa.Column("list_namespace", sa.String(), nullable=False),
        sa.Column("list_media_key", sa.String(), nullable=True),
        sa.Column("media_kind", sa.String(length=7), nullable=False),
        sa.Column("animap_provider", sa.String(), nullable=True),
        sa.Column("animap_id", sa.String(), nullable=True),
        sa.Column("animap_scope", sa.String(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("before_state", sa.JSON(), nullable=True),
        sa.Column("after_state", sa.JSON(), nullable=True),
        sa.Column("info", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("ephemeral", sa.Boolean(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    if inspector.has_table(SYNC_HISTORY_BACKUP_TABLE):
        op.execute(
            f'INSERT INTO "sync_history" '
            f'SELECT * FROM "{SYNC_HISTORY_BACKUP_TABLE}"'
        )
        op.drop_table(SYNC_HISTORY_BACKUP_TABLE)

    with op.batch_alter_table("sync_history") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_sync_history_profile_name"),
            ["profile_name"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_library_namespace"),
            ["library_namespace"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_library_section_key"),
            ["library_section_key"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_library_media_key"),
            ["library_media_key"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_list_namespace"),
            ["list_namespace"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_list_media_key"),
            ["list_media_key"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_media_kind"),
            ["media_kind"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_animap_provider"),
            ["animap_provider"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_animap_id"),
            ["animap_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_animap_scope"),
            ["animap_scope"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_outcome"),
            ["outcome"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_ephemeral"),
            ["ephemeral"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_timestamp"),
            ["timestamp"],
            unique=False,
        )
        batch_op.create_index(
            "ix_sync_history_profile_timestamp",
            ["profile_name", "timestamp"],
            unique=False,
        )
        batch_op.create_index(
            "ix_sync_history_profile_library_media_outcome",
            [
                "profile_name",
                "library_namespace",
                "library_section_key",
                "library_media_key",
                "outcome",
            ],
            unique=False,
        )

    with op.batch_alter_table("pin") as batch_op:
        batch_op.drop_index("ix_pin_profile_updated_at", if_exists=True)
        batch_op.drop_index(batch_op.f("ix_pin_target_namespace"), if_exists=True)
        batch_op.drop_index(batch_op.f("ix_pin_profile_name"), if_exists=True)

    op.drop_table("pin")

    op.create_table(
        "pin",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_name", sa.String(), nullable=False),
        sa.Column("list_namespace", sa.String(), nullable=False),
        sa.Column("list_media_key", sa.String(), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_name",
            "list_namespace",
            "list_media_key",
            name="uq_pin_profile_list_media",
        ),
    )

    if inspector.has_table(PIN_BACKUP_TABLE):
        op.execute(
            f'INSERT INTO "pin" '
            f'SELECT * FROM "{PIN_BACKUP_TABLE}"'
        )
        op.drop_table(PIN_BACKUP_TABLE)

    with op.batch_alter_table("pin") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_pin_profile_name"),
            ["profile_name"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_pin_list_namespace"),
            ["list_namespace"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_pin_list_media_key"),
            ["list_media_key"],
            unique=False,
        )
        batch_op.create_index(
            "ix_pin_profile_updated_at",
            ["profile_name", "updated_at"],
            unique=False,
        )