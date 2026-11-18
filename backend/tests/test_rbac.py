"""the role model, the permission matrix, role grants, and the
self-approval requester fix).

Standard in-memory SQLite fixtures, dev auth mode (`X-Forge-User-Id`) — no
Docker, no network, no IdP. The role gates are mode-agnostic by construction:
they read `User.role` from the row `deps.get_current_user` resolved, whichever
mode resolved it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.models.approval import ApprovalStatus
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.policy_verdict import PolicyVerdict
from app.models.project import Project
from app.config import settings
from app.models.user import User
from app.seed import SEED_USERS
from app.services.account_status import AccountStatus
from app.services.approval import create_approval_request_for_verdict, resolve_requester_for_build
from app.services.policy_checks import PolicyRiskTier
from app.services.roles import (
    ALL_ROLES,
    LastAdminError,
    Role,
    UnknownRoleError,
    change_user_role,
    ensure_not_last_admin,
    holds_at_least,
    usable_admin_ids,
    would_leave_no_usable_admin,
)
from app.services.status import ProjectStatus


def _headers(user_id: str) -> dict[str, str]:
    return {"X-Forge-User-Id": user_id}


@pytest.fixture()
def requester(db_session) -> User:
    return db_session.query(User).filter_by(role=Role.REQUESTER).first()


@pytest.fixture()
def approver(db_session) -> User:
    return db_session.query(User).filter_by(role=Role.APPROVER).first()


@pytest.fixture()
def admin(db_session) -> User:
    return db_session.query(User).filter_by(role=Role.ADMIN).first()


def _user(db_session, *, name: str, role: str) -> User:
    user = User(name=name, email=f"{name}@example.com", role=role)
    db_session.add(user)
    db_session.flush()
    return user


# ---------------------------------------------------------------------------
# The ladder (services/roles.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role,minimum,expected",
    [
        (Role.REQUESTER, Role.REQUESTER, True),
        (Role.REQUESTER, Role.APPROVER, False),
        (Role.REQUESTER, Role.ADMIN, False),
        (Role.APPROVER, Role.REQUESTER, True),
        (Role.APPROVER, Role.APPROVER, True),
        (Role.APPROVER, Role.ADMIN, False),
        (Role.ADMIN, Role.REQUESTER, True),
        (Role.ADMIN, Role.APPROVER, True),
        (Role.ADMIN, Role.ADMIN, True),
    ],
)
def test_the_ladder_is_total(role, minimum, expected):
    """`admin > approver > requester`, so every rung holds
    everything below it — which is why the old `("approver", "admin")`
    membership tuple collapses to one comparison."""
    assert holds_at_least(role, minimum) is expected


@pytest.mark.parametrize("unknown", ["wizard", "ADMIN", "Admin", "", None, "administrator"])
@pytest.mark.parametrize("minimum", [Role.REQUESTER, Role.APPROVER, Role.ADMIN])
def test_an_unrecognised_role_holds_no_privileges(unknown, minimum):
    """Fail closed. An unknown value is not
    baseline (note `requester` is included in `minimum` above) and not an
    error — it simply holds nothing. Case matters: the stored values are
    lower-case, and a near-miss must not be quietly accepted."""
    assert holds_at_least(unknown, minimum) is False


def test_the_declared_role_set_matches_what_the_db_already_holds():
    """the values stay exactly the three strings already
    seeded — no migration, no rename. If a role is ever renamed this test
    fails before the mismatch reaches a permission check."""
    assert set(ALL_ROLES) == {u["role"] for u in SEED_USERS}
    assert set(ALL_ROLES) == {Role.REQUESTER, Role.APPROVER, Role.ADMIN}


# ADR 0022 §5: the
# ONE module-level constant name the role-literal scan below may not flag.
# `app/config.py` needs the literal "admin" once, to refuse it as a prod
# bootstrap-password default -- a PASSWORD comparison, not a ROLE
# comparison, but the scan matches by bare string value and cannot tell
# the two apart otherwise. Matched by this ASSIGNMENT TARGET NAME only --
# never by line number, file path, or the value "admin" itself -- so a
# second role literal written anywhere else, including elsewhere in
# config.py, still fails (see test_the_bootstrap_password_exemption_is_narrow
# below). Exempting the whole file would surrender the invariant there
# permanently to buy one node; exempting by line number would silently go
# stale on the next edit. `services/roles.py` remains the only place role
# values are actually DECLARED -- this exemption does not change that.
_ROLE_SCAN_EXEMPT_ASSIGNMENT_TARGET = "_DEFAULT_DEV_BOOTSTRAP_PASSWORD"


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """Every string-constant node that is the *first statement* of a
    module/class/function body, i.e. an actual docstring -- so prose
    *about* a role in a docstring is never mistaken for a role check."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def _role_scan_exempted_constant_ids(tree: ast.AST) -> set[int]:
    """Constant nodes on the right-hand side of
    `_ROLE_SCAN_EXEMPT_ASSIGNMENT_TARGET = ...` -- matched purely by the
    assignment's target NAME, per the module comment above."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.target is not None:
            targets = [node.target]
        else:
            continue
        if isinstance(node.value, ast.Constant) and any(
            isinstance(t, ast.Name) and t.id == _ROLE_SCAN_EXEMPT_ASSIGNMENT_TARGET
            for t in targets
        ):
            found.add(id(node.value))
    return found


def _role_literal_offenders(source: str, role_values: set[str]) -> list[str]:
    """The one scanning core both the real `app/`-wide walk (below) and its
    own non-vacuity check (`test_the_bootstrap_password_exemption_is_narrow`)
    run -- so the non-vacuity check exercises the *exact* matching logic the
    real scan uses, never a re-implementation of it:
    a test must not re-implement the production logic it is checking."""
    tree = ast.parse(source)
    docstrings = _docstring_constant_ids(tree)
    exempted = _role_scan_exempted_constant_ids(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and node.value in role_values
            and id(node) not in docstrings
            and id(node) not in exempted
        ):
            offenders.append(f"{node.lineno}: {node.value!r}")
    return offenders


def test_no_app_module_compares_a_role_string_inline():
    """No endpoint compares role strings inline.

    Pins the invariant mechanically rather than by review, since the whole
    point of the module is that the next endpoint cannot re-introduce a
    hand-written role string. `services/roles.py` declares them and
    `app/seed.py` seeds them; **nowhere else under `app/`** may spell one —
    which is why even `User.role`'s column default is `Role.REQUESTER`.
    There is exactly one further exemption, by assignment-target name —
    see `_ROLE_SCAN_EXEMPT_ASSIGNMENT_TARGET` above for the full reasoning.

    Scans the whole of `app/` rather than just `routers/`+`services/`+
    `models/`: `deps.py` is where authorization now lives (`require_role`)
    and is therefore the likeliest future home for a hand-written
    `user.role == "admin"`, and it sits in none of those three packages.

    Parsed with `ast` rather than grepped, so that prose *about* a role in a
    docstring is not mistaken for a role check; only role strings appearing
    as real code constants count."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    allowed = {app_dir / "services" / "roles.py", app_dir / "seed.py"}
    role_values = set(ALL_ROLES)

    offenders: list[str] = []
    exemptions_used = 0
    scanned = sorted(app_dir.rglob("*.py"))
    assert app_dir / "deps.py" in scanned  # the module the widening is for
    for path in scanned:
        if path in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        exemptions_used += len(_role_scan_exempted_constant_ids(ast.parse(source)))
        offenders.extend(
            f"{path.name}:{offender}"
            for offender in _role_literal_offenders(source, role_values)
        )
    assert offenders == []
    # Non-vacuity (one direction): the exemption actually fired,
    # exactly once, across the whole scan. If `_DEFAULT_DEV_BOOTSTRAP_
    # PASSWORD` is ever renamed or moved, this line goes red -- an empty
    # `offenders` list must not be allowed to read as "the exemption still
    # works" when it has quietly stopped firing at all.
    assert exemptions_used == 1


