"""`local` auth mode end-to-end: signup request, login, attempt limiting,
the admin approval surface, and the identity-seam status check
(ADR 0022).

Every functional test here builds its **own** app via `main.create_app`,
after flipping the process-wide `app.config.settings` singleton to `local`
mode (`local_mode` fixture) — the shared `client`/`db_session` fixtures'
`app` was already built at import time in `dev` mode, and conditional
route registration can never be
retro-flipped by monkeypatching settings afterward. This mirrors
`test_rbac.py`'s oidc-mode factory test for route *absence*, and extends it
to full request/response behaviour for the new routes.

No Docker, no network, no real IdP — everything here is in-memory SQLite
plus the app's own bcrypt/session-cookie machinery.
"""

from __future__ import annotations

import ast
import json
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import get_db
from app.main import create_app
from app.models.audit import AuditLog
from app.models.user import User
from app.seed import SEED_USERS
from app.services import login_lockout, passwords, platform_session
from app.services.account_status import ALL_STATUSES, AccountStatus
from app.services.local_bootstrap import ensure_local_bootstrap_admin
from app.services.passwords import verify_password
from app.services.roles import Role

BOOTSTRAP_EMAIL = "bootstrap-admin@example.com"
BOOTSTRAP_PASSWORD = "correct horse battery staple"  # 27 chars, not "admin"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_lockout_counters():
    """`services/login_lockout` is a module-level, process-lifetime store
    (by design — see that module's docstring), so it persists across tests
    in the same pytest process unless reset explicitly."""
    login_lockout.reset_all()
    yield
    login_lockout.reset_all()


@pytest.fixture()
def local_mode(monkeypatch):
    """Flips the process-wide singleton into `local` auth mode — the same
    monkeypatch idiom `test_auth_oidc.py`'s `oidc_mode` fixture uses, and
    for the same reason: `deps.py`/`platform_session.py`/`login_lockout.py`
    all read `app.config.settings` at call time, so mutating the one shared
    object is what makes runtime behaviour (not just route registration)
    actually flip."""
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "bootstrap_admin_emails", BOOTSTRAP_EMAIL)
    monkeypatch.setattr(settings, "bootstrap_admin_password", BOOTSTRAP_PASSWORD)
    monkeypatch.setattr(settings, "session_signing_secret", "test-session-secret")
    monkeypatch.setattr(settings, "local_login_max_attempts", 5)
    monkeypatch.setattr(settings, "local_login_lockout_minutes", 15.0)
    yield settings


@pytest.fixture()
def local_client(local_mode, db_session):
    """A **second** app, built through the `create_app` factory *after*
    `local_mode` has flipped the singleton, so its conditional route
    registrations (`auth_local.router` present, `dev_identity.router`
    absent) reflect `local` mode. The shared `client` fixture's `app` was
    built at import time in `dev` mode and cannot register these routes no
    matter what gets monkeypatched afterward.

    Also creates the bootstrap admin the same way `main.py`'s lifespan
    would (`ensure_local_bootstrap_admin`) — every test that logs in as
    `BOOTSTRAP_EMAIL` depends on this fixture already having done so,
    mirroring a real deployment where nobody can log in before this row
    exists."""
    local_app = create_app(settings)

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    local_app.dependency_overrides[get_db] = _override_get_db
    ensure_local_bootstrap_admin(db_session)
    test_client = TestClient(local_app)
    yield test_client
    local_app.dependency_overrides.clear()


@pytest.fixture()
def bootstrap_admin(local_client, db_session) -> User:
    """The row `local_client` already created — fetched for tests that need
    to assert on it directly (e.g. the last-admin guard)."""
    return db_session.query(User).filter(User.email == BOOTSTRAP_EMAIL).first()


def _login(client, email: str, password: str):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def _signup(client, *, email="new@example.com", name="New Person", department="Ops", password="a-good-password", **extra):
    body = {"email": email, "name": name, "department": department, "password": password}
    body.update(extra)
    return client.post("/api/auth/signup", json=body)


def _assert_indistinguishable(r1, r2):
    assert r1.status_code == r2.status_code
    assert r1.json() == r2.json()
    ignore = {"date"}
    h1 = {k.lower(): v for k, v in r1.headers.items() if k.lower() not in ignore}
    h2 = {k.lower(): v for k, v in r2.headers.items() if k.lower() not in ignore}
    assert h1 == h2


# ---------------------------------------------------------------------------
# Route registration (as for the dev switcher
# bootstrap, extended to `local`)
# ---------------------------------------------------------------------------


def test_local_routes_exist_only_in_local_mode():
    """Absence asserted against the app's own OpenAPI schema, not a runtime
    404 (a 404 is also what a registered-but-broken route returns — only
    absence distinguishes them), exactly as the dev switcher bootstrap
    requires. Includes a positive control (dev mode
    genuinely has these paths absent AND `/api/dev/identities` present) so
    the absence assertion cannot be vacuous — a flat
    route scan can silently find nothing and pass forever."""
    from app.config import Settings

    local_settings = Settings(
        _env_file=None,
        forge_env="dev",
        auth_mode="local",
        bootstrap_admin_emails=BOOTSTRAP_EMAIL,
        bootstrap_admin_password=BOOTSTRAP_PASSWORD,
        session_signing_secret="test-session-secret",
    )
    local_app = create_app(local_settings)
    local_paths = local_app.openapi()["paths"]

    for path in (
        "/api/auth/signup",
        "/api/auth/password",
        "/api/admin/signup-requests",
        "/api/admin/signup-requests/{user_id}/approve",
        "/api/admin/signup-requests/{user_id}/reject",
        "/api/admin/signup-requests/{user_id}/reopen",
        "/api/admin/users/{user_id}/deactivate",
        "/api/admin/users/{user_id}/password",
    ):
        assert path in local_paths, f"{path} should be registered in local mode"
    assert "get" in local_paths["/api/auth/login"] or "post" in local_paths["/api/auth/login"]
    assert "post" in local_paths["/api/auth/login"]
    # `local` mode also has no use for the dev-header switcher (both resolve
    # identity from the session cookie) — same conditional-registration
    # idiom, widened from "not oidc" to "not session-backed".
    assert "/api/dev/identities" not in local_paths

    dev_app = create_app()  # the default, unmonkeypatched singleton: dev mode
    dev_paths = dev_app.openapi()["paths"]
    assert "/api/auth/signup" not in dev_paths
    assert "/api/admin/signup-requests" not in dev_paths
    # The control: dev mode really does register the dev switcher, so the
    # absence assertions above are about the mode, not a typo'd path.
    assert "/api/dev/identities" in dev_paths

    oidc_settings = Settings(
        _env_file=None,
        forge_env="dev",
        auth_mode="oidc",
        oidc_issuer="https://idp.example.test",
        oidc_client_id="forge",
        oidc_client_secret="shh",
        session_signing_secret="test-session-secret",
    )
    oidc_paths = create_app(oidc_settings).openapi()["paths"]
    assert "/api/auth/signup" not in oidc_paths
    assert "/api/dev/identities" not in oidc_paths


# ---------------------------------------------------------------------------
# B — data model: the single declaration site + password_hash never exposed
# ---------------------------------------------------------------------------


def test_account_status_values_match_the_spec_transition_table():
    assert set(ALL_STATUSES) == {"PENDING", "ACTIVE", "REJECTED"}


