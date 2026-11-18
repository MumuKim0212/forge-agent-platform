"""add builds.smoke_checks

Revision ID: d7a3f1c8e254
Revises: c5d8e2a7f196
Create Date: 2026-11-09 14:32:36.508005

One nullable JSON column: what the platform saw when it called the preview's own parameterless
GET routes. Existing rows keep NULL --
"not measured", which is honest: those previews are long gone. `op.batch_alter_table` per the
project convention (runs unmodified on SQLite and Postgres).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7a3f1c8e254'
down_revision: Union[str, None] = 'c5d8e2a7f196'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('builds') as batch_op:
        batch_op.add_column(sa.Column('smoke_checks', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('builds') as batch_op:
        batch_op.drop_column('smoke_checks')
