"""add users password_hash department status

Revision ID: 752f15b718ce
Revises: b8e21c47d5f0
Create Date: 2026-08-26 15:56:06.488801

ADR 0022.
Adds the three columns `local` auth mode needs on `users`:

- `password_hash` (nullable): the bcrypt hash of a local password. NULL for
  every OIDC-provisioned or dev-seeded row, and for every existing row
  before this migration -- `password_hash IS NULL` can never authenticate
  (services/passwords.py), so leaving it NULL is the correct, safe default
  for rows that never had a local password.
- `department` (nullable): free-text evidence for the approving admin.
  Never populated for pre-existing rows.
- `status` (NOT NULL): the account-status lifecycle
  (`services/account_status.AccountStatus` — PENDING/ACTIVE/REJECTED).

**Every existing row is backfilled to ACTIVE, not PENDING.** A NOT NULL
column with no backfill (or a PENDING backfill) would either fail the
migration outright or instantly lock every pre-existing user — including
every dev-seeded user and every OIDC-provisioned user already in a
database — out of a platform that, for them, was never gated by approval
in the first place (rows created by oidc JIT
provisioning and by the dev seed are active). The backfill is done with a
`server_default` at column-add time (applies to SQLite and Postgres alike
in one statement), then the server default is dropped in a second
`batch_alter_table` block so future inserts go through the ORM's
Python-side default (`User.status`'s `mapped_column(default=AccountStatus.
ACTIVE)`, same shape as `role`) instead of a second, DB-level copy of the
same literal that could silently drift from it.

**This migration is not exercised by the test suite** (`tests/conftest.py`
builds its schema with `Base.metadata.create_all`, which reads the current
model definitions directly and never touches Alembic). It was instead
verified by hand against a populated dev SQLite database — see the task's
`## Result` for what was observed.

All three columns altered via `op.batch_alter_table` (project convention
since edd59525c05f_*), so the same migration runs unmodified on SQLite and
Postgres.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '752f15b718ce'
down_revision: Union[str, None] = 'b8e21c47d5f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(sa.Column('password_hash', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('department', sa.String(), nullable=True))
        # server_default backfills every existing row to 'ACTIVE' in the
        # same statement that adds the NOT NULL column.
        batch_op.add_column(
            sa.Column('status', sa.String(), nullable=False, server_default='ACTIVE')
        )

    # Drop the server default now that the backfill has happened: future
    # inserts should go through the ORM's Python-side default (the single
    # declaration in services/account_status.AccountStatus), not a second,
    # DB-level copy of the same literal.
    with op.batch_alter_table('users') as batch_op:
        batch_op.alter_column('status', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('status')
        batch_op.drop_column('department')
        batch_op.drop_column('password_hash')
