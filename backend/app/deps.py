from collections.abc import Callable

from fastapi import Depends, Header, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models.user import User
from app.services.account_status import is_active
from app.services.platform_session import (
    session_cookie_name,
    SessionError,
    refresh_session,
    set_session_cookie,
    should_refresh_session,
    verify_session,
)
from app.services.roles import holds_at_least


def get_current_user(
    request: Request,
    response: Response,
    x_forge_user_id: str | None = Header(default=None, alias="X-Forge-User-Id"),
    db: Session = Depends(get_db),
) -> User:
    """The single identity seam (ADR 0018 §6).

    All three auth modes resolve here and nothing downstream knows which one
    is active. `oidc` and `local` both resolve the signed session cookie
    (`local`
    reuses the `oidc` session representation verbatim) — the branch is on
    the `session_auth_enabled` *capability*, derived once at startup in
    `config.py`, never on `AUTH_MODE` itself.
    """
    if settings.session_auth_enabled:
        return _user_from_session(request, response, db)
    return _user_from_dev_header(x_forge_user_id, db)


def require_role(minimum: str) -> Callable[..., User]:
    """Authorization sits at the same seam as authentication:
    `Depends(require_role(R))`
    both resolves the current user and asserts they hold at least `R`.

    The role is read from the DB row `get_current_user` just loaded — never
    from the session cookie, and never cached
    — so a demotion takes effect on the user's very next request."""

    def _require(current_user: User = Depends(get_current_user)) -> User:
        if not holds_at_least(current_user.role, minimum):
            raise HTTPException(status_code=403, detail=f"{minimum} role required")
        return current_user

    return _require


def _user_from_dev_header(x_forge_user_id: str | None, db: Session) -> User:
    """Dev-mode stand-in: `X-Forge-User-Id` resolved against seeded users.
    Unreachable in prod — `Settings` refuses to construct with
    `FORGE_ENV=prod` + `AUTH_MODE=dev`."""
    if not x_forge_user_id:
        raise HTTPException(status_code=401, detail="Missing X-Forge-User-Id header")
    user = db.get(User, x_forge_user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Unknown user identity")
    return user


def _user_from_session(request: Request, response: Response, db: Session) -> User:
    """`oidc` and `local` modes both land here: the signed session cookie is
    the identity. An expired, unrecognised or tampered session is 401, never
    a silent downgrade to anonymous."""
    try:
        session = verify_session(request.cookies.get(session_cookie_name()))
    except SessionError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid session: {exc}") from exc

    user = db.get(User, session.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Unknown user identity")

    # status is
    # enforced HERE, at the seam, not at the login endpoint — a `PENDING` or
    # `REJECTED` row presenting an otherwise-valid session cookie is 401,
    # never a logged-in user with fewer permissions. Checked on every
    # request (like `role`, never cached), so access revoked mid-session
    # stops working on the very next request rather than at the cookie's
    # 7-day expiry. A no-op for `oidc`/dev-seeded rows, which are always
    # ACTIVE (the `User.status` column default) — this line does not change
    # their behaviour.
    if not is_active(user.status):
        raise HTTPException(status_code=401, detail="Account is not active")

    # `role` is read from this fresh DB row, never from the cookie — a
    # demotion applies from the next request on.
    if should_refresh_session(session):
        set_session_cookie(response, refresh_session(session))
    return user