def test_the_bootstrap_password_exemption_is_narrow():
    """Non-vacuity, the other direction: the exemption in
    `test_no_app_module_compares_a_role_string_inline` is scoped to the one
    named constant, not to `config.py` as a file. A role literal written
    anywhere else in that file -- including right next to the exempted
    one -- must still fail the scan. Proven by planting one into a parsed
    copy of the real source (never the file on disk) and watching
    `_role_literal_offenders` catch it, using the exact same helper the
    real scan calls."""
    config_path = Path(__file__).resolve().parents[1] / "app" / "config.py"
    real_source = config_path.read_text(encoding="utf-8")
    role_values = set(ALL_ROLES)

    # The real file, unmodified: exempted and clean, same as the main scan.
    assert _role_literal_offenders(real_source, role_values) == []

    anchor = '_DEFAULT_DEV_BOOTSTRAP_PASSWORD = "admin"'
    assert anchor in real_source  # non-vacuous: the replace below will land
    planted_source = real_source.replace(
        anchor,
        anchor + '\n_A_ROLE_LITERAL_PLANTED_BY_A_TEST = "admin"',
        1,
    )
    assert planted_source != real_source

    offenders = _role_literal_offenders(planted_source, role_values)
    assert len(offenders) == 1
    assert offenders[0].endswith(": 'admin'")


# ---------------------------------------------------------------------------
# Permission matrix — the two closed gaps
# ---------------------------------------------------------------------------


def test_audit_log_is_admin_only(client, requester, approver, admin):
    """Reading the audit log is `admin` only. Was
    unauthenticated, and accepted an arbitrary `actor_id` filter."""
    assert client.get("/api/audit-log").status_code == 401
    assert client.get("/api/audit-log", headers=_headers(requester.id)).status_code == 403
    assert client.get("/api/audit-log", headers=_headers(approver.id)).status_code == 403
    assert client.get("/api/audit-log", headers=_headers(admin.id)).status_code == 200


def test_the_audit_logs_actor_filter_is_no_longer_reachable_unauthenticated(client, requester):
    """The gap was not just the listing: `?actor_id=` let anyone pull one
    named user's activity. The filter still works — for an admin."""
    assert client.get(f"/api/audit-log?actor_id={requester.id}").status_code == 401


@pytest.mark.parametrize("path", ["/api/users", "/api/teams", "/api/users/me"])
def test_the_user_directory_requires_authentication(client, path, requester):
    """List users / teams requires authentication. The
    directory exposed every name, email and role with no identity at all.
    It stays readable by any authenticated user — it cannot become
    admin-only, since the owner display and the dev identity switcher read
    it."""
    assert client.get(path).status_code == 401
    assert client.get(path, headers=_headers(requester.id)).status_code == 200


# --- who `GET /users` shows to whom ---------------------------------


