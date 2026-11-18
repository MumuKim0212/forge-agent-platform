"""add builds.agent_summary

Revision ID: d56a8b3e1f47
Revises: c55d7a2e9f10
Create Date: 2026-10-06 00:00:00.000000

One nullable text column holding what the agent wrote about its own work
. It is written only when a build
ends SUCCEEDED, bounded by `build_queue.mark_succeeded`. Existing rows keep NULL: the
text was never stored before, and the UI shows nothing for NULL.

`op.batch_alter_table` per the project convention (runs unmodified on SQLite and
Postgres).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd56a8b3e1f47'
down_revision: Union[str, None] = 'c55d7a2e9f10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('builds') as batch_op:
        batch_op.add_column(sa.Column('agent_summary', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('builds') as batch_op:
        batch_op.drop_column('agent_summary')
