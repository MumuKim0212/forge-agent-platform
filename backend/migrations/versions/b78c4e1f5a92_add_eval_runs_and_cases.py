"""add eval_runs, eval_cases, projects.kind and builds.cost_budget_usd

Revision ID: b78c4e1f5a92
Revises: d91c3e7a5b28
Create Date: 2026-10-13 00:00:00.000000

The eval pipeline's storage:

- `eval_runs`: one row per admin-triggered run (engine, model, request-set version, budget,
  spend, status, timing). A partial unique index on `status = 'RUNNING'` makes "at most one
  run in progress" a database fact (SQLite and Postgres alike).
- `eval_cases`: one row per request-set case per run, unique on (run, case id) and on
  (run, position).
- `projects.kind` (NOT NULL): `normal` or `eval`. Every existing row is `normal`: the column is
  added with a `server_default` that backfills them, then the default is dropped so future
  inserts go through the ORM's Python-side default (`ProjectKind.NORMAL`), the same shape as
  `users.status` (752f15b718ce).
- `builds.cost_budget_usd` (nullable): the per-build cost ceiling carried on a build. NULL, the
  state of every existing and every normal build, means "the BUILD_COST_BUDGET_USD setting".

Column changes on existing tables go through `op.batch_alter_table`, so the migration runs
unmodified on SQLite and Postgres.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b78c4e1f5a92'
down_revision: Union[str, None] = 'd91c3e7a5b28'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('projects') as batch_op:
        batch_op.add_column(sa.Column('kind', sa.String(), nullable=False, server_default='normal'))
    with op.batch_alter_table('projects') as batch_op:
        batch_op.alter_column('kind', server_default=None)

    with op.batch_alter_table('builds') as batch_op:
        batch_op.add_column(sa.Column('cost_budget_usd', sa.Float(), nullable=True))

    op.create_table(
        'eval_runs',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('engine', sa.String(), nullable=False),
        sa.Column('model', sa.String(), nullable=True),
        sa.Column('request_set_version', sa.Integer(), nullable=False),
        sa.Column('budget_usd', sa.Float(), nullable=False),
        sa.Column('spend_usd', sa.Float(), nullable=False),
        sa.Column('started_by_id', sa.String(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_progress_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['started_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'uq_eval_runs_one_running',
        'eval_runs',
        ['status'],
        unique=True,
        sqlite_where=sa.text("status = 'RUNNING'"),
        postgresql_where=sa.text("status = 'RUNNING'"),
    )

    op.create_table(
        'eval_cases',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('run_id', sa.String(), nullable=False),
        sa.Column('case_id', sa.String(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('result', sa.String(), nullable=True),
        sa.Column('failure_reason', sa.String(), nullable=True),
        sa.Column('failed_check_index', sa.Integer(), nullable=True),
        sa.Column('check_outcomes', sa.JSON(), nullable=True),
        sa.Column('project_id', sa.String(), nullable=True),
        sa.Column('build_id', sa.String(), nullable=True),
        sa.Column('cost_usd', sa.Float(), nullable=True),
        sa.Column('cost_tokens', sa.Integer(), nullable=True),
        sa.Column('duration_seconds', sa.Float(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['build_id'], ['builds.id']),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.ForeignKeyConstraint(['run_id'], ['eval_runs.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('run_id', 'case_id', name='uq_eval_cases_run_case'),
        sa.UniqueConstraint('run_id', 'position', name='uq_eval_cases_run_position'),
    )


def downgrade() -> None:
    op.drop_table('eval_cases')
    op.drop_index('uq_eval_runs_one_running', table_name='eval_runs')
    op.drop_table('eval_runs')
    with op.batch_alter_table('builds') as batch_op:
        batch_op.drop_column('cost_budget_usd')
    with op.batch_alter_table('projects') as batch_op:
        batch_op.drop_column('kind')