def test_no_app_module_spells_an_account_status_literal_against_dot_status():
    """Mirrors `test_rbac.py::test_no_app_module_compares_a_role_string_inline`'s
    "single declaration site" discipline, but a blind value-membership scan
    (any string constant equal to "PENDING"/"ACTIVE"/"REJECTED" anywhere in
    `app/`) is unsound here: unlike role strings, these are common English
    status words **already legitimately reused by other status enums** in
    this codebase (`ApprovalStatus.PENDING`/`.REJECTED` in
    `models/approval.py`; `ProjectStatus.ACTIVE` and `DeploymentStatus.ACTIVE`
    in `services/status.py`/`models/deployment.py`) — a value-only scan
    would flag every one of them as a false positive.

    So this scans for the specific *shape* a re-spelled account-status
    literal would take: a raw string constant compared against, or assigned
    to, a `.status` attribute access (`user.status == "PENDING"`,
    `target.status = "ACTIVE"`) instead of going through
    `AccountStatus.PENDING`/`.ACTIVE`/`.REJECTED`. Every legitimate use in
    this codebase already goes through that class, so this should find
    nothing."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    allowed = {app_dir / "services" / "account_status.py"}
    status_values = set(ALL_STATUSES)

    def _is_status_attr(node) -> bool:
        return isinstance(node, ast.Attribute) and node.attr == "status"

    def _is_offending_constant(node) -> bool:
        return isinstance(node, ast.Constant) and node.value in status_values

    offenders: list[str] = []
    scanned = sorted(app_dir.rglob("*.py"))
    assert app_dir / "models" / "user.py" in scanned
    for path in scanned:
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                sides = [node.left, *node.comparators]
                if any(_is_status_attr(s) for s in sides):
                    for s in sides:
                        if _is_offending_constant(s):
                            offenders.append(f"{path.name}:{node.lineno}: {s.value!r}")
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if (
                    any(_is_status_attr(t) for t in targets)
                    and node.value is not None
                    and _is_offending_constant(node.value)
                ):
                    offenders.append(f"{path.name}:{node.lineno}: {node.value.value!r}")

    assert offenders == []


SENTINEL_HASH = "SENTINEL-bcrypt-hash-6f19c2a0"  # noqa: S105 - test fixture, not a secret


def test_password_hash_never_appears_in_any_user_returning_response(
    local_client, db_session, bootstrap_admin
):
    """Plants a sentinel hash on a live row (the `service_env_json`
    sentinel-scan idiom) and scans the *raw response text* — not the
    schema — across every endpoint that returns user data, so a field
    absent from a Pydantic model today staying absent tomorrow is actually
    verified rather than assumed."""
    signup = _signup(local_client, email="scan-target@example.com")
    assert signup.status_code == 202
    target = db_session.query(User).filter(User.email == "scan-target@example.com").first()
    target.password_hash = SENTINEL_HASH
    db_session.commit()
    # Non-vacuous: the sentinel really is on the row every endpoint below reads.
    assert db_session.get(User, target.id).password_hash == SENTINEL_HASH

    login = _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)
    assert login.status_code == 200

    responses = [
        local_client.get("/api/users"),
        local_client.get("/api/users/me"),
        local_client.get("/api/admin/signup-requests"),
        local_client.post(f"/api/admin/signup-requests/{target.id}/approve", json={}),
        local_client.get("/api/admin/signup-requests?status=ACTIVE"),
    ]
    for response in responses:
        assert SENTINEL_HASH not in response.text
        assert "password_hash" not in response.text


def test_no_schema_module_serialises_user_via_model_dump_or_attribute_iteration():
    """`schemas/user.py`/`schemas/local_accounts.py` list fields by name
    (the `UserOut`/`PendingSignupOut` shape) rather than serialising `User`
    wholesale — the same ast-guard idiom `test_console.py` applies to
    `Settings`, applied here to `User`."""
    module_paths = [
        Path(__file__).resolve().parents[1] / "app" / "schemas" / "user.py",
        Path(__file__).resolve().parents[1] / "app" / "schemas" / "local_accounts.py",
        Path(__file__).resolve().parents[1] / "app" / "routers" / "auth_local.py",
    ]
    banned_attributes = {
        "model_dump",
        "model_dump_json",
        "model_fields",
        "model_fields_set",
        "__dict__",
        "dict",
    }
    banned_functions = {"getattr", "vars"}

    offenders: list[str] = []
    for module_path in module_paths:
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in banned_attributes:
                offenders.append(f"{module_path.name}:{node.lineno}: .{node.attr}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in banned_functions
            ):
                offenders.append(f"{module_path.name}:{node.lineno}: {node.func.id}()")
    assert offenders == []


# ---------------------------------------------------------------------------
# C — the identity seam
# ---------------------------------------------------------------------------


def test_local_session_cookie_is_the_same_platform_session(local_client, bootstrap_admin):
    """`local` reuses the `oidc` session
    cookie verbatim — same name, same signed payload shape, `role` absent."""
    response = _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)
    assert response.status_code == 200

    raw_cookie = response.cookies.get(platform_session.session_cookie_name())
    assert raw_cookie is not None

    session = platform_session.verify_session(raw_cookie)
    assert session.user_id == bootstrap_admin.id


def test_a_pending_or_rejected_row_with_a_valid_session_is_401_at_the_seam(
    local_client, db_session
):
    """Tested end to end:
    issue a REAL session via a normal login, flip `status` directly on the
    row afterward, then make a normal authenticated request with that same
    cookie — not by calling the login endpoint again."""
    signup = _signup(local_client, email="seam@example.com", password="a-good-password")
    assert signup.status_code == 202
    user = db_session.query(User).filter(User.email == "seam@example.com").first()
    user.status = AccountStatus.ACTIVE
    db_session.commit()

    login = _login(local_client, "seam@example.com", "a-good-password")
    assert login.status_code == 200
    assert local_client.get("/api/users/me").status_code == 200

    user.status = AccountStatus.PENDING
    db_session.commit()
    resp_pending = local_client.get("/api/users/me")
    assert resp_pending.status_code == 401

    user.status = AccountStatus.REJECTED
    db_session.commit()
    resp_rejected = local_client.get("/api/users/me")
    assert resp_rejected.status_code == 401

    user.status = AccountStatus.ACTIVE
    db_session.commit()
    assert local_client.get("/api/users/me").status_code == 200


def test_password_hash_is_null_can_never_authenticate(local_client, db_session):
    """ADR 0022 §3: a row that never set a password is
    not a row with an empty password."""
    user = User(
        email="no-password@example.com",
        name="No Password",
        department="Ops",
        role=Role.REQUESTER,
        status=AccountStatus.ACTIVE,
        password_hash=None,
    )
    db_session.add(user)
    db_session.commit()

    response = _login(local_client, "no-password@example.com", "")
    assert response.status_code == 401


def test_dev_mode_identity_switcher_is_unaffected_by_local_accounts(client):
    """spec: `dev`/`oidc` behaviour is byte-for-byte unchanged. The shared
    `client` fixture is unconditionally `dev` mode — this is the plainest
    possible smoke check that adding `local` did not disturb it."""
    response = client.get("/api/dev/identities")
    assert response.status_code == 200
    assert len(response.json()) == len(SEED_USERS)


def test_bootstrap_admin_password_and_role_are_applied_only_at_row_creation(
    local_client, db_session, bootstrap_admin
):
    """ADR 0022 §4: re-applying on a later
    startup would silently reset a password the admin had since changed
    in-app, and would make the `.env` default immortal. `local_client`
    already called `ensure_local_bootstrap_admin` once (row creation); this
    simulates a second process startup with the exact same settings."""
    changed_hash = "a-hash-the-admin-set-in-app-not-from-env"
    bootstrap_admin.password_hash = changed_hash
    bootstrap_admin.role = Role.REQUESTER  # simulates a deliberate demotion
    db_session.commit()

    ensure_local_bootstrap_admin(db_session)  # the "later startup"

    db_session.refresh(bootstrap_admin)
    assert bootstrap_admin.password_hash == changed_hash
    assert bootstrap_admin.role == Role.REQUESTER
    assert (
        db_session.query(User).filter(User.email == BOOTSTRAP_EMAIL).count() == 1
    )


# ---------------------------------------------------------------------------
# D — signup, login, attempt limiting
# ---------------------------------------------------------------------------


def test_signup_creates_a_pending_requester_row_with_a_hashed_password(local_client, db_session):
    response = _signup(local_client, email="alice@example.com", password="a-good-password")
    assert response.status_code == 202

    user = db_session.query(User).filter(User.email == "alice@example.com").first()
    assert user is not None
    assert user.status == AccountStatus.PENDING
    assert user.role == Role.REQUESTER
    assert user.password_hash is not None
    assert user.password_hash != "a-good-password"
    assert verify_password("a-good-password", user.password_hash) is True


def test_signup_ignores_a_role_supplied_in_the_request_body(local_client, db_session):
    response = _signup(local_client, email="bob@example.com", role=Role.ADMIN)
    assert response.status_code == 202

    user = db_session.query(User).filter(User.email == "bob@example.com").first()
    assert user.role == Role.REQUESTER


def test_signup_for_an_existing_email_is_a_complete_no_op(local_client, db_session):
    first = _signup(local_client, email="carol@example.com", password="first-password")
    assert first.status_code == 202
    before = db_session.query(User).filter(User.email == "carol@example.com").first()
    before_snapshot = {
        "id": before.id,
        "password_hash": before.password_hash,
        "status": before.status,
        "role": before.role,
        "name": before.name,
        "department": before.department,
        "created_at": before.created_at,
    }
    row_count_before = db_session.query(User).filter(User.email == "carol@example.com").count()

    second = _signup(
        local_client,
        email="carol@example.com",
        name="Someone Else",
        department="A Different Dept",
        password="a-totally-different-password",
    )

    # rule 2: the response is identical whether or not the email existed.
    assert second.status_code == first.status_code
    assert second.json() == first.json()

    after = db_session.query(User).filter(User.email == "carol@example.com").first()
    after_snapshot = {
        "id": after.id,
        "password_hash": after.password_hash,
        "status": after.status,
        "role": after.role,
        "name": after.name,
        "department": after.department,
        "created_at": after.created_at,
    }
    assert after_snapshot == before_snapshot
    assert db_session.query(User).filter(User.email == "carol@example.com").count() == row_count_before == 1
    # The would-be second signup's audit entry must not exist either.
    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "user.signup_requested", AuditLog.target == f"user:{after.id}")
        .count()
        == 1
    )


def test_signup_writes_an_audit_entry_with_a_null_actor(local_client, db_session):
    response = _signup(local_client, email="dave@example.com")
    assert response.status_code == 202
    user = db_session.query(User).filter(User.email == "dave@example.com").first()

    entry = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "user.signup_requested", AuditLog.target == f"user:{user.id}")
        .one()
    )
    assert entry.actor_id is None


def test_login_rejects_unknown_email_wrong_password_and_null_hash_identically(
    local_client, db_session
):
    """One generic message for every rejection cause."""
    _signup(local_client, email="erin@example.com", password="the-real-password")
    erin = db_session.query(User).filter(User.email == "erin@example.com").first()
    erin.status = AccountStatus.ACTIVE
    db_session.commit()

    null_hash_user = User(
        email="null-hash@example.com",
        name="Null Hash",
        department="Ops",
        role=Role.REQUESTER,
        status=AccountStatus.ACTIVE,
        password_hash=None,
    )
    db_session.add(null_hash_user)
    db_session.commit()

    unknown = _login(local_client, "does-not-exist@example.com", "whatever")
    wrong_password = _login(local_client, "erin@example.com", "the-wrong-password")
    null_hash = _login(local_client, "null-hash@example.com", "anything")

    _assert_indistinguishable(unknown, wrong_password)
    _assert_indistinguishable(wrong_password, null_hash)
    assert unknown.status_code == 401


def test_login_on_a_pending_row_gets_the_awaiting_approval_response(local_client, db_session):
    """The deliberate exception."""
    _signup(local_client, email="frank@example.com", password="a-good-password")

    response = _login(local_client, "frank@example.com", "a-good-password")

    assert response.status_code == 403
    assert "session" not in response.cookies
    assert platform_session.session_cookie_name() not in response.cookies


def test_login_on_a_rejected_row_gets_the_generic_message(local_client, db_session):
    _signup(local_client, email="grace@example.com", password="a-good-password")
    grace = db_session.query(User).filter(User.email == "grace@example.com").first()
    grace.status = AccountStatus.REJECTED
    db_session.commit()

    rejected = _login(local_client, "grace@example.com", "a-good-password")
    wrong_password = _login(local_client, "grace@example.com", "not-the-password")

    assert rejected.status_code == 401
    _assert_indistinguishable(rejected, wrong_password)


def test_five_consecutive_failures_lock_the_account_and_a_success_resets_it(
    local_client, db_session
):
    _signup(local_client, email="heidi@example.com", password="the-real-password")
    heidi = db_session.query(User).filter(User.email == "heidi@example.com").first()
    heidi.status = AccountStatus.ACTIVE
    db_session.commit()

    for _ in range(5):
        resp = _login(local_client, "heidi@example.com", "wrong")
        assert resp.status_code == 401

    # The 6th attempt, even with the CORRECT password, is locked out --
    # indistinguishable from a wrong-password response (checked before any
    # password verification happens, by construction).
    locked = _login(local_client, "heidi@example.com", "the-real-password")
    wrong_password = _login(local_client, "heidi@example.com", "still-wrong")
    assert locked.status_code == 401
    _assert_indistinguishable(locked, wrong_password)

    # Directly exercise the lockout module's own success-reset path (a
    # correct login after the lock has EXPIRED resets the counter) rather
    # than sleeping 15 real minutes in a test.
    login_lockout.reset_all()
    ok = _login(local_client, "heidi@example.com", "the-real-password")
    assert ok.status_code == 200
    assert login_lockout.is_locked(heidi.id) is False


def test_attempt_limiting_is_configurable_and_not_an_account_status(local_client, monkeypatch, db_session):
    monkeypatch.setattr(settings, "local_login_max_attempts", 2)
    monkeypatch.setattr(settings, "local_login_lockout_minutes", 15.0)
    _signup(local_client, email="ivan@example.com", password="the-real-password")
    ivan = db_session.query(User).filter(User.email == "ivan@example.com").first()
    ivan.status = AccountStatus.ACTIVE
    db_session.commit()

    assert _login(local_client, "ivan@example.com", "wrong").status_code == 401
    assert _login(local_client, "ivan@example.com", "wrong-again").status_code == 401
    assert login_lockout.is_locked(ivan.id) is True

    # The lock is a timed counter, not a status -- the row itself is
    # unaffected.
    db_session.refresh(ivan)
    assert ivan.status == AccountStatus.ACTIVE
    assert ivan.status not in ("LOCKED", "locked")


def test_the_lock_expires_on_its_own_with_no_admin_action():
    """Unit-level, against `services/login_lockout` directly (it accepts an
    explicit `now`), rather than sleeping 15 real minutes or monkeypatching
    `time.time` through an HTTP round trip."""
    key = "account-id-for-the-expiry-test"  # the counter is keyed by `User.id`
    for _ in range(5):
        login_lockout.record_failure(key, now=1_000.0)
    assert login_lockout.is_locked(key, now=1_000.0) is True
    assert login_lockout.is_locked(key, now=1_000.0 + 15 * 60 - 1) is True
    # No call to record_success -- the lock alone expires with the clock.
    assert login_lockout.is_locked(key, now=1_000.0 + 15 * 60 + 1) is False


# ---------------------------------------------------------------------------
# E — the admin signup-approval surface
# ---------------------------------------------------------------------------


def test_pending_queue_lists_email_name_department_and_requested_at(local_client, db_session):
    _signup(local_client, email="judy@example.com", name="Judy Requester", department="Sales")
    _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)

    response = local_client.get("/api/admin/signup-requests")
    assert response.status_code == 200
    rows = response.json()
    judy = next(r for r in rows if r["email"] == "judy@example.com")
    assert judy["name"] == "Judy Requester"
    assert judy["department"] == "Sales"
    assert judy["requested_at"] is not None
    assert judy["status"] == AccountStatus.PENDING


def test_only_admin_may_list_or_decide_signup_requests(local_client, db_session):
    _signup(local_client, email="kim@example.com", password="a-good-password")
    kim = db_session.query(User).filter(User.email == "kim@example.com").first()
    kim.status = AccountStatus.ACTIVE
    db_session.commit()
    _login(local_client, "kim@example.com", "a-good-password")

    assert local_client.get("/api/admin/signup-requests").status_code == 403
    assert (
        local_client.post(f"/api/admin/signup-requests/{kim.id}/approve", json={}).status_code
        == 403
    )


def test_approve_defaults_to_requester_role_and_sets_active(local_client, db_session):
    _signup(local_client, email="leo@example.com")
    leo = db_session.query(User).filter(User.email == "leo@example.com").first()
    _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)

    response = local_client.post(f"/api/admin/signup-requests/{leo.id}/approve", json={})
    assert response.status_code == 200
    assert response.json()["role"] == Role.REQUESTER

    db_session.refresh(leo)
    assert leo.status == AccountStatus.ACTIVE
    assert leo.role == Role.REQUESTER


def test_approve_may_set_a_different_role(local_client, db_session):
    _signup(local_client, email="mia@example.com")
    mia = db_session.query(User).filter(User.email == "mia@example.com").first()
    _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)

    response = local_client.post(
        f"/api/admin/signup-requests/{mia.id}/approve", json={"role": Role.APPROVER}
    )
    assert response.status_code == 200

    db_session.refresh(mia)
    assert mia.status == AccountStatus.ACTIVE
    assert mia.role == Role.APPROVER


def test_reject_sets_rejected_and_records_the_reason(local_client, db_session):
    _signup(local_client, email="nate@example.com")
    nate = db_session.query(User).filter(User.email == "nate@example.com").first()
    _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)

    response = local_client.post(
        f"/api/admin/signup-requests/{nate.id}/reject", json={"reason": "not a real employee"}
    )
    assert response.status_code == 200
    db_session.refresh(nate)
    assert nate.status == AccountStatus.REJECTED

    entry = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "user.signup_rejected", AuditLog.target == f"user:{nate.id}")
        .one()
    )
    assert entry.metadata_json["reason"] == "not a real employee"
    assert entry.metadata_json["old_status"] == AccountStatus.PENDING
    assert entry.metadata_json["new_status"] == AccountStatus.REJECTED


def test_reopen_moves_a_rejected_row_back_to_pending(local_client, db_session):
    _signup(local_client, email="olga@example.com")
    olga = db_session.query(User).filter(User.email == "olga@example.com").first()
    _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)
    local_client.post(f"/api/admin/signup-requests/{olga.id}/reject", json={})

    response = local_client.post(f"/api/admin/signup-requests/{olga.id}/reopen")
    assert response.status_code == 200

    db_session.refresh(olga)
    assert olga.status == AccountStatus.PENDING


def test_approve_also_works_directly_from_rejected(local_client, db_session):
    """Status transition: "rejected -> pending or active"."""
    _signup(local_client, email="pete@example.com")
    pete = db_session.query(User).filter(User.email == "pete@example.com").first()
    _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)
    local_client.post(f"/api/admin/signup-requests/{pete.id}/reject", json={})

    response = local_client.post(f"/api/admin/signup-requests/{pete.id}/approve", json={})
    assert response.status_code == 200
    db_session.refresh(pete)
    assert pete.status == AccountStatus.ACTIVE


@pytest.mark.parametrize(
    "action_path,body",
    [
        ("approve", {}),
        ("reject", {}),
    ],
)
def test_illegal_transitions_are_rejected(local_client, db_session, action_path, body):
    _signup(local_client, email="quinn@example.com")
    quinn = db_session.query(User).filter(User.email == "quinn@example.com").first()
    _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)
    local_client.post(f"/api/admin/signup-requests/{quinn.id}/approve", json={})  # now ACTIVE

    response = local_client.post(f"/api/admin/signup-requests/{quinn.id}/{action_path}", json=body)
    assert response.status_code == 400


def test_deactivate_sets_rejected_for_an_active_user(local_client, db_session):
    _signup(local_client, email="rex@example.com")
    rex = db_session.query(User).filter(User.email == "rex@example.com").first()
    _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)
    local_client.post(f"/api/admin/signup-requests/{rex.id}/approve", json={})

    response = local_client.post(f"/api/admin/users/{rex.id}/deactivate")
    assert response.status_code == 200
    db_session.refresh(rex)
    assert rex.status == AccountStatus.REJECTED


def test_the_last_admin_cannot_be_deactivated(local_client, db_session, bootstrap_admin):
    # `db_session` also seeds a `dev`-mode admin (`seed()`'s dev branch,
    # unrelated to `local` mode) -- demote it out of the way so
    # `bootstrap_admin` really is the platform's only admin for this test.
    dev_admin = (
        db_session.query(User)
        .filter(User.role == Role.ADMIN, User.id != bootstrap_admin.id)
        .first()
    )
    dev_admin.role = Role.REQUESTER
    db_session.commit()

    _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)
    response = local_client.post(f"/api/admin/users/{bootstrap_admin.id}/deactivate")

    assert response.status_code == 409
    db_session.refresh(bootstrap_admin)
    assert bootstrap_admin.status == AccountStatus.ACTIVE


def test_admin_can_set_another_users_password(local_client, db_session):
    _signup(local_client, email="sam@example.com", password="original-password")
    sam = db_session.query(User).filter(User.email == "sam@example.com").first()
    _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)
    local_client.post(f"/api/admin/signup-requests/{sam.id}/approve", json={})

    response = local_client.post(
        f"/api/admin/users/{sam.id}/password", json={"password": "a-brand-new-password"}
    )
    assert response.status_code == 204

    # Log out the admin, log in as sam with the new password.
    local_client.post("/api/auth/logout")
    login = _login(local_client, "sam@example.com", "a-brand-new-password")
    assert login.status_code == 200
    old_password_login = _login(local_client, "sam@example.com", "original-password")
    assert old_password_login.status_code == 401


def test_user_can_change_their_own_password(local_client, db_session):
    _signup(local_client, email="tara@example.com", password="original-password")
    tara = db_session.query(User).filter(User.email == "tara@example.com").first()
    tara.status = AccountStatus.ACTIVE
    db_session.commit()
    _login(local_client, "tara@example.com", "original-password")

    wrong_current = local_client.post(
        "/api/auth/password",
        json={"current_password": "not-it", "new_password": "a-new-password-123"},
    )
    assert wrong_current.status_code == 400

    ok = local_client.post(
        "/api/auth/password",
        json={"current_password": "original-password", "new_password": "a-new-password-123"},
    )
    assert ok.status_code == 204

    local_client.post("/api/auth/logout")
    assert _login(local_client, "tara@example.com", "a-new-password-123").status_code == 200
    assert _login(local_client, "tara@example.com", "original-password").status_code == 401


def test_every_admin_transition_is_audited_with_actor_and_old_new_values(local_client, db_session):
    _signup(local_client, email="uma@example.com")
    uma = db_session.query(User).filter(User.email == "uma@example.com").first()
    login = _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)
    admin_id = login.json()["id"]

    local_client.post(f"/api/admin/signup-requests/{uma.id}/approve", json={"role": Role.APPROVER})

    entry = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "user.signup_approved", AuditLog.target == f"user:{uma.id}")
        .one()
    )
    assert entry.actor_id == admin_id
    assert entry.metadata_json == {
        "old_status": AccountStatus.PENDING,
        "new_status": AccountStatus.ACTIVE,
        "old_role": Role.REQUESTER,
        "new_role": Role.APPROVER,
    }


def test_no_audit_entry_or_response_ever_contains_a_password_or_hash(local_client, db_session):
    plaintext_sentinel = "SENTINEL-plaintext-9f21ab"
    _signup(local_client, email="vera@example.com", password=plaintext_sentinel)
    vera = db_session.query(User).filter(User.email == "vera@example.com").first()

    login = _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)
    admin_set = local_client.post(
        f"/api/admin/users/{vera.id}/password", json={"password": plaintext_sentinel + "-2"}
    )
    approve = local_client.post(f"/api/admin/signup-requests/{vera.id}/approve", json={})

    for response in (login, admin_set, approve):
        assert plaintext_sentinel not in response.text
        assert (plaintext_sentinel + "-2") not in response.text

    rows = db_session.query(AuditLog).all()
    for row in rows:
        blob = str(row.metadata_json)
        assert plaintext_sentinel not in blob
        assert (plaintext_sentinel + "-2") not in blob
        assert "password_hash" not in blob


def test_email_must_look_like_an_address(local_client):
    response = _signup(local_client, email="not-an-email")
    assert response.status_code == 422


def test_password_shorter_than_eight_characters_is_rejected(local_client):
    response = _signup(local_client, email="short-pw@example.com", password="short1")
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# auth hardening
# ---------------------------------------------------------------------------


def _make_active_user(local_client, db_session, *, email: str, password: str = "a-good-password", role=None) -> User:
    """A real approved account, made the way production makes one: signup, then the bootstrap admin
    approves it. Leaves the client with no session."""
    assert _signup(local_client, email=email, password=password).status_code == 202
    user = db_session.query(User).filter(User.email == email).first()
    assert _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD).status_code == 200
    body = {"role": role} if role else {}
    assert local_client.post(f"/api/admin/signup-requests/{user.id}/approve", json=body).status_code == 200
    local_client.cookies.clear()
    db_session.refresh(user)
    return user


def _demote(client, target_id: str):
    return client.patch(f"/api/users/{target_id}/role", json={"role": Role.REQUESTER})


# --- the last-admin guard counts usable admins ------------------------------


def test_a_deactivated_or_reopened_admin_does_not_let_the_other_admin_step_down(
    local_client, db_session, bootstrap_admin
):
    """The finding: A deactivates B, then demotes themselves, and zero admins who can act remain.
    B keeps `role == admin` the whole time (deactivation does not clear the role) -- only the
    status decides whether the row counts, in the deactivated state and again once reopened to
    PENDING."""
    b = _make_active_user(local_client, db_session, email="b-admin@example.com", role=Role.ADMIN)
    assert b.role == Role.ADMIN and b.status == AccountStatus.ACTIVE
    _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)

    # While B is a real second admin, A could step down: the guard is not simply "always no".
    assert local_client.post(f"/api/admin/users/{b.id}/deactivate").status_code == 200
    db_session.refresh(b)
    assert (b.role, b.status) == (Role.ADMIN, AccountStatus.REJECTED)

    assert local_client.post(f"/api/admin/users/{bootstrap_admin.id}/deactivate").status_code == 409
    assert _demote(local_client, bootstrap_admin.id).status_code == 409

    assert local_client.post(f"/api/admin/signup-requests/{b.id}/reopen").status_code == 200
    db_session.refresh(b)
    assert (b.role, b.status) == (Role.ADMIN, AccountStatus.PENDING)

    assert local_client.post(f"/api/admin/users/{bootstrap_admin.id}/deactivate").status_code == 409
    assert _demote(local_client, bootstrap_admin.id).status_code == 409

    db_session.refresh(bootstrap_admin)
    assert (bootstrap_admin.role, bootstrap_admin.status) == (Role.ADMIN, AccountStatus.ACTIVE)
    assert db_session.query(AuditLog).filter(AuditLog.target == f"user:{bootstrap_admin.id}", AuditLog.action.in_(["user.deactivated", "user.role_changed"])).count() == 0


def test_a_second_usable_admin_lets_the_first_step_down(local_client, db_session, bootstrap_admin):
    """The guard's other side: with B a real, active, password-holding admin, A may demote themselves."""
    b = _make_active_user(local_client, db_session, email="b-admin@example.com", role=Role.ADMIN)
    _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)

    assert _demote(local_client, bootstrap_admin.id).status_code == 200
    db_session.refresh(bootstrap_admin)
    assert bootstrap_admin.role == Role.REQUESTER
    assert b.role == Role.ADMIN


