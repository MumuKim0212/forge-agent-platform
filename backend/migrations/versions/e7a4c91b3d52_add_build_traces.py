"""add build_traces

Revision ID: e7a4c91b3d52
Revises: d56a8b3e1f47
Create Date: 2026-10-10 00:00:00.000000

One row per build that reached the agent, holding its trace:
the LLM proxy's call record, the platform-computed diff and the sandbox log
tail. A new table, not columns on `builds`: the content is large, is read only on demand,
and must stay out of every `SELECT * FROM builds` the timeline and the approval console do.
Existing builds have no row and are shown as having no trace; nothing backfills them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7a4c91b3d52'
down_revision: Union[str, None] = 'd56a8b3e1f47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'build_traces',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('build_id', sa.String(), nullable=False),
        sa.Column('calls_state', sa.String(), nullable=False),
        sa.Column('calls', sa.JSON(), nullable=True),
        sa.Column('diff_state', sa.String(), nullable=False),
        sa.Column('diff', sa.JSON(), nullable=True),
        sa.Column('log_tail', sa.Text(), nullable=True),
        sa.Column('log_truncated', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['build_id'], ['builds.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('build_id'),
    )


def downgrade() -> None:
    op.drop_table('build_traces')
