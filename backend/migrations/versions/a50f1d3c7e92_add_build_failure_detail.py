"""add builds.failure_detail

Revision ID: a50f1d3c7e92
Revises: e188503deab6
Create Date: 2026-10-01 00:00:00.000000

A nullable Text column, set only when a build is FAILED: the platform-authored
sentence that goes with `failure_reason`.
Existing FAILED rows keep NULL -- there is no
honest text to backfill, and the UI treats NULL as "no detail recorded".

`op.batch_alter_table` per the project convention (runs unmodified on SQLite and
Postgres). Not exercised by the suite (`conftest` builds the schema with
`create_all`); verified by hand.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a50f1d3c7e92'
down_revision: Union[str, None] = 'e188503deab6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('builds') as batch_op:
        batch_op.add_column(sa.Column('failure_detail', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('builds') as batch_op:
        batch_op.drop_column('failure_detail')
