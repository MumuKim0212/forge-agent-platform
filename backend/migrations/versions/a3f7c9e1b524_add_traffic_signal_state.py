"""add traffic signal state and the per-deployment counter snapshot

Revision ID: a3f7c9e1b524
Revises: d7a3f1c8e254
Create Date: 2026-11-12 00:00:00.000000

the traffic signal read from Traefik's request counters persists its snapshot
and its continuity, so a worker restart is not a blind spot. `deployments.traffic_counter_seen`
is the last summed counter value seen for that deployment (NULL: none yet, counts from zero),
and `traffic_signal_state` is one row (the primary key is pinned to a literal by a CHECK) with
the Traefik start time, the last successful poll and `observed_since`. No row is the state of
every existing installation; the first successful poll inserts it. No backfill.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f7c9e1b524'
down_revision: Union[str, None] = 'd7a3f1c8e254'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'traffic_signal_state',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('traefik_started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_poll_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('observed_since', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 'platform'", name='ck_traffic_signal_state_singleton'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('deployments') as batch_op:
        batch_op.add_column(sa.Column('traffic_counter_seen', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('deployments') as batch_op:
        batch_op.drop_column('traffic_counter_seen')
    op.drop_table('traffic_signal_state')
