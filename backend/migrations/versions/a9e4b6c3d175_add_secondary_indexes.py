"""secondary indexes on the hot filters

Revision ID: a9e4b6c3d175
Revises: d2c8f5a71e39
Create Date: 2026-10-19 00:00:00.000000

Nothing but the primary keys, the unique constraints and the two partial unique
indexes on `deployments` was indexed, and Postgres does not index a foreign key on its own.
Each index below serves a query the application runs on every request or every loop; the
queries were EXPLAINed before and after. Index names are
`ix_<table>_<columns>`, SQLAlchemy's own convention, so `index=True` on the model column
produces the same name and the parity test compares like with like.

  ix_audit_log_target              the timeline: `target = 'project:<id>'` and
                                   `target IN ('deployment:<id>', ...)`; the un-archive
                                   lookup (`target = ... AND action = 'project.archived'`);
                                   `GET /audit-log?target=`
  ix_audit_log_ts                  `GET /audit-log`, newest first: `ORDER BY ts DESC LIMIT n`
                                   without a filter, over the one table that only grows
  ix_builds_project_id             the timeline's builds, `has_work_in_flight`, the latest
                                   successful build, the per-project usage rollup, and the
                                   joins from verdicts and approval requests to their project
  ix_builds_status_created_at      the worker's claim, every 2 s:
                                   `WHERE status = 'QUEUED' ORDER BY created_at` (the
                                   composite answers the filter and the order; the leading
                                   `status` also answers the `RUNNING` count and sweeps)
  ix_builds_ended_at               the usage windows: `ended_at >= start AND ended_at < end`
  ix_approval_requests_project_id  superseding a project's older PENDING requests
  ix_change_requests_project_id    the timeline and the change-request list
  ix_deployments_project_id        the timeline's deployment history; the two partial unique
                                   indexes on this column only cover ACTIVE and DEPLOYING rows
  ix_eval_cases_project_id         the foreign key to `projects`, which Postgres checks (by
                                   scanning the referencing table) whenever a project row is
                                   deleted -- the deprovision path and `demo_data --reset`

Schema only: no row is read or changed, so there is nothing to back-fill and nothing that can
fail on existing data. Downgrade drops them.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a9e4b6c3d175'
down_revision: Union[str, None] = 'd2c8f5a71e39'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXES = (
    ('ix_audit_log_target', 'audit_log', ['target']),
    ('ix_audit_log_ts', 'audit_log', ['ts']),
    ('ix_builds_project_id', 'builds', ['project_id']),
    ('ix_builds_status_created_at', 'builds', ['status', 'created_at']),
    ('ix_builds_ended_at', 'builds', ['ended_at']),
    ('ix_approval_requests_project_id', 'approval_requests', ['project_id']),
    ('ix_change_requests_project_id', 'change_requests', ['project_id']),
    ('ix_deployments_project_id', 'deployments', ['project_id']),
    ('ix_eval_cases_project_id', 'eval_cases', ['project_id']),
)


def upgrade() -> None:
    for name, table, columns in _INDEXES:
        op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
