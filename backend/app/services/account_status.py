"""The account-status lifecycle on `User` (ADR 0022 §6-7).

One declared value set, same "single declaration site" discipline as
`services/roles.Role` (only `ACTIVE` resolves
to a user, enforced at the identity seam -- not the login endpoint). No
router or service may spell one of these strings a second time; import the
class instead.

Transitions:

    (none)  -> PENDING            signup request creates the row
    PENDING -> ACTIVE             admin approval
    PENDING -> REJECTED           admin rejection
    REJECTED -> PENDING | ACTIVE  admin reopen / direct approval
    ACTIVE  -> REJECTED           admin deactivation (last-admin guard)

Rows created by `oidc` JIT provisioning and by the dev seed are `ACTIVE`
(the `User.status` column default, `models/user.py`) -- approval applies to
`local` signup requests only. This is a plain UPPER_SNAKE constant class,
not an `Enum`, matching the `ProjectStatus`/`BuildStatus`/`ApprovalStatus`
precedent rather than the lowercase `services/roles.Role`
one -- `role` reused pre-existing seeded lowercase values;
`status` is a brand-new column with no such constraint.
"""

from __future__ import annotations


class AccountStatus:
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"


ALL_STATUSES: tuple[str, ...] = (
    AccountStatus.PENDING,
    AccountStatus.ACTIVE,
    AccountStatus.REJECTED,
)


def is_active(status: str | None) -> bool:
    """The one "does this row resolve to a logged-in user?" check.
    Fail-closed: `None` or any value outside
    `ALL_STATUSES` is never active."""
    return status == AccountStatus.ACTIVE
