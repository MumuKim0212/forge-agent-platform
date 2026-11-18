"""add users.monthly_quota_usd and projects.monthly_quota_usd

Revision ID: b8e21c47d5f0
Revises: c3a71f6b9d24
Create Date: 2026-07-30 00:00:00.000000

The per-subject monthly
cost limit override, on the two rollup subjects. Nullable, with `NULL` meaning
"use `QUOTA_MONTHLY_USD_DEFAULT`" — **not** unlimited — so this migration
needs no backfill: every existing row starts on the global default by virtue
of being NULL.

**Column-add only, and deliberately the whole schema change for cost
metering.** Rollups stay derived: monthly spend is a `SUM` over
`builds.cost_usd`, and the long-deferred `ResourceUsage` table stays deferred
because a materialized rollup is a second copy of a fact `builds` already
holds. There is no new table here, and no trigger/job writing one.

Two columns on the two existing tables rather than a polymorphic
`quotas(subject_type, subject_id, limit)` table: with exactly two subject
kinds a column keeps referential integrity for free.

Both tables are altered via `op.batch_alter_table` (project convention since
edd59525c05f_*), so the same migration runs unmodified on SQLite and Postgres.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b8e21c47d5f0'
down_revision: Union[str, None] = 'c3a71f6b9d24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(sa.Column('monthly_quota_usd', sa.Float(), nullable=True))
    with op.batch_alter_table('projects') as batch_op:
        batch_op.add_column(sa.Column('monthly_quota_usd', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('projects') as batch_op:
        batch_op.drop_column('monthly_quota_usd')
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('monthly_quota_usd')
