"""The role model: one declared role set, one comparison ladder, one grant
operation.

Before this module authorization was scattered string comparison
(`user.role == "admin"`, `role not in ("approver", "admin")`). Everything
that asks "may this user do X?" now goes through `holds_at_least`, so no
router hand-writes a role string again.

Two things this module deliberately does *not* do:

- It does not model project ownership. Ownership is a separate axis on
  `projects.owner_id`, checked independently of role — see `routers/deployments.py`.
- It is not a policy engine. Three roles and one total ladder do not need
  one; a table lookup is the whole mechanism.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.services.account_status import AccountStatus
from app.services.audit import write_audit_log

if TYPE_CHECKING:  # `models/user.py` imports `Role` for its column default
    from app.models.user import User


class Role:
    """The three values already stored in `users.role` — `app/seed.py` seeds
    exactly these, so declaring them costs no migration and no rename
.

    Plain strings in a constant class, not an `Enum`, matching the
    `ProjectStatus`/`BuildStatus` precedent in `services/status.py`
    ("validated by a Python class of constants, not a
    DB-native enum")."""

    REQUESTER = "requester"
    APPROVER = "approver"
    ADMIN = "admin"


# The ladder is total — admin > approver > requester. A value that
# is NOT a key here holds *no* privileges: see `holds_at_least`.
_RANK: dict[str, int] = {Role.REQUESTER: 1, Role.APPROVER: 2, Role.ADMIN: 3}

# Every value `users.role` may legally hold, and therefore every value a role
# grant may set. `requester` is included: the spec's "grantable: no" means
# nobody needs it *granted* (it is the default every row starts with), but
# revoking `approver`/`admin` means writing it back, and self-demotion
# must work whenever another admin remains.
ALL_ROLES: tuple[str, ...] = (Role.REQUESTER, Role.APPROVER, Role.ADMIN)


def holds_at_least(role: str | None, minimum: str) -> bool:
    """The one "does this user hold at least role R?" helper.

    **Fail-closed**: an unrecognised value — or `None` — grants no
    privileges. Not baseline, and not an error either: such a user is still
    authenticated and can do everything that needs no role at all, they just
    hold no rank on the ladder.

    `minimum` is always one of `Role`'s constants supplied by our own code; a
    typo there is a programming error and raises rather than silently
    admitting or denying everyone."""
    rank = _RANK.get(role or "")
    if rank is None:
        return False
    return rank >= _RANK[minimum]


def may_read_build_trace(user: User, project_owner_id: str | None) -> bool:
    """a build's trace is
    for the project's owner, and for `approver` and above. NARROWER than the timeline on
    purpose -- the trace carries diff content and whatever the agent read.

    Ownership is its own axis, so the two halves are independent: the owner
    needs no role, and `approver` needs no ownership. `admin` clears the `approver` rung.
    Fail-closed: a project with no owner, or an unrecognised role, leaves the other half
    as the only way in."""
    is_owner = project_owner_id is not None and user.id == project_owner_id
    return is_owner or holds_at_least(user.role, Role.APPROVER)


class UnknownRoleError(ValueError):
    """The requested new role is not one of `ALL_ROLES`. Surfaced as 400 —
    accepting it would write a value that `holds_at_least` then treats as no
    privileges at all, i.e. a silent lockout of that user."""


class LastAdminError(RuntimeError):
    """Demoting this user would leave the platform with zero admins
    (the last `admin` cannot be demoted or deactivated).
    Without the guard the platform locks itself out of role management
    permanently, since only an admin may grant roles. Surfaced as 409."""


def usable_admin_ids(db: Session) -> set[str]:
    """The ids of the admins the platform can still be run by: the one
    predicate every last-admin guard reads.

    A row is a *usable* admin when ALL of these hold:

    - `role == admin`;
    - `status == ACTIVE` -- a `REJECTED` (deactivated) or `PENDING` admin row
      resolves to nobody at the identity seam, so counting it let A deactivate B and then demote
      themselves, leaving zero admins who could act;
    - it holds the credential the running mode authenticates with. In `local`
      mode that is a password: `password_hash IS NULL` can never log in,
      and the dev seed's admin has none, so it is not an admin
      anyone can sign in as. In `oidc` mode it is the IdP subject: login looks
      a row up by `oidc_sub`, never by email, so a row with no `oidc_sub` cannot be
      logged in as. In
      `dev` mode identity is a header naming the row, so there is no further
      condition. Read through the two capabilities, never `auth_mode` itself.

    Role and status are separate axes and stay separate: deactivating an admin
    does NOT clear `role` (a role change is an audited
    admin grant with both values recorded; the status already makes the role
    inert, and re-approval sets the role afresh, defaulting to `requester`).
    The predicate is what makes the leftover role harmless.

    Returns a set rather than a count so the callers can ask both "is the target
    one of them?" and "is anyone else?" from one query against one predicate."""
    # Local import, same reason as before: `models/user.py` reads `Role` for the
    # column default, so a module-level import back into models is a cycle.
    from app.models.user import User

    conditions = [User.role == Role.ADMIN, User.status == AccountStatus.ACTIVE]
    if settings.local_accounts_enabled:
        conditions.append(User.password_hash.is_not(None))
    if settings.oidc_enabled:
        conditions.append(User.oidc_sub.is_not(None))
    return set(db.scalars(select(User.id).where(*conditions)))


def would_leave_no_usable_admin(db: Session, user: User) -> bool:
    """True when `user` is the only usable admin, i.e. taking admin away from
    them -- by demotion or by deactivation -- leaves nobody. A target who is not a
    usable admin themselves (a deactivated admin, a passwordless seed row) cannot
    strand the platform by losing a role that already does nothing, so this is
    False for them even when no usable admin exists."""
    return usable_admin_ids(db) == {user.id}


def ensure_not_last_admin(db: Session, user: User) -> None:
    """Raises `LastAdminError` if `user` is the platform's only remaining
    usable admin (`usable_admin_ids`). `change_user_role` applies this exact
    check to a role *demotion*; it is reused verbatim for account
    *deactivation* ("the last admin
    cannot be deactivated... obeys the same guard" as demotion) rather than
    re-implementing the admin query a second time."""
    if would_leave_no_usable_admin(db, user):
        raise LastAdminError("cannot deactivate the last remaining admin")


def change_user_role(db: Session, *, actor_id: str, target: User, new_role: str) -> str:
    """Applies one role change and returns the *previous* role.

    Raises `UnknownRoleError` / `LastAdminError` rather than an HTTPException
    — the router maps those to status codes.
    Does not commit; the caller owns the transaction (same convention as
    `services/audit.py` / `services/approval.py`).

    The change is recorded in the append-only `AuditLog` with the granting
    admin as actor and **both** role values (history is
    not lost by using a mutable column)."""
    if new_role not in ALL_ROLES:
        raise UnknownRoleError(f"unknown role {new_role!r}")

    old_role = target.role

    # Only a demotion *away from* admin can strand the platform, so the query
    # runs only on that path.
    if old_role == Role.ADMIN and new_role != Role.ADMIN:
        if would_leave_no_usable_admin(db, target):
            raise LastAdminError("cannot demote the last remaining admin")

    target.role = new_role
    write_audit_log(
        db,
        actor_id=actor_id,
        action="user.role_changed",
        target=f"user:{target.id}",
        metadata={"old_role": old_role, "new_role": new_role},
    )
    return old_role
