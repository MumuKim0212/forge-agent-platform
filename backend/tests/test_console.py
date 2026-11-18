"""the two console read APIs — cross-project deployment health and the
effective platform configuration.

Standard in-memory SQLite fixtures, dev auth mode (`X-Forge-User-Id`) — no
Docker, no network, no Docker-dependent skips in this module.

The configuration tests are the security core of this task, and they are written
to survive a rename. Every secret field name and every expected response key is a
**literal here**, never imported from `app.config` (a test
must not share a source of truth with the code it tests). If a settings field
were renamed and re-added to the allowlist under an innocent-looking key, the key
assertions would go red on the new key AND the value assertions would go red on
the leaked value — the failure mode a name-only check would sail straight past.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentFailureReason, DeploymentStatus
from app.models.project import Project
from app.models.user import User
from app.services.roles import Role
from app.services.status import BuildStatus, ProjectStatus
from tests.fake_admin_dsn import FAKE_ADMIN_DSN

DEPLOYMENT_HEALTH_PATH = "/api/console/deployments"
CONFIGURATION_PATH = "/api/console/configuration"

HOST_HEALTH_PATH = "/api/console/health"
CONSOLE_PATHS = (DEPLOYMENT_HEALTH_PATH, CONFIGURATION_PATH, HOST_HEALTH_PATH)


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


def _project(db_session, seeded_ids, owner: User, *, name: str) -> Project:
    row = Project(
        name=name,
        owner_id=owner.id,
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture()
def project(db_session, seeded_ids, requester) -> Project:
    return _project(db_session, seeded_ids, requester, name="Console Test Project")


def _build(db_session, project: Project) -> Build:
    row = Build(project_id=project.id, type="create", status=BuildStatus.SUCCEEDED)
    db_session.add(row)
    db_session.flush()
    return row


def _deployment(
    db_session,
    project: Project,
    actor: User,
    *,
    status: str,
    failure_reason: str | None = None,
    created_at: datetime | None = None,
) -> Deployment:
    row = Deployment(
        project_id=project.id,
        build_id=_build(db_session, project).id,
        confirmed_by_id=actor.id,
        status=status,
        failure_reason=failure_reason,
    )
    if created_at is not None:
        row.created_at = created_at
    db_session.add(row)
    db_session.flush()
    return row


def _entry(payload: dict, project: Project) -> dict:
    return next(row for row in payload["projects"] if row["project_id"] == project.id)


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", CONSOLE_PATHS)
def test_a_console_read_requires_authentication(client, path):
    """The backend is the enforcement point for every
    console read, so an unauthenticated request never gets a body."""
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", CONSOLE_PATHS)
def test_a_console_read_refuses_every_rung_below_admin(client, path, requester, approver):
    """The whole console requires the top rung. A
    `requester` and an `approver` are both authenticated and both refused — the
    console is not "any privileged user", and an `approver` who can decide
    approvals still has no business reading platform configuration."""
    assert client.get(path, headers=_headers(requester.id)).status_code == 403
    assert client.get(path, headers=_headers(approver.id)).status_code == 403


@pytest.mark.parametrize("path", CONSOLE_PATHS)
def test_a_console_read_answers_the_admin_rung(client, path, admin):
    assert client.get(path, headers=_headers(admin.id)).status_code == 200


def test_the_console_router_exposes_no_write_action():
    """No second control path for
    deploy/rollback/stop: it is explicitly out of scope — those stay on the
    per-project router where owner-or-admin already applies.

    Asserted over the app's own generated OpenAPI schema rather than by reading
    the module, so a `POST` added to this router in any future slice fails here
    instead of quietly becoming a second, differently-authorized way to deploy.
    (The schema, not `app.routes`: FastAPI keeps included routers nested, so a
    flat scan of `app.routes` silently finds nothing and would pass forever.)
    """
    from app.main import app

    console_paths = {
        path: operations
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/console")
    }

    # Non-vacuous: the two console endpoints must actually be registered.
    assert set(console_paths) == {
        "/api/console/deployments",
        "/api/console/configuration",
        "/api/console/health",
    }
    for path, operations in console_paths.items():
        assert set(operations) == {"get"}, path


# ---------------------------------------------------------------------------
# Deployment health
# ---------------------------------------------------------------------------


def test_deployment_health_covers_every_project_including_never_deployed_ones(
    client, db_session, seeded_ids, admin, requester
):
    """The view is "across all projects", and a project
    that has never been deployed is exactly the one an operator is looking for.
    Omitting it would make the view silently partial."""
    deployed = _project(db_session, seeded_ids, requester, name="Alpha Deployed")
    never = _project(db_session, seeded_ids, requester, name="Beta Never Deployed")
    active = _deployment(db_session, deployed, admin, status=DeploymentStatus.ACTIVE)

    payload = client.get(DEPLOYMENT_HEALTH_PATH, headers=_headers(admin.id)).json()

    # Every Project row is represented — the independent truth is the table.
    assert {row["project_id"] for row in payload["projects"]} == {
        row.id for row in db_session.query(Project).all()
    }

    deployed_entry = _entry(payload, deployed)
    assert deployed_entry["project_name"] == "Alpha Deployed"
    assert deployed_entry["active_deployment"]["id"] == active.id
    assert deployed_entry["active_deployment"]["status"] == "ACTIVE"
    # The active row is also the newest one, so both slots agree.
    assert deployed_entry["latest_deployment"]["id"] == active.id

    never_entry = _entry(payload, never)
    assert never_entry["active_deployment"] is None
    assert never_entry["latest_deployment"] is None


@pytest.mark.parametrize(
    "status,failure_reason",
    [
        ("DEPLOYING", None),
        ("FAILED", "HEALTH_TIMEOUT"),
        ("STOPPED", None),
        ("SUPERSEDED", None),
    ],
)
def test_a_project_without_an_active_deployment_still_says_what_happened(
    client, db_session, admin, project, status, failure_reason
):
    """The reason `latest_deployment` exists at all.

    Four different situations all have no `ACTIVE` row, and with the active row
    alone every one of them renders as "not deployed" — including a project that
    is deploying *right now*, which would make the console misreport the state of
    every deploy while it is in progress.

    Status and reason strings are literals here, not `DeploymentStatus.*`: these
    values cross the API boundary into the console UI, and a one-sided rename in
    the model would otherwise move both sides together and stay green
    """
    _deployment(db_session, project, admin, status=status, failure_reason=failure_reason)

    payload = client.get(DEPLOYMENT_HEALTH_PATH, headers=_headers(admin.id)).json()
    entry = _entry(payload, project)

    assert entry["active_deployment"] is None
    assert entry["latest_deployment"]["status"] == status
    assert entry["latest_deployment"]["failure_reason"] == failure_reason


def test_the_newest_deployment_wins_the_latest_slot(client, db_session, admin, project):
    """History is append-only, so "latest" has to be
    resolved by recency rather than by whichever row the DB returns first."""
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    _deployment(
        db_session, project, admin, status=DeploymentStatus.SUPERSEDED, created_at=base
    )
    newest = _deployment(
        db_session,
        project,
        admin,
        status=DeploymentStatus.FAILED,
        failure_reason=DeploymentFailureReason.IMAGE_MISSING,
        created_at=base + timedelta(hours=5),
    )
    _deployment(
        db_session,
        project,
        admin,
        status=DeploymentStatus.SUPERSEDED,
        created_at=base + timedelta(hours=1),
    )

    entry = _entry(
        client.get(DEPLOYMENT_HEALTH_PATH, headers=_headers(admin.id)).json(), project
    )
    assert entry["latest_deployment"]["id"] == newest.id
    assert entry["latest_deployment"]["failure_reason"] == "IMAGE_MISSING"


def test_recent_failures_span_projects_and_report_their_reason(
    client, db_session, seeded_ids, admin, requester
):
    """The cross-project view the per-project router could not give: failures
    from different projects in one list."""
    first = _project(db_session, seeded_ids, requester, name="Gamma")
    second = _project(db_session, seeded_ids, requester, name="Delta")
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    older = _deployment(
        db_session,
        first,
        admin,
        status=DeploymentStatus.FAILED,
        failure_reason=DeploymentFailureReason.MERGE_FAILED,
        created_at=base,
    )
    newer = _deployment(
        db_session,
        second,
        admin,
        status=DeploymentStatus.FAILED,
        failure_reason=DeploymentFailureReason.START_FAILED,
        created_at=base + timedelta(hours=1),
    )
    # An ACTIVE deployment is not a failure and must not appear in the list.
    _deployment(db_session, first, admin, status=DeploymentStatus.ACTIVE)

    payload = client.get(DEPLOYMENT_HEALTH_PATH, headers=_headers(admin.id)).json()

    assert [row["id"] for row in payload["recent_failures"]] == [newer.id, older.id]
    assert [row["failure_reason"] for row in payload["recent_failures"]] == [
        "START_FAILED",
        "MERGE_FAILED",
    ]
    assert {row["project_id"] for row in payload["recent_failures"]} == {first.id, second.id}
    # 2 failures is nowhere near the bound, so nothing is hidden.
    assert payload["recent_failures_limit"] == 50
    assert payload["recent_failures_truncated"] is False


def test_the_recent_failure_list_is_bounded_and_says_so(client, db_session, admin, project):
    """A silently truncated list reads as "this is everything", so the bound is
    part of the response contract: `recent_failures_limit` is the cap and
    `recent_failures_truncated` says older failures exist beyond it.

    The 50 here is the shipped value written as a literal, not read back from
    `routers.console.RECENT_FAILURE_LIMIT` — otherwise changing the cap would
    move both sides together and this test would never notice.
    """
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    rows = [
        _deployment(
            db_session,
            project,
            admin,
            status=DeploymentStatus.FAILED,
            failure_reason=DeploymentFailureReason.INFRA_ERROR,
            created_at=base + timedelta(minutes=index),
        )
        for index in range(52)
    ]

    payload = client.get(DEPLOYMENT_HEALTH_PATH, headers=_headers(admin.id)).json()

    assert payload["recent_failures_limit"] == 50
    assert payload["recent_failures_truncated"] is True
    assert len(payload["recent_failures"]) == 50
    # Newest first, and it is the *newest* 50 that survive the cut — a bound that
    # kept the oldest would hide exactly the failures an operator is looking for.
    newest_first = [row.id for row in reversed(rows)]
    assert [row["id"] for row in payload["recent_failures"]] == newest_first[:50]


def test_no_secret_bearing_field_appears_in_the_deployment_health_payload(
    client, db_session, admin, project
):
    """`Project.service_env_json` is excluded from
    API responses by design and the console
    must not reintroduce it.

    Asserted against the **serialised payload text**, not against the schema: a
    field absent from a Pydantic model is one `from_attributes` away from being
    present, and the thing that actually matters is that the bytes on the wire do
    not carry the value.
    """
    # the stored column is Fernet ciphertext, not a plain dict — the
    # sentinel has to go through the same encryption seam production code
    # uses, or this would stop exercising the real leak path.
    from app.services.service_env_crypto import encrypt_service_env

    project.service_env_json = encrypt_service_env(
        {
            "FORGE_SERVICE_ID": "svc-console-test",
            "FORGE_APP_SECRET_KEY": "SENTINEL-service-env-app-secret-4b7e19",
        }
    )
    _deployment(db_session, project, admin, status=DeploymentStatus.ACTIVE)
    db_session.flush()
    # Non-vacuous: the secret really is on the row the endpoint reads.
    assert db_session.get(Project, project.id).service_env_json is not None

    response = client.get(DEPLOYMENT_HEALTH_PATH, headers=_headers(admin.id))
    body = response.text

    # ...and the project really is in the response, so absence is not "empty".
    assert _entry(response.json(), project)["active_deployment"] is not None

    assert "service_env_json" not in body
    assert "FORGE_APP_SECRET_KEY" not in body
    assert "SENTINEL-service-env-app-secret-4b7e19" not in body
    assert "FORGE_SERVICE_ID" not in body


# ---------------------------------------------------------------------------
# Effective configuration — the allowlist
# (exposing configuration is an explicit allowlist, never a serialization)
# ---------------------------------------------------------------------------

# The complete response shape, as literals. These two sets ARE the contract: a
# field added to `Settings` that reaches the response fails the equality, and so
# does a field quietly removed from the view.
CONFIGURATION_TOP_LEVEL_KEYS = {"source", "editable", "change_requires_restart", "values"}

CONFIGURATION_VALUE_KEYS = {
    "policy_medium_tier_checks",
    "policy_high_tier_checks",
    "policy_medium_approvals_required",
    "policy_high_approvals_required",
    "build_token_budget",
    "build_cost_budget_usd",
    "sandbox_memory_limit_mb",
    "sandbox_cpu_limit_cores",
    "sandbox_pids_limit",
    "build_max_duration_minutes",
    "preview_ttl_minutes",
    "quota_monthly_usd_default",
    "platform_timezone",
    "service_data_expected_services",
    "service_data_connections_per_service",
    "project_lifecycle_idle_threshold_days",
    "project_lifecycle_live_services_exempt",
    "traffic_signal_configured",
    "traffic_signal_observed_since",
    "traffic_signal_last_poll_at",
    "host_disk_check_interval_seconds",
    "host_disk_min_free_percent",
    "host_disk_min_free_gb",
}

# Every known secret-shaped setting, spelled out here rather than imported. The
# values are sentinels so the test can assert on the VALUE too: a field renamed
# in the allowlist would slip its value through under an innocent key, and only
# a value assertion catches that.
SECRET_SETTING_NAMES = (
    "github_token",
    "oidc_client_secret",
    "session_signing_secret",
    "preview_signing_secret",
    "anthropic_api_key",
    # ADR 0035: the second engine's key; the upstream URL is not a secret.
    "openai_api_key",
    # ADR 0023 §Amendment 5: a credential of higher privilege than
    # anything the platform has held before it — never added to this view,
    # per the console's own rule (never even an "is set"
    # boolean). The two SIZING settings join the allowlist above; the DSN
    # itself must not.
    "service_postgres_admin_dsn",
)

SECRET_SENTINELS = {
    "github_token": "SENTINEL-github-token-8f21ab",
    "oidc_client_secret": "SENTINEL-oidc-client-secret-3c90de",
    "session_signing_secret": "SENTINEL-session-signing-secret-77b1e4",
    "preview_signing_secret": "SENTINEL-preview-signing-secret-a05fc2",
    "anthropic_api_key": "SENTINEL-anthropic-api-key-be34d9",
    "openai_api_key": "SENTINEL-openai-api-key-4d7a19",
    # the shared fake (distinctive, valid-looking, serialised here and never connected to).
    "service_postgres_admin_dsn": FAKE_ADMIN_DSN,
}

# One distinct value per allowlisted setting, so the assertion below pins each
# response key to the right *source field* and not merely to some plausible
# number. Cross-wiring two same-typed fields (say pids limit <- memory limit)
# would pass a per-key type check and fails here.
DISTINCT_SETTINGS_INPUT = {
    "policy_medium_tier_checks": "check_alpha,check_beta",
    "policy_high_tier_checks": "check_gamma",
    "policy_medium_approvals_required": 3,
    "policy_high_approvals_required": 4,
    "build_token_budget": 111_222,
    "build_cost_budget_usd": 7.25,
    "sandbox_memory_limit_mb": 3333,
    "sandbox_cpu_limit_cores": 5.5,
    "sandbox_pids_limit": 444,
    "build_max_duration_minutes": 77,
    "preview_ttl_minutes": 88,
    "quota_monthly_usd_default": 999.5,
    "platform_timezone": "Asia/Seoul",
    "service_data_expected_services": 55,
    "service_data_connections_per_service": 6,
    "project_lifecycle_idle_threshold_days": 61,
    "host_disk_check_interval_seconds": 123.0,
    "host_disk_min_free_percent": 17.5,
    "host_disk_min_free_gb": 9.25,
    # not shown itself -- the view reports what it means for the idle sweep (the expected
    # value below). Under FORGE_ENV=dev the default is False, so True here is not the default.
    "traefik_enabled": True,
}

EXPECTED_CONFIGURATION_VALUES = {
    "policy_medium_tier_checks": ["check_alpha", "check_beta"],
    "policy_high_tier_checks": ["check_gamma"],
    "policy_medium_approvals_required": 3,
    "policy_high_approvals_required": 4,
    "build_token_budget": 111_222,
    "build_cost_budget_usd": 7.25,
    "sandbox_memory_limit_mb": 3333,
    "sandbox_cpu_limit_cores": 5.5,
    "sandbox_pids_limit": 444,
    "build_max_duration_minutes": 77,
    "preview_ttl_minutes": 88,
    "quota_monthly_usd_default": 999.5,
    "platform_timezone": "Asia/Seoul",
    "service_data_expected_services": 55,
    "service_data_connections_per_service": 6,
    "project_lifecycle_idle_threshold_days": 61,
    "project_lifecycle_live_services_exempt": True,
    "traffic_signal_configured": False,
    "traffic_signal_observed_since": None,
    "traffic_signal_last_poll_at": None,
    "host_disk_check_interval_seconds": 123.0,
    "host_disk_min_free_percent": 17.5,
    "host_disk_min_free_gb": 9.25,
}


def _serve_configuration_from(monkeypatch, settings_obj: Settings) -> None:
    """Point the endpoint at a `Settings` instance this test built.

    Patches the router module's own reference, which is what the handler reads —
    the endpoint takes no settings parameter, and an undeclared field cannot be
    assigned onto the live singleton (pydantic refuses), so a purpose-built
    instance is the only way to ask "what would this endpoint do with a
    different/larger Settings?".
    """
    monkeypatch.setattr("app.routers.console.settings", settings_obj)


def test_the_configuration_response_is_a_fixed_set_of_keys(client, admin):
    """In its most direct form: the output
    shape is closed. Key-set **equality** at both levels, not a spot check of a
    few names — a spot check passes happily while a sixth secret sits next to the
    names it checked."""
    payload = client.get(CONFIGURATION_PATH, headers=_headers(admin.id)).json()

    assert set(payload) == CONFIGURATION_TOP_LEVEL_KEYS
    assert set(payload["values"]) == CONFIGURATION_VALUE_KEYS


def test_the_configuration_view_does_not_grow_when_a_setting_is_added(
    client, admin, monkeypatch
):
    """The assertion that actually protects the credentials.

    An allowlist is only worth anything if the *next* field added to `Settings`
    stays out of the response without anyone remembering to keep it out. So this
    adds one — named and valued like a secret — and asserts the response shape is
    byte-for-byte the same set of keys and carries neither the name nor the value.

    A `model_dump()`, a field loop, or a denylist would all fail here. That is the
    whole point: the failure arrives at the moment of the leak, not at review
    time.
    """

    class _SettingsWithAnExtraSecret(Settings):
        a_newly_added_platform_secret: str = "SENTINEL-newly-added-setting-41d7c8"

    grown = _SettingsWithAnExtraSecret(_env_file=None)
    # Non-vacuous: the new field really is on the object the endpoint will read.
    assert grown.a_newly_added_platform_secret == "SENTINEL-newly-added-setting-41d7c8"
    _serve_configuration_from(monkeypatch, grown)

    response = client.get(CONFIGURATION_PATH, headers=_headers(admin.id))
    payload = response.json()

    assert set(payload) == CONFIGURATION_TOP_LEVEL_KEYS
    assert set(payload["values"]) == CONFIGURATION_VALUE_KEYS
    assert "a_newly_added_platform_secret" not in response.text
    assert "SENTINEL-newly-added-setting-41d7c8" not in response.text


def test_no_secret_setting_name_or_value_reaches_the_configuration_response(
    client, admin, monkeypatch
):
    """Secret-shaped settings are absent
    entirely — not redacted, not an "is set" boolean — and `admin` is not a
    reason to relax that. An operator does not need the signing keys to read the
    thresholds.

    Both halves are asserted, keys **and** values, because they fail on different
    mistakes: the name check catches an allowlist line copied in verbatim, and the
    value check catches the same line renamed to something innocuous.
    """
    leaky = Settings(_env_file=None, **SECRET_SENTINELS)
    # Non-vacuous: every sentinel really is loaded on the object under test, so
    # "not in the response" cannot pass because it was never anywhere.
    for name in SECRET_SETTING_NAMES:
        assert getattr(leaky, name) == SECRET_SENTINELS[name]
    _serve_configuration_from(monkeypatch, leaky)

    response = client.get(CONFIGURATION_PATH, headers=_headers(admin.id))
    body = response.text

    for name in SECRET_SETTING_NAMES:
        assert name not in body
        assert SECRET_SENTINELS[name] not in body

    # Not redacted and not reported as present, either (rule 3).
    assert "redact" not in body.lower()
    assert "***" not in body
    # Still a real answer rather than an empty one.
    assert set(response.json()["values"]) == CONFIGURATION_VALUE_KEYS


def test_every_allowlisted_value_is_read_from_its_own_setting(client, admin, monkeypatch):
    """One equality that pins the key set *and* the wiring behind every key.

    Each input value is unique, so an allowlist line assigned from the wrong
    settings field — the likeliest copy-paste in a 13-line hand-written block —
    shows up as a wrong value rather than passing a shape check.

    Also confirms the two tier lists are served parsed: an operator reads check
    names, not a comma-separated config line.
    """
    configured = Settings(_env_file=None, **DISTINCT_SETTINGS_INPUT)
    _serve_configuration_from(monkeypatch, configured)

    payload = client.get(CONFIGURATION_PATH, headers=_headers(admin.id)).json()
    assert payload["values"] == EXPECTED_CONFIGURATION_VALUES


@pytest.mark.parametrize(
    ("traefik_enabled", "expected"),
    [(True, True), (False, False)],
)
def test_the_configuration_view_says_whether_live_services_are_exempt_from_the_idle_sweep(
    client, admin, monkeypatch, traefik_enabled, expected
):
    """the one place an operator learns why a long-idle
    live service is never flagged. Both polarities, read from a `Settings` this test built, and
    the setting it comes from (`traefik_enabled`) is not itself put on the view."""
    _serve_configuration_from(monkeypatch, Settings(_env_file=None, traefik_enabled=traefik_enabled))

    response = client.get(CONFIGURATION_PATH, headers=_headers(admin.id))

    assert response.json()["values"]["project_lifecycle_live_services_exempt"] is expected
    assert "traefik_enabled" not in response.text


def test_the_configuration_response_marks_itself_read_only_and_restart_only(client, admin):
    """The values must be
    labelled as read from the environment and changeable only by a restart, so a
    client cannot present them as editable (a disabled control with no
    explanation is worse than a plainly read-only display). That design is what
    makes this true, and making it false would reverse that decision."""
    payload = client.get(CONFIGURATION_PATH, headers=_headers(admin.id)).json()

    assert payload["source"] == "environment"
    assert payload["editable"] is False
    assert payload["change_requires_restart"] is True


def test_the_configuration_endpoint_neither_serialises_nor_iterates_settings():
    """Pinned mechanically: "Never
    `model_dump()`, never a loop over fields, never everything-except-a-denylist."

    Parsed with `ast` rather than grepped so prose *about* `model_dump` in this
    module's docstrings (there is some, deliberately) is not a false positive.
    `getattr` is banned alongside the serialisation calls because a
    `{name: getattr(settings, name) for name in NAMES}` allowlist would make the
    response key set data-driven — which is field iteration with extra steps, and
    it defeats both the fixed-key-set test above and rule 2's "adding a field is a
    deliberate edit".
    """
    module_path = Path(__file__).resolve().parents[1] / "app" / "routers" / "console.py"
    source = module_path.read_text(encoding="utf-8")
    # Non-vacuous: this is the module that builds the allowlist.
    assert "PlatformConfigurationValuesOut(" in source

    tree = ast.parse(source)

    banned_attributes = {
        "model_dump",
        "model_dump_json",
        "model_fields",
        "model_fields_set",
        "model_extra",
        "__fields__",
        "__dict__",
        "dict",
        "json",
    }
    banned_functions = {"getattr", "setattr", "vars"}

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in banned_attributes:
            offenders.append(f"line {node.lineno}: .{node.attr}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in banned_functions
        ):
            offenders.append(f"line {node.lineno}: {node.func.id}()")
        # No loop or comprehension may iterate over settings.
        if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            if "settings" in ast.dump(node.iter):
                offenders.append(f"line {getattr(node.iter, 'lineno', '?')}: iterates settings")

    assert offenders == []


def test_the_configuration_view_reports_the_traffic_signal_state_and_the_runtime_exemption(
    client, admin, db_session, monkeypatch
):
    """with Traefik on and the port set, the exemption is read from the
    persisted signal state (fresh poll AND observed for a whole threshold), and the two state
    timestamps are shown. Literal expectations; unconfigured shows null (the fixed-values test)."""
    from app.models.traffic_signal import TrafficSignalState

    now = datetime.now(timezone.utc)
    observed_since = now - timedelta(days=100)
    last_poll = now - timedelta(seconds=30)
    db_session.add(
        TrafficSignalState(
            id="platform", traefik_started_at=observed_since, last_poll_at=last_poll, observed_since=observed_since
        )
    )
    db_session.commit()
    _serve_configuration_from(
        monkeypatch, Settings(_env_file=None, traefik_enabled=True, traefik_metrics_port=8082)
    )

    values = client.get(CONFIGURATION_PATH, headers=_headers(admin.id)).json()["values"]

    assert values["project_lifecycle_live_services_exempt"] is False  # 100 d observed >= 90 d, poll fresh
    shown = datetime.fromisoformat(values["traffic_signal_observed_since"]).replace(tzinfo=timezone.utc)
    assert abs(shown - observed_since) < timedelta(seconds=1)
    assert values["traffic_signal_last_poll_at"] is not None

    # the same state with the poll gone stale: exempt again
    state = db_session.get(TrafficSignalState, "platform")
    state.last_poll_at = now - timedelta(minutes=10)
    db_session.commit()
    values = client.get(CONFIGURATION_PATH, headers=_headers(admin.id)).json()["values"]
    assert values["project_lifecycle_live_services_exempt"] is True


def test_the_traffic_signal_fields_are_null_while_the_port_is_unset(client, admin, db_session, monkeypatch):
    from app.models.traffic_signal import TrafficSignalState

    now = datetime.now(timezone.utc)
    db_session.add(TrafficSignalState(id="platform", traefik_started_at=now, last_poll_at=now, observed_since=now))
    db_session.commit()
    _serve_configuration_from(monkeypatch, Settings(_env_file=None, traefik_enabled=True))

    values = client.get(CONFIGURATION_PATH, headers=_headers(admin.id)).json()["values"]

    assert values["traffic_signal_observed_since"] is None
    assert values["traffic_signal_last_poll_at"] is None
    assert values["project_lifecycle_live_services_exempt"] is True


@pytest.mark.parametrize(
    ("traefik_enabled", "port", "expected"),
    [(True, 8082, True), (True, None, False), (False, 8082, False), (False, None, False)],
)
def test_traffic_signal_configured_is_true_only_with_traefik_on_and_the_port_set(
    client, admin, monkeypatch, traefik_enabled, port, expected
):
    """distinguishes "configured but never read" from "not configured" -- both send null
    timestamps. Derived by `signal_is_configured`; the port itself is not put on the view."""
    _serve_configuration_from(
        monkeypatch, Settings(_env_file=None, traefik_enabled=traefik_enabled, traefik_metrics_port=port)
    )

    response = client.get(CONFIGURATION_PATH, headers=_headers(admin.id))

    assert response.json()["values"]["traffic_signal_configured"] is expected
    assert "traefik_metrics_port" not in response.text


def test_the_traffic_signal_timestamps_carry_a_utc_offset_even_though_sqlite_drops_it(
    client, admin, db_session, monkeypatch
):
    """a naive ISO string is read as local time by a browser (9 h off in KST), so a fresh
    poll showed as stale. The row is expired and reloaded so the values really come back naive."""
    from app.models.traffic_signal import TrafficSignalState

    now = datetime.now(timezone.utc)
    db_session.add(TrafficSignalState(id="platform", traefik_started_at=now, last_poll_at=now, observed_since=now))
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(TrafficSignalState, "platform").last_poll_at.tzinfo is None  # premise
    _serve_configuration_from(
        monkeypatch, Settings(_env_file=None, traefik_enabled=True, traefik_metrics_port=8082)
    )

    values = client.get(CONFIGURATION_PATH, headers=_headers(admin.id)).json()["values"]

    for key in ("traffic_signal_observed_since", "traffic_signal_last_poll_at"):
        assert values[key].endswith(("Z", "+00:00")), values[key]
