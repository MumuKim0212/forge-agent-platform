"""add service_health_checks, the worker's latest check of each ACTIVE service

Revision ID: c7d3a915e2b8
Revises: b5e2d8a4c716
Create Date: 2026-11-14 12:00:00.000000

one row per deployment, created by the first check and removed when the
deployment stops being ACTIVE. No row is the state of every existing deployment ("not checked
yet"); nothing is backfilled.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d3a915e2b8'
down_revision: Union[str, None] = 'b5e2d8a4c716'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'service_health_checks',
        sa.Column('deployment_id', sa.String(), nullable=False),
        sa.Column('project_id', sa.String(), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('last_result', sa.String(), nullable=False),
        sa.Column('consecutive_failures', sa.Integer(), nullable=False),
        sa.Column('last_failure_kind', sa.String(), nullable=True),
        sa.Column('last_failure_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failing_since', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_restart_count', sa.Integer(), nullable=True),
        sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_passed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('state_changed_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id']),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.PrimaryKeyConstraint('deployment_id'),
    )
    with op.batch_alter_table('service_health_checks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_service_health_checks_project_id'), ['project_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('service_health_checks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_service_health_checks_project_id'))
    op.drop_table('service_health_checks')
