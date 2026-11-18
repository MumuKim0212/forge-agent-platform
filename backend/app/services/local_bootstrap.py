"""The `local`-mode bootstrap admin (ADR 0022 §4).

Creates the first admin row at **startup**, from configuration -- never at
first login, because in `local` mode nobody can authenticate before this
row exists (there is no IdP to prove membership, and no other admin to
approve the first signup). Mirrors the existing OIDC bootstrap-admin
discipline (ADR 0018 §5) one level further out: applied only when the row
is first **created**, never on a later startup, so a restart can never
silently reset a password the admin has since changed in-app.

Called from `main.py`'s `lifespan`, after `seed()`, and only when
`settings.local_accounts_enabled` -- `Settings.model_post_init` has
already guaranteed exactly one bootstrap email and (in prod) a compliant
password by the time this module ever runs.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.config import settings
from app.models.user import User
from app.services.account_status import AccountStatus
from app.services.passwords import hash_password
from app.services.roles import Role
from app.services.signup import normalize_email

logger = logging.getLogger("app.local_bootstrap")

# Not a real employee's display name -- deliberately generic so it reads
# unmistakably as "the row from .env", the same reason the row itself
# exists to be rotated (rotating the
# bootstrap password after creation is an in-app action).
_BOOTSTRAP_ADMIN_DISPLAY_NAME = "Bootstrap Admin"


def ensure_local_bootstrap_admin(db: Session) -> None:
    """Idempotent: a no-op once the row exists for this email. Safe to call
    on every startup, exactly like `seed()`."""
    emails = settings.bootstrap_admin_email_list
    if len(emails) != 1:
        # `Settings.model_post_init` already enforces this at construction
        # time; defensive only, never expected to trip in a running process.
        return

    email = normalize_email(emails[0])
    existing = db.query(User).filter(User.email == email).first()
    if existing is not None:
        return

    user = User(
        email=email,
        name=_BOOTSTRAP_ADMIN_DISPLAY_NAME,
        department=None,
        role=Role.ADMIN,
        status=AccountStatus.ACTIVE,
        password_hash=hash_password(settings.bootstrap_admin_password or ""),
    )
    db.add(user)
    db.commit()
    logger.info("created local-mode bootstrap admin user %s", user.id)
