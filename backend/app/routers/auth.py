import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import get_current_user, require_role
from app.models.user import Team, User
from app.schemas.user import TeamOut, UsableAdminCountOut, UserOut, UserRoleUpdate
from app.services import oidc, platform_session
from app.services.roles import (
    LastAdminError,
    Role,
    UnknownRoleError,
    change_user_role,
    usable_admin_ids,
)
from app.services.user_directory import list_visible_users
from app.services.user_provisioning import (
    IdentityClaimsError,
    IdentityConflictError,
    provision_user_from_claims,
)

logger = logging.getLogger("app.auth")

router = APIRouter(tags=["auth"])


@router.get("/users", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[User]:
    """"List users / teams —
    authenticated". Closes a pre-existing gap: the directory exposed
    every name, email and role unauthenticated.

    what a non-admin sees is narrowed to `ACTIVE` users plus deactivated
    ones the platform still refers to; `PENDING` signup requests (attacker-chosen
    names and emails) are admin-only, along with never-used `REJECTED` rows. See
    `services/user_directory`. The frontend's only readers of this endpoint are
    admin-console panels, which see everything."""
    return list_visible_users(db, user)


@router.get("/users/usable-admin-count", response_model=UsableAdminCountOut)
def get_usable_admin_count(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_role(Role.ADMIN)),
) -> UsableAdminCountOut:
    """the number of admins the last-admin guard counts, for the role console
    to show. Admin-only, like the console it feeds. It calls `usable_admin_ids` -- the
    one predicate behind the 409 on demotion and deactivation -- so the panel cannot
    say 2 while the guard answers 1. `GET /users` stays as it was: `UserOut` carries
    neither status nor any credential flag, and a browser-side recount of `role` rows
    could not know either."""
    return UsableAdminCountOut(count=len(usable_admin_ids(db)))


@router.get("/users/me", response_model=UserOut)
def get_me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@router.patch("/users/{user_id}/role", response_model=UserOut)
def change_user_role_endpoint(
    user_id: str,
    body: UserRoleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(Role.ADMIN)),
) -> User:
    """only `admin` may change a role,
    the change is audit-logged with both old and new values, and the last
    remaining `admin` cannot be demoted (409) — otherwise the platform locks
    itself out of role management permanently. Self-demotion is allowed while
    another admin remains.

    The API only; the role-management console is a later addition."""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        change_user_role(db, actor_id=current_user.id, target=target, new_role=body.role)
    except UnknownRoleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LastAdminError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    db.commit()
    db.refresh(target)
    return target


