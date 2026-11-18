"""Just-in-time user provisioning from ID-token claims (ADR 0018
§3/§5).

`sub` is the identity key: lookup is **by `sub`, never by email** — email is
mutable and reassignable in some directories. First login creates the row
(authenticating at the org IdP is what establishes org membership, so there
is no pre-registration step); every later login refreshes `name`/`email`.

Does not commit — the caller owns the transaction (same convention as
`services/audit.py` / `services/approval.py`).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.config import settings
from app.models.user import User
from app.services.roles import Role

logger = logging.getLogger("app.user_provisioning")

# the two role values this module writes now come from the single role
# declaration (`services/roles.Role`) instead of being re-spelled here — the
# role set must have exactly one definition site. Behaviour is unchanged.
DEFAULT_ROLE = Role.REQUESTER
ADMIN_ROLE = Role.ADMIN


class IdentityClaimsError(RuntimeError):
    """The ID token's claims cannot identify a platform user (no `sub`, no
    `email`) or do not vouch for its email (`email_verified` false). Surfaced
    as 401."""


class IdentityConflictError(RuntimeError):
    """The claimed email already belongs to a row with a *different* `sub`.

    The login fails with a logged conflict rather
    than reassigning or duplicating the row — that state means either a
    directory anomaly or a stale row, and guessing is worse than stopping."""


def _email_is_unverified(claims: dict) -> bool:
    """True when the IdP says it has NOT verified the address in this token.

    An **absent** claim (or an explicit `null`) is accepted: several IdPs, Entra
    ID among them, never send `email_verified`, and refusing them would make an
    IdP feature a requirement of this platform (ADR 0018 §Amendment). A claim
    that is present is read strictly: only boolean `true`, or the string "true"
    in any case (some IdPs serialize the claim as a string), counts as verified.
    `false`, "false", and any value this code does not recognise are refused --
    a claim the IdP bothered to send, in a shape that cannot be read as "yes",
    is not evidence of ownership, and guessing is worse than stopping."""
    if "email_verified" not in claims or claims["email_verified"] is None:
        return False
    value = claims["email_verified"]
    if value is True:
        return False
    if isinstance(value, str) and value.strip().lower() == "true":
        return False
    return True


def provision_user_from_claims(db: Session, claims: dict) -> User:
    # First thing, before any query, so that nothing below can create or update a
    # row for an address its owner has not proved -- including the
    # BOOTSTRAP_ADMIN_EMAILS grant at creation. An IdP that lets a user
    # self-register an unverified copy of a listed admin address would otherwise
    # hand that address to whoever got there before its owner. The refusal goes
    # through the same path as every other claim failure (a logged 401 in
    # `routers/auth.oidc_callback`); the message names no address.
    if _email_is_unverified(claims):
        logger.warning("oidc login refused: the IdP reports the email as not verified")
        raise IdentityClaimsError("id token reports email_verified as false")

    sub = str(claims.get("sub") or "").strip()
    if not sub:
        raise IdentityClaimsError("id token carried no sub claim")

    email = str(claims.get("email") or "").strip()
    if not email:
        # `User.email` is non-null and unique; without it there is no row to
        # create or refresh. Fail closed rather than inventing a placeholder.
        raise IdentityClaimsError("id token carried no email claim")

    # Display name falls back to email (required claims are
    # `sub`, `email`, and a display name).
    name = str(claims.get("name") or "").strip() or email

    user = db.query(User).filter(User.oidc_sub == sub).first()

    email_owner = db.query(User).filter(User.email == email).first()
    if email_owner is not None and (user is None or email_owner.id != user.id):
        # Includes the dev-seeded case: a row with this email but a NULL
        # `oidc_sub` is still "a different sub", so it is a conflict, not a
        # row to adopt (ADR 0018 §7: dev identities are discarded, never
        # mapped onto real ones).
        logger.warning(
            "oidc login conflict: email already belongs to user %s with a different sub",
            email_owner.id,
        )
        raise IdentityConflictError(
            "the email in this identity already belongs to another platform user"
        )

    if user is None:
        # ADR 0018 §5: the bootstrap grant applies **only here**, at row
        # creation. Re-checking on every login would silently re-promote
        # anyone an admin had deliberately demoted.
        role = (
            ADMIN_ROLE if email.lower() in settings.bootstrap_admin_email_list else DEFAULT_ROLE
        )
        user = User(oidc_sub=sub, email=email, name=name, role=role)
        db.add(user)
        db.flush()
        logger.info("provisioned platform user %s with role %s", user.id, role)
        return user

    # A changed email updates the row; it never creates a second one. `role`
    # is untouched — the platform DB, not the IdP, owns authorization
    # (ADR 0018 §4).
    user.name = name
    user.email = email
    db.flush()
    return user
