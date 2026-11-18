"""make audit_log actor_id nullable for system-driven entries

Revision ID: edd59525c05f
Revises: 4dac5b92369c
Create Date: 2026-07-06 09:49:16.745017

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'edd59525c05f'
down_revision: Union[str, None] = '4dac5b92369c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite has no native ALTER COLUMN; batch mode recreates the table.
    # (Autogenerate emits a plain alter_column, which fails on SQLite with
    # "near ALTER: syntax error" — adjusted by hand, as the header invites.)
    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.alter_column(
            "actor_id",
            existing_type=sa.VARCHAR(),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.alter_column(
            "actor_id",
            existing_type=sa.VARCHAR(),
            nullable=False,
        )
