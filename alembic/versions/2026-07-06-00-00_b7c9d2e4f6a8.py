"""Normalize provider refs and sync history.

Revision ID: b7c9d2e4f6a8
Revises: a1b2c3d4e5f6
Create Date: 2026-07-06 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7c9d2e4f6a8"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


sync_outcome = sa.Enum(
    "SYNCED",
    "SKIPPED",
    "FAILED",
    "NOT_FOUND",
    "DELETED",
    "PENDING",
    "UNDONE",
    name="syncoutcome",
)
sync_resource_kind = sa.Enum(
    "RECORD",
    "EVENT",
    "NODE",
    name="syncresourcekind",
)
sync_operation_action = sa.Enum(
    "UPSERT",
    "DELETE",
    "UNDO",
    name="syncoperationaction",
)


def upgrade() -> None:
    with op.batch_alter_table("pin", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_pin_list_media_key"))
        batch_op.drop_index(batch_op.f("ix_pin_list_namespace"))
        batch_op.drop_index(batch_op.f("ix_pin_profile_name"))
        batch_op.drop_index("ix_pin_profile_updated_at")
        batch_op.alter_column(
            "list_namespace",
            existing_type=sa.String(),
            new_column_name="target_namespace",
            existing_nullable=False,
        )
        batch_op.add_column(sa.Column("target_ref", sa.JSON(), nullable=True))

    op.execute(
        """
        UPDATE pin
        SET target_ref = json_object('key', list_media_key, 'path', json_array())
        """
    )

    with op.batch_alter_table("pin", schema=None) as batch_op:
        batch_op.alter_column("target_ref", existing_type=sa.JSON(), nullable=False)
        batch_op.drop_column("list_media_key")
        batch_op.create_unique_constraint(
            "uq_pin_profile_target_ref",
            ["profile_name", "target_namespace", "target_ref"],
        )
        batch_op.create_index(
            batch_op.f("ix_pin_profile_name"), ["profile_name"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_pin_target_namespace"), ["target_namespace"], unique=False
        )
        batch_op.create_index(
            "ix_pin_profile_updated_at", ["profile_name", "updated_at"], unique=False
        )

    op.drop_table("sync_history")

    with op.batch_alter_table("animap_entry", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_animap_entry_entry_id"))
        batch_op.drop_index(batch_op.f("ix_animap_entry_entry_scope"))
        batch_op.drop_index(batch_op.f("ix_animap_entry_provider"))
        batch_op.drop_index("ix_animap_entry_provider_entry_id")
        batch_op.alter_column(
            "provider",
            existing_type=sa.String(),
            new_column_name="authority",
            existing_nullable=False,
        )
        batch_op.alter_column(
            "entry_id",
            existing_type=sa.String(),
            new_column_name="value",
            existing_nullable=False,
        )
        batch_op.alter_column(
            "entry_scope",
            existing_type=sa.String(),
            new_column_name="scope",
            existing_nullable=True,
        )

    with op.batch_alter_table("animap_entry", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_animap_entry_authority"), ["authority"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_animap_entry_value"), ["value"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_animap_entry_scope"), ["scope"], unique=False
        )
        batch_op.create_index(
            "ix_animap_entry_authority_value", ["authority", "value"], unique=False
        )

    op.create_table(
        "sync_history_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_name", sa.String(), nullable=False),
        sa.Column("source_namespace", sa.String(), nullable=False),
        sa.Column("target_namespace", sa.String(), nullable=False),
        sa.Column("trigger", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("outcome", sync_outcome, nullable=True),
        sa.Column("info", sa.JSON(), nullable=True),
        sa.Column("ephemeral", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("sync_history_run", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_sync_history_run_completed_at"),
            ["completed_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_run_ephemeral"), ["ephemeral"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_run_outcome"), ["outcome"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_run_profile_name"),
            ["profile_name"],
            unique=False,
        )
        batch_op.create_index(
            "ix_sync_history_run_profile_started",
            ["profile_name", "started_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_run_source_namespace"),
            ["source_namespace"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_run_started_at"), ["started_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_run_target_namespace"),
            ["target_namespace"],
            unique=False,
        )

    op.create_table(
        "sync_history_group",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("profile_name", sa.String(), nullable=False),
        sa.Column("source_namespace", sa.String(), nullable=False),
        sa.Column("source_parent_ref", sa.JSON(), nullable=False),
        sa.Column("target_namespace", sa.String(), nullable=False),
        sa.Column("target_parent_ref", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("animap_authority", sa.String(), nullable=True),
        sa.Column("animap_value", sa.String(), nullable=True),
        sa.Column("animap_scope", sa.String(), nullable=True),
        sa.Column("outcome", sync_outcome, nullable=False),
        sa.Column("operation_count", sa.Integer(), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("node_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("info", sa.JSON(), nullable=True),
        sa.Column("ephemeral", sa.Boolean(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["animap_authority", "animap_value", "animap_scope"],
            ["animap_entry.authority", "animap_entry.value", "animap_entry.scope"],
            onupdate="CASCADE",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["sync_history_run.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
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
    )
    with op.batch_alter_table("sync_history_group", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_sync_history_group_animap_authority"),
            ["animap_authority"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_group_animap_scope"),
            ["animap_scope"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_group_animap_value"),
            ["animap_value"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_group_ephemeral"), ["ephemeral"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_group_outcome"), ["outcome"], unique=False
        )
        batch_op.create_index(
            "ix_sync_history_group_profile_scope_outcome",
            ["profile_name", "source_namespace", "target_namespace", "outcome"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_group_profile_name"),
            ["profile_name"],
            unique=False,
        )
        batch_op.create_index(
            "ix_sync_history_group_profile_timestamp",
            ["profile_name", "timestamp"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_group_run_id"), ["run_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_group_source_namespace"),
            ["source_namespace"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_group_target_namespace"),
            ["target_namespace"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_group_timestamp"), ["timestamp"], unique=False
        )

    op.create_table(
        "sync_history_operation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("profile_name", sa.String(), nullable=False),
        sa.Column("resource_kind", sync_resource_kind, nullable=False),
        sa.Column("action", sync_operation_action, nullable=False),
        sa.Column("source_namespace", sa.String(), nullable=False),
        sa.Column("source_ref", sa.JSON(), nullable=False),
        sa.Column("target_namespace", sa.String(), nullable=False),
        sa.Column("target_ref", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("source_surface", sa.String(), nullable=True),
        sa.Column("target_surface", sa.String(), nullable=True),
        sa.Column("resource_key", sa.String(), nullable=True),
        sa.Column("outcome", sync_outcome, nullable=False),
        sa.Column("before_state", sa.JSON(), nullable=True),
        sa.Column("after_state", sa.JSON(), nullable=True),
        sa.Column("info", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("ephemeral", sa.Boolean(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"], ["sync_history_group.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("sync_history_operation", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_sync_history_operation_action"), ["action"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_operation_ephemeral"),
            ["ephemeral"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_operation_group_id"), ["group_id"], unique=False
        )
        batch_op.create_index(
            "ix_sync_history_operation_group_resource_outcome",
            ["group_id", "resource_kind", "outcome"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_operation_outcome"), ["outcome"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_operation_profile_name"),
            ["profile_name"],
            unique=False,
        )
        batch_op.create_index(
            "ix_sync_history_operation_profile_resource_timestamp",
            ["profile_name", "resource_kind", "timestamp"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_operation_resource_key"),
            ["resource_key"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_operation_resource_kind"),
            ["resource_kind"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_operation_source_namespace"),
            ["source_namespace"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_operation_source_surface"),
            ["source_surface"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_operation_target_namespace"),
            ["target_namespace"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_operation_target_surface"),
            ["target_surface"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_operation_timestamp"),
            ["timestamp"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("sync_history_operation", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sync_history_operation_timestamp"))
        batch_op.drop_index(batch_op.f("ix_sync_history_operation_target_surface"))
        batch_op.drop_index(batch_op.f("ix_sync_history_operation_target_namespace"))
        batch_op.drop_index(batch_op.f("ix_sync_history_operation_source_surface"))
        batch_op.drop_index(batch_op.f("ix_sync_history_operation_source_namespace"))
        batch_op.drop_index(batch_op.f("ix_sync_history_operation_resource_kind"))
        batch_op.drop_index(batch_op.f("ix_sync_history_operation_resource_key"))
        batch_op.drop_index("ix_sync_history_operation_profile_resource_timestamp")
        batch_op.drop_index(batch_op.f("ix_sync_history_operation_profile_name"))
        batch_op.drop_index(batch_op.f("ix_sync_history_operation_outcome"))
        batch_op.drop_index("ix_sync_history_operation_group_resource_outcome")
        batch_op.drop_index(batch_op.f("ix_sync_history_operation_group_id"))
        batch_op.drop_index(batch_op.f("ix_sync_history_operation_ephemeral"))
        batch_op.drop_index(batch_op.f("ix_sync_history_operation_action"))

    op.drop_table("sync_history_operation")

    with op.batch_alter_table("sync_history_group", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sync_history_group_timestamp"))
        batch_op.drop_index(batch_op.f("ix_sync_history_group_target_namespace"))
        batch_op.drop_index(batch_op.f("ix_sync_history_group_source_namespace"))
        batch_op.drop_index(batch_op.f("ix_sync_history_group_run_id"))
        batch_op.drop_index("ix_sync_history_group_profile_timestamp")
        batch_op.drop_index(batch_op.f("ix_sync_history_group_profile_name"))
        batch_op.drop_index("ix_sync_history_group_profile_scope_outcome")
        batch_op.drop_index(batch_op.f("ix_sync_history_group_outcome"))
        batch_op.drop_index(batch_op.f("ix_sync_history_group_ephemeral"))
        batch_op.drop_index(batch_op.f("ix_sync_history_group_animap_value"))
        batch_op.drop_index(batch_op.f("ix_sync_history_group_animap_scope"))
        batch_op.drop_index(batch_op.f("ix_sync_history_group_animap_authority"))

    op.drop_table("sync_history_group")

    with op.batch_alter_table("sync_history_run", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sync_history_run_target_namespace"))
        batch_op.drop_index(batch_op.f("ix_sync_history_run_started_at"))
        batch_op.drop_index(batch_op.f("ix_sync_history_run_source_namespace"))
        batch_op.drop_index("ix_sync_history_run_profile_started")
        batch_op.drop_index(batch_op.f("ix_sync_history_run_profile_name"))
        batch_op.drop_index(batch_op.f("ix_sync_history_run_outcome"))
        batch_op.drop_index(batch_op.f("ix_sync_history_run_ephemeral"))
        batch_op.drop_index(batch_op.f("ix_sync_history_run_completed_at"))

    op.drop_table("sync_history_run")

    with op.batch_alter_table("animap_entry", schema=None) as batch_op:
        batch_op.drop_index("ix_animap_entry_authority_value")
        batch_op.drop_index(batch_op.f("ix_animap_entry_scope"))
        batch_op.drop_index(batch_op.f("ix_animap_entry_value"))
        batch_op.drop_index(batch_op.f("ix_animap_entry_authority"))
        batch_op.alter_column(
            "scope",
            existing_type=sa.String(),
            new_column_name="entry_scope",
            existing_nullable=True,
        )
        batch_op.alter_column(
            "value",
            existing_type=sa.String(),
            new_column_name="entry_id",
            existing_nullable=False,
        )
        batch_op.alter_column(
            "authority",
            existing_type=sa.String(),
            new_column_name="provider",
            existing_nullable=False,
        )

    with op.batch_alter_table("animap_entry", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_animap_entry_entry_id"), ["entry_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_animap_entry_entry_scope"), ["entry_scope"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_animap_entry_provider"), ["provider"], unique=False
        )
        batch_op.create_index(
            "ix_animap_entry_provider_entry_id", ["provider", "entry_id"], unique=False
        )

    op.create_table(
        "sync_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_name", sa.String(), nullable=False),
        sa.Column("library_namespace", sa.String(), nullable=False),
        sa.Column("library_section_key", sa.String(), nullable=False),
        sa.Column("library_media_key", sa.String(), nullable=False),
        sa.Column("list_namespace", sa.String(), nullable=False),
        sa.Column("list_media_key", sa.String(), nullable=True),
        sa.Column("animap_provider", sa.String(), nullable=True),
        sa.Column("animap_id", sa.String(), nullable=True),
        sa.Column("animap_scope", sa.String(), nullable=True),
        sa.Column(
            "media_kind",
            sa.Enum("MOVIE", "SHOW", "SEASON", "EPISODE", name="mediakind"),
            nullable=False,
        ),
        sa.Column("outcome", sync_outcome, nullable=False),
        sa.Column("before_state", sa.JSON(), nullable=True),
        sa.Column("after_state", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("info", sa.JSON(), nullable=True),
        sa.Column("ephemeral", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["animap_provider", "animap_id", "animap_scope"],
            [
                "animap_entry.provider",
                "animap_entry.entry_id",
                "animap_entry.entry_scope",
            ],
            name="fk_sync_history_animap_descriptor",
            onupdate="CASCADE",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("sync_history", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_sync_history_animap_id"), ["animap_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_animap_provider"),
            ["animap_provider"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_animap_scope"), ["animap_scope"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_ephemeral"), ["ephemeral"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_library_media_key"),
            ["library_media_key"],
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
            batch_op.f("ix_sync_history_list_media_key"),
            ["list_media_key"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_list_namespace"),
            ["list_namespace"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_media_kind"), ["media_kind"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_outcome"), ["outcome"], unique=False
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
        batch_op.create_index(
            batch_op.f("ix_sync_history_profile_name"), ["profile_name"], unique=False
        )
        batch_op.create_index(
            "ix_sync_history_profile_timestamp",
            ["profile_name", "timestamp"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_sync_history_timestamp"), ["timestamp"], unique=False
        )

    with op.batch_alter_table("pin", schema=None) as batch_op:
        batch_op.drop_index("ix_pin_profile_updated_at")
        batch_op.drop_index(batch_op.f("ix_pin_target_namespace"))
        batch_op.drop_index(batch_op.f("ix_pin_profile_name"))
        batch_op.drop_constraint("uq_pin_profile_target_ref", type_="unique")
        batch_op.add_column(sa.Column("list_media_key", sa.String(), nullable=True))

    op.execute(
        """
        UPDATE pin
        SET list_media_key = json_extract(target_ref, '$.key')
        """
    )

    with op.batch_alter_table("pin", schema=None) as batch_op:
        batch_op.alter_column(
            "list_media_key", existing_type=sa.String(), nullable=False
        )
        batch_op.drop_column("target_ref")
        batch_op.alter_column(
            "target_namespace",
            existing_type=sa.String(),
            new_column_name="list_namespace",
            existing_nullable=False,
        )

    with op.batch_alter_table("pin", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_pin_profile_list_media",
            ["profile_name", "list_namespace", "list_media_key"],
        )
        batch_op.create_index(
            batch_op.f("ix_pin_list_media_key"), ["list_media_key"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_pin_list_namespace"), ["list_namespace"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_pin_profile_name"), ["profile_name"], unique=False
        )
        batch_op.create_index(
            "ix_pin_profile_updated_at", ["profile_name", "updated_at"], unique=False
        )
