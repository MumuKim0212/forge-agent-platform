"""Signup requests and the admin approval surface (ADR 0022 §6-9).

Does not commit -- callers own the transaction (same convention as
`services/audit.py` / `services/roles.py` / `services/approval.py`); every
write function here returns after `write_audit_log`, which itself only
`db.add()`s the entry.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.user import User
from app.services.account_status import AccountStatus, ALL_STATUSES
from app.services.audit import write_audit_log
from app.services.passwords import hash_password, verify_password
from app.services.roles import ALL_ROLES, LastAdminError, Role, UnknownRoleError, ensure_not_last_admin


def normalize_email(email: str) -> str:
    """The one email-normalization function every local-mode lookup goes
    through (signup, login, the bootstrap admin, and login-attempt
    lockout's counter key) -- spec: "normalized to lower case"; login
    "email match is case-insensitive"."""
    return email.strip().lower()


class InvalidTransitionError(RuntimeError):
    """The requested admin action does not apply to the target row's
    current `status` -- the account-status transition
    table is the complete list of legal moves; anything else is rejected
    rather than silently reinterpreted. Surfaced as 400."""


def request_signup(
    db: Session, *, email: str, name: str, department: str, password: str
) -> User | None:
    """Creates a `PENDING` row with `role = requester` and a hashed
    password. Returns `None`, and leaves the existing row **completely
    untouched**, when the (normalized) email already belongs to a row --
    spec rule 2: no password overwrite, no status change, no second row,
    and the caller's HTTP response must be identical either way (an
    informative "that email is taken" reply would turn the public form into
    an account-existence oracle; overwriting would turn it into a
    password-reset primitive).

    **The password is hashed before the lookup, on both paths**:
    "the miss paths cost what the hit paths cost". The
    response is identical whether or not the email exists, and that includes
    its timing: a request for an existing email that skipped the hash would
    return a couple of hundred milliseconds sooner and tell the caller the
    address is taken, which is the oracle rule 2 forbids. On the existing-email
    path the hash is computed and thrown away."""
    normalized = normalize_email(email)
    password_hash = hash_password(password)
    existing = db.query(User).filter(User.email == normalized).first()
    if existing is not None:
        return None

    user = User(
        email=normalized,
        name=name,
        department=department,
        role=Role.REQUESTER,
        status=AccountStatus.PENDING,
        password_hash=password_hash,
    )
    db.add(user)
    db.flush()
    return user


def list_signup_requests(db: Session, *, status: str) -> list[User]:
    if status not in ALL_STATUSES:
        raise ValueError(f"unknown status filter {status!r}")
    return list(
        db.query(User).filter(User.status == status).order_by(User.created_at)
    )


def approve_signup(db: Session, *, actor_id: str, target: User, role: str | None) -> None:
    """`PENDING` or `REJECTED` -> `ACTIVE`. The approving admin may set the role
    at the same time; default `requester`."""
    if target.status not in (AccountStatus.PENDING, AccountStatus.REJECTED):
        raise InvalidTransitionError("only a pending or rejected request can be approved")

    new_role = role or Role.REQUESTER
    if new_role not in ALL_ROLES:
        raise UnknownRoleError(f"unknown role {new_role!r}")

    old_status, old_role = target.status, target.role
    target.status = AccountStatus.ACTIVE
    target.role = new_role
    write_audit_log(
        db,
        actor_id=actor_id,
        action="user.signup_approved",
        target=f"user:{target.id}",
        metadata={
            "old_status": old_status,
            "new_status": target.status,
            "old_role": old_role,
            "new_role": target.role,
        },
    )


def reject_signup(db: Session, *, actor_id: str, target: User, reason: str | None) -> None:
    """`PENDING` -> `REJECTED`. A reason is optional and recorded on the
    audit entry, never on the row itself."""
    if target.status != AccountStatus.PENDING:
        raise InvalidTransitionError("only a pending request can be rejected")

    old_status = target.status
    target.status = AccountStatus.REJECTED
    write_audit_log(
        db,
        actor_id=actor_id,
        action="user.signup_rejected",
        target=f"user:{target.id}",
        metadata={"old_status": old_status, "new_status": target.status, "reason": reason},
    )


def reopen_signup(db: Session, *, actor_id: str, target: User) -> None:
    """`REJECTED` -> `PENDING` -- puts the request back in the queue for
    reconsideration without granting access (that's `approve_signup`, which
    also accepts a `REJECTED` source directly for "approve without
    reopening first")."""
    if target.status != AccountStatus.REJECTED:
        raise InvalidTransitionError("only a rejected request can be reopened")

    old_status = target.status
    target.status = AccountStatus.PENDING
    write_audit_log(
        db,
        actor_id=actor_id,
        action="user.signup_reopened",
        target=f"user:{target.id}",
        metadata={"old_status": old_status, "new_status": target.status},
    )


def deactivate_user(db: Session, *, actor_id: str, target: User) -> None:
    """`ACTIVE` -> `REJECTED`: an admin revoking access. Obeys the **same**
    last-admin guard as a role demotion -- deactivation is the same lock-out
    risk under a different name, so this reuses `services/roles`'s
    guard (which counts *usable* admins -- `roles.usable_admin_ids`) rather than
    re-implementing the admin query.

    Does **not** clear the target's `role`: status and role are
    separate axes, a role change is an audited admin grant,
    the `REJECTED` status already stops the row resolving at the identity seam,
    and approving it again sets the role afresh."""
    if target.status != AccountStatus.ACTIVE:
        raise InvalidTransitionError("only an active user can be deactivated")

    ensure_not_last_admin(db, target)

    old_status = target.status
    target.status = AccountStatus.REJECTED
    write_audit_log(
        db,
        actor_id=actor_id,
        action="user.deactivated",
        target=f"user:{target.id}",
        metadata={"old_status": old_status, "new_status": target.status},
    )


def admin_set_password(db: Session, *, actor_id: str, target: User) -> None:
    """The audited half of an admin-set password: the caller (router)
    already computed the new hash and assigned it to `target.password_hash`
    before calling this -- kept out of this function's own signature and
    out of the audit metadata so nothing here can accidentally end up
    logging the plaintext (spec: "every admin-set password is audited
    (actor, target -- never the value)")."""
    write_audit_log(
        db,
        actor_id=actor_id,
        action="user.password_reset_by_admin",
        target=f"user:{target.id}",
        metadata={},
    )


def change_own_password(
    db: Session, *, user: User, current_password: str, new_password: str
) -> bool:
    """Returns `False` (no change made, nothing audited) when
    `current_password` doesn't match the row's hash -- the router maps that
    to a 400 rather than this function raising, since a wrong current
    password is an expected, user-triggerable outcome, not a programming
    error."""
    if not verify_password(current_password, user.password_hash):
        return False

    user.password_hash = hash_password(new_password)
    write_audit_log(
        db,
        actor_id=user.id,
        action="user.password_changed",
        target=f"user:{user.id}",
        metadata={},
    )
    return True


__all__ = [
    "InvalidTransitionError",
    "LastAdminError",
    "UnknownRoleError",
    "admin_set_password",
    "approve_signup",
    "change_own_password",
    "deactivate_user",
    "list_signup_requests",
    "normalize_email",
    "reject_signup",
    "reopen_signup",
    "request_signup",
]
