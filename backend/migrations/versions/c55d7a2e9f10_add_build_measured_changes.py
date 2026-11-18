"""add builds.changed_files and builds.protected_path_drift

Revision ID: c55d7a2e9f10
Revises: b53e9d2a4c18
Create Date: 2026-10-06 00:00:00.000000

Two nullable JSON columns holding what the platform measured about the agent's change
: the changed-file list with its
totals, and the protected-path drift. Existing rows keep NULL -- "not measured", which
is honest: the input tree they were measured against is long gone, and the UI shows
nothing for NULL rather than an invented zero.

`op.batch_alter_table` per the project convention (runs unmodified on SQLite and
Postgres).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c55d7a2e9f10'
down_revision: Union[str, None] = 'b53e9d2a4c18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('builds') as batch_op:
        batch_op.add_column(sa.Column('changed_files', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('protected_path_drift', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('builds') as batch_op:
        batch_op.drop_column('protected_path_drift')
        batch_op.drop_column('changed_files')
