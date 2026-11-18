"""add engine_selection

Revision ID: a74e5c2b8d31
Revises: f3b8c1d96a24
Create Date: 2026-10-12 00:00:00.000000

The platform-level agent engine selection (ADR 0035 §Decision 5): a single row holding the
engine an admin chose, who chose it and when. The primary key is pinned to one literal by a
CHECK, so a second row cannot exist. No row is the state of every existing deployment, and it
means "nobody has chosen" (the environment default, or the only available engine, applies).
New table only: no backfill, no change to an existing table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a74e5c2b8d31'
down_revision: Union[str, None] = 'f3b8c1d96a24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'engine_selection',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('engine', sa.String(), nullable=False),
        sa.Column('set_by_id', sa.String(), nullable=False),
        sa.Column('set_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 'agent_engine'", name='ck_engine_selection_singleton'),
        sa.ForeignKeyConstraint(['set_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('engine_selection')
