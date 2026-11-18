<%!
# ADR 0023 §Schema migrations: this service's migrations are
# forward-only; rollback is a runtime-only container switch, never a
# schema downgrade. The default Alembic
# scaffold's `downgrade()` stub is a bare "pass" when autogenerate finds
# nothing to reverse -- a no-op that LOOKS safe to run and isn't, since
# every caller of `alembic downgrade` assumes the reverse actually
# happened. This template's revision template raises instead, so a
# freshly generated migration says "this path does not exist" rather than
# silently promising one it can't keep. (Also keeps a hand-authored
# downgrade() from ever containing an `op.drop_*` call that would flag
# the destructive-DDL policy check on a file the shipped initial
# migration itself doesn't touch.)
DOWNGRADE_NOT_SUPPORTED = (
    'raise NotImplementedError('
    '"downgrades are not supported -- migrations are forward-only '
    '(ADR 0023 section Schema migrations); roll back by redeploying '
    'the previous image instead.")'
)
%>"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else DOWNGRADE_NOT_SUPPORTED}
