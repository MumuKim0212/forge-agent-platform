"""backfill project status from history

Revision ID: d91c3e7a5b28
Revises: a74e5c2b8d31
Create Date: 2026-10-13 00:00:00.000000

Data migration, no schema change.
Previously nothing moved `projects.status` past
`SUBMITTED`, so every existing project reads `SUBMITTED` however far it got.
Each non-ARCHIVED project is moved once to the LATEST stage its own history
evidences, using exactly these columns (a stage is only ever moved forward to,
never back from, whatever the row holds now):

  ACTIVE             `deployments.status` IN (ACTIVE, SUPERSEDED, STOPPED) for
                     the project -- those states are only reachable through
                     ACTIVE (a FAILED/DEPLOYING row never was), so "was ever
                     activated" is read off the row, not inferred.
  AWAITING_APPROVAL  `approval_requests.status` = PENDING for the project.
  PREVIEW_READY      `builds.status` = SUCCEEDED AND (`builds.preview_url` is
                     set, OR `audit_log` holds a `preview.issued` entry whose
                     target is `build:<that build id>`). The audit half is
                     needed because a preview's columns are cleared when its
                     TTL container is reaped or the build is promoted, which
                     is exactly when an old project's evidence would vanish.
  BUILD_PENDING      `builds.status` <> NOT_IMPLEMENTED for the project. A
                     `NOT_IMPLEMENTED` build is the slice-1 stub that was
                     never queued (ADR 0001), and the live transition fires on
                     a QUEUED build only, so the backfill agrees with it: a
                     project whose only builds are stubs stays SUBMITTED.
  (none of these)    unchanged.

`ARCHIVED` rows are left as they are, and so is the `previous_status` their
`project.archived` audit entry recorded: un-archiving a legacy project restores
what that entry says. Each project that is moved gets one
`project.status_changed` audit entry (old, new, cause `backfill`, no actor), so
the timeline can say why a status changed under it.

The migration is deliberately frozen: literal status strings and raw SQL, no
import from `app`, so a later rename in application code cannot change what
this did.

Downgrade is a no-op. The old value (always SUBMITTED, or a seed-set status)
is not recoverable per row without re-deriving it, the schema is untouched,
and a project that has really reached a stage must not be reset to a status
that is wrong about it. The `project.status_changed` entries stay as the
record of what was done.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd91c3e7a5b28'
down_revision: Union[str, None] = 'a74e5c2b8d31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Forward order, earliest first. ARCHIVED is not in it: those rows are skipped.
_ORDER = (
    'DRAFT',
    'SUBMITTED',
    'BUILD_PENDING',
    'PREVIEW_READY',
    'AWAITING_APPROVAL',
    'ACTIVE',
)

_EVIDENCE = (
    (
        'ACTIVE',
        "SELECT 1 FROM deployments WHERE project_id = :p "
        "AND status IN ('ACTIVE', 'SUPERSEDED', 'STOPPED') LIMIT 1",
    ),
    (
        'AWAITING_APPROVAL',
        "SELECT 1 FROM approval_requests WHERE project_id = :p AND status = 'PENDING' LIMIT 1",
    ),
    (
        'PREVIEW_READY',
        "SELECT 1 FROM builds b WHERE b.project_id = :p AND b.status = 'SUCCEEDED' "
        "AND (b.preview_url IS NOT NULL OR EXISTS ("
        "SELECT 1 FROM audit_log a WHERE a.action = 'preview.issued' "
        "AND a.target = 'build:' || b.id)) LIMIT 1",
    ),
    (
        'BUILD_PENDING',
        "SELECT 1 FROM builds WHERE project_id = :p AND status <> 'NOT_IMPLEMENTED' LIMIT 1",
    ),
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


def _latest_evidenced_stage(bind, project_id: str):
    # _EVIDENCE is ordered latest-first, so the first hit is the latest stage.
    for stage, query in _EVIDENCE:
        if bind.execute(sa.text(query), {'p': project_id}).first() is not None:
            return stage
    return None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, status FROM projects WHERE status <> 'ARCHIVED'")
    ).fetchall()
    for project_id, current in rows:
        target = _latest_evidenced_stage(bind, project_id)
        if target is None or current not in _ORDER:
            continue
        if _ORDER.index(target) <= _ORDER.index(current):
            continue  # forward only: never move a row back, or sideways
        bind.execute(
            sa.text("UPDATE projects SET status = :new WHERE id = :p"),
            {'new': target, 'p': project_id},
        )
        bind.execute(
            sa.insert(_audit_log).values(
                id=str(uuid.uuid4()),
                actor_id=None,
                action='project.status_changed',
                target=f'project:{project_id}',
                metadata_json={'old': current, 'new': target, 'cause': 'backfill'},
                ts=datetime.now(timezone.utc),
            )
        )


def downgrade() -> None:
    # No-op by design: see the module docstring.
    pass