def test_the_passwordless_seeded_admin_is_not_a_second_admin_in_local_mode(
    local_client, db_session, bootstrap_admin
):
    """`password_hash IS NULL` can never log in. The dev seed's admin is
    ACTIVE, `role == admin` and has no password, so in `local` mode it cannot stand in for a
    second admin -- and the moment an admin gives it a password, it can."""
    seeded = db_session.query(User).filter(User.role == Role.ADMIN, User.id != bootstrap_admin.id).one()
    assert seeded.password_hash is None and seeded.status == AccountStatus.ACTIVE
    _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)

    assert local_client.post(f"/api/admin/users/{bootstrap_admin.id}/deactivate").status_code == 409
    assert _demote(local_client, bootstrap_admin.id).status_code == 409

    assert local_client.post(f"/api/admin/users/{seeded.id}/password", json={"password": "a-fresh-password"}).status_code == 204
    assert _demote(local_client, bootstrap_admin.id).status_code == 200


def test_deactivating_the_passwordless_seeded_admin_is_allowed(local_client, db_session, bootstrap_admin):
    """It is not a usable admin, so losing the role it cannot use strands nobody -- the guard must
    not treat it as "the last admin" and refuse."""
    seeded = db_session.query(User).filter(User.role == Role.ADMIN, User.id != bootstrap_admin.id).one()
    _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)

    assert local_client.post(f"/api/admin/users/{seeded.id}/deactivate").status_code == 200
    db_session.refresh(seeded)
    assert seeded.status == AccountStatus.REJECTED


