"""add builds.repair_attempts (ADR 0047)

Revision ID: a7c3e5d19b04
Revises: f4a9d2c7e163

One integer column, NOT NULL: how many in-build repair attempts the platform started for the
build. Existing rows are backfilled to 0 ("no repair") with a `server_default` at column-add time
(SQLite and Postgres alike), then the default is dropped so the model stays the single source of
the default (`Build.repair_attempts`, `default=0`). Forward-only, `op.batch_alter_table` per the
project convention.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c3e5d19b04'
down_revision: Union[str, None] = 'f4a9d2c7e163'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('builds') as batch_op:
        batch_op.add_column(
            sa.Column('repair_attempts', sa.Integer(), nullable=False, server_default='0')
        )
    with op.batch_alter_table('builds') as batch_op:
        batch_op.alter_column('repair_attempts', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('builds') as batch_op:
        batch_op.drop_column('repair_attempts')
