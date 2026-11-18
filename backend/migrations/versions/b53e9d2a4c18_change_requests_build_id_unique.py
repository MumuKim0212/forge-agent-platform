"""one change request per build: unique change_requests.build_id

Revision ID: b53e9d2a4c18
Revises: a50f1d3c7e92
Create Date: 2026-10-02 00:00:00.000000

the requester of a build is the requester
of the one `ChangeRequest` that references it, and the database -- not merely
the API's write path -- guarantees there is at most one. NULL `build_id` stays
allowed and distinct (a request that never got a build).

**The upgrade refuses duplicate data; it never resolves it.** If any `build_id`
already appears on more than one row, it raises and names them. It does not
delete, merge or pick a row: choosing which of two requests is the real one is
exactly the silent choice this constraint exists to remove, and only an operator
can make it. Nothing in the API or `demo_data` can produce such a row.

`op.batch_alter_table` per the project convention (SQLite cannot add a UNIQUE
constraint without a table rebuild, which batch mode performs).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b53e9d2a4c18'
down_revision: Union[str, None] = 'a50f1d3c7e92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# How many offending build ids the refusal message names (the rest are counted).
_NAMED_LIMIT = 10


def upgrade() -> None:
    bind = op.get_bind()
    duplicated = [
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT build_id FROM change_requests "
                "WHERE build_id IS NOT NULL "
                "GROUP BY build_id HAVING COUNT(*) > 1 "
                "ORDER BY build_id"
            )
        )
    ]
    if duplicated:
        named = ", ".join(duplicated[:_NAMED_LIMIT])
        more = (
            f" (and {len(duplicated) - _NAMED_LIMIT} more)"
            if len(duplicated) > _NAMED_LIMIT
            else ""
        )
        raise RuntimeError(
            f"cannot add the unique constraint on change_requests.build_id: "
            f"{len(duplicated)} build id(s) are referenced by more than one change request: "
            f"{named}{more}. This migration does not delete or merge rows -- an operator must "
            "decide which request is the real one for each build, remove the others, and re-run."
        )

    with op.batch_alter_table('change_requests') as batch_op:
        batch_op.create_unique_constraint('uq_change_requests_build_id', ['build_id'])


def downgrade() -> None:
    with op.batch_alter_table('change_requests') as batch_op:
        batch_op.drop_constraint('uq_change_requests_build_id', type_='unique')