@router.get("/teams", response_model=list[TeamOut])
def list_teams(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[Team]:
    """Authenticated, same gap and same reason as `GET /users`."""
    return list(db.scalars(select(Team).order_by(Team.name)))


# --- OIDC login flow --------------------


def _require_oidc_mode() -> None:
    if not settings.oidc_enabled:
        raise HTTPException(status_code=404, detail="OIDC login is not enabled")


def _safe_next_path(raw: str | None) -> str:
    """only a same-site
    absolute path is honoured, so `?next=` can never be turned into an open
    redirect to another host. Each rejection condition stands on its own —
    empty, not `/`-prefixed, `//`-prefixed (protocol-relative), a backslash
    anywhere, or any ASCII control character (U+0000-U+001F, U+007F)
    anywhere. The backslash and control-character rules exist for the same
    reason: a browser treats `\\` as `/` and strips tab/LF/CR before parsing
    a URL, so `/\\host` and `/<TAB>/host` both resolve to `//host` — another
    origin — while looking like same-site paths to a check that only tests
    the first two conditions. Everything else passes through **unchanged**:
    this function never repairs input, it only accepts or falls back."""
    if (
        not raw
        or not raw.startswith("/")
        or raw.startswith("//")
        or "\\" in raw
        or any(ord(ch) <= 0x1F or ord(ch) == 0x7F for ch in raw)
    ):
        return "/"
    return raw


def _redirect_uri(request: Request) -> str:
    """The callback URL as registered with the IdP. Derived from the callback
    route itself unless `OIDC_REDIRECT_URL` overrides it (needed behind a
    TLS-terminating proxy, where the request's own scheme/host is internal).

    Prod never trusts the request's own URL: unset,
    it is `PLATFORM_PUBLIC_URL` + the callback route's path (`Settings` guarantees
    the origin exists in prod)."""
    if settings.oidc_redirect_url:
        return settings.oidc_redirect_url
    if settings.is_prod:
        assert settings.platform_public_url is not None  # refused at startup otherwise
        return settings.platform_public_url + request.app.url_path_for("oidc_callback")
    return str(request.url_for("oidc_callback"))


@router.get("/auth/login")
def oidc_login(request: Request, next: str | None = None) -> RedirectResponse:
    """Generates `state` + a PKCE `code_verifier`,
    carries both through the redirect in a short-lived signed cookie (no
    server-side store), and sends the browser to the IdP."""
    _require_oidc_mode()
    try:
        auth_request = oidc.build_authorization_request(_redirect_uri(request))
    except oidc.OidcError as exc:
        logger.warning("oidc login start failed: %s", exc)
        raise HTTPException(status_code=502, detail="identity provider unavailable") from exc

    response = RedirectResponse(auth_request.authorization_url, status_code=302)
    platform_session.set_login_state_cookie(
        response,
        platform_session.issue_login_state(
            state=auth_request.state,
            code_verifier=auth_request.code_verifier,
            next_path=_safe_next_path(next),
        ),
    )
    return response


@router.get("/auth/callback", name="oidc_callback")
def oidc_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Verifies `state`, exchanges the code with
    the PKCE verifier + client secret, validates the ID token, provisions the
    user, and sets the session cookie.

    Every failure is a 401 whose logged reason never contains the code or the
    token (spec step 3)."""
    _require_oidc_mode()

    def _fail(reason: str) -> HTTPException:
        logger.warning("oidc callback rejected: %s", reason)
        return HTTPException(status_code=401, detail="login failed")

    if error:
        # The IdP's own error code (e.g. access_denied) — a fixed vocabulary,
        # not user-supplied token material.
        raise _fail(f"identity provider returned error={error!r}")
    if not code or not state:
        raise _fail("callback missing code or state")

    try:
        login_state = platform_session.verify_login_state(
            request.cookies.get(platform_session.login_state_cookie_name())
        )
    except platform_session.SessionError as exc:
        raise _fail(f"login-state cookie: {exc}")

    # Bytes, not str: a non-ASCII `state` from the client would make `compare_digest` raise
    # TypeError (a 500); it must be the same 401 as any other mismatch.
    if not hmac.compare_digest(login_state.state.encode(), state.encode()):
        raise _fail("state mismatch")

    try:
        id_token = oidc.exchange_code_for_id_token(
            code=code,
            code_verifier=login_state.code_verifier,
            redirect_uri=_redirect_uri(request),
        )
        claims = oidc.validate_id_token(id_token)
    except oidc.OidcError as exc:
        raise _fail(str(exc))

    try:
        user = provision_user_from_claims(db, claims)
    except (IdentityClaimsError, IdentityConflictError) as exc:
        db.rollback()
        raise _fail(f"provisioning: {exc}")
    db.commit()

    response = RedirectResponse(login_state.next_path, status_code=302)
    platform_session.set_session_cookie(response, platform_session.issue_session(user.id))
    platform_session.clear_login_state_cookie(response)
    return response


@router.post("/auth/logout", status_code=204)
def logout(response: Response) -> None:
    """Clears the session cookie. IdP-side single
    logout is not attempted, and the stateless cookie cannot be
    invalidated server-side."""
    platform_session.clear_session_cookie(response)
