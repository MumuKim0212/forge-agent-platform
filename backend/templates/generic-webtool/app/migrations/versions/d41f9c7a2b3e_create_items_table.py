"""create items table

Revision ID: d41f9c7a2b3e
Revises:
Create Date: 2026-09-07 00:00:00.000000

ADR 0023 §Data services: the template's one shipped table (ADR
0006: shape, not product) -- created by `alembic upgrade head` (this
template's Dockerfile CMD) before the app ever serves a request.
Additive only, so the destructive-DDL policy check
passes on this file.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d41f9c7a2b3e"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    # ADR 0023 §Schema migrations: this service's migrations are
    # forward-only. Rollback is a runtime-only container switch, never a
    # schema downgrade -- raising here is
    # the honest statement that this path does not exist, not a promise
    # this file can't keep.
    raise NotImplementedError(
        "downgrades are not supported -- migrations are forward-only "
        "(ADR 0023 §Schema migrations); roll back by redeploying the "
        "previous image instead."
    )
