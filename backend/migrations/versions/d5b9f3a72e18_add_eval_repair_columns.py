"""add eval_runs.repair_max_attempts and eval_cases.repair_attempts

Revision ID: d5b9f3a72e18
Revises: a7c3e5d19b04
Create Date: 2026-11-18 00:00:00.000000

The effective `BUILD_REPAIR_MAX_ATTEMPTS` an eval run started with, and the number of repair
attempts each judged case's build ran, so runs with the loop on and off are
distinguishable. Both are nullable and additive, and NULL is "not recorded / not measured": it
is never backfilled to 0, because a run from before this migration did not record the setting
and a case that was not run has no build. Column changes on an existing table go through
`op.batch_alter_table` so the migration runs unmodified on SQLite and Postgres.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5b9f3a72e18'
down_revision: Union[str, None] = 'a7c3e5d19b04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('eval_runs') as batch_op:
        batch_op.add_column(sa.Column('repair_max_attempts', sa.Integer(), nullable=True))
    with op.batch_alter_table('eval_cases') as batch_op:
        batch_op.add_column(sa.Column('repair_attempts', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('eval_cases') as batch_op:
        batch_op.drop_column('repair_attempts')
    with op.batch_alter_table('eval_runs') as batch_op:
        batch_op.drop_column('repair_max_attempts')