# --- the 72-byte password limit ----------------------------------------------

# Literals: the limit is bcrypt's 72 bytes of UTF-8, and a drift of the production constant must
# not move both sides of the assertion.
_AT_THE_LIMIT = [("a" * 72, "72-ascii-bytes"), ("가" * 24, "24-korean-characters-72-bytes")]
_OVER_THE_LIMIT = [("a" * 73, "73-ascii-bytes"), ("가" * 25, "25-korean-characters-75-bytes")]


def _set_password_via(path: str, local_client, db_session, password: str):
    """Drives one of the three paths that SET a password and returns (response, user_id, old_password)."""
    old = "the-original-password"
    if path == "signup":
        response = _signup(local_client, email="setter@example.com", password=password)
        user = db_session.query(User).filter(User.email == "setter@example.com").first()
        return response, user.id if user else None, None
    user = _make_active_user(local_client, db_session, email="setter@example.com", password=old)
    if path == "own-change":
        assert _login(local_client, "setter@example.com", old).status_code == 200
        response = local_client.post("/api/auth/password", json={"current_password": old, "new_password": password})
    else:
        assert _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD).status_code == 200
        response = local_client.post(f"/api/admin/users/{user.id}/password", json={"password": password})
    return response, user.id, old


@pytest.mark.parametrize("path", ["signup", "own-change", "admin-set"])
@pytest.mark.parametrize("password", [p for p, _ in _AT_THE_LIMIT], ids=[i for _, i in _AT_THE_LIMIT])
def test_a_password_of_exactly_72_bytes_is_accepted_on_every_set_path(local_client, db_session, path, password):
    response, user_id, _old = _set_password_via(path, local_client, db_session, password)

    assert response.status_code in (202, 204)
    stored = db_session.get(User, user_id)
    db_session.refresh(stored)
    # Stored whole: the hash verifies the full 72 bytes (nothing was silently cut).
    assert verify_password(password, stored.password_hash) is True


