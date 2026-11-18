"""add users.oidc_sub

Revision ID: c3a71f6b9d24
Revises: f92284851e69
Create Date: 2026-07-29 00:00:00.000000

ADR 0018 §3. Adds the OIDC
subject identifier as the identity key on `users` — nullable (dev-seeded
users have no IdP identity) and unique (one platform row per `sub`).

**Column-add only, by design.** ADR 0018 §7 decided dev-mode identities are
discarded, not migrated: that is a deliberate local `forge_dev.db` deletion +
re-seed, never a `DELETE` inside a migration. A migration that removed user
rows would cascade into projects/approvals/deployments on whichever machine
ran `alembic upgrade` next.

`users` is altered via `op.batch_alter_table` (project convention since
edd59525c05f_*, so the same migration runs unmodified on SQLite and
Postgres — SQLite cannot add a UNIQUE constraint to an existing column
without a table rebuild, which batch mode performs).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c3a71f6b9d24'
down_revision: Union[str, None] = 'f92284851e69'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(sa.Column('oidc_sub', sa.String(), nullable=True))
        batch_op.create_unique_constraint('uq_users_oidc_sub', ['oidc_sub'])


def downgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_constraint('uq_users_oidc_sub', type_='unique')
        batch_op.drop_column('oidc_sub')
