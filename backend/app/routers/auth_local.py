"""`local` auth mode: signup request, local login, own-password change, and
the admin signup-approval surface (ADR 0022).

**Registered only when `settings.local_accounts_enabled`** — conditional
registration in `main.create_app`, the identical idiom `routers/
dev_identity.py` uses for the dev switcher bootstrap
: every route in this
module is meaningless without a password concept, so the whole module is
gated behind the one capability rather than each handler checking the mode
at runtime. `deps.get_current_user`/`deps.require_role` are otherwise
untouched — this file adds a third branch behind the existing identity
seam, not a second one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, require_role
from app.models.user import User
from app.schemas.local_accounts import (
    AdminSetPasswordInput,
    ChangePasswordInput,
    LocalLoginInput,
    PendingSignupOut,
    SignupAck,
    SignupApproveInput,
    SignupCreate,
    SignupRejectInput,
)
from app.schemas.user import UserOut
from app.services import login_lockout, platform_session
from app.services.account_status import ALL_STATUSES, AccountStatus
from app.services.audit import write_audit_log
from app.services.passwords import hash_password, verify_password
from app.services.roles import LastAdminError, Role, UnknownRoleError
from app.services.signup import (
    InvalidTransitionError,
    admin_set_password,
    approve_signup,
    change_own_password,
    deactivate_user,
    list_signup_requests,
    normalize_email,
    reject_signup,
    reopen_signup,
    request_signup,
)

router = APIRouter(tags=["local-accounts"])


def _generic_login_failure() -> HTTPException:
    """**One** generic message for unknown email, wrong
    password, and `password_hash IS NULL` — distinguishing them tells an
    attacker which half to keep guessing. A single call site, not a
    per-branch literal, is what keeps the status/body/headers identical
    across every rejection cause (there is nothing to accidentally vary)."""
    return HTTPException(status_code=401, detail="invalid email or password")


def _find_by_email(db: Session, normalized_email: str) -> User | None:
    return db.query(User).filter(User.email == normalized_email).first()


def _get_target_user_or_404(db: Session, user_id: str) -> User:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    return target


# --- public: signup request + local login


@router.post("/auth/signup", response_model=SignupAck, status_code=202)
def signup(body: SignupCreate, db: Session = Depends(get_db)) -> SignupAck:
    """The response is identical whether or not the
    email already exists — this handler always returns the same
    ack, and `request_signup` leaves an existing row completely untouched
    rather than raising a distinguishable error."""
    user = request_signup(
        db, email=body.email, name=body.name, department=body.department, password=body.password
    )
    if user is not None:
        # rule 4: a null actor — nobody is authenticated yet (the existing
        # nullable-actor precedent).
        write_audit_log(
            db,
            actor_id=None,
            action="user.signup_requested",
            target=f"user:{user.id}",
            metadata={"email": user.email},
        )
    db.commit()
    return SignupAck(detail="Signup request received. An administrator will review it.")


@router.post("/auth/login", response_model=UserOut)
def local_login(body: LocalLoginInput, response: Response, db: Session = Depends(get_db)) -> User:
    """Attempt limiting is checked *before* any password
    verification (`login_lockout.is_locked`), so a locked account's
    response never depends on whether the supplied password happens to be
    correct — indistinguishable from a wrong-password response by
    construction.

    **Exactly one password-hash check runs on every path** (the miss
    paths cost what the hit paths cost). An
    unknown email, a locked account and a row with no password are checked
    against a dummy hash (`verify_password(..., None)`) and the answer is
    discarded; only a real, unlocked account is checked against its own hash.
    Before this the unknown-email and locked paths returned without hashing,
    a couple of hundred milliseconds faster than a wrong password, and the
    difference told a caller which emails have accounts. The lock check is
    still made first and still decides the outcome; it just no longer skips
    the work."""
    email = normalize_email(body.email)

    user = _find_by_email(db, email)
    # The counter is keyed by the account's id: an email with no account has
    # nothing to look up and nothing to record (`services/login_lockout`).
    account_locked = user is not None and login_lockout.is_locked(user.id)

    checkable_hash = user.password_hash if user is not None and not account_locked else None
    password_ok = verify_password(body.password, checkable_hash)

    if user is None or account_locked:
        raise _generic_login_failure()

    if not password_ok:
        login_lockout.record_failure(user.id)
        raise _generic_login_failure()

    if user.status == AccountStatus.PENDING:
        # rule 3, the deliberate exception: correct credentials on a
        # pending row prove the caller holds the password, so this is not a
        # guess and does not count as a failure — reset the counter exactly
        # like a real successful login would.
        login_lockout.record_success(user.id)
        raise HTTPException(status_code=403, detail="account is awaiting admin approval")

    if user.status != AccountStatus.ACTIVE:
        # REJECTED (or any future non-pending, non-active status): the
        # generic message, per rule 3's last sentence — and counted as a
        # failure, same as a wrong password, so a rejected account cannot be
        # distinguished from a merely-mistyped one via the lockout state.
        login_lockout.record_failure(user.id)
        raise _generic_login_failure()

    login_lockout.record_success(user.id)
    platform_session.set_session_cookie(response, platform_session.issue_session(user.id))
    return user


# --- authenticated: change own password ------------


@router.post("/auth/password", status_code=204)
def change_password(
    body: ChangePasswordInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    ok = change_own_password(
        db,
        user=current_user,
        current_password=body.current_password,
        new_password=body.new_password,
    )
    if not ok:
        raise HTTPException(status_code=400, detail="current password is incorrect")
    db.commit()


# --- admin: the signup-approval surface ----


@router.get("/admin/signup-requests", response_model=list[PendingSignupOut])
def list_signup_requests_endpoint(
    status: str = AccountStatus.PENDING,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_role(Role.ADMIN)),
) -> list[PendingSignupOut]:
    if status not in ALL_STATUSES:
        raise HTTPException(status_code=400, detail=f"unknown status {status!r}")
    users = list_signup_requests(db, status=status)
    return [
        PendingSignupOut(
            id=u.id,
            email=u.email,
            name=u.name,
            department=u.department,
            requested_at=u.created_at,
            status=u.status,
        )
        for u in users
    ]


@router.post("/admin/signup-requests/{user_id}/approve", response_model=UserOut)
def approve_signup_endpoint(
    user_id: str,
    body: SignupApproveInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(Role.ADMIN)),
) -> User:
    target = _get_target_user_or_404(db, user_id)
    try:
        approve_signup(db, actor_id=current_user.id, target=target, role=body.role)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnknownRoleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    db.refresh(target)
    return target


@router.post("/admin/signup-requests/{user_id}/reject", response_model=UserOut)
def reject_signup_endpoint(
    user_id: str,
    body: SignupRejectInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(Role.ADMIN)),
) -> User:
    target = _get_target_user_or_404(db, user_id)
    try:
        reject_signup(db, actor_id=current_user.id, target=target, reason=body.reason)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    db.refresh(target)
    return target


@router.post("/admin/signup-requests/{user_id}/reopen", response_model=UserOut)
def reopen_signup_endpoint(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(Role.ADMIN)),
) -> User:
    target = _get_target_user_or_404(db, user_id)
    try:
        reopen_signup(db, actor_id=current_user.id, target=target)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    db.refresh(target)
    return target


@router.post("/admin/users/{user_id}/deactivate", response_model=UserOut)
def deactivate_user_endpoint(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(Role.ADMIN)),
) -> User:
    target = _get_target_user_or_404(db, user_id)
    try:
        deactivate_user(db, actor_id=current_user.id, target=target)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LastAdminError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    db.refresh(target)
    return target


@router.post("/admin/users/{user_id}/password", status_code=204)
def admin_set_password_endpoint(
    user_id: str,
    body: AdminSetPasswordInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(Role.ADMIN)),
) -> None:
    target = _get_target_user_or_404(db, user_id)
    target.password_hash = hash_password(body.password)
    admin_set_password(db, actor_id=current_user.id, target=target)
    db.commit()