@pytest.mark.parametrize("path", ["signup", "own-change", "admin-set"])
@pytest.mark.parametrize("password", [p for p, _ in _OVER_THE_LIMIT], ids=[i for _, i in _OVER_THE_LIMIT])
def test_a_password_over_72_bytes_is_a_422_not_a_500_on_every_set_path(local_client, db_session, path, password):
    """bcrypt 5 raises on a 73rd byte, so without the schema check each of these was a 500. A 25-character
    Korean passphrase is 75 bytes and is the case a character-counting check would let through."""
    response, user_id, old = _set_password_via(path, local_client, db_session, password)

    assert response.status_code == 422
    message = response.json()["detail"][0]["msg"]
    assert "72바이트" in message
    assert "한글" in message
    if path == "signup":
        assert db_session.query(User).filter(User.email == "setter@example.com").first() is None, "no row for a refused signup"
    else:
        # The old password still works; nothing was changed.
        local_client.cookies.clear()
        assert _login(local_client, "setter@example.com", old).status_code == 200


def test_a_login_with_a_password_over_72_bytes_is_the_generic_failure_not_a_500(local_client, db_session):
    """Presenting a password is not setting one: the schema does not refuse it (that would be an
    oracle for nothing), the hash check just cannot match it."""
    response = _login(local_client, BOOTSTRAP_EMAIL, "a" * 73)

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid email or password"


