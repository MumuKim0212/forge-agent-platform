"""add worker_lease

Revision ID: c5d8e2a7f196
Revises: a9e4b6c3d175
Create Date: 2026-10-28 00:00:00.000000

The one-worker-per-installation lease (ADR 0038 §Decision 1): a single row naming the worker
process that holds it (random id, host, pid for the operator's message) with the time it was
taken and the time of its last heartbeat. The primary key is pinned to one literal by a CHECK,
so a second row cannot exist. No row is the state of every existing installation; the first
worker to start after this revision inserts it (`services/worker_lease.py`), so the migration
itself invents nothing. New table only: no backfill, no change to an existing table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5d8e2a7f196'
down_revision: Union[str, None] = 'a9e4b6c3d175'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'worker_lease',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('holder_id', sa.String(), nullable=True),
        sa.Column('holder_host', sa.String(), nullable=True),
        sa.Column('holder_pid', sa.Integer(), nullable=True),
        sa.Column('acquired_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 'platform'", name='ck_worker_lease_singleton'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('worker_lease')
