"""add notification_states and notification_channel_status

Revision ID: e1b7c4a92d58
Revises: d9a4e6b1f3c7
Create Date: 2026-11-15 10:00:00.000000

Both tables start empty: no row is "nothing announced yet". No backfill.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1b7c4a92d58'
down_revision: Union[str, None] = 'd9a4e6b1f3c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'notification_states',
        sa.Column('family', sa.String(), nullable=False),
        sa.Column('subject', sa.String(), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('changed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('down_notified', sa.Boolean(), nullable=False),
        sa.Column('last_down_notified_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('family', 'subject'),
    )
    op.create_table(
        'notification_channel_status',
        sa.Column('channel', sa.String(), nullable=False),
        sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_result', sa.String(), nullable=True),
        sa.Column('last_error_class', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('channel'),
    )


def downgrade() -> None:
    op.drop_table('notification_channel_status')
    op.drop_table('notification_states')
