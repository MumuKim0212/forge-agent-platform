"""one DEPLOYING deployment per project

Revision ID: e9b4d27a6c13
Revises: c4a8e2f61d37
Create Date: 2026-10-17 00:00:00.000000

Per-project serialization. Adds a
partial unique index on `deployments(project_id) WHERE status = 'DEPLOYING'`,
so the database -- not a check-then-insert in the API -- guarantees at most one
in-flight deployment per project. A partial index is supported by both SQLite
and Postgres (`sqlite_where` / `postgresql_where`), the same shape as the
existing `uq_deployments_one_active_per_project`.

Existing rows first. A project that already holds two or more `DEPLOYING` rows
(possible before this index: two confirms racing past the check) would make
`CREATE UNIQUE INDEX` fail and `alembic upgrade` stop half way. So, per
project, ONE row is kept and every other `DEPLOYING` row is moved to its
terminal state in the same migration:

  kept       the row a worker has already claimed (`started_at` set), else the
             oldest by `created_at` (then `id`, so the choice is deterministic).
             The queue is oldest-first, so that is the row that would have run.
  demoted    `status` = FAILED, `failure_reason` = INFRA_ERROR (the taxonomy's
             "anything else infrastructural": it never ran, and nothing in the
             taxonomy says "refused by a migration"), `ended_at` = now, plus a
             `deployment.failed` audit entry (no actor, `reason` and
             `cause = migration e9b4d27a6c13`) so the timeline says why a row
             changed under it, like every other transition.

The migration is deliberately frozen: literal status strings and raw SQL, no
import from `app`, so a later rename in application code cannot change what
this did.

Downgrade drops the index only. The demoted rows stay FAILED -- the rows that
were running are not recoverable, and re-opening a row the platform has
already recorded as failed would be a lie.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e9b4d27a6c13'
down_revision: Union[str, None] = 'c4a8e2f61d37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = 'uq_deployments_one_deploying_per_project'

_deployments = sa.table(
    'deployments',
    sa.column('id', sa.String()),
    sa.column('status', sa.String()),
    sa.column('failure_reason', sa.String()),
    sa.column('ended_at', sa.DateTime(timezone=True)),
)

_audit_log = sa.table(
    'audit_log',
    sa.column('id', sa.String()),
    sa.column('actor_id', sa.String()),
    sa.column('action', sa.String()),
    sa.column('target', sa.String()),
    sa.column('metadata_json', sa.JSON()),
    sa.column('ts', sa.DateTime(timezone=True)),
)


def _demote_duplicate_deploying_rows(bind) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT id, project_id, build_id, started_at, created_at FROM deployments "
            "WHERE status = 'DEPLOYING'"
        )
    ).fetchall()

    by_project: dict[str, list] = {}
    for row in rows:
        by_project.setdefault(row.project_id, []).append(row)

    now = datetime.now(timezone.utc)
    for project_id, group in by_project.items():
        if len(group) < 2:
            continue
        # Claimed first (started_at set sorts before NULL), then oldest, then
        # id. created_at is only an ordering key, so its driver-specific type
        # (a string on SQLite, a datetime on Postgres) is compared as text.
        group.sort(key=lambda r: (r.started_at is None, str(r.created_at), r.id))
        for row in group[1:]:
            bind.execute(
                sa.update(_deployments)
                .where(_deployments.c.id == row.id)
                .values(status='FAILED', failure_reason='INFRA_ERROR', ended_at=now)
            )
            bind.execute(
                sa.insert(_audit_log).values(
                    id=str(uuid.uuid4()),
                    actor_id=None,
                    action='deployment.failed',
                    target=f'deployment:{row.id}',
                    metadata_json={
                        'project_id': project_id,
                        'build_id': row.build_id,
                        'reason': 'INFRA_ERROR',
                        'cause': f'migration {revision}: more than one DEPLOYING row for the project',
                    },
                    ts=now,
                )
            )


def upgrade() -> None:
    _demote_duplicate_deploying_rows(op.get_bind())
    op.create_index(
        _INDEX,
        'deployments',
        ['project_id'],
        unique=True,
        sqlite_where=sa.text("status = 'DEPLOYING'"),
        postgresql_where=sa.text("status = 'DEPLOYING'"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name='deployments')
