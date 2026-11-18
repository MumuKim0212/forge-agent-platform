"""one approve decision per actor per approval request

Revision ID: d2c8f5a71e39
Revises: b3f6a1d94c28
Create Date: 2026-10-19 00:00:00.000000

An approval count is a
count of DISTINCT approvers, and the database enforces it. Adds a partial unique index on
`approval_decisions (approval_request_id, actor_id) WHERE decision = 'approve'`, so one actor
holds at most one approve decision per request -- two parallel clicks by one approver can no
longer both be stored, however the read-then-insert check in `decide` is interleaved. A partial
index is supported by both SQLite and Postgres (`sqlite_where` / `postgresql_where`), the same
shape as the existing `uq_deployments_one_*` indexes.

Approvals only, on purpose. A reject is terminal (`REJECTED`), so a request takes at most one;
and an approver who has approved a still-PENDING request may later reject it (the console
offers that), which an index over every decision would forbid.

Existing rows first. A request on which one actor already holds several approve rows (possible
before this index: two clicks racing past the check) would make `CREATE UNIQUE INDEX` fail and
`alembic upgrade` stop half way. So, per (request, actor), the EARLIEST approve row is kept
(`created_at`, then `id`) and each later one is deleted, with an `approval_decision.duplicate_removed`
audit entry (no actor: the migration is not a user) that carries everything the row held --
its id and the id of the row kept, the approver, the comment, when it was made -- plus the
request's status, `required_approvals` and the number of DISTINCT approvers, so the removal is
reconstructible from the audit log. The removal is recorded as audit action
`approval_decision.duplicate_removed` and is findable through
`GET /audit-log?action=approval_decision.duplicate_removed`. The project timeline does not read
these entries (it never reads `approval_request:` audit targets), so it does not show why the row
is gone.

A request that was APPROVED on the strength of those duplicates (one person counted twice
toward a two-person rule) is NOT moved back to PENDING: it may already be deployed, and
un-approving it is not a migration's call. Its audit entry shows `distinct_approvers` below
`required_approvals` while `request_status` is `APPROVED`, which is how an operator finds it.

The migration is deliberately frozen: literal strings and raw SQL, no import from `app`, so a
later rename in application code cannot change what this did.

Downgrade drops the index only. The removed rows are not restored: they were counted twice,
and what they held is in the audit entries.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd2c8f5a71e39'
down_revision: Union[str, None] = 'b3f6a1d94c28'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = 'uq_approval_decisions_one_approve_per_actor'

_decisions = sa.table('approval_decisions', sa.column('id', sa.String()))

_audit_log = sa.table(
    'audit_log',
    sa.column('id', sa.String()),
    sa.column('actor_id', sa.String()),
    sa.column('action', sa.String()),
    sa.column('target', sa.String()),
    sa.column('metadata_json', sa.JSON()),
    sa.column('ts', sa.DateTime(timezone=True)),
)


def _remove_duplicate_approvals(bind) -> None:
    # Ordered in SQL so that the first row of each (request, actor) group is the earliest:
    # `created_at` is a string on SQLite and a timestamptz on Postgres, and each sorts correctly
    # in its own engine. Python only groups.
    rows = bind.execute(
        sa.text(
            "SELECT d.id, d.approval_request_id, d.actor_id, d.comment, d.created_at, "
            "r.status AS request_status, r.required_approvals "
            "FROM approval_decisions d JOIN approval_requests r ON r.id = d.approval_request_id "
            "WHERE d.decision = 'approve' "
            "ORDER BY d.approval_request_id, d.actor_id, d.created_at, d.id"
        )
    ).fetchall()

    first: dict[tuple[str, str], str] = {}
    actors_by_request: dict[str, set[str]] = {}
    extras = []
    for row in rows:
        key = (row.approval_request_id, row.actor_id)
        actors_by_request.setdefault(row.approval_request_id, set()).add(row.actor_id)
        if key in first:
            extras.append((row, first[key]))
        else:
            first[key] = row.id

    now = datetime.now(timezone.utc)
    for row, kept_id in extras:
        bind.execute(sa.delete(_decisions).where(_decisions.c.id == row.id))
        bind.execute(
            sa.insert(_audit_log).values(
                id=str(uuid.uuid4()),
                actor_id=None,
                action='approval_decision.duplicate_removed',
                target=f'approval_request:{row.approval_request_id}',
                metadata_json={
                    'removed_decision_id': row.id,
                    'kept_decision_id': kept_id,
                    'actor_id': row.actor_id,
                    'comment': row.comment,
                    'decision_created_at': str(row.created_at),
                    'request_status': row.request_status,
                    'required_approvals': row.required_approvals,
                    'distinct_approvers': len(actors_by_request[row.approval_request_id]),
                    'cause': f'migration {revision}: more than one approve decision by one actor',
                },
                ts=now,
            )
        )


def upgrade() -> None:
    _remove_duplicate_approvals(op.get_bind())
    op.create_index(
        _INDEX,
        'approval_decisions',
        ['approval_request_id', 'actor_id'],
        unique=True,
        sqlite_where=sa.text("decision = 'approve'"),
        postgresql_where=sa.text("decision = 'approve'"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name='approval_decisions')