@pytest.mark.parametrize(
    "field, value",
    [("name", "n" * 101), ("department", "d" * 101), ("email", "e" * 243 + "@example.com")],
    ids=["name-101", "department-101", "email-255"],
)
def test_signup_free_text_fields_are_bounded(local_client, db_session, field, value):
    users_before = db_session.query(User).count()

    response = _signup(local_client, **{field: value})

    assert response.status_code == 422
    assert db_session.query(User).count() == users_before


@pytest.mark.parametrize(
    "field, value",
    [("name", "n" * 100), ("department", "d" * 100), ("email", "e" * 242 + "@example.com")],
    ids=["name-100", "department-100", "email-254"],
)
def test_signup_free_text_fields_at_the_bound_are_accepted(local_client, db_session, field, value):
    assert _signup(local_client, **{field: value}).status_code == 202


# --- the miss paths do the hash work the hit paths do ------------------------


class _HashCalls:
    """Counts the two bcrypt entry points the platform uses, delegating to the real ones -- so the
    code under test behaves as it does in production and the test asserts on WORK DONE, which is
    deterministic, rather than on wall-clock time, which is not."""

    def __init__(self) -> None:
        self.checkpw = 0
        self.hashpw = 0

    def reset(self) -> None:
        self.checkpw = 0
        self.hashpw = 0


@pytest.fixture()
def hash_calls(monkeypatch) -> _HashCalls:
    calls = _HashCalls()
    real_checkpw, real_hashpw = passwords.bcrypt.checkpw, passwords.bcrypt.hashpw

    def counting_checkpw(*args, **kwargs):
        calls.checkpw += 1
        return real_checkpw(*args, **kwargs)

    def counting_hashpw(*args, **kwargs):
        calls.hashpw += 1
        return real_hashpw(*args, **kwargs)

    monkeypatch.setattr(passwords.bcrypt, "checkpw", counting_checkpw)
    monkeypatch.setattr(passwords.bcrypt, "hashpw", counting_hashpw)
    return calls


def _login_scenario(name: str, local_client, db_session, monkeypatch):
    """Sets up one login situation and returns (email, password) to present."""
    if name == "unknown-email":
        return "nobody-has-this@example.com", "whatever-password"
    if name == "null-hash":
        db_session.add(User(email="no-password@example.com", name="No Password", role=Role.REQUESTER, status=AccountStatus.ACTIVE, password_hash=None))
        db_session.commit()
        return "no-password@example.com", "whatever-password"

    user = _make_active_user(local_client, db_session, email="scenario@example.com", password="the-real-password")
    if name == "wrong-password":
        return user.email, "the-wrong-password"
    if name == "correct-password":
        return user.email, "the-real-password"
    if name == "rejected-row":
        user.status = AccountStatus.REJECTED
        db_session.commit()
        return user.email, "the-real-password"
    if name == "pending-row":
        user.status = AccountStatus.PENDING
        db_session.commit()
        return user.email, "the-real-password"
    assert name == "locked-account"
    monkeypatch.setattr(settings, "local_login_max_attempts", 2)
    for _ in range(2):
        assert _login(local_client, user.email, "the-wrong-password").status_code == 401
    assert login_lockout.is_locked(user.id)
    return user.email, "the-real-password"


