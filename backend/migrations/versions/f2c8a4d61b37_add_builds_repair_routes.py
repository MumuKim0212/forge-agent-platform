"""add builds.repair_routes (ADR 0047 §Amendment 2026-11-19)

Revision ID: f2c8a4d61b37
Revises: d5b9f3a72e18
Create Date: 2026-11-19 00:00:00.000000

One nullable JSON column: the failing routes each repair attempt was started for, as
`[{"attempt": n, "routes": [{"path", "status", "outcome"}]}]`. NULL means no repair attempt was
started, or the build ran before this column existed; it is never backfilled and never an empty
list ("no failing routes" is not what a started repair means). Column changes on an existing table
go through `op.batch_alter_table` so the migration runs unmodified on SQLite and Postgres.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2c8a4d61b37'
down_revision: Union[str, None] = 'd5b9f3a72e18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('builds') as batch_op:
        batch_op.add_column(sa.Column('repair_routes', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('builds') as batch_op:
        batch_op.drop_column('repair_routes')
