"""add service_checker_status, when the service checker last ran a pass

Revision ID: d9a4e6b1f3c7
Revises: c7d3a915e2b8
Create Date: 2026-11-14 18:00:00.000000

One singleton row written by the worker's checker; no row is "never ran". No backfill.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9a4e6b1f3c7'
down_revision: Union[str, None] = 'c7d3a915e2b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'service_checker_status',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('last_pass_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_pass_finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('service_checker_status')