@pytest.mark.parametrize(
    "scenario",
    ["unknown-email", "null-hash", "wrong-password", "correct-password", "rejected-row", "pending-row", "locked-account"],
)
def test_login_runs_exactly_one_hash_check_on_every_path(local_client, db_session, hash_calls, monkeypatch, scenario):
    """The miss paths cost what the hit paths cost. A login against an unknown email
    (or a row with no password, or a locked account) used to return without hashing, a few hundred
    milliseconds before a wrong password on a real account, and the gap said which emails have accounts.
    Counted by the work performed: one `checkpw` on every path, no `hashpw`."""
    email, password = _login_scenario(scenario, local_client, db_session, monkeypatch)
    local_client.cookies.clear()
    hash_calls.reset()

    _login(local_client, email, password)

    assert (hash_calls.checkpw, hash_calls.hashpw) == (1, 0)


@pytest.mark.parametrize("existing", [False, True], ids=["new-email", "existing-email"])
def test_signup_runs_exactly_one_hash_for_a_new_and_an_existing_email(local_client, db_session, hash_calls, existing):
    """The existing-email response is identical in content, so it
    must not be faster either. The hash is computed on both paths, and thrown away on the existing one."""
    if existing:
        assert _signup(local_client, email="taken@example.com").status_code == 202
    hash_calls.reset()

    response = _signup(local_client, email="taken@example.com" if existing else "fresh@example.com")

    assert response.status_code == 202
    assert (hash_calls.hashpw, hash_calls.checkpw) == (1, 0)


def test_the_dummy_hash_costs_what_every_stored_hash_costs():
    """The dummy only equalises the work if it is as expensive as a real check. Pinned to the literal cost
    factor, and to whatever `hash_password` actually produces."""
    dummy = passwords.DUMMY_PASSWORD_HASH.split("$")
    real = passwords.hash_password("x" * 8).split("$")

    assert dummy[1:3] == ["2b", "12"]
    assert real[1:3] == dummy[1:3]


def test_a_match_against_the_dummy_hash_can_never_authenticate(monkeypatch):
    """The dummy check's answer is discarded. Even if it came back True, a call with no real hash is False."""
    monkeypatch.setattr(passwords.bcrypt, "checkpw", lambda *args, **kwargs: True)

    assert passwords.verify_password("anything", None) is False
    assert passwords.verify_password("anything", "") is False


# --- the lockout map is bounded ----------------------------------------------


def test_a_thousand_failures_against_emails_with_no_account_leave_the_lockout_map_empty(
    local_client, db_session, monkeypatch
):
    """No counter for an email with no account, so random emails cannot grow
    the platform's memory. 1,000 distinct random addresses, each a failed login (bcrypt is stubbed to a fast
    False -- the map is under test, not the hash's speed). The last step keeps the test honest: a failure
    against a real account DOES create one entry, so zero above is not a broken counter."""
    monkeypatch.setattr(passwords.bcrypt, "checkpw", lambda *args, **kwargs: False)

    for _ in range(1000):
        response = _login(local_client, f"{uuid.uuid4().hex}@example.com", "some-password")
        assert response.status_code == 401

    assert login_lockout.tracked_count() == 0

    assert _login(local_client, BOOTSTRAP_EMAIL, "some-password").status_code == 401
    assert login_lockout.tracked_count() == 1


# --- pending signups are hidden from non-admins in the directory --------------


def test_a_pending_signup_is_hidden_from_a_non_admin_and_shown_to_an_admin(local_client, db_session):
    """End to end through the real signup and the session seam: an unauthenticated signup's attacker-chosen name
    and email must not appear in the directory every user can read. The admin's approval queue still needs them."""
    _make_active_user(local_client, db_session, email="colleague@example.com")
    assert _signup(local_client, email="mallory@example.com", name="Mallory Chosen-Name").status_code == 202

    assert _login(local_client, "colleague@example.com", "a-good-password").status_code == 200
    seen_by_user = local_client.get("/api/users")
    assert seen_by_user.status_code == 200
    assert "mallory@example.com" not in seen_by_user.text
    assert "Mallory Chosen-Name" not in seen_by_user.text
    assert "colleague@example.com" in seen_by_user.text

    local_client.cookies.clear()
    assert _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD).status_code == 200
    seen_by_admin = local_client.get("/api/users")
    assert "mallory@example.com" in seen_by_admin.text


# ---------------------------------------------------------------------------
# a validation error never echoes what the caller submitted
# ---------------------------------------------------------------------------
#
# FastAPI's default 422 carries pydantic's `input`: the value that failed and, for a missing field or a
# body that is not an object, the whole parent object or body. So a password that failed a rule came
# straight back, and so did a *valid* password sitting next to the field that failed (a password is
# never "returned in any response schema"). Every route that takes a password is driven through
# every way a request can fail validation, with distinctive values, and the response must hold no
# four-character slice of any value sent (so no longer substring either). The handler under test is
# `services/validation_errors.py`; `tests/test_validation_errors.py` pins that these four routes are
# every route that takes a password, and tests the handler on the routes that do not.

# Literals, each long enough for the slice check to mean something, and none sharing a four-character
# slice with a validation message, a field name or a path.
_VALID_A = "Pm8-Wt3-Kd5-Bx"  # a password that would be accepted
_VALID_B = "Cv6-Rn2-Hz9-Lq"  # a second one, for the route that takes two
_PROBE = "Rb5-Dn8-Yt2-Ke"  # a value the field should never have been given
_SHORT = "Zq7!vK"  # 6 characters, under the minimum of 8
_OVER_ASCII = "Hw3Rk9Zx5Bn2Qm8Lc4Vt7Yd1Fs6Jg0PaEu!NiOo+TrXy~CbMeWlKhGfDzSqAjUvIpNxYtHrBv"  # 73 bytes
_OVER_KOREAN = "휘귀뭉쟁팽켈횡붉쉬곰닷헉맙찰뇌흑톰뻑쑥잼옆킁핍쪽둥"  # 25 characters, 75 bytes

# route -> (path, a body that would be accepted, the fields that hold a password, the field that holds
# the password being SET -- None for login, which only presents one). The path of the admin route is
# filled in per test; `tests/test_validation_errors.py` reads this table to check that every route that
# takes a password is in it.
ECHO_TESTED_ROUTES = {
    "signup": (
        "/api/auth/signup",
        {"email": "echo@example.com", "name": "Echo Person", "department": "Ops", "password": _VALID_A},
        ("password",),
        "password",
    ),
    "login": ("/api/auth/login", {"email": "echo@example.com", "password": _VALID_A}, ("password",), None),
    "own-change": (
        "/api/auth/password",
        {"current_password": _VALID_B, "new_password": _VALID_A},
        ("current_password", "new_password"),
        "new_password",
    ),
    "admin-set": ("/api/admin/users/{user_id}/password", {"password": _VALID_A}, ("password",), "password"),
}


def test_the_probe_values_are_the_sizes_the_echo_tests_claim():
    assert len(_SHORT) == 6
    assert len(_OVER_ASCII.encode("utf-8")) == 73 and _OVER_ASCII.isascii()
    assert len(_OVER_KOREAN) == 25 and len(_OVER_KOREAN.encode("utf-8")) == 75
    for accepted in (_VALID_A, _VALID_B):
        assert 8 <= len(accepted.encode("utf-8")) <= 72


def _as_json(body):
    return {"json": body}


def _as_raw(text, content_type):
    return {"content": text, "headers": {"content-type": content_type}}


