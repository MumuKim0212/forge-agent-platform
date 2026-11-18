"""add platform_instance

Revision ID: b3f6a1d94c28
Revises: e9b4d27a6c13
Create Date: 2026-10-19 00:00:00.000000

The installation's own id (ADR 0037 §Decision 2): a single row holding the value every
container and network this installation creates carries as the `forge.instance` label, and
the only value the reconciler will act on. The primary key is pinned to one literal by a
CHECK, so a second row cannot exist. No row is the state of every existing installation; the
worker generates the id at its first start after this revision (`instance_id.py`), so the
migration itself invents nothing. New table only: no backfill, no change to an existing table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3f6a1d94c28'
down_revision: Union[str, None] = 'e9b4d27a6c13'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'platform_instance',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('instance_id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 'platform'", name='ck_platform_instance_singleton'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('platform_instance')
