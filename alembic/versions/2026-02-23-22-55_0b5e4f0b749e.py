"""Add sync history info payload

Revision ID: 0b5e4f0b749e
Revises: 38ae72352337
Create Date: 2026-02-23 22:55:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0b5e4f0b749e"
down_revision: Union[str, None] = "38ae72352337"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("sync_history", schema=None) as batch_op:
        batch_op.add_column(sa.Column("info", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sync_history", schema=None) as batch_op:
        batch_op.drop_column("info")
