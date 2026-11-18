"""Who a given viewer may see in the user directory (`GET /users`).

The permission matrix ("List users / teams --
authenticated"). The directory is readable by every authenticated user, so
anything in it is visible to anyone who can log in. Two kinds of row were in it
that had no business there:

- `PENDING` rows are signup requests. In `local` mode they are created by an
  **unauthenticated** form, so their names and email addresses are strings an
  attacker chose, shown to every user as though they were colleagues.
- `REJECTED` rows that were never anyone's account -- a signup an admin
  declined -- hold the same attacker-chosen strings and are kept forever
.

What a non-admin sees instead: every `ACTIVE` user, plus a `REJECTED` user only
when the platform still refers to them -- they own a project or submitted a
change request. A deactivated colleague's name must keep resolving next to the
work they did; a declined stranger's must not appear anywhere. A `REJECTED` row
can only be referenced if it was `ACTIVE` once (a row that could never log in
cannot create a project), so "rejected and referenced" is exactly "deactivated
after real use". An unrecognised status is shown to no one but an admin
(fail-closed, like `account_status.is_active`).

An admin keeps the full view -- the approval queue and the role console need
every row.

Presentation of an owner's *name* next to a project (`owner_name`,
`requester_name`) is resolved server-side from the row and does not go through
this listing, so narrowing it cannot blank those.
"""

from __future__ import annotations

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.change_request import ChangeRequest
from app.models.project import Project
from app.models.user import User
from app.services.account_status import AccountStatus
from app.services.roles import Role, holds_at_least


def list_visible_users(db: Session, viewer: User) -> list[User]:
    """The directory as `viewer` is allowed to see it, ordered by name."""
    query = select(User).order_by(User.name)
    if holds_at_least(viewer.role, Role.ADMIN):
        return list(db.scalars(query))

    still_referenced = or_(
        User.id.in_(select(Project.owner_id)),
        User.id.in_(select(ChangeRequest.requester_id)),
    )
    visible = or_(
        User.status == AccountStatus.ACTIVE,
        and_(User.status == AccountStatus.REJECTED, still_referenced),
    )
    return list(db.scalars(query.where(visible)))