@pytest.fixture()
def populated_directory(db_session, seeded_ids, admin) -> dict[str, User]:
    """Every kind of non-ACTIVE row, each named for what it is. `oscar` and `rita` are deactivated
    users the platform still refers to (a project owner, a change requester); the others are
    signups nobody should see."""

    def _row(name: str, status: str) -> User:
        user = User(name=name, email=f"{name}@example.com", role=Role.REQUESTER, status=status)
        db_session.add(user)
        db_session.flush()
        return user

    rows = {
        "mallory": _row("mallory-pending", AccountStatus.PENDING),
        "eve": _row("eve-declined-signup", AccountStatus.REJECTED),
        "dan": _row("dan-deactivated-unreferenced", AccountStatus.REJECTED),
        "oscar": _row("oscar-deactivated-owner", AccountStatus.REJECTED),
        "rita": _row("rita-deactivated-requester", AccountStatus.REJECTED),
        "weird": _row("walt-unrecognised-status", "NOT_A_STATUS"),
    }
    oscars_project = Project(
        name="oscars-project",
        owner_id=rows["oscar"].id,
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    ritas_target = Project(
        name="admins-project-rita-asked-for-a-change",
        owner_id=admin.id,
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add_all([oscars_project, ritas_target])
    db_session.flush()
    _change_request_and_build(db_session, ritas_target, requester_id=rows["rita"].id)
    db_session.commit()
    rows["oscars_project"] = oscars_project  # type: ignore[assignment]
    return rows


def _directory_names(client, viewer: User) -> set[str]:
    response = client.get("/api/users", headers=_headers(viewer.id))
    assert response.status_code == 200
    return {entry["name"] for entry in response.json()}


_SEEDED_NAMES = {entry["name"] for entry in SEED_USERS}


@pytest.mark.parametrize("viewer_email", ["minji.kim@example.com", "dohyun.lee@example.com"], ids=["requester", "approver"])
def test_a_non_admin_sees_active_users_and_referenced_deactivated_ones_only(
    client, db_session, populated_directory, viewer_email
):
    """No PENDING row, no declined signup, no deactivated stranger -- the attacker-chosen names
    and emails -- but a deactivated colleague the platform still refers to stays visible."""
    viewer = db_session.query(User).filter_by(email=viewer_email).one()

    names = _directory_names(client, viewer)

    assert names == _SEEDED_NAMES | {"oscar-deactivated-owner", "rita-deactivated-requester"}


def test_an_unrecognised_role_gets_the_narrow_directory(client, db_session, populated_directory):
    """Fail-closed: a role the ladder does not know is no admin, so it gets the non-admin view."""
    odd = User(name="odd-role", email="odd@example.com", role="wizard")
    db_session.add(odd)
    db_session.commit()

    names = _directory_names(client, odd)

    assert "mallory-pending" not in names
    assert "eve-declined-signup" not in names


def test_an_admin_sees_every_row(client, db_session, admin, populated_directory):
    names = _directory_names(client, admin)

    assert names == _SEEDED_NAMES | {
        "mallory-pending",
        "eve-declined-signup",
        "dan-deactivated-unreferenced",
        "oscar-deactivated-owner",
        "rita-deactivated-requester",
        "walt-unrecognised-status",
    }


def test_a_deactivated_owner_still_displays_on_their_project(client, db_session, requester, populated_directory):
    """The owner display reads the project row, not the directory, so narrowing the directory
    cannot blank it -- and the owner is also still in the directory for anyone who looks."""
    project = populated_directory["oscars_project"]

    response = client.get(f"/api/projects/{project.id}", headers=_headers(requester.id))

    assert response.status_code == 200
    assert response.json()["owner_name"] == "oscar-deactivated-owner"
    assert "oscar-deactivated-owner" in _directory_names(client, requester)


def test_writes_that_create_work_require_authentication(client, seeded_ids):
    """Create project / submit change request —
    authenticated."""
    body = {
        "name": "unauthenticated",
        "template_id": seeded_ids["template_id"],
        "team_id": seeded_ids["team_id"],
        "nl_request": "please",
    }
    assert client.post("/api/projects", json=body).status_code == 401


@pytest.fixture()
def read_row_paths(db_session, seeded_ids) -> list[str]:
    """One **real** URL per endpoint in the spec's read row.

    Real ids matter here: a made-up id returns 404 whether or not the gate
    exists, so a table of invented paths would still pass with every
    `Depends(get_current_user)` deleted. These all reach a handler that would
    hand back data."""
    project = Project(
        name="read-row-project",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    build = Build(project_id=project.id, type="create", diff_ref="forge-build-read#1")
    db_session.add(build)
    db_session.flush()
    verdict = PolicyVerdict(build_id=build.id, risk_tier=PolicyRiskTier.MEDIUM)
    db_session.add(verdict)
    db_session.flush()
    approval = create_approval_request_for_verdict(
        db_session,
        build=build,
        verdict=verdict,
        project_id=project.id,
        requester_id=project.owner_id,
    )
    db_session.commit()
    return [
        "/api/projects",
        f"/api/projects/{project.id}",
        f"/api/projects/{project.id}/timeline",
        f"/api/projects/{project.id}/change-requests",
        f"/api/projects/{project.id}/deployments",
        "/api/approvals",
        f"/api/approvals/{approval.id}",
        "/api/templates",
        f"/api/templates/{seeded_ids['template_id']}",
    ]


def test_every_read_endpoint_requires_authentication(client, read_row_paths):
    """List/read projects, timeline, builds,
    deployments, approvals — authenticated, and list/read templates —
    authenticated. All nine were reachable with no credentials at all,
    which handed an anonymous caller the whole project registry plus every
    approval's requester, risk tier and flagged checks.

    Asserted as a list comprehension rather than nine separate calls so the
    failure message names *which* endpoint regressed."""
    assert [p for p in read_row_paths if client.get(p).status_code != 401] == []


def test_the_read_row_needs_no_role_beyond_authentication(client, read_row_paths, requester):
    """The other half of the same row: "authenticated" is the whole bar, so
    the lowest rung reads all nine. Without this, gating them to a role
    would pass the test above while breaking every requester."""
    headers = _headers(requester.id)
    assert [
        (p, response.status_code)
        for p in read_row_paths
        for response in [client.get(p, headers=headers)]
        if response.status_code != 200
    ] == []


def test_the_health_check_needs_no_identity(client):
    """Health check — none. The preview /
    deployed-service proxy routes and preview forward-auth are pinned
    identity-free by their own suites (test_preview_auth.py,
    test_deployments_api.py, test_preview_manager_integration.py)."""
    assert client.get("/api/health").status_code == 200


def test_an_unrecognised_role_is_authenticated_but_powerless(client, db_session, requester):
    """The fail-closed rule seen from the API: such a user can still do
    everything that needs no role, and nothing that needs one."""
    requester.role = "wizard"
    db_session.commit()

    assert client.get("/api/users/me", headers=_headers(requester.id)).status_code == 200
    assert client.get("/api/audit-log", headers=_headers(requester.id)).status_code == 403


# ---------------------------------------------------------------------------
# The dev switcher bootstrap
# ---------------------------------------------------------------------------


def test_the_dev_switcher_bootstrap_lists_identities_without_a_credential(client, db_session):
    """The switcher must list users
    *before* any identity exists, which `GET /users` (now authenticated)
    cannot serve. This is the one endpoint that answers unauthenticated."""
    response = client.get("/api/dev/identities")

    assert response.status_code == 200
    assert len(response.json()) == len(SEED_USERS)


def test_the_bootstrap_exposes_only_id_and_display_name(client):
    """Requirement 3: an unauthenticated endpoint must not ship the email and
    role that `UserOut` carries, even in dev. Asserted as exact key equality
    so widening the response model to `UserOut` fails here."""
    identities = client.get("/api/dev/identities").json()

    assert identities
    assert all(set(identity) == {"id", "name"} for identity in identities)


def test_in_oidc_mode_the_bootstrap_route_does_not_exist_at_all():
    """Requirement 2, and the reason `main.create_app` is a factory.

    The section's security rests on *absence*, not on a handler check: a
    route that was never registered cannot be reached by a misconfiguration,
    a middleware ordering bug, or a refactor that drops a check. That claim
    is fixed at registration time, so the suite's usual idiom — monkeypatch
    the `settings` singleton — cannot verify it: monkeypatching `auth_mode`
    does not *unregister* an already-registered route. Hence a second app
    built from a second `Settings`, asserting the path is absent from *that*
    app's schema.

    Checked against the app's own OpenAPI schema rather than by calling and
    expecting 404, because a 404 is also what a registered-but-broken route
    returns — only absence distinguishes them."""
    from app.config import Settings
    from app.main import create_app

    oidc_app = create_app(
        Settings(
            auth_mode="oidc",
            oidc_issuer="https://idp.example.com",
            oidc_client_id="forge",
            oidc_client_secret="shh",
            session_signing_secret="test-session-secret",
        )
    )

    assert "/api/dev/identities" not in oidc_app.openapi()["paths"]
    # The control: the same factory in dev mode does register it, so the
    # assertion above is about the mode and not about a typo'd path.
    assert "/api/dev/identities" in create_app(Settings()).openapi()["paths"]


# ---------------------------------------------------------------------------
# Permission matrix — ownership is a separate axis (routers/deployments.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role,is_owner,allowed",
    [
        # `admin` overrides ownership for operational reach (ADR 0008/0019)...
        (Role.ADMIN, False, True),
        # ...`approver` does not — it ranks above requester on the ladder but
        # ownership is a different axis entirely.
        (Role.APPROVER, False, False),
        (Role.REQUESTER, False, False),
        # A requester is a full owner of their own project and needs no grant.
        (Role.REQUESTER, True, True),
        # An unrecognised role leaves ownership as the only way in — it must
        # not become a second denial for the legitimate owner...
        ("wizard", True, True),
        # ...nor a way in for anyone else.
        ("wizard", False, False),
    ],
)
def test_confirm_rollback_and_stop_are_own_or_admin(db_session, seeded_ids, role, is_owner, allowed):
    """Confirm deploy / rollback / stop — `own` or
    `admin`. Exercises `_require_owner_or_admin` directly: confirm, rollback
    and stop all call it, so the rule has one decision point, not three."""
    from fastapi import HTTPException

    from app.routers.deployments import _require_owner_or_admin

    actor = _user(db_session, name=f"actor-{role}-{is_owner}", role=role)
    project = Project(
        name="ownership-axis",
        owner_id=actor.id if is_owner else seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()

    if allowed:
        assert _require_owner_or_admin(actor, project) is None
    else:
        with pytest.raises(HTTPException) as exc:
            _require_owner_or_admin(actor, project)
        assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Role grants (PATCH /users/{id}/role)
# ---------------------------------------------------------------------------


def _grant(client, actor_id: str, target_id: str, role: str):
    return client.patch(
        f"/api/users/{target_id}/role", json={"role": role}, headers=_headers(actor_id)
    )


def test_admin_can_grant_a_role_and_the_change_is_audited(client, db_session, admin, requester):
    """Role grants are audited "with the granting admin as actor, the
    target user, and both old and new values"."""
    resp = _grant(client, admin.id, requester.id, Role.APPROVER)
    assert resp.status_code == 200
    assert resp.json()["role"] == Role.APPROVER

    db_session.refresh(requester)
    assert requester.role == Role.APPROVER

    entry = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "user.role_changed", AuditLog.target == f"user:{requester.id}")
        .one()
    )
    assert entry.actor_id == admin.id
    assert entry.metadata_json == {"old_role": Role.REQUESTER, "new_role": Role.APPROVER}


def test_granting_a_role_requires_admin(client, db_session, requester, approver):
    """Only `admin` may change a role — `approver` outranks `requester` but
    role management is the top rung."""
    assert _grant(client, requester.id, approver.id, Role.ADMIN).status_code == 403
    assert _grant(client, approver.id, approver.id, Role.ADMIN).status_code == 403

    db_session.refresh(approver)
    assert approver.role == Role.APPROVER


def test_granting_a_role_requires_authentication(client, approver):
    resp = client.patch(f"/api/users/{approver.id}/role", json={"role": Role.ADMIN})
    assert resp.status_code == 401


@pytest.mark.parametrize("bad_role", ["wizard", "ADMIN", "", "superadmin"])
def test_an_unknown_target_role_is_rejected(client, db_session, admin, requester, bad_role):
    """A role that does not exist must not be written: `holds_at_least` would
    then treat the row as holding nothing, silently locking that user out."""
    assert _grant(client, admin.id, requester.id, bad_role).status_code == 400
    db_session.refresh(requester)
    assert requester.role == Role.REQUESTER


def test_granting_to_an_unknown_user_is_404(client, admin):
    assert _grant(client, admin.id, "no-such-user", Role.APPROVER).status_code == 404


def test_the_last_admin_cannot_be_demoted(client, db_session, admin):
    """The last `admin` cannot be demoted or
    deactivated — without it the platform locks itself out of role
    management permanently, since only an admin may grant roles."""
    assert db_session.query(User).filter_by(role=Role.ADMIN).count() == 1

    resp = _grant(client, admin.id, admin.id, Role.REQUESTER)
    assert resp.status_code == 409

    db_session.refresh(admin)
    assert admin.role == Role.ADMIN
    assert (
        db_session.query(AuditLog).filter(AuditLog.action == "user.role_changed").count() == 0
    ), "a rejected grant must not leave an audit entry"


def test_the_last_admin_cannot_be_demoted_by_another_admin_either(client, db_session, admin):
    """The guard counts admins, so it does not matter who asks — including
    an admin demoting a *different* last-standing admin, which is the same
    lockout by another route."""
    other = _user(db_session, name="second-admin", role=Role.ADMIN)
    db_session.commit()

    # Two admins: demoting either is fine.
    assert _grant(client, other.id, admin.id, Role.REQUESTER).status_code == 200
    # One admin left: `other` is now the last one and is pinned.
    assert _grant(client, other.id, other.id, Role.REQUESTER).status_code == 409


def test_self_demotion_succeeds_while_another_admin_remains(client, db_session, admin):
    """Self-demotion is allowed as long as another admin
    remains."""
    _user(db_session, name="spare-admin", role=Role.ADMIN)
    db_session.commit()

    assert _grant(client, admin.id, admin.id, Role.REQUESTER).status_code == 200
    db_session.refresh(admin)
    assert admin.role == Role.REQUESTER


# --- the last-admin guard counts USABLE admins -----------------------
#
# The wrong predicate this replaces counted every `role == admin` row, so an admin whose
# access had been revoked (`REJECTED`), or whose request was reopened (`PENDING`), still
# stood in as "another admin": A deactivates B, then demotes themselves, and nobody who can
# act is left. The rows below are given `role == admin` and a status, so only the status
# decides whether they count.


def _admin_row(db_session, *, name: str, status: str = AccountStatus.ACTIVE) -> User:
    user = _user(db_session, name=name, role=Role.ADMIN)
    user.status = status
    db_session.commit()
    return user


@pytest.mark.parametrize(
    "status",
    [AccountStatus.REJECTED, AccountStatus.PENDING, "NOT_A_STATUS"],
    ids=["deactivated", "pending", "unrecognised"],
)
def test_an_admin_who_cannot_act_does_not_count_as_another_admin(client, db_session, admin, status):
    """The A/B case: A is the only admin who can sign in, B holds the role but resolves to
    nobody. A cannot step down -- by role change or through the guard `deactivate_user`
    calls -- even though a second `role == admin` row exists."""
    _admin_row(db_session, name="b-cannot-act", status=status)
    assert db_session.query(User).filter_by(role=Role.ADMIN).count() == 2

    assert _grant(client, admin.id, admin.id, Role.REQUESTER).status_code == 409
    with pytest.raises(LastAdminError):
        ensure_not_last_admin(db_session, admin)

    db_session.refresh(admin)
    assert admin.role == Role.ADMIN
    assert db_session.query(AuditLog).filter(AuditLog.action == "user.role_changed").count() == 0


def test_an_admin_who_can_act_still_counts_beside_one_who_cannot(client, db_session, admin):
    """Not "any extra row blocks everything": with a second ACTIVE admin present, A may step
    down, whatever else is lying around."""
    _admin_row(db_session, name="b-deactivated", status=AccountStatus.REJECTED)
    _admin_row(db_session, name="c-active")

    assert _grant(client, admin.id, admin.id, Role.REQUESTER).status_code == 200


def test_a_deactivated_admins_role_can_still_be_changed(client, db_session, admin):
    """Taking `admin` from a row that already does nothing cannot strand the platform, so the
    guard does not stand in the way -- including when A is the only usable admin."""
    b = _admin_row(db_session, name="b-deactivated", status=AccountStatus.REJECTED)

    assert would_leave_no_usable_admin(db_session, b) is False
    assert _grant(client, admin.id, b.id, Role.REQUESTER).status_code == 200
    db_session.refresh(b)
    assert b.role == Role.REQUESTER


def test_the_guard_does_not_fire_when_no_usable_admin_is_left_to_protect(db_session, admin):
    """Zero usable admins already (the only admin row is deactivated): demoting that row is
    not what stranded the platform, and the guard must not make a bad state unfixable."""
    admin.status = AccountStatus.REJECTED
    db_session.commit()
    assert usable_admin_ids(db_session) == set()

    assert would_leave_no_usable_admin(db_session, admin) is False


# Which rows are usable admins, per running mode and per credential the row holds. The seeded
# dev admin has neither a password nor an IdP subject; `b` below is an active admin holding
# exactly the credential named, and `a` holds both so it is usable in every mode.
@pytest.mark.parametrize(
    "mode, has_password, has_sub, counts",
    [
        ("dev", False, False, True),
        ("dev", True, False, True),
        ("dev", False, True, True),
        ("local", False, False, False),
        ("local", False, True, False),
        ("local", True, False, True),
        ("oidc", False, False, False),
        ("oidc", True, False, False),
        ("oidc", False, True, True),
    ],
)
def test_a_row_that_cannot_log_in_under_the_running_mode_is_not_a_usable_admin(
    db_session, admin, monkeypatch, mode, has_password, has_sub, counts
):
    """`password_hash IS NULL` can never log in, and the
    dev seed's admin has none -- so in `local` mode it is not a second admin. The same holds in
    `oidc` for a row with no IdP subject (login looks rows up by `sub`). In `dev` the identity
    is a header naming the row, so every ACTIVE admin counts."""
    monkeypatch.setattr(settings, "auth_mode", mode)
    a = _admin_row(db_session, name="a-holds-both")
    a.password_hash = "a-stored-hash-of-some-kind"
    a.oidc_sub = "sub-a"
    b = _admin_row(db_session, name="b-under-test")
    b.password_hash = "a-stored-hash-of-some-kind" if has_password else None
    b.oidc_sub = "sub-b" if has_sub else None
    # The seeded admin is the credential-less row of the table; keep it out of the way.
    admin.status = AccountStatus.REJECTED
    db_session.commit()

    assert usable_admin_ids(db_session) == ({a.id, b.id} if counts else {a.id})
    assert would_leave_no_usable_admin(db_session, a) is (not counts)


def test_in_local_mode_giving_the_seeded_admin_a_password_makes_it_count(db_session, admin, monkeypatch):
    """The same row flips from "does not count" to "counts" the moment it can log in."""
    monkeypatch.setattr(settings, "auth_mode", "local")
    a = _admin_row(db_session, name="a-local-admin")
    a.password_hash = "a-stored-hash-of-some-kind"
    db_session.commit()

    with pytest.raises(LastAdminError):
        change_user_role(db_session, actor_id=a.id, target=a, new_role=Role.REQUESTER)

    admin.password_hash = "a-stored-hash-of-some-kind"
    db_session.commit()
    assert change_user_role(db_session, actor_id=a.id, target=a, new_role=Role.REQUESTER) == Role.ADMIN


# --- the roles panel's admin count is the guard's count --------------
#
# The panel used to count `role == admin` rows in the browser, so a deactivated admin and a
# credential-less one were shown as admins while the guard answered 409 as if they did not
# exist. `GET /users/usable-admin-count` returns `len(usable_admin_ids)`. The credential-less
# half needs a mode that has credentials, so it lives in `test_local_accounts.py`.


def _usable_admin_count(client, viewer_id: str) -> int:
    response = client.get("/api/users/usable-admin-count", headers=_headers(viewer_id))
    assert response.status_code == 200
    return response.json()["count"]


def test_the_usable_admin_count_is_the_guards_count_so_one_means_409_and_two_means_demotable(
    client, db_session, admin
):
    """The displayed number and the guard cannot disagree: at 1 the only usable admin cannot
    be demoted, at 2 the same demotion goes through."""
    deactivated = _admin_row(db_session, name="deactivated-admin", status=AccountStatus.REJECTED)
    assert deactivated.role == Role.ADMIN  # a third and a second `role == admin` row, neither usable

    assert _usable_admin_count(client, admin.id) == 1
    assert _grant(client, admin.id, admin.id, Role.REQUESTER).status_code == 409

    spare = _admin_row(db_session, name="spare-admin")
    assert _usable_admin_count(client, admin.id) == 2
    assert _grant(client, admin.id, admin.id, Role.REQUESTER).status_code == 200


def test_the_usable_admin_count_is_admin_only(client, db_session, admin, approver, requester):
    assert _usable_admin_count(client, admin.id) == 1
    for viewer in (approver, requester):
        response = client.get("/api/users/usable-admin-count", headers=_headers(viewer.id))
        assert response.status_code == 403
        assert "count" not in response.json()
    assert client.get("/api/users/usable-admin-count").status_code == 401


def test_the_user_directory_shape_is_unchanged_and_carries_no_status_or_credential(
    client, db_session, admin, requester
):
    """The count is a separate read; `UserOut` is not widened to let a client recount."""
    expected_keys = {"id", "name", "email", "role", "team_id", "created_at"}
    for viewer in (admin, requester):
        rows = client.get("/api/users", headers=_headers(viewer.id)).json()
        assert rows
        assert all(set(row) == expected_keys for row in rows)


def test_demoting_a_non_admin_never_consults_the_admin_count(client, db_session, admin, approver):
    """Revoking `approver` back to `requester` is a normal grant — the
    last-admin guard is specific to leaving zero admins."""
    assert _grant(client, admin.id, approver.id, Role.REQUESTER).status_code == 200
    db_session.refresh(approver)
    assert approver.role == Role.REQUESTER


def test_change_user_role_raises_rather_than_returning_http(db_session, admin, requester):
    """The service raises domain errors; the
    router maps them. Also pins the returned *old* role, which is what the
    audit entry records."""
    assert change_user_role(
        db_session, actor_id=admin.id, target=requester, new_role=Role.APPROVER
    ) == Role.REQUESTER

    with pytest.raises(UnknownRoleError):
        change_user_role(db_session, actor_id=admin.id, target=requester, new_role="wizard")
    with pytest.raises(LastAdminError):
        change_user_role(db_session, actor_id=admin.id, target=admin, new_role=Role.REQUESTER)


# ---------------------------------------------------------------------------
# A role change is effective on the very next request (no caching anywhere)
# ---------------------------------------------------------------------------


def _pending_request(db_session, project, *, requester_id: str):
    build = Build(project_id=project.id, type="create")
    db_session.add(build)
    db_session.flush()
    verdict = PolicyVerdict(build_id=build.id, risk_tier=PolicyRiskTier.MEDIUM)
    db_session.add(verdict)
    db_session.flush()
    request = create_approval_request_for_verdict(
        db_session,
        build=build,
        verdict=verdict,
        project_id=project.id,
        requester_id=requester_id,
    )
    db_session.commit()
    return build, request


@pytest.fixture()
def admins_project(db_session, seeded_ids, admin) -> Project:
    """A project the admin owns and requested, so nobody else is caught by
    the self-approval ban when approving its builds."""
    project = Project(
        name="admin-owned",
        owner_id=admin.id,
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    return project


def test_a_grant_takes_effect_on_the_next_request(
    client, db_session, admin, requester, admins_project
):
    """`role` is read from the DB per request
    and never cached (and never carried in the session), so both a promotion
    and a demotion apply immediately."""
    _build, request = _pending_request(db_session, admins_project, requester_id=admin.id)
    approve = f"/api/approvals/{request.id}/approve"

    assert client.post(approve, headers=_headers(requester.id), json={}).status_code == 403
    assert _grant(client, admin.id, requester.id, Role.APPROVER).status_code == 200
    assert client.post(approve, headers=_headers(requester.id), json={}).status_code == 200


def test_a_demotion_takes_effect_on_the_next_request(
    client, db_session, admin, approver, admins_project
):
    _build, request = _pending_request(db_session, admins_project, requester_id=admin.id)
    reject = f"/api/approvals/{request.id}/reject"
    body = {"comment": "no"}

    assert _grant(client, admin.id, approver.id, Role.REQUESTER).status_code == 200
    assert client.post(reject, headers=_headers(approver.id), json=body).status_code == 403


# ---------------------------------------------------------------------------
# Self-approval: the requester is the ChangeRequest's requester
# ---------------------------------------------------------------------------


def _change_request_and_build(db_session, project, *, requester_id: str) -> Build:
    """Exactly what `routers/projects._create_change_request_and_build`
    writes: both rows in one operation, with `ChangeRequest.build_id` set."""
    build = Build(project_id=project.id, type="create")
    db_session.add(build)
    db_session.flush()
    db_session.add(
        ChangeRequest(
            project_id=project.id,
            requester_id=requester_id,
            nl_request="add a thing",
            build_id=build.id,
        )
    )
    db_session.flush()
    return build


@pytest.fixture()
def owned_by_someone_else(db_session, seeded_ids, admin) -> Project:
    """The case the old stand-in got wrong: requester != project owner."""
    project = Project(
        name="requester-is-not-the-owner",
        owner_id=admin.id,
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    return project


def test_the_requester_of_a_build_is_the_change_requests_requester(
    db_session, owned_by_someone_else, approver
):
    """The requester of a build is the requester of the
    `ChangeRequest` referencing that build."""
    build = _change_request_and_build(db_session, owned_by_someone_else, requester_id=approver.id)

    assert (
        resolve_requester_for_build(
            db_session, build_id=build.id, project_owner_id=owned_by_someone_else.owner_id
        )
        == approver.id
    )


def test_resolution_falls_back_to_the_owner_and_never_fails_open(
    db_session, owned_by_someone_else
):
    """If none is found, the check falls back to the
    project owner and the request is still evaluated — the ban must never
    fail open. A build created outside the API path has no ChangeRequest."""
    orphan = Build(project_id=owned_by_someone_else.id, type="create")
    db_session.add(orphan)
    db_session.flush()

    resolved = resolve_requester_for_build(
        db_session, build_id=orphan.id, project_owner_id=owned_by_someone_else.owner_id
    )
    assert resolved == owned_by_someone_else.owner_id
    assert resolved is not None, "a null requester would drop the ban entirely"


def test_a_change_request_for_another_build_is_not_mistaken_for_this_one(
    db_session, owned_by_someone_else, approver, requester
):
    """The lookup is keyed on `build_id`, not on the project — two builds of
    the same project must not borrow each other's requester."""
    mine = _change_request_and_build(db_session, owned_by_someone_else, requester_id=approver.id)
    theirs = _change_request_and_build(db_session, owned_by_someone_else, requester_id=requester.id)

    owner_id = owned_by_someone_else.owner_id
    assert resolve_requester_for_build(db_session, build_id=mine.id, project_owner_id=owner_id) == (
        approver.id
    )
    assert resolve_requester_for_build(
        db_session, build_id=theirs.id, project_owner_id=owner_id
    ) == requester.id


def test_the_executor_records_the_real_requester_not_the_project_owner(
    db_session, owned_by_someone_else, approver
):
    """The exact line the fix replaces (`sandbox_build_executor._requester_for`,
    which returned `project.owner_id` outright).

    **This test fails against the pre-fix code** — it returned the owner
    (`admin`) rather than the approver who submitted the change request.

    Constructed with a sentinel Docker client: every collaborator in
    `__init__` merely stores the client, so no Docker Engine is contacted and
    this stays a plain unit test."""
    from app.services.sandbox_build_executor import SandboxBuildExecutor

    build = _change_request_and_build(db_session, owned_by_someone_else, requester_id=approver.id)
    executor = SandboxBuildExecutor(docker_client=object())

    assert executor._requester_for(build, owned_by_someone_else) == approver.id
    assert executor._requester_for(build, owned_by_someone_else) != owned_by_someone_else.owner_id


def _request_through_the_production_path(db_session, project, build):
    """Builds the ApprovalRequest the way the executor does, so the stored
    `requester_id` is whatever `_requester_for` resolves."""
    from app.services.sandbox_build_executor import SandboxBuildExecutor

    verdict = PolicyVerdict(build_id=build.id, risk_tier=PolicyRiskTier.MEDIUM)
    db_session.add(verdict)
    db_session.flush()
    request = create_approval_request_for_verdict(
        db_session,
        build=build,
        verdict=verdict,
        project_id=project.id,
        requester_id=SandboxBuildExecutor(docker_client=object())._requester_for(build, project),
    )
    db_session.commit()
    return request


def test_a_requester_who_is_not_the_owner_cannot_approve_their_own_build(
    client, db_session, owned_by_someone_else, approver
):
    """The self-approval defect, end to end (it is a security-boundary fix,
    not a cleanup).

    `approver` submitted the change request but does not own the project, and
    holds the role the approval console requires. **Pre-fix this returned
    200**: the ApprovalRequest recorded the *owner* as requester, so the ban
    checked the wrong identity twice and never covered the person who asked
    for the change."""
    build = _change_request_and_build(db_session, owned_by_someone_else, requester_id=approver.id)
    request = _request_through_the_production_path(db_session, owned_by_someone_else, build)

    # Asserted before the stored-id check so that against the pre-fix code
    # this test fails on the *security* outcome (200, the request went
    # through), not merely on the bookkeeping that caused it.
    resp = client.post(
        f"/api/approvals/{request.id}/approve", headers=_headers(approver.id), json={}
    )
    assert resp.status_code == 403
    assert request.requester_id == approver.id

    # Still decidable by someone uninvolved — the ban is targeted, not a
    # deadlock.
    other = _user(db_session, name="uninvolved-approver", role=Role.APPROVER)
    db_session.commit()
    resp = client.post(f"/api/approvals/{request.id}/approve", headers=_headers(other.id), json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == ApprovalStatus.APPROVED


def test_the_self_approval_ban_has_no_admin_exception(client, db_session, seeded_ids, admin):
    """The self-approval ban has no admin exception: "the one place
    where the capability ladder does not apply, and it is intentional."
    Blocked both as requester and as owner."""
    owned_by_the_admin = Project(
        name="admin-requests-own-change",
        owner_id=admin.id,
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(owned_by_the_admin)
    db_session.flush()
    build = _change_request_and_build(db_session, owned_by_the_admin, requester_id=admin.id)
    request = _request_through_the_production_path(db_session, owned_by_the_admin, build)

    resp = client.post(
        f"/api/approvals/{request.id}/approve", headers=_headers(admin.id), json={}
    )
    assert resp.status_code == 403


def test_an_admin_who_is_only_the_requester_is_still_blocked(
    client, db_session, seeded_ids, admin, requester
):
    """The owner-side half of the ban already worked; this is the half the
    stand-in was hiding — the admin here owns nothing, they merely asked."""
    someone_elses_project = Project(
        name="owned-by-a-requester",
        owner_id=requester.id,
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(someone_elses_project)
    db_session.flush()
    build = _change_request_and_build(db_session, someone_elses_project, requester_id=admin.id)
    request = _request_through_the_production_path(db_session, someone_elses_project, build)

    resp = client.post(
        f"/api/approvals/{request.id}/approve", headers=_headers(admin.id), json={}
    )
    assert resp.status_code == 403
