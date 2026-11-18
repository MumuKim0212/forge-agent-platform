"""add eval_runs.prompt_fingerprint

Revision ID: c4a8e2f61d37
Revises: b78c4e1f5a92
Create Date: 2026-10-14 00:00:00.000000

A short fingerprint (the first 12 hex characters of a SHA-256) of the system prompt a create
build would receive when an eval run starts, so two runs made under different prompts are
visibly different. Nullable and additive: every existing run keeps NULL, which the console
shows as "no record". Column changes on an existing table go through `op.batch_alter_table`
so the migration runs unmodified on SQLite and Postgres.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4a8e2f61d37'
down_revision: Union[str, None] = 'b78c4e1f5a92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('eval_runs') as batch_op:
        batch_op.add_column(sa.Column('prompt_fingerprint', sa.String(length=12), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('eval_runs') as batch_op:
        batch_op.drop_column('prompt_fingerprint')
