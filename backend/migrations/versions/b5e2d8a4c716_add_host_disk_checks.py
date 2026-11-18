"""add host_disk_checks, the worker's latest disk measurement per location

Revision ID: b5e2d8a4c716
Revises: a3f7c9e1b524
Create Date: 2026-11-14 00:00:00.000000

one row per checked location, overwritten by each worker pass. No row is
the state of every existing installation; the first pass inserts them. No backfill.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b5e2d8a4c716'
down_revision: Union[str, None] = 'a3f7c9e1b524'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'host_disk_checks',
        sa.Column('label', sa.String(), nullable=False),
        sa.Column('path', sa.String(), nullable=True),
        sa.Column('free_bytes', sa.BigInteger(), nullable=True),
        sa.Column('total_bytes', sa.BigInteger(), nullable=True),
        sa.Column('free_percent', sa.Float(), nullable=True),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('label'),
    )


def downgrade() -> None:
    op.drop_table('host_disk_checks')
