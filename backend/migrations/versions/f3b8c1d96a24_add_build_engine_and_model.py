"""add builds.engine and builds.model

Revision ID: f3b8c1d96a24
Revises: e7a4c91b3d52
Create Date: 2026-10-12 00:00:00.000000

Two nullable string columns written when the worker claims a build (ADR 0035 §Decision 5):
the engine it resolved (`anthropic` | `openai`) and the model the runner was configured to
use. Both are measured facts about the build, not agent claims. `model` stays NULL for an
engine that runs its own default (the Claude runner passes none). Existing rows keep NULL on
both: nothing recorded them, and the UI shows nothing for NULL.

`op.batch_alter_table` per the project convention (runs unmodified on SQLite and Postgres).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3b8c1d96a24'
down_revision: Union[str, None] = 'e7a4c91b3d52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('builds') as batch_op:
        batch_op.add_column(sa.Column('engine', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('model', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('builds') as batch_op:
        batch_op.drop_column('model')
        batch_op.drop_column('engine')
