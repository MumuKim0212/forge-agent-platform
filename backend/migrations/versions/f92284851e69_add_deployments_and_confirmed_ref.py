"""add deployments and projects.confirmed_ref

Revision ID: f92284851e69
Revises: af4d0b1a314b
Create Date: 2026-07-14 00:00:00.000000

Adds the
`deployments` table (durable, append-only history of L4 deployment
attempts) and `projects.confirmed_ref` (the repo ref of the last build
merged via a successful confirm; nullable — unset until a project's first
confirm).

`projects` is altered via `op.batch_alter_table` (SQLite has no native
`ALTER TABLE ... ADD COLUMN` restriction here, but the project convention
established in edd59525c05f_* is to always use batch mode for `projects`/
`builds` alterations so the same migration runs unmodified on SQLite and
Postgres).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f92284851e69'
down_revision: Union[str, None] = 'af4d0b1a314b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'deployments',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('project_id', sa.String(), nullable=False),
        sa.Column('build_id', sa.String(), nullable=False),
        sa.Column('confirmed_by_id', sa.String(), nullable=False),
        sa.Column('rollback_of', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('failure_reason', sa.String(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.ForeignKeyConstraint(['build_id'], ['builds.id']),
        sa.ForeignKeyConstraint(['confirmed_by_id'], ['users.id']),
        sa.ForeignKeyConstraint(['rollback_of'], ['deployments.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    # "At most one ACTIVE deployment
    # per project" — enforced at the DB level with a partial unique index
    # (supported by both SQLite and Postgres), belt-and-braces on top of the
    # switchover code marking old ACTIVE -> SUPERSEDED in the same
    # transaction that activates the new one.
    op.create_index(
        'uq_deployments_one_active_per_project',
        'deployments',
        ['project_id'],
        unique=True,
        sqlite_where=sa.text("status = 'ACTIVE'"),
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    with op.batch_alter_table('projects') as batch_op:
        batch_op.add_column(sa.Column('confirmed_ref', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('service_env_json', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('projects') as batch_op:
        batch_op.drop_column('service_env_json')
        batch_op.drop_column('confirmed_ref')

    op.drop_index('uq_deployments_one_active_per_project', table_name='deployments')
    op.drop_table('deployments')
