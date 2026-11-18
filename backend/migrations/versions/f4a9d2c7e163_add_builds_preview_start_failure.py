"""add builds.preview_start_failure

Revision ID: f4a9d2c7e163
Revises: e1b7c4a92d58

One nullable string column: the platform-authored code (`PreviewStartFailure`) recorded when a
build's preview failed to start. Existing rows keep NULL -- "no failure recorded". Forward-only,
`op.batch_alter_table` per the project convention.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4a9d2c7e163'
down_revision: Union[str, None] = 'e1b7c4a92d58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('builds') as batch_op:
        batch_op.add_column(sa.Column('preview_start_failure', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('builds') as batch_op:
        batch_op.drop_column('preview_start_failure')