def _echo_cases(route):
    """case id -> (the request's keyword arguments, every secret that request carries)."""
    _path, valid, secret_fields, set_field = ECHO_TESTED_ROUTES[route]
    held = {name: valid[name] for name in secret_fields}
    cases = {}

    def others(*omit):
        return tuple(value for name, value in held.items() if name not in omit)

    # A field that failed next to a password that did not (pydantic reports the whole parent object).
    for name in valid:
        cases[f"missing-{name}"] = (_as_json({k: v for k, v in valid.items() if k != name}), others(name))
    for name in secret_fields:
        cases[f"{name}-is-a-list"] = (_as_json({**valid, name: [_PROBE]}), others(name) + (_PROBE,))
        cases[f"{name}-is-an-object"] = (_as_json({**valid, name: {"inner": _PROBE}}), others(name) + (_PROBE,))
        # The realistic mistake: the password sent under a name the server does not know.
        misspelled = {k: v for k, v in valid.items() if k != name}
        misspelled[name + "x"] = held[name]
        cases[f"{name}-is-misspelled"] = (_as_json(misspelled), tuple(held.values()))
    if set_field is not None:
        for case_id, value in (("too-short", _SHORT), ("over-72-ascii", _OVER_ASCII), ("over-72-korean", _OVER_KOREAN)):
            cases[case_id] = (_as_json({**valid, set_field: value}), others(set_field) + (value,))
    if "email" in valid:
        cases["bad-email"] = (_as_json({**valid, "email": "not-an-email"}), tuple(held.values()))
    if route == "signup":
        cases["name-too-long"] = (_as_json({**valid, "name": "n" * 101}), tuple(held.values()))
    # The body is not an object at all, or not even JSON.
    first = secret_fields[0]
    cases["body-is-a-json-string"] = (_as_json(_PROBE), (_PROBE,))
    cases["body-is-a-json-list"] = (_as_json([_PROBE]), (_PROBE,))
    cases["body-is-a-form"] = (_as_raw(f"{first}={_PROBE}", "application/x-www-form-urlencoded"), (_PROBE,))
    cases["body-is-broken-json"] = (_as_raw('{"' + first + '": "' + _PROBE + '", ', "application/json"), (_PROBE,))
    return cases


def _open_route(route, local_client, db_session, bootstrap_admin) -> str:
    """The URL for `route`, with the client signed in as whoever the route needs. The session seam is
    reached the way a browser reaches it, a signed cookie; the bcrypt work of a real login is not what
    is under test and dozens of them would only slow the suite."""
    path = ECHO_TESTED_ROUTES[route][0]
    if route in ("own-change", "admin-set"):
        cookie = platform_session.issue_session(bootstrap_admin.id)
        local_client.cookies.set(platform_session.session_cookie_name(), cookie)
    if route == "admin-set":
        target = db_session.query(User).filter(User.id != bootstrap_admin.id, User.role == Role.REQUESTER).first()
        path = path.format(user_id=target.id)
    return path


def _assert_no_trace_of(secrets, response):
    """Neither the value nor any four-character slice of it (so no longer substring) is in the body,
    as sent or JSON-escaped."""
    for secret in secrets:
        assert len(secret) >= 6, "a probe under 6 characters makes the slice check close to vacuous"
        for start in range(len(secret) - 3):
            piece = secret[start : start + 4]
            assert piece not in response.text, f"a slice of a submitted secret came back: {piece!r}"
            assert json.dumps(piece)[1:-1] not in response.text, f"an escaped slice came back: {piece!r}"


def _assert_entries_are_type_loc_msg(response):
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    for entry in detail:
        assert set(entry) <= {"type", "loc", "msg", "ctx"}, sorted(entry)


_ECHO_PARAMS = [
    pytest.param(route, case_id, id=f"{route}--{case_id}")
    for route in ECHO_TESTED_ROUTES
    for case_id in _echo_cases(route)
]


@pytest.mark.parametrize("route, case_id", _ECHO_PARAMS)
def test_a_validation_error_never_echoes_a_submitted_password(local_client, db_session, bootstrap_admin, route, case_id):
    path = _open_route(route, local_client, db_session, bootstrap_admin)
    kwargs, secrets = _echo_cases(route)[case_id]

    response = local_client.post(path, **kwargs)

    assert response.status_code == 422, (route, case_id)
    _assert_no_trace_of(secrets, response)
    _assert_entries_are_type_loc_msg(response)


@pytest.mark.parametrize("route", [name for name, spec in ECHO_TESTED_ROUTES.items() if spec[3] is not None])
def test_a_rejected_password_still_names_its_field_and_the_rule(local_client, db_session, bootstrap_admin, route):
    """What the form maps to its message stays: the field in `loc`, the rule in `type` and `msg`, and the
    rule's own bound in `ctx`. Only the value is gone."""
    _path, valid, _held, field = ECHO_TESTED_ROUTES[route]
    path = _open_route(route, local_client, db_session, bootstrap_admin)

    too_short = local_client.post(path, json={**valid, field: _SHORT})
    assert too_short.json() == {
        "detail": [
            {
                "type": "string_too_short",
                "loc": ["body", field],
                "msg": "String should have at least 8 characters",
                "ctx": {"min_length": 8},
            }
        ]
    }

    over = local_client.post(path, json={**valid, field: _OVER_KOREAN})
    (entry,) = over.json()["detail"]
    assert (entry["type"], entry["loc"]) == ("value_error", ["body", field])
    assert "72바이트" in entry["msg"] and "한글" in entry["msg"]
    assert set(entry) == {"type", "loc", "msg"}


def test_a_signup_with_a_missing_field_names_it_and_nothing_else(local_client):
    body = {k: v for k, v in ECHO_TESTED_ROUTES["signup"][1].items() if k != "name"}

    response = local_client.post("/api/auth/signup", json=body)

    assert response.status_code == 422
    assert response.json() == {"detail": [{"type": "missing", "loc": ["body", "name"], "msg": "Field required"}]}


# ---------------------------------------------------------------------------
# The roles panel's admin count
# ---------------------------------------------------------------------------


def test_the_usable_admin_count_leaves_out_a_deactivated_and_a_credential_less_admin(
    local_client, db_session, bootstrap_admin
):
    """Four `role == admin` rows beside the bootstrap admin, none of which can act: the dev
    seed's admin (ACTIVE, no password), plus one deactivated with a password, one more ACTIVE
    with no password and one PENDING with a password. The panel used to count all five; the
    guard counts one, and the endpoint reports the guard's number."""
    for name, status, password_hash in [
        ("deactivated-admin", AccountStatus.REJECTED, "a-stored-hash-of-some-kind"),
        ("passwordless-admin", AccountStatus.ACTIVE, None),
        ("pending-admin", AccountStatus.PENDING, "a-stored-hash-of-some-kind"),
    ]:
        db_session.add(
            User(
                name=name,
                email=f"{name}@example.com",
                role=Role.ADMIN,
                status=status,
                password_hash=password_hash,
            )
        )
    db_session.commit()
    assert db_session.query(User).filter(User.role == Role.ADMIN).count() == 5
    assert _login(local_client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD).status_code == 200

    response = local_client.get("/api/users/usable-admin-count")

    assert response.status_code == 200
    assert response.json() == {"count": 1}
    # ...and the guard agrees: the one usable admin cannot be demoted.
    demotion = local_client.patch(f"/api/users/{bootstrap_admin.id}/role", json={"role": Role.REQUESTER})
    assert demotion.status_code == 409
