"""Unit tests for `app/services/deploy.py` — no
Docker, no network. Docker is faked via `FakeDeployManager`, the git host
via `FakeMergeProvider`, so these cover: confirm eligibility/serialization,
the merge step (preflight before L4, the merge and confirmed-ref
advance after the health gate and routing, MERGE_FAILED on failure),
the health-gated switchover state machine, rollback target selection, stop,
secret issuance stability, and the worker-side queue (claim + stale sweep).
Real-Docker behavior is covered by tests/test_deploy_integration.py.

ADR 0023: `confirm_deploy`/`execute_deployment` now refuse without a
configured `SERVICE_POSTGRES_ADMIN_DSN`, and `ensure_service_env` now
provisions the project's Postgres database/role + Redis container before
composing the env dict. The `_fake_data_services` autouse fixture below
keeps every OTHER test in this file — which predates provisioning and is not about
provisioning — on a fake, deterministic provisioner and a syntactically
configured (but non-functional) admin DSN, so the six-key shape is produced
without any real Postgres/Docker I/O. Tests that specifically exercise the
DSN-unset refusal (d2/d3) or a REAL provisioning failure (d4) override this
per-test via `monkeypatch`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.config import settings
from app.models.approval import ApprovalRequest, ApprovalStatus
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentFailureReason, DeploymentStatus
from app.models.project import Project
from app.services import service_data
from app.services.service_data import (
    provision_project_data_services as _real_provision_project_data_services,
)
from app.services.deploy import (
    DeployConflictError,
    DeployProjectArchivedError,
    DeployValidationError,
    claim_next_deployment,
    confirm_deploy,
    ensure_service_env,
    execute_deployment,
    get_active_deployment,
    get_in_flight_deployment,
    request_rollback,
    stop_active_deployment,
    sweep_stale_deployments,
)
from app.services.git_provider import GitProviderError, PullRequestRef, RepoRef
from app.services.service_env_crypto import decrypt_service_env
from app.services.status import BuildStatus, ProjectStatus
from tests.fake_admin_dsn import FAKE_ADMIN_DSN

MERGE_SHA = "cafebabe" * 5


def _fake_provision_project_data_services(project_id: str, *, docker_client=None) -> dict[str, str]:
    """Deterministic stand-in for `service_data.provision_project_data_services`
    — no real Postgres/Docker I/O, so every earlier test in this file keeps
    working against the new six-key env shape unchanged."""
    return {
        "DATABASE_URL": f"postgresql://svc_{project_id}:pw@forge-service-postgres:5432/svc_{project_id}",
        "REDIS_URL": f"redis://forge-service-redis-{project_id}:6379/0",
    }


def _fake_ensure_project_redis(
    project_id: str, *, docker_client=None, redis_url: str | None = None
) -> str:
    """Stand-in for `service_data.ensure_project_redis` (re-ensured on
    every deployment start, so the already-issued path calls it too). Returns
    the stored URL unchanged -- no Docker I/O, no upgrade -- which is what an
    already-correct Redis does; the real behaviour is proven against a real
    engine in `tests/test_deploy_integration.py`, and the upgrade seam in
    `test_an_issued_env_is_reensured_...` below."""
    return redis_url or f"redis://forge-service-redis-{project_id}:6379/0"


@pytest.fixture(autouse=True)
def _fake_data_services(monkeypatch):
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    monkeypatch.setattr(
        "app.services.deploy.service_data.provision_project_data_services",
        _fake_provision_project_data_services,
    )
    monkeypatch.setattr(
        "app.services.deploy.service_data.ensure_project_redis", _fake_ensure_project_redis
    )


class FakeDeployManager:
    """Docker double: records calls, behavior toggled per test."""

    def __init__(self, *, image_present=True, healthy=True, start_ok=True, connect_ok=True):
        self.image_present = image_present
        self.healthy = healthy
        self.start_ok = start_ok
        self.connect_ok = connect_ok
        self.started: list[dict] = []
        self.removed: list[str] = []
        self.connected: list[str] = []
        self.confirmed_ref_at_start: str | None = None
        self.confirmed_ref_at_connect: str | None = None
        self._project_for_ref_capture: Project | None = None

    def image_exists(self, image_ref):
        return self.image_present and bool(image_ref)

    def start_service(self, *, project_id, deployment_id, build_id, image_ref, env):
        if not self.start_ok:
            raise RuntimeError("simulated container start failure")
        if self._project_for_ref_capture is not None:
            # Captures ordering: what was confirmed_ref when L4 was first touched?
            # (still the previous value -- the merge comes after the health gate.)
            self.confirmed_ref_at_start = self._project_for_ref_capture.confirmed_ref
        self.started.append(
            {
                "project_id": project_id,
                "deployment_id": deployment_id,
                "build_id": build_id,
                "image_ref": image_ref,
                "env": dict(env),
            }
        )

    def wait_healthy(self, deployment_id, *, timeout_seconds=None):
        return self.healthy

    def connect_to_traefik(self, deployment_id):
        # no-op-shaped double — Traefik routing is exercised for real
        # in tests/test_deploy_traefik_integration.py; unit tests here only
        # need this method to exist and, for one ROUTE_FAILED test, to be
        # able to fail (mirrors DeployManager.connect_to_traefik, which is
        # itself a no-op when settings.traefik_enabled is False — the
        # default in every unit test run).
        if not self.connect_ok:
            raise RuntimeError("simulated traefik attach failure")
        if self._project_for_ref_capture is not None:
            self.confirmed_ref_at_connect = self._project_for_ref_capture.confirmed_ref
        self.connected.append(deployment_id)

    def remove_service(self, deployment_id):
        self.removed.append(deployment_id)
        return True


class FakeMergeProvider:
    """Git-host double for the merge step. `pr_state` is what the host reports for the PR
    (`open`, `merged`, `closed`, or anything else a test wants to show is refused) and
    `pr_merge_commit` the commit it names when it says `merged`."""

    def __init__(
        self,
        *,
        pr_state="open",
        merge_error=False,
        pr_merge_commit=None,
        merge_sha=MERGE_SHA,
        on_merge=None,
    ):
        self.pr_state = pr_state
        self.merge_sha = merge_sha
        self.on_merge = on_merge  # called as the provider merges: lets a test observe what else happened
        self.pr_merge_commit = pr_merge_commit
        self.merge_error = merge_error
        self.merge_calls: list[int] = []
        self.any_calls = 0

    def create_repo(self, *, name):
        self.any_calls += 1
        return RepoRef(full_name=f"org/{name}", clone_url="unused", default_branch="main")

    def push_branch(self, *, repo, branch, local_path):  # pragma: no cover - unused
        raise NotImplementedError

    def open_pr(self, *, repo, branch, base_branch, title, body):  # pragma: no cover - unused
        return PullRequestRef(number=1, url="u", state="open")

    def resolve_ref(self, *, repo, branch):  # pragma: no cover - unused
        return MERGE_SHA

    def get_pr_state(self, *, repo, pr_number):
        self.any_calls += 1
        return PullRequestRef(
            number=pr_number, url="u", state=self.pr_state, merge_commit=self.pr_merge_commit
        )

    def merge_pr(self, *, repo, pr_number):
        self.any_calls += 1
        self.merge_calls.append(pr_number)
        if self.on_merge is not None:
            self.on_merge()
        if self.merge_error:
            raise GitProviderError("simulated merge conflict")
        self.pr_state = "merged"  # a real host now reports it merged
        self.pr_merge_commit = self.merge_sha
        return self.merge_sha


@pytest.fixture()
def project(db_session, seeded_ids):
    project = Project(
        name="deploy-unit-project",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.commit()
    return project


def _make_build(
    db,
    project,
    *,
    build_id="bld-1",
    status=BuildStatus.SUCCEEDED,
    image_ref="forge-build-img",
    diff_ref="forge-build-bld-1#1",
    eligible=True,
    created_at=None,
):
    build = Build(
        id=build_id,
        project_id=project.id,
        type="create",
        status=status,
        image_ref=image_ref,
        diff_ref=diff_ref,
    )
    if created_at is not None:
        build.created_at = created_at
    db.add(build)
    db.flush()
    if eligible:
        db.add(
            ApprovalRequest(
                build_id=build.id,
                project_id=project.id,
                requester_id=project.owner_id,
                risk_tier="low",
                status=ApprovalStatus.AUTO_APPROVED,
                required_approvals=1,
            )
        )
    db.commit()
    db.refresh(build)
    return build


def _confirm(db, project, build, *, actor_id=None, image_exists=lambda ref: True):
    deployment = confirm_deploy(
        db,
        project=project,
        build=build,
        actor_id=actor_id or project.owner_id,
        image_exists=image_exists,
    )
    db.commit()
    db.refresh(deployment)
    return deployment


# -- confirm validation --------------------------------------------------


def test_confirm_creates_deploying_row_and_audits_actor(db_session, project):
    build = _make_build(db_session, project)

    deployment = _confirm(db_session, project, build)

    assert deployment.status == DeploymentStatus.DEPLOYING
    assert deployment.build_id == build.id
    assert deployment.confirmed_by_id == project.owner_id
    assert deployment.rollback_of is None
    audit = (
        db_session.query(AuditLog)
        .filter_by(action="deployment.confirmed", target=f"deployment:{deployment.id}")
        .one()
    )
    assert audit.actor_id == project.owner_id


def test_confirm_rejects_non_succeeded_build(db_session, project):
    build = _make_build(db_session, project, status=BuildStatus.FAILED)
    with pytest.raises(DeployValidationError, match="not SUCCEEDED"):
        _confirm(db_session, project, build)


def test_confirm_rejects_non_eligible_build(db_session, project):
    build = _make_build(db_session, project, eligible=False)
    with pytest.raises(DeployValidationError, match="not deploy-eligible"):
        _confirm(db_session, project, build)


def test_confirm_rejects_non_latest_successful_build(db_session, project):
    old = _make_build(
        db_session,
        project,
        build_id="bld-old",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    _make_build(db_session, project, build_id="bld-new")
    with pytest.raises(DeployValidationError, match="latest successful"):
        _confirm(db_session, project, old)


def test_confirm_rejects_missing_image(db_session, project):
    build = _make_build(db_session, project, image_ref=None)
    with pytest.raises(DeployValidationError, match="no available image"):
        _confirm(db_session, project, build)

    build2 = _make_build(db_session, project, build_id="bld-2")
    with pytest.raises(DeployValidationError, match="no available image"):
        _confirm(db_session, project, build2, image_exists=lambda ref: False)


def test_confirm_rejects_while_another_deployment_in_flight(db_session, project):
    build = _make_build(db_session, project)
    _confirm(db_session, project, build)  # first one is now DEPLOYING (in flight)

    with pytest.raises(DeployConflictError):
        _confirm(db_session, project, build)


# -- ADR 0023 §Data services: PROVISION_UNCONFIGURED refusal -------


def test_confirm_rejects_when_no_admin_dsn_configured(db_session, project, monkeypatch):
    """Rule: "no admin DSN configured =>
    promotion is refused" -- at confirm, before a Deployment row exists."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)
    build = _make_build(db_session, project)

    with pytest.raises(DeployValidationError, match="SERVICE_POSTGRES_ADMIN_DSN"):
        confirm_deploy(
            db_session,
            project=project,
            build=build,
            actor_id=project.owner_id,
            image_exists=lambda ref: True,
        )

    # No Deployment row was created, and no merge was attempted.
    assert db_session.query(Deployment).count() == 0


def test_confirm_rejects_when_admin_dsn_is_blank(db_session, project, monkeypatch):
    """A blank string is NOT
    configured, not a third state."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", "   ")
    build = _make_build(db_session, project)

    with pytest.raises(DeployValidationError, match="SERVICE_POSTGRES_ADMIN_DSN"):
        confirm_deploy(
            db_session,
            project=project,
            build=build,
            actor_id=project.owner_id,
            image_exists=lambda ref: True,
        )


def test_execute_fails_provision_unconfigured_before_starting_any_container(
    db_session, project, monkeypatch
):
    """(d3) A SEPARATE check from (d2): the worker is a different process
    (ADR 0023 §Amendment 3), so this is exercised directly against
    `execute_deployment`, bypassing `confirm_deploy` entirely (a Deployment
    row is built directly, the way an operator's `.env` edit landing
    between confirm and worker-claim would leave one)."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)
    build = _make_build(db_session, project)
    deployment = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=project.owner_id,
        status=DeploymentStatus.DEPLOYING,
    )
    db_session.add(deployment)
    db_session.commit()
    manager = FakeDeployManager()

    execute_deployment(db_session, deployment, manager=manager, git_provider=FakeMergeProvider())

    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.FAILED
    assert deployment.failure_reason == DeploymentFailureReason.PROVISION_UNCONFIGURED
    assert manager.started == []  # no container was ever started


def test_execute_provision_failure_against_configured_instance_is_provision_failed(
    db_session, project, monkeypatch
):
    """(d4) Distinguishes PROVISION_FAILED from both PROVISION_UNCONFIGURED
    (the DSN below IS configured) and INFRA_ERROR (a bare `Exception` from
    elsewhere in `execute_deployment` -- this is specifically a data-service
    provisioning failure). Uses the REAL `service_data.provision_project_
    data_services` against the shared fake DSN, a syntactically valid one
    whose host cannot resolve (it fails in milliseconds, reaches no
    server and carries its own `connect_timeout`), rather than the
    file-wide fake provisioner, so this is a genuine provisioning failure,
    not a canned one."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    monkeypatch.setattr(
        "app.services.deploy.service_data.provision_project_data_services",
        _real_provision_project_data_services,
    )
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    manager = FakeDeployManager()

    execute_deployment(db_session, deployment, manager=manager, git_provider=FakeMergeProvider())

    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.FAILED
    assert deployment.failure_reason == DeploymentFailureReason.PROVISION_FAILED
    assert manager.started == []  # never reached container start


# -- merge step (after the health gate) -----------


def test_execute_advances_confirmed_ref_only_after_the_container_is_healthy_and_routed(
    db_session, project
):
    """Was `..._advances_confirmed_ref_before_l4`, which asserted `confirmed_ref ==
    MERGE_SHA` at container start; that assertion is replaced by its inverse. The merge
    itself is observed from inside the provider: the container is already started and
    routed, and the deployment is still DEPLOYING (so the merge precedes the flip)."""
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    manager = FakeDeployManager()
    manager._project_for_ref_capture = project
    seen_at_merge: dict = {}

    def observe():
        seen_at_merge["started"] = len(manager.started)
        seen_at_merge["connected"] = list(manager.connected)
        seen_at_merge["deployment_status"] = db_session.get(Deployment, deployment.id).status
        seen_at_merge["confirmed_ref"] = project.confirmed_ref

    provider = FakeMergeProvider(on_merge=observe)

    execute_deployment(db_session, deployment, manager=manager, git_provider=provider)

    db_session.refresh(deployment)
    db_session.refresh(project)
    assert deployment.status == DeploymentStatus.ACTIVE
    assert provider.merge_calls == [1]
    assert project.confirmed_ref == MERGE_SHA
    assert manager.confirmed_ref_at_start is None  # NOT advanced when the container started
    assert manager.confirmed_ref_at_connect is None  # nor when it was routed
    assert seen_at_merge == {
        "started": 1,
        "connected": [deployment.id],
        "deployment_status": DeploymentStatus.DEPLOYING,  # merged before the flip
        "confirmed_ref": None,
    }


def test_execute_merge_failure_after_health_removes_the_new_container_and_keeps_the_old_one(
    db_session, project
):
    """Was `..._merge_failure_fails_deployment_l4_untouched` (asserted
    `manager.started == []`): a merge conflict that survives preflight now surfaces after the
    container started, so the assertion is replaced by its inverse (started, then removed)."""
    build1 = _make_build(db_session, project, build_id="bld-1", diff_ref="b1#1")
    first = _confirm(db_session, project, build1)
    execute_deployment(
        db_session,
        first,
        manager=FakeDeployManager(),
        git_provider=FakeMergeProvider(merge_sha="first-sha"),
    )
    db_session.refresh(project)
    assert project.confirmed_ref == "first-sha"

    build2 = _make_build(db_session, project, build_id="bld-2", diff_ref="b2#2")
    second = _confirm(db_session, project, build2)
    provider = FakeMergeProvider(merge_error=True)
    manager = FakeDeployManager()
    execute_deployment(db_session, second, manager=manager, git_provider=provider)

    db_session.refresh(first)
    db_session.refresh(second)
    db_session.refresh(project)
    assert second.status == DeploymentStatus.FAILED
    assert second.failure_reason == DeploymentFailureReason.MERGE_FAILED
    assert provider.merge_calls == [2]  # the merge was attempted, and refused
    assert [entry["deployment_id"] for entry in manager.started] == [second.id]  # it had started
    assert manager.removed == [second.id]  # ... and was removed again
    assert project.confirmed_ref == "first-sha"  # never advanced
    assert first.status == DeploymentStatus.ACTIVE  # the previous deployment still serves
    assert get_active_deployment(db_session, project.id).id == first.id
    assert db_session.query(AuditLog).filter_by(action="deployment.merged").count() == 1  # only the first
    (failed,) = _audit_for(db_session, "deployment.failed", second.id)
    assert "container_removed" not in failed.metadata_json


def test_execute_merge_failure_on_a_first_deployment_leaves_confirmed_ref_unset(db_session, project):
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    manager = FakeDeployManager()

    execute_deployment(
        db_session, deployment, manager=manager, git_provider=FakeMergeProvider(merge_error=True)
    )

    db_session.refresh(deployment)
    db_session.refresh(project)
    assert deployment.failure_reason == DeploymentFailureReason.MERGE_FAILED
    assert project.confirmed_ref is None
    assert manager.removed == [deployment.id]
    assert project.status == ProjectStatus.SUBMITTED  # never activated


def test_a_merge_failure_after_health_whose_cleanup_was_incomplete_says_so(db_session, project):
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    manager = StubbornDeployManager()
    manager.stubborn.add(deployment.id)

    execute_deployment(
        db_session, deployment, manager=manager, git_provider=FakeMergeProvider(merge_error=True)
    )

    db_session.refresh(deployment)
    assert deployment.failure_reason == DeploymentFailureReason.MERGE_FAILED
    (entry,) = _audit_for(db_session, "deployment.failed", deployment.id)
    assert entry.metadata_json["container_removed"] is False


@pytest.mark.parametrize("failure", ["image", "start", "health", "route", "provision"])
def test_a_failure_before_the_merge_leaves_the_pr_open_and_the_ref_unchanged(
    db_session, project, monkeypatch, failure
):
    """the PR stays open and `confirmed_ref` stays where it was, on a first deployment
    (None) and on a later one (the previous ref), for every reason that comes before the merge."""
    if failure == "provision":
        monkeypatch.setattr(
            "app.services.deploy.service_data.provision_project_data_services",
            _real_provision_project_data_services,
        )
    expected_reason = {
        "image": DeploymentFailureReason.IMAGE_MISSING,
        "start": DeploymentFailureReason.START_FAILED,
        "health": DeploymentFailureReason.HEALTH_TIMEOUT,
        "route": DeploymentFailureReason.ROUTE_FAILED,
        "provision": DeploymentFailureReason.PROVISION_FAILED,
    }[failure]

    for previous_ref in (None, "previous-sha"):
        manager = {
            "image": FakeDeployManager(image_present=False),
            "start": FakeDeployManager(start_ok=False),
            "health": FakeDeployManager(healthy=False),
            "route": FakeDeployManager(connect_ok=False),
            "provision": FakeDeployManager(),
        }[failure]
        project.confirmed_ref = previous_ref
        build = _make_build(db_session, project, build_id=f"bld-{failure}-{previous_ref}")
        deployment = _confirm(db_session, project, build)
        provider = FakeMergeProvider(pr_state="open")
        execute_deployment(db_session, deployment, manager=manager, git_provider=provider)

        db_session.refresh(deployment)
        db_session.refresh(project)
        assert deployment.status == DeploymentStatus.FAILED
        assert deployment.failure_reason == expected_reason
        assert provider.merge_calls == []  # the PR was never merged ...
        assert provider.pr_state == "open"  # ... so it is still open
        assert project.confirmed_ref == previous_ref
        assert db_session.query(AuditLog).filter_by(action="deployment.merged").count() == 0


def test_a_retry_of_a_build_that_failed_before_the_merge_merges_the_still_open_pr(db_session, project):
    build = _make_build(db_session, project)
    first_try = _confirm(db_session, project, build)
    provider = FakeMergeProvider(pr_state="open")
    execute_deployment(
        db_session, first_try, manager=FakeDeployManager(healthy=False), git_provider=provider
    )
    db_session.refresh(project)
    assert first_try.failure_reason == DeploymentFailureReason.HEALTH_TIMEOUT
    assert project.confirmed_ref is None and provider.merge_calls == []

    retry = _confirm(db_session, project, build)
    execute_deployment(db_session, retry, manager=FakeDeployManager(), git_provider=provider)

    db_session.refresh(retry)
    db_session.refresh(project)
    assert retry.status == DeploymentStatus.ACTIVE
    assert provider.merge_calls == [1]  # merged now, once
    assert project.confirmed_ref == MERGE_SHA


@pytest.mark.parametrize(
    ("manager_kwargs", "provider_kwargs", "expected_reason", "expected_status", "has_ref"),
    [
        ({"healthy": False}, {}, DeploymentFailureReason.HEALTH_TIMEOUT, 409, False),
        ({"start_ok": False}, {}, DeploymentFailureReason.START_FAILED, 409, False),
        ({"connect_ok": False}, {}, DeploymentFailureReason.ROUTE_FAILED, 409, False),
        ({}, {"merge_error": True}, DeploymentFailureReason.MERGE_FAILED, 409, False),
        ({}, {}, None, 201, True),  # the inverse: a deployment that went ACTIVE opens the gate
    ],
    ids=["health", "start", "route", "merge", "active"],
)
def test_a_modify_request_is_refused_after_a_failed_first_deployment_through_the_router(
    client,
    db_session,
    seeded_ids,
    project,
    manager_kwargs,
    provider_kwargs,
    expected_reason,
    expected_status,
    has_ref,
):
    """End to end through the real router (not only the `_project_has_confirmed_ref`
    predicate): a first deployment that fails before it is ACTIVE leaves `confirmed_ref` unset,
    so the gate still says `NO_CONFIRMED_REF`; a first deployment that succeeds opens it."""
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    execute_deployment(
        db_session,
        deployment,
        manager=FakeDeployManager(**manager_kwargs),
        git_provider=FakeMergeProvider(**provider_kwargs),
    )
    db_session.refresh(deployment)
    db_session.refresh(project)
    assert deployment.failure_reason == expected_reason
    assert (project.confirmed_ref is not None) is has_ref

    resp = client.post(
        f"/api/projects/{project.id}/change-requests",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={"nl_request": "목록에 검색창 추가해줘"},
    )

    assert resp.status_code == expected_status
    if expected_status == 409:
        assert resp.json()["detail"].startswith("NO_CONFIRMED_REF: ")


def test_a_retry_after_a_failure_that_came_after_the_merge_skips_the_merge(
    db_session, project, monkeypatch
):
    """The residual window: the merge committed and the flip then failed. The
    retry sees `merged`, equal ref, and merges nothing a second time (equality repair)."""
    build = _make_build(db_session, project)
    first_try = _confirm(db_session, project, build)
    provider = FakeMergeProvider(pr_state="open")

    def failing_activate(db, deployment, old_active):
        raise RuntimeError("simulated flip failure")

    with monkeypatch.context() as patched:
        patched.setattr("app.services.deploy._activate", failing_activate)
        execute_deployment(
            db_session, first_try, manager=FakeDeployManager(), git_provider=provider
        )
    db_session.refresh(first_try)
    db_session.refresh(project)
    assert first_try.failure_reason == DeploymentFailureReason.ROUTE_FAILED
    assert provider.merge_calls == [1]
    assert project.confirmed_ref == MERGE_SHA  # the merge happened before the flip

    retry = _confirm(db_session, project, build)
    execute_deployment(db_session, retry, manager=FakeDeployManager(), git_provider=provider)

    db_session.refresh(retry)
    db_session.refresh(project)
    assert retry.status == DeploymentStatus.ACTIVE
    assert provider.merge_calls == [1]  # still one: the retry did not merge again
    assert project.confirmed_ref == MERGE_SHA
    assert db_session.query(AuditLog).filter_by(action="deployment.merged").count() == 1


# This test used to be
# `test_execute_skips_merge_when_pr_already_merged`, with `pr_state="closed"` standing for
# "already merged" and an ACTIVE deployment as the expected result: it pinned the bug
# (a PR closed WITHOUT merging let unmerged code go live) as intended behaviour. A PR the
# host says is `merged` is the already-merged case now, and `closed` is a failure.


def _a_deployment_whose_provider_reports(db_session, project, *, pr_state, pr_merge_commit=None, confirmed_ref=None):
    project.confirmed_ref = confirmed_ref
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    provider = FakeMergeProvider(pr_state=pr_state, pr_merge_commit=pr_merge_commit)
    manager = FakeDeployManager()
    return deployment, provider, manager


def test_execute_a_pr_closed_without_merging_is_merge_failed_and_l4_is_never_touched(db_session, project):
    # The provider names a commit although the PR is closed: GitHub fills `merge_commit_sha` on
    # a PR that was never merged (a test merge), so a closed PR that is read as merged would
    # promote exactly that commit. Without it the refusal below could come from "merged, but no
    # commit named" instead, and `closed` itself would go untested.
    deployment, provider, manager = _a_deployment_whose_provider_reports(
        db_session, project, pr_state="closed", pr_merge_commit=MERGE_SHA, confirmed_ref="previous-sha"
    )

    execute_deployment(db_session, deployment, manager=manager, git_provider=provider)

    db_session.refresh(deployment)
    db_session.refresh(project)
    assert deployment.status == DeploymentStatus.FAILED
    assert deployment.failure_reason == DeploymentFailureReason.MERGE_FAILED
    assert provider.merge_calls == []  # it is not open, so nothing to merge
    assert project.confirmed_ref == "previous-sha"  # unmerged code never advances it
    assert manager.started == [] and manager.removed == [] and manager.connected == []  # L4 untouched
    assert db_session.query(AuditLog).filter_by(action="deployment.merged").count() == 0
    assert project.status == ProjectStatus.SUBMITTED  # activation (which advances the project) never ran


def test_execute_a_closed_pr_on_a_first_deploy_leaves_confirmed_ref_unset_and_fails(db_session, project):
    deployment, provider, manager = _a_deployment_whose_provider_reports(
        db_session, project, pr_state="closed", pr_merge_commit=MERGE_SHA
    )

    execute_deployment(db_session, deployment, manager=manager, git_provider=provider)

    db_session.refresh(deployment)
    db_session.refresh(project)
    assert deployment.failure_reason == DeploymentFailureReason.MERGE_FAILED
    assert project.confirmed_ref is None
    assert manager.started == []


@pytest.mark.parametrize("state", ["unknown", "", "CLOSED", "Merged", None])
def test_execute_a_pr_state_that_is_not_one_of_the_three_fails_closed(db_session, project, state):
    """Only the exact words `open`, `merged`, `closed` mean anything; everything else is a failure,
    never a pass (the old code let every non-`open` word through as "already merged")."""
    deployment, provider, manager = _a_deployment_whose_provider_reports(
        db_session, project, pr_state=state, pr_merge_commit=MERGE_SHA, confirmed_ref="previous-sha"
    )

    execute_deployment(db_session, deployment, manager=manager, git_provider=provider)

    db_session.refresh(deployment)
    db_session.refresh(project)
    assert deployment.failure_reason == DeploymentFailureReason.MERGE_FAILED
    assert project.confirmed_ref == "previous-sha"
    assert manager.started == []


def test_execute_a_provider_that_still_returns_a_bare_state_string_fails_closed(db_session, project):
    """A provider written against the old interface (`get_pr_state` -> `str`) is refused: a string
    carries no merge commit, and the old reading of "not open" as merged is what this removes."""

    class _StringProvider(FakeMergeProvider):
        def get_pr_state(self, *, repo, pr_number):
            return "merged"

    project.confirmed_ref = "previous-sha"
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    manager = FakeDeployManager()

    execute_deployment(db_session, deployment, manager=manager, git_provider=_StringProvider())

    db_session.refresh(deployment)
    db_session.refresh(project)
    assert deployment.failure_reason == DeploymentFailureReason.MERGE_FAILED
    assert project.confirmed_ref == "previous-sha"
    assert manager.started == []


def test_execute_a_merged_pr_on_a_first_deploy_sets_confirmed_ref_from_the_merge_commit_and_proceeds(
    db_session, project
):
    """The worker died between the provider merge and its own commit, and this is the first
    deploy, so `confirmed_ref` is None. The retry must not leave it that way: the next modify
    build would stage an empty workspace."""
    deployment, provider, manager = _a_deployment_whose_provider_reports(
        db_session, project, pr_state="merged", pr_merge_commit=MERGE_SHA
    )
    manager._project_for_ref_capture = project

    execute_deployment(db_session, deployment, manager=manager, git_provider=provider)

    db_session.refresh(deployment)
    db_session.refresh(project)
    assert deployment.status == DeploymentStatus.ACTIVE
    assert provider.merge_calls == []  # already merged: not merged a second time
    assert project.confirmed_ref == MERGE_SHA
    # set from the provider's merge commit only after the container is healthy and
    # routed (the old assertion was `== MERGE_SHA` at start; this is its inverse).
    assert manager.confirmed_ref_at_start is None
    assert manager.confirmed_ref_at_connect is None
    merged = db_session.query(AuditLog).filter_by(action="deployment.merged").one()
    assert merged.metadata_json == {
        "build_id": "bld-1",
        "confirmed_ref": MERGE_SHA,
        "already_merged": True,
        "previous_confirmed_ref": None,
    }


def test_execute_a_merged_pr_whose_confirmed_ref_is_behind_sets_it_to_the_merge_commit(db_session, project):
    """The same crash on a later deploy: `confirmed_ref` is the previous build merge, behind
    the one the provider made."""
    deployment, provider, manager = _a_deployment_whose_provider_reports(
        db_session, project, pr_state="merged", pr_merge_commit=MERGE_SHA, confirmed_ref="previous-sha"
    )

    execute_deployment(db_session, deployment, manager=manager, git_provider=provider)

    db_session.refresh(deployment)
    db_session.refresh(project)
    assert deployment.status == DeploymentStatus.ACTIVE
    assert project.confirmed_ref == MERGE_SHA
    merged = db_session.query(AuditLog).filter_by(action="deployment.merged").one()
    assert merged.metadata_json["previous_confirmed_ref"] == "previous-sha"
    assert merged.metadata_json["already_merged"] is True


def test_execute_a_merged_pr_whose_confirmed_ref_already_is_the_merge_commit_changes_nothing(db_session, project):
    """Re-deploying the same build after a stop: merged, confirmed_ref already the merge commit."""
    deployment, provider, manager = _a_deployment_whose_provider_reports(
        db_session, project, pr_state="merged", pr_merge_commit=MERGE_SHA, confirmed_ref=MERGE_SHA
    )

    execute_deployment(db_session, deployment, manager=manager, git_provider=provider)

    db_session.refresh(deployment)
    db_session.refresh(project)
    assert deployment.status == DeploymentStatus.ACTIVE
    assert provider.merge_calls == []
    assert project.confirmed_ref == MERGE_SHA
    assert db_session.query(AuditLog).filter_by(action="deployment.merged").count() == 0  # nothing was recorded


def test_execute_a_merged_pr_with_no_named_commit_keeps_an_existing_confirmed_ref(db_session, project):
    """A host or an old record that says merged and cannot say which commit: with a confirmed ref
    there is nothing better than leaving it, which is what every merged PR got before."""
    deployment, provider, manager = _a_deployment_whose_provider_reports(
        db_session, project, pr_state="merged", pr_merge_commit=None, confirmed_ref="previous-sha"
    )

    execute_deployment(db_session, deployment, manager=manager, git_provider=provider)

    db_session.refresh(deployment)
    db_session.refresh(project)
    assert deployment.status == DeploymentStatus.ACTIVE
    assert project.confirmed_ref == "previous-sha"


def test_execute_a_merged_pr_with_no_named_commit_and_no_confirmed_ref_fails_closed(db_session, project):
    """Nothing to set it from and nothing to keep: going live would stage the next modify build empty."""
    deployment, provider, manager = _a_deployment_whose_provider_reports(
        db_session, project, pr_state="merged", pr_merge_commit=None
    )

    execute_deployment(db_session, deployment, manager=manager, git_provider=provider)

    db_session.refresh(deployment)
    db_session.refresh(project)
    assert deployment.status == DeploymentStatus.FAILED
    assert deployment.failure_reason == DeploymentFailureReason.MERGE_FAILED
    assert project.confirmed_ref is None
    assert manager.started == []


def test_execute_an_open_pr_is_still_merged_and_audited_without_the_recovery_fields(db_session, project):
    deployment, provider, manager = _a_deployment_whose_provider_reports(
        db_session, project, pr_state="open", confirmed_ref="previous-sha"
    )

    execute_deployment(db_session, deployment, manager=manager, git_provider=provider)

    db_session.refresh(project)
    assert provider.merge_calls == [1]
    assert project.confirmed_ref == MERGE_SHA
    merged = db_session.query(AuditLog).filter_by(action="deployment.merged").one()
    assert merged.metadata_json == {"build_id": "bld-1", "confirmed_ref": MERGE_SHA}


def test_execute_build_without_diff_ref_fails_merge(db_session, project):
    build = _make_build(db_session, project, diff_ref=None)
    deployment = _confirm(db_session, project, build)

    manager = FakeDeployManager()
    execute_deployment(db_session, deployment, manager=manager, git_provider=FakeMergeProvider())

    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.FAILED
    assert deployment.failure_reason == DeploymentFailureReason.MERGE_FAILED
    assert manager.started == [] and manager.connected == []  # a preflight refusal: L4 untouched


# -- switchover state machine --------------------------------------------


def test_execute_healthy_activation_supersedes_old_and_removes_its_container(db_session, project):
    build1 = _make_build(db_session, project, build_id="bld-1", diff_ref="b1#1")
    first = _confirm(db_session, project, build1)
    manager = FakeDeployManager()
    execute_deployment(db_session, first, manager=manager, git_provider=FakeMergeProvider())
    assert first.status == DeploymentStatus.ACTIVE

    build2 = _make_build(db_session, project, build_id="bld-2", diff_ref="b2#2")
    second = _confirm(db_session, project, build2)
    execute_deployment(db_session, second, manager=manager, git_provider=FakeMergeProvider())

    db_session.refresh(first)
    db_session.refresh(second)
    assert second.status == DeploymentStatus.ACTIVE
    assert first.status == DeploymentStatus.SUPERSEDED
    assert first.ended_at is not None
    assert first.id in manager.removed  # old container cleaned up after the flip
    # Every transition audited.
    for action, target_id in [
        ("deployment.activated", second.id),
        ("deployment.superseded", first.id),
    ]:
        assert (
            db_session.query(AuditLog)
            .filter_by(action=action, target=f"deployment:{target_id}")
            .count()
            == 1
        )


def test_execute_health_timeout_leaves_old_active_and_removes_new(db_session, project):
    build1 = _make_build(db_session, project, build_id="bld-1", diff_ref="b1#1")
    first = _confirm(db_session, project, build1)
    execute_deployment(
        db_session, first, manager=FakeDeployManager(), git_provider=FakeMergeProvider()
    )

    build2 = _make_build(db_session, project, build_id="bld-2", diff_ref="b2#2")
    second = _confirm(db_session, project, build2)
    unhealthy = FakeDeployManager(healthy=False)
    execute_deployment(db_session, second, manager=unhealthy, git_provider=FakeMergeProvider())

    db_session.refresh(first)
    db_session.refresh(second)
    assert second.status == DeploymentStatus.FAILED
    assert second.failure_reason == DeploymentFailureReason.HEALTH_TIMEOUT
    assert second.id in unhealthy.removed  # new container removed
    assert first.status == DeploymentStatus.ACTIVE  # old untouched, still routed
    assert get_active_deployment(db_session, project.id).id == first.id


def test_execute_traefik_attach_failure_is_route_failed_leaves_old_active(db_session, project):
    """a healthy container that fails to join the Traefik network is
    ROUTE_FAILED, same taxonomy slot as a route-flip failure — new container
    removed, old deployment untouched and still routed. (Traefik routing is
    disabled by default in tests; this exercises the failure branch via the
    fake regardless of settings.traefik_enabled — DeployManager itself only
    calls the real Docker API when the setting is on.)"""
    build1 = _make_build(db_session, project, build_id="bld-1", diff_ref="b1#1")
    first = _confirm(db_session, project, build1)
    execute_deployment(
        db_session, first, manager=FakeDeployManager(), git_provider=FakeMergeProvider()
    )

    build2 = _make_build(db_session, project, build_id="bld-2", diff_ref="b2#2")
    second = _confirm(db_session, project, build2)
    unattachable = FakeDeployManager(connect_ok=False)
    execute_deployment(db_session, second, manager=unattachable, git_provider=FakeMergeProvider())

    db_session.refresh(first)
    db_session.refresh(second)
    assert second.status == DeploymentStatus.FAILED
    assert second.failure_reason == DeploymentFailureReason.ROUTE_FAILED
    assert second.id in unattachable.removed  # new container removed
    assert first.status == DeploymentStatus.ACTIVE  # old untouched, still routed
    assert get_active_deployment(db_session, project.id).id == first.id


def test_execute_start_failure_fails_deployment(db_session, project):
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)

    execute_deployment(
        db_session,
        deployment,
        manager=FakeDeployManager(start_ok=False),
        git_provider=FakeMergeProvider(),
    )

    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.FAILED
    assert deployment.failure_reason == DeploymentFailureReason.START_FAILED


def test_execute_image_missing_at_execution_time(db_session, project):
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)

    execute_deployment(
        db_session,
        deployment,
        manager=FakeDeployManager(image_present=False),
        git_provider=FakeMergeProvider(),
    )

    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.FAILED
    assert deployment.failure_reason == DeploymentFailureReason.IMAGE_MISSING


# -- secret injection ----------------------------------------------------


def test_service_env_issued_once_and_stable(db_session, project):
    env1 = ensure_service_env(db_session, project)
    db_session.commit()
    env2 = ensure_service_env(db_session, project)

    assert env1 == env2  # stable across redeploys/rollbacks
    assert env1["FORGE_SERVICE_ID"] == project.id
    assert env1["FORGE_APP_SECRET_KEY"]  # non-empty secret
    # Issuance audit carries key NAMES only — never a value.
    audit = (
        db_session.query(AuditLog).filter_by(action="deployment.service_env_issued").one()
    )
    assert "FORGE_APP_SECRET_KEY" in audit.metadata_json["keys"]
    assert env1["FORGE_APP_SECRET_KEY"] not in str(audit.metadata_json)


# -- ADR 0023 §Data services: DATABASE_URL / REDIS_URL join the env -


def test_service_env_issues_database_and_redis_urls(db_session, project):
    """(c1) Provisioning happens BEFORE the dict is composed -- both new
    keys are present on the very first issuance, alongside the original
    four."""
    env = ensure_service_env(db_session, project)

    assert set(env) == {
        "FORGE_SERVICE_ID",
        "FORGE_SERVICE_NAME",
        "FORGE_SERVICE_BASE_URL",
        "FORGE_APP_SECRET_KEY",
        "DATABASE_URL",
        "REDIS_URL",
    }
    assert env["DATABASE_URL"]
    assert env["REDIS_URL"]


def test_service_env_upgrades_a_legacy_four_key_env_without_regenerating_the_secret(
    db_session, project
):
    """A project whose `service_env_json` predates data-service provisioning (four
    keys only, the original shape) gains DATABASE_URL/REDIS_URL on its
    NEXT deploy, without touching FORGE_APP_SECRET_KEY -- provisioning is
    additive to an already-issued env, never a re-issue. Simulated by
    writing a stored four-key env directly (the exact shape
    `ensure_service_env` produced before data-service provisioning), bypassing this function
    entirely, then calling it as a later deploy would."""
    from app.services.service_env_crypto import encrypt_service_env

    legacy_env = {
        "FORGE_SERVICE_ID": project.id,
        "FORGE_SERVICE_NAME": project.name,
        "FORGE_SERVICE_BASE_URL": f"/api/projects/{project.id}/service",
        "FORGE_APP_SECRET_KEY": "pre-existing-secret-unchanged",
    }
    project.service_env_json = encrypt_service_env(legacy_env)
    db_session.commit()

    env = ensure_service_env(db_session, project)
    db_session.commit()

    assert set(env) == {
        "FORGE_SERVICE_ID",
        "FORGE_SERVICE_NAME",
        "FORGE_SERVICE_BASE_URL",
        "FORGE_APP_SECRET_KEY",
        "DATABASE_URL",
        "REDIS_URL",
    }
    assert env["FORGE_APP_SECRET_KEY"] == "pre-existing-secret-unchanged"  # NOT regenerated
    assert env["DATABASE_URL"]
    assert env["REDIS_URL"]

    # Stored, and stable on the NEXT call too (no further provisioning).
    stored = decrypt_service_env(project.service_env_json)
    assert stored == env


def test_service_env_audit_carries_six_key_names_and_no_secret_values(db_session, project):
    """(c3) The audit entry records key NAMES only, now six of them -- never
    a DSN, password, or URL value anywhere in the metadata."""
    env = ensure_service_env(db_session, project)
    db_session.commit()

    audit = db_session.query(AuditLog).filter_by(action="deployment.service_env_issued").one()
    assert set(audit.metadata_json["keys"]) == set(env)
    assert len(audit.metadata_json["keys"]) == 6

    metadata_text = str(audit.metadata_json)
    for value in env.values():
        assert value not in metadata_text


def test_redeploy_reuses_data_services_and_does_not_regenerate_password(
    db_session, project, monkeypatch
):
    """(b4) Provisioning is idempotent per project: a second deploy of the
    same project reuses the existing database/role and Redis container and
    does not regenerate the password -- because `ensure_service_env`'s own
    caching means the SECOND call never calls the provisioner again at all
    (a call counter proves that directly, rather than only inferring it
    from the returned values being equal)."""
    calls: list[str] = []

    def _counting_fake(project_id, *, docker_client=None):
        calls.append(project_id)
        return _fake_provision_project_data_services(project_id, docker_client=docker_client)

    monkeypatch.setattr(
        "app.services.deploy.service_data.provision_project_data_services", _counting_fake
    )

    env1 = ensure_service_env(db_session, project)
    db_session.commit()
    env2 = ensure_service_env(db_session, project)

    assert calls == [project.id]  # provisioned exactly once
    assert env1["DATABASE_URL"] == env2["DATABASE_URL"]
    assert env1["REDIS_URL"] == env2["REDIS_URL"]


# -- ADR 0023 §Amendment 12: the stored env does not vouch for the ---
# -- Redis container ---------------------------------------------------------


def test_an_issued_env_re_ensures_its_redis_on_every_call(db_session, project, monkeypatch):
    """The short-circuit that used to return a fully issued env untouched no
    longer covers Redis: the stored `REDIS_URL` says a container was once
    created, not that it exists now. Every call hands the stored URL and the
    Docker client to `ensure_project_redis`, which starts / recreates what is
    missing."""
    calls: list[tuple] = []

    def _spy(project_id, *, docker_client=None, redis_url=None):
        calls.append((project_id, redis_url, docker_client))
        return redis_url

    monkeypatch.setattr("app.services.deploy.service_data.ensure_project_redis", _spy)
    client = object()

    env1 = ensure_service_env(db_session, project, docker_client=client)  # first issuance
    db_session.commit()
    ensure_service_env(db_session, project, docker_client=client)
    ensure_service_env(db_session, project, docker_client=client)

    stored_url = env1["REDIS_URL"]
    assert calls == [(project.id, stored_url, client)] * 2


def test_a_password_less_stored_redis_url_is_upgraded_once_and_re_encrypted(
    db_session, project, monkeypatch
):
    """A service issued before this change holds `redis://<name>:6379/0`. The first
    re-ensure returns a URL with a password; the stored env takes it through
    the same encrypted path as issuance, the audit entry names the key and
    never a value, and the next call finds nothing to upgrade."""
    legacy = f"redis://forge-service-redis-{project.id}:6379/0"
    upgraded = f"redis://:newpw-caseac@forge-service-redis-{project.id}:6379/0"

    def _ensure(project_id, *, docker_client=None, redis_url=None):
        return redis_url if redis_url == upgraded else upgraded

    monkeypatch.setattr("app.services.deploy.service_data.ensure_project_redis", _ensure)
    env_before = ensure_service_env(db_session, project)
    env_before["REDIS_URL"] = legacy
    from app.services.service_env_crypto import encrypt_service_env

    project.service_env_json = encrypt_service_env(env_before)
    db_session.commit()

    env = ensure_service_env(db_session, project)
    db_session.commit()

    assert env["REDIS_URL"] == upgraded
    stored = decrypt_service_env(project.service_env_json)
    assert stored["REDIS_URL"] == upgraded
    assert {k: v for k, v in stored.items() if k != "REDIS_URL"} == {
        k: v for k, v in env_before.items() if k != "REDIS_URL"
    }
    assert "newpw-caseac" not in project.service_env_json  # ciphertext at rest, not plaintext
    audit = db_session.query(AuditLog).filter_by(action="deployment.service_env_upgraded").one()
    assert audit.metadata_json == {"keys": ["REDIS_URL"]}
    assert "newpw-caseac" not in str(audit.metadata_json)

    ensure_service_env(db_session, project)
    db_session.commit()
    assert db_session.query(AuditLog).filter_by(action="deployment.service_env_upgraded").count() == 1


def test_a_redis_that_cannot_be_ensured_fails_the_deployment_as_provision_failed(
    db_session, project, monkeypatch
):
    """Re-ensuring happens before the service container is started, and a
    failure there is PROVISION_FAILED (an operator problem with the data
    service), not START_FAILED or INFRA_ERROR -- on a redeploy of an already
    issued env, which is the path that used to skip the check entirely."""
    from app.services.service_data import ServiceDataProvisionError

    build1 = _make_build(db_session, project, build_id="bld-1", diff_ref="b1#1")
    first = _confirm(db_session, project, build1)
    execute_deployment(
        db_session, first, manager=FakeDeployManager(), git_provider=FakeMergeProvider()
    )
    assert first.status == DeploymentStatus.ACTIVE

    def _cannot(project_id, *, docker_client=None, redis_url=None):
        raise ServiceDataProvisionError("simulated: the Redis container will not start")

    monkeypatch.setattr("app.services.deploy.service_data.ensure_project_redis", _cannot)
    build2 = _make_build(db_session, project, build_id="bld-2", diff_ref="b2#2")
    second = _confirm(db_session, project, build2)
    manager = FakeDeployManager()

    execute_deployment(db_session, second, manager=manager, git_provider=FakeMergeProvider())

    db_session.refresh(second)
    db_session.refresh(first)
    assert second.status == DeploymentStatus.FAILED
    assert second.failure_reason == DeploymentFailureReason.PROVISION_FAILED
    assert manager.started == []  # never reached container start
    assert first.status == DeploymentStatus.ACTIVE  # the running deployment is untouched


# -- service base URL scheme is operator configuration ---


def test_service_env_base_url_defaults_to_http(db_session, project, monkeypatch):
    """Byte-for-byte with the earlier behaviour: traefik_enabled on, scheme
    left at its default, base URL is still http://."""
    from app.config import settings

    monkeypatch.setattr(settings, "traefik_enabled", True)
    assert settings.service_url_scheme == "http"

    env = ensure_service_env(db_session, project)

    assert env["FORGE_SERVICE_BASE_URL"] == f"http://{project.id}.{settings.apps_base_domain}"


def test_service_env_base_url_follows_configured_scheme(db_session, project, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "traefik_enabled", True)
    monkeypatch.setattr(settings, "service_url_scheme", "https")

    env = ensure_service_env(db_session, project)

    assert env["FORGE_SERVICE_BASE_URL"] == f"https://{project.id}.{settings.apps_base_domain}"


def test_service_env_injected_into_container_env(db_session, project):
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    manager = FakeDeployManager()

    execute_deployment(db_session, deployment, manager=manager, git_provider=FakeMergeProvider())

    injected = manager.started[0]["env"]
    # the stored column is Fernet ciphertext now — decrypt through
    # the same seam production code uses, rather than the raw dict this
    # used to be.
    assert injected == decrypt_service_env(project.service_env_json)
    assert injected["FORGE_APP_SECRET_KEY"]


# -- encryption at rest --------------------------


def test_service_env_json_is_stored_as_ciphertext_not_plaintext(db_session, project):
    """Criterion: assert on the STORED BYTES, not the round-trip — a
    round-trip assertion alone would pass even if encryption were a no-op
    (e.g. `encrypt_service_env` returning `json.dumps(env)` unchanged)."""
    env = ensure_service_env(db_session, project)
    db_session.commit()

    raw = db_session.execute(
        text("SELECT service_env_json FROM projects WHERE id = :id"),
        {"id": project.id},
    ).scalar_one()

    assert raw is not None
    assert isinstance(raw, str)
    # The plaintext secret value must not appear verbatim in the stored bytes.
    assert env["FORGE_APP_SECRET_KEY"] not in raw
    # Nor may the raw JSON text of the plaintext env appear as a substring —
    # this is the assertion a no-op "encryption" (e.g. base64 of the JSON,
    # or the JSON unchanged) would fail where a bare "is it valid JSON"
    # check would not.
    assert json.dumps(env) not in raw
    for key in env:
        assert key not in raw
    # It IS valid Fernet ciphertext under the configured key — proves this
    # is real encryption, not e.g. garbage or a hash.
    decrypted = decrypt_service_env(raw)
    assert decrypted == env


def test_service_env_decryption_uses_current_key_before_old_key(db_session, project, monkeypatch):
    """Pins ORDERING, not just that the fallback exists: a value written
    under the CURRENT key must still decrypt when SERVICE_ENV_ENCRYPTION_KEY_OLD
    is set to a WRONG (unrelated) key — proving the current key is tried
    first, not `_OLD`."""
    from cryptography.fernet import Fernet

    from app.config import settings

    env = ensure_service_env(db_session, project)
    db_session.commit()
    stored = project.service_env_json

    monkeypatch.setattr(settings, "service_env_encryption_key_old", Fernet.generate_key().decode())

    assert decrypt_service_env(stored) == env


def test_service_env_key_rotation_old_key_decrypts_after_rotation(db_session, project, monkeypatch):
    """The rotation scenario: a value written under the
    OLD key must still be readable once SERVICE_ENV_ENCRYPTION_KEY has
    moved on and SERVICE_ENV_ENCRYPTION_KEY_OLD is set to the retired key."""
    from app.config import settings

    old_key = settings.service_env_encryption_key
    env = ensure_service_env(db_session, project)
    db_session.commit()
    stored_under_old_key = project.service_env_json

    from cryptography.fernet import Fernet

    new_key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "service_env_encryption_key", new_key)
    monkeypatch.setattr(settings, "service_env_encryption_key_old", old_key)

    assert decrypt_service_env(stored_under_old_key) == env


def test_service_env_key_old_is_decrypt_only_not_used_to_write(db_session, project, monkeypatch):
    """`_OLD` must never be used to WRITE. Something newly encrypted while
    `_OLD` is set must still decrypt after `_OLD` is removed entirely
    (proves it was written under the CURRENT key, not `_OLD`)."""
    from cryptography.fernet import Fernet

    from app.config import settings

    monkeypatch.setattr(settings, "service_env_encryption_key_old", Fernet.generate_key().decode())

    env = ensure_service_env(db_session, project)
    db_session.commit()
    stored = project.service_env_json

    monkeypatch.setattr(settings, "service_env_encryption_key_old", None)

    assert decrypt_service_env(stored) == env


def test_service_env_decrypt_failure_in_prod_raises_and_leaves_row_untouched(
    db_session, project, monkeypatch
):
    from app.config import ProdServiceEnvEncryptionKeyError, settings
    from cryptography.fernet import Fernet

    ensure_service_env(db_session, project)
    db_session.commit()
    stored_before = project.service_env_json

    # Simulate "cannot decrypt": swap the key out from under the stored value.
    monkeypatch.setattr(settings, "service_env_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "service_env_encryption_key_old", None)
    monkeypatch.setattr(settings, "forge_env", "prod")

    with pytest.raises(ProdServiceEnvEncryptionKeyError) as exc_info:
        ensure_service_env(db_session, project)

    # Distinguishes this from the prod-unset-at-startup message (same
    # exception TYPE, must NOT share a message phrase).
    assert "decrypted" in str(exc_info.value)
    assert "requires SERVICE_ENV_ENCRYPTION_KEY to be set explicitly" not in str(exc_info.value)
    # The row is left exactly as it was -- never silently cleared/re-issued.
    assert project.service_env_json == stored_before


def test_service_env_decrypt_failure_with_both_keys_wrong_in_prod_raises(
    db_session, project, monkeypatch
):
    """(g2, boundary level) The same both-keys-wrong branch
    `test_service_env_crypto.py::test_decrypt_with_both_keys_wrong_raises`
    guards at the crypto seam, exercised through the seam the platform
    actually calls (`ensure_service_env`) in prod. Before this test,
    nothing asserted that this specific sub-case propagates all the way
    out to the caller rather than being swallowed somewhere on the way —
    `ensure_service_env` returns `decrypt_service_env(...)`'s result
    directly, so a regression collapsing the crypto-level branch to a
    silent `{}` would surface here as an EMPTY service env handed back in
    prod, with no exception, no warning, no row mutation and no audit
    entry — the outcome this task's own criterion calls the one most
    worth getting right."""
    from app.config import ProdServiceEnvEncryptionKeyError, settings
    from cryptography.fernet import Fernet

    ensure_service_env(db_session, project)
    db_session.commit()
    stored_before = project.service_env_json

    # Both the current key AND `_OLD` are wrong -- distinct from the
    # sibling prod test above, which leaves `_OLD` unset entirely.
    monkeypatch.setattr(settings, "service_env_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "service_env_encryption_key_old", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "forge_env", "prod")

    with pytest.raises(ProdServiceEnvEncryptionKeyError) as exc_info:
        ensure_service_env(db_session, project)

    assert "decrypted" in str(exc_info.value)
    assert project.service_env_json == stored_before


def test_service_env_decrypt_failure_in_dev_reissues_with_warning(
    db_session, project, monkeypatch, caplog
):
    from app.config import settings
    from cryptography.fernet import Fernet

    original_env = ensure_service_env(db_session, project)
    db_session.commit()
    stored_before = project.service_env_json

    monkeypatch.setattr(settings, "service_env_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "service_env_encryption_key_old", None)
    assert settings.forge_env == "dev"

    with caplog.at_level("WARNING", logger="app.deploy"):
        reissued_env = ensure_service_env(db_session, project)

    # No exception at all -- a bare "did not raise" assertion alone would
    # also pass a silent no-op that just kept the old (now-undecryptable)
    # value, so both of the following must hold:
    assert reissued_env["FORGE_APP_SECRET_KEY"] != original_env["FORGE_APP_SECRET_KEY"]
    assert project.service_env_json != stored_before
    assert any("could not be decrypted" in r.message for r in caplog.records)


# -- rollback ------------------------------------------------------------


def _superseded_deployment(db, project, build, *, ended_minutes_ago=10):
    deployment = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=project.owner_id,
        status=DeploymentStatus.SUPERSEDED,
        ended_at=datetime.now(timezone.utc) - timedelta(minutes=ended_minutes_ago),
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)
    return deployment


def test_rollback_targets_most_recent_superseded_with_existing_image(db_session, project):
    build_old = _make_build(db_session, project, build_id="bld-old", image_ref="img-old")
    build_gone = _make_build(db_session, project, build_id="bld-gone", image_ref="img-gone")
    older = _superseded_deployment(db_session, project, build_old, ended_minutes_ago=60)
    newer_but_gone = _superseded_deployment(db_session, project, build_gone, ended_minutes_ago=5)

    rollback = request_rollback(
        db_session,
        project=project,
        actor_id=project.owner_id,
        image_exists=lambda ref: ref != "img-gone",
    )
    db_session.commit()

    # The most recent SUPERSEDED whose image is gone is skipped; the next
    # one back is the target.
    assert rollback.rollback_of == older.id
    assert rollback.build_id == build_old.id
    assert rollback.status == DeploymentStatus.DEPLOYING
    audit = (
        db_session.query(AuditLog)
        .filter_by(action="deployment.rollback_requested", target=f"deployment:{rollback.id}")
        .one()
    )
    assert audit.actor_id == project.owner_id
    assert newer_but_gone.id != rollback.rollback_of


def test_rollback_without_target_raises(db_session, project):
    with pytest.raises(DeployValidationError, match="no previous"):
        request_rollback(
            db_session, project=project, actor_id=project.owner_id, image_exists=lambda ref: True
        )


def test_rollback_execution_skips_merge_no_git_changes(db_session, project):
    build = _make_build(db_session, project, build_id="bld-rb", image_ref="img-rb")
    _superseded_deployment(db_session, project, build)
    rollback = request_rollback(
        db_session, project=project, actor_id=project.owner_id, image_exists=lambda ref: True
    )
    db_session.commit()

    provider = FakeMergeProvider()
    execute_deployment(db_session, rollback, manager=FakeDeployManager(), git_provider=provider)

    db_session.refresh(rollback)
    db_session.refresh(project)
    assert rollback.status == DeploymentStatus.ACTIVE
    assert provider.any_calls == 0  # runtime-only: git never touched
    assert project.confirmed_ref is None  # confirmed ref does not move


def test_rollback_rejected_while_deployment_in_flight(db_session, project):
    build = _make_build(db_session, project)
    _superseded_deployment(db_session, project, build)
    _confirm(db_session, project, build)  # in-flight DEPLOYING

    with pytest.raises(DeployConflictError):
        request_rollback(
            db_session, project=project, actor_id=project.owner_id, image_exists=lambda ref: True
        )


# -- stop ----------------------------------------------------------------


def test_stop_active_deployment(db_session, project):
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    manager = FakeDeployManager()
    execute_deployment(db_session, deployment, manager=manager, git_provider=FakeMergeProvider())

    stopped = stop_active_deployment(
        db_session, project=project, actor_id=project.owner_id, manager=manager
    )
    db_session.commit()

    assert stopped.id == deployment.id
    assert stopped.status == DeploymentStatus.STOPPED
    assert stopped.ended_at is not None
    assert deployment.id in manager.removed
    audit = (
        db_session.query(AuditLog)
        .filter_by(action="deployment.stopped", target=f"deployment:{deployment.id}")
        .one()
    )
    assert audit.actor_id == project.owner_id


def test_stop_active_deployment_with_touch_activity_false_does_not_advance_last_active_at(
    db_session, project
):
    """The idle-project sweep stops a deployment via
    `touch_activity=False` and must NOT reset the very staleness that made
    the project a candidate in the same transaction that archives it. Pins
    the value EXACTLY equal to the pre-stop stale value (not merely "not
    advanced"), so a bug that moves it by a small coarsened amount instead
    of leaving it untouched still fails this test."""
    from app.services.project_activity import _as_aware_utc

    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    manager = FakeDeployManager()
    execute_deployment(db_session, deployment, manager=manager, git_provider=FakeMergeProvider())

    stale = datetime.now(timezone.utc) - timedelta(days=200)
    project.last_active_at = stale
    db_session.commit()

    stop_active_deployment(
        db_session,
        project=project,
        actor_id=None,
        manager=manager,
        touch_activity=False,
    )
    db_session.commit()
    db_session.refresh(project)

    assert _as_aware_utc(project.last_active_at) == stale


def test_stop_without_active_deployment_raises(db_session, project):
    with pytest.raises(DeployValidationError, match="no ACTIVE deployment"):
        stop_active_deployment(
            db_session, project=project, actor_id=project.owner_id, manager=FakeDeployManager()
        )


# -- worker queue (claim + sweep) -----------------------


def test_claim_next_deployment_oldest_first_and_stamps_started_at(db_session, seeded_ids):
    projects = []
    for i in range(2):
        p = Project(
            name=f"claim-{i}",
            owner_id=seeded_ids["user_id"],
            team_id=seeded_ids["team_id"],
            template_id=seeded_ids["template_id"],
            status=ProjectStatus.SUBMITTED,
        )
        db_session.add(p)
        projects.append(p)
    db_session.flush()
    builds = [
        _make_build(db_session, p, build_id=f"bld-claim-{i}", diff_ref=f"b{i}#1")
        for i, p in enumerate(projects)
    ]
    older = Deployment(
        project_id=projects[0].id,
        build_id=builds[0].id,
        confirmed_by_id=projects[0].owner_id,
    )
    older.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    newer = Deployment(
        project_id=projects[1].id,
        build_id=builds[1].id,
        confirmed_by_id=projects[1].owner_id,
    )
    db_session.add_all([older, newer])
    db_session.commit()

    claimed = claim_next_deployment(db_session)
    assert claimed.id == older.id
    assert claimed.started_at is not None

    claimed2 = claim_next_deployment(db_session)
    assert claimed2.id == newer.id

    assert claim_next_deployment(db_session) is None  # nothing unclaimed left


def test_sweep_stale_deployments_fails_stuck_rows(db_session, project, seeded_ids):
    build = _make_build(db_session, project)
    # one DEPLOYING row per project is a database rule now, so the
    # fresh in-flight row belongs to another project.
    other_project = Project(
        name="deploy-unit-other-project",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(other_project)
    db_session.flush()
    other_build = _make_build(db_session, other_project, build_id="bld-other")
    stuck = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=project.owner_id,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=90),
    )
    fresh = Deployment(
        project_id=other_project.id,
        build_id=other_build.id,
        confirmed_by_id=project.owner_id,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add_all([stuck, fresh])
    db_session.commit()

    swept = sweep_stale_deployments(db_session, max_duration_minutes=20)

    assert [d.id for d in swept] == [stuck.id]
    db_session.refresh(stuck)
    db_session.refresh(fresh)
    assert stuck.status == DeploymentStatus.FAILED
    assert stuck.failure_reason == DeploymentFailureReason.INFRA_ERROR
    assert fresh.status == DeploymentStatus.DEPLOYING


def test_worker_run_iteration_claims_deployment_before_build(db_session, project):
    """The worker loop picks a pending deployment ahead of a QUEUED build
    (a deploy takes seconds, a build minutes)."""
    from app.services.build_executor import PlaceholderBuildExecutor
    from app.worker import run_iteration

    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    queued_build = Build(project_id=project.id, type="modify", status=BuildStatus.QUEUED)
    db_session.add(queued_build)
    db_session.commit()

    ran = run_iteration(
        db_session,
        PlaceholderBuildExecutor(),
        deploy_manager=FakeDeployManager(),
        git_provider=FakeMergeProvider(),
    )

    assert isinstance(ran, Deployment)
    assert ran.id == deployment.id
    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.ACTIVE
    db_session.refresh(queued_build)
    assert queued_build.status == BuildStatus.QUEUED  # untouched this tick


# -- Traefik label generation (must still be exercised by tests without Docker) ---


def test_traefik_service_labels_shape():
    from app.config import settings
    from app.services.deploy_manager import traefik_service_labels

    labels = traefik_service_labels(
        project_id="proj-x", deployment_id="dep-y", network_name="forge-deploy-net-abc", priority=42
    )
    host = f"proj-x.{settings.apps_base_domain}"
    assert labels["traefik.enable"] == "true"
    assert labels["traefik.http.routers.forge-deploy-dep-y.rule"] == f"Host(`{host}`)"
    assert labels["traefik.http.routers.forge-deploy-dep-y.priority"] == "42"
    # Routed via the deployment's OWN private network (per-service networks).
    assert labels["traefik.docker.network"] == "forge-deploy-net-abc"
    assert labels[
        "traefik.http.services.forge-deploy-dep-y.loadbalancer.server.port"
    ] == str(settings.preview_container_port)


# -- entrypoint + TLS become operator configuration ------


def test_traefik_service_labels_entrypoint_is_operator_configured(monkeypatch):
    """No literal "web" may remain in the builder — it must read
    settings.traefik_entrypoint, whatever that is set to."""
    from app.config import settings
    from app.services.deploy_manager import traefik_service_labels

    monkeypatch.setattr(settings, "traefik_entrypoint", "websecure")
    labels = traefik_service_labels(
        project_id="proj-x", deployment_id="dep-y", network_name="net", priority=1
    )
    assert labels["traefik.http.routers.forge-deploy-dep-y.entrypoints"] == "websecure"


def test_traefik_service_labels_default_entrypoint_is_web():
    """Byte-for-byte with the earlier behaviour when the setting is left at
    its default."""
    from app.config import settings
    from app.services.deploy_manager import traefik_service_labels

    assert settings.traefik_entrypoint == "web"
    labels = traefik_service_labels(
        project_id="proj-x", deployment_id="dep-y", network_name="net", priority=1
    )
    assert labels["traefik.http.routers.forge-deploy-dep-y.entrypoints"] == "web"


def test_traefik_service_labels_omit_tls_keys_entirely_when_resolver_unset():
    """Criterion: with TRAEFIK_CERT_RESOLVER unset, the label dict must
    contain NO `tls` key and NO `certresolver` key at all — not a falsy
    `tls=false`, which would be a different (wrong) outcome. Anchored
    against vacuity by also asserting a label the builder DOES emit
    (`traefik.docker.network`), so a builder returning `{}` could not pass
    this test."""
    from app.config import settings
    from app.services.deploy_manager import traefik_service_labels

    assert settings.traefik_cert_resolver is None
    labels = traefik_service_labels(
        project_id="proj-x", deployment_id="dep-y", network_name="net-abc", priority=1
    )
    assert labels["traefik.docker.network"] == "net-abc"  # vacuity anchor
    assert "traefik.http.routers.forge-deploy-dep-y.tls" not in labels
    assert "traefik.http.routers.forge-deploy-dep-y.tls.certresolver" not in labels
    assert not any(key.endswith(".tls") or key.endswith(".certresolver") for key in labels)


def test_traefik_service_labels_carry_tls_when_resolver_set(monkeypatch):
    from app.config import settings
    from app.services.deploy_manager import traefik_service_labels

    monkeypatch.setattr(settings, "traefik_cert_resolver", "myresolver")
    labels = traefik_service_labels(
        project_id="proj-x", deployment_id="dep-y", network_name="net", priority=1
    )
    assert labels["traefik.http.routers.forge-deploy-dep-y.tls"] == "true"
    assert labels["traefik.http.routers.forge-deploy-dep-y.tls.certresolver"] == "myresolver"


def test_traefik_router_priority_strictly_decreases_over_time():
    """Older deployment's router must outprioritize a newer one while both
    exist (the dual-router window during a redeploy) — so the priority
    value strictly decreases as time advances."""
    from app.services.deploy_manager import traefik_router_priority

    earlier = traefik_router_priority(now=1_000_000.0)
    later = traefik_router_priority(now=1_000_010.0)
    assert earlier > later > 0


# ===========================================================================
# archived projects, the one-DEPLOYING-row rule, the rollback target,
# failure paths that do not strand a row, and removals that say when they
# did not happen.
# ===========================================================================


def _archive(db, project):
    project.status = ProjectStatus.ARCHIVED
    db.commit()


def _unarchive(db, project):
    project.status = ProjectStatus.ACTIVE
    db.commit()


def _deploying_row(db, project, build, *, started_at=None):
    row = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=project.owner_id,
        status=DeploymentStatus.DEPLOYING,
        started_at=started_at,
    )
    db.add(row)
    db.commit()
    return row


# -- archived projects refuse confirm and rollback ---------------------------


def test_confirm_on_an_archived_project_is_refused_with_its_own_error(db_session, project):
    """Everything else about the confirm is valid (eligible, latest, image
    present, nothing in flight), so the archived rule is the only thing that
    can be refusing."""
    build = _make_build(db_session, project)
    _archive(db_session, project)

    with pytest.raises(DeployProjectArchivedError) as excinfo:
        _confirm(db_session, project, build)

    message = str(excinfo.value)
    assert message.startswith("PROJECT_ARCHIVED: ")
    assert "un-archive" in message
    assert project.id in message
    assert "in flight" not in message  # not the in-flight conflict
    assert db_session.query(Deployment).count() == 0


def test_after_un_archive_the_same_confirm_goes_through(db_session, project):
    build = _make_build(db_session, project)
    _archive(db_session, project)
    with pytest.raises(DeployProjectArchivedError):
        _confirm(db_session, project, build)

    _unarchive(db_session, project)

    deployment = _confirm(db_session, project, build)
    assert deployment.status == DeploymentStatus.DEPLOYING


def test_the_in_flight_conflict_is_not_the_archived_error(db_session, project):
    build = _make_build(db_session, project)
    _confirm(db_session, project, build)

    with pytest.raises(DeployConflictError) as excinfo:
        _confirm(db_session, project, build)

    assert not isinstance(excinfo.value, DeployProjectArchivedError)
    assert "PROJECT_ARCHIVED" not in str(excinfo.value)
    assert "already in flight" in str(excinfo.value)


def test_rollback_on_an_archived_project_is_refused_with_its_own_error(db_session, project):
    """A valid rollback target exists, so the archived rule is the only thing
    that can be refusing."""
    build = _make_build(db_session, project, build_id="bld-rb-arch", image_ref="img-rb")
    _superseded_deployment(db_session, project, build)
    _archive(db_session, project)

    with pytest.raises(DeployProjectArchivedError) as excinfo:
        request_rollback(
            db_session, project=project, actor_id=project.owner_id, image_exists=lambda ref: True
        )

    message = str(excinfo.value)
    assert message.startswith("PROJECT_ARCHIVED: ")
    assert "un-archive" in message
    assert "rolling back" in message
    assert db_session.query(Deployment).filter_by(status=DeploymentStatus.DEPLOYING).count() == 0

    _unarchive(db_session, project)
    rollback = request_rollback(
        db_session, project=project, actor_id=project.owner_id, image_exists=lambda ref: True
    )
    db_session.commit()
    assert rollback.status == DeploymentStatus.DEPLOYING


# -- one DEPLOYING row per project, enforced by the database -----------------


def test_the_database_refuses_a_second_deploying_row_for_one_project(db_session, project):
    from sqlalchemy.exc import IntegrityError

    build = _make_build(db_session, project)
    _deploying_row(db_session, project, build)

    db_session.add(
        Deployment(
            project_id=project.id,
            build_id=build.id,
            confirmed_by_id=project.owner_id,
            status=DeploymentStatus.DEPLOYING,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    assert (
        db_session.query(Deployment)
        .filter_by(project_id=project.id, status=DeploymentStatus.DEPLOYING)
        .count()
        == 1
    )


def test_the_deploying_rule_leaves_every_legitimate_shape_alone(db_session, project, seeded_ids):
    """The index is partial: one ACTIVE plus one DEPLOYING (a redeploy in
    flight), another project's own DEPLOYING row, and a new DEPLOYING row
    once the previous one is terminal are all ordinary."""
    build = _make_build(db_session, project)
    active = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=project.owner_id,
        status=DeploymentStatus.ACTIVE,
    )
    db_session.add(active)
    db_session.commit()
    in_flight = _deploying_row(db_session, project, build)  # ACTIVE + DEPLOYING

    other = Project(
        name="deploy-unit-other",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(other)
    db_session.flush()
    other_build = _make_build(db_session, other, build_id="bld-other-1")
    _deploying_row(db_session, other, other_build)  # another project

    in_flight.status = DeploymentStatus.FAILED
    db_session.commit()
    _deploying_row(db_session, project, build)  # the slot is free again

    assert (
        db_session.query(Deployment)
        .filter_by(project_id=project.id, status=DeploymentStatus.DEPLOYING)
        .count()
        == 1
    )


def test_a_confirm_that_loses_the_race_for_the_deploying_slot_gets_the_in_flight_conflict(
    db_session, project, monkeypatch
):
    """The concurrent-confirm simulation: another request's DEPLOYING row is
    already committed, but THIS request's check-then-insert looked before it
    existed (the first `get_in_flight_deployment` call is made blind). Only
    the database can refuse now, and the loser must see the same conflict the
    check gives -- with a session that still works."""
    build = _make_build(db_session, project)
    winner = _deploying_row(db_session, project, build)

    real_lookup = get_in_flight_deployment
    calls: list[str] = []

    def blind_first_look(db, project_id):
        calls.append(project_id)
        return None if len(calls) == 1 else real_lookup(db, project_id)

    monkeypatch.setattr("app.services.deploy.get_in_flight_deployment", blind_first_look)

    with pytest.raises(DeployConflictError, match="already in flight") as excinfo:
        _confirm(db_session, project, build)

    assert not isinstance(excinfo.value, DeployProjectArchivedError)
    assert len(calls) == 2  # blind check, then the second look after the database said no
    rows = db_session.query(Deployment).filter_by(project_id=project.id).all()
    assert [row.id for row in rows] == [winner.id]  # no second row, and the session is usable


def test_a_rollback_that_loses_the_race_gets_the_in_flight_conflict_too(
    db_session, project, monkeypatch
):
    build = _make_build(db_session, project, build_id="bld-rb-race", image_ref="img-rb")
    _superseded_deployment(db_session, project, build)
    winner = _deploying_row(db_session, project, build)

    real_lookup = get_in_flight_deployment
    calls: list[str] = []

    def blind_first_look(db, project_id):
        calls.append(project_id)
        return None if len(calls) == 1 else real_lookup(db, project_id)

    monkeypatch.setattr("app.services.deploy.get_in_flight_deployment", blind_first_look)

    with pytest.raises(DeployConflictError, match="already in flight"):
        request_rollback(
            db_session, project=project, actor_id=project.owner_id, image_exists=lambda ref: True
        )

    assert len(calls) == 2
    in_flight = (
        db_session.query(Deployment)
        .filter_by(project_id=project.id, status=DeploymentStatus.DEPLOYING)
        .all()
    )
    assert [row.id for row in in_flight] == [winner.id]


def test_an_integrity_error_that_is_not_the_deploying_slot_is_not_reported_as_a_conflict(
    db_session, project, monkeypatch
):
    """The translation is conditional on a DEPLOYING row really existing; any
    other integrity failure (a missing foreign key, say) is a different
    problem and must not read as "already in flight"."""
    from sqlalchemy.exc import IntegrityError

    build = _make_build(db_session, project)

    def refuses(*args, **kwargs):
        raise IntegrityError("INSERT INTO deployments", {}, Exception("FOREIGN KEY constraint failed"))

    monkeypatch.setattr(db_session, "flush", refuses)

    with pytest.raises(IntegrityError):
        _confirm(db_session, project, build)


# -- the rollback target excludes the ACTIVE build ---------------------------


def _activate_build(db, project, build, manager):
    deployment = _confirm(db, project, build)
    execute_deployment(db, deployment, manager=manager, git_provider=FakeMergeProvider())
    db.refresh(deployment)
    assert deployment.status == DeploymentStatus.ACTIVE
    return deployment


def test_rollback_never_targets_the_build_that_is_active(db_session, project):
    """Re-confirming the active build leaves a SUPERSEDED row of that very
    build -- the NEWEST superseded row, which is what an ordering by `ended_at`
    would pick. The target must be the previous build instead."""
    manager = FakeDeployManager()
    build_b = _make_build(
        db_session,
        project,
        build_id="bld-b",
        diff_ref="b#1",
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    b1 = _activate_build(db_session, project, build_b, manager)
    build_a = _make_build(db_session, project, build_id="bld-a", diff_ref="a#1")
    a1 = _activate_build(db_session, project, build_a, manager)  # b1 -> SUPERSEDED
    a2 = _activate_build(db_session, project, build_a, manager)  # the same build again: a1 -> SUPERSEDED
    db_session.refresh(a1)
    assert a1.status == DeploymentStatus.SUPERSEDED
    assert a2.status == DeploymentStatus.ACTIVE

    rollback = request_rollback(
        db_session, project=project, actor_id=project.owner_id, image_exists=lambda ref: True
    )
    db_session.commit()

    assert rollback.rollback_of == b1.id
    assert rollback.build_id == build_b.id


def test_rollback_has_no_target_when_the_only_superseded_row_is_the_active_build(db_session, project):
    manager = FakeDeployManager()
    build_a = _make_build(db_session, project, build_id="bld-a-only", diff_ref="a#1")
    _activate_build(db_session, project, build_a, manager)
    _activate_build(db_session, project, build_a, manager)  # a re-confirm of the same build

    with pytest.raises(DeployValidationError, match="no previous"):
        request_rollback(
            db_session, project=project, actor_id=project.owner_id, image_exists=lambda ref: True
        )


# -- a database error inside execute_deployment does not strand the row ------


def test_a_database_error_inside_execute_leaves_the_row_failed_not_stranded(db_session, project):
    """`image_exists` is called outside every inner handler, so the exception
    reaches the outer one -- after a failed flush, which leaves the session
    unusable until it is rolled back. Earlier, `fail_deployment` itself
    then raised (the session still wanted its rollback) and the row stayed
    DEPLOYING until the stale sweep."""
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)

    class DbBreakingManager(FakeDeployManager):
        def image_exists(self, image_ref):
            db_session.add(
                Deployment(
                    project_id=project.id,
                    build_id=build.id,
                    confirmed_by_id=project.owner_id,
                    status=DeploymentStatus.DEPLOYING,  # the project already has its DEPLOYING row
                )
            )
            db_session.flush()
            return True

    manager = DbBreakingManager()
    result = execute_deployment(
        db_session, deployment, manager=manager, git_provider=FakeMergeProvider()
    )

    assert result.status == DeploymentStatus.FAILED
    assert result.failure_reason == DeploymentFailureReason.INFRA_ERROR
    assert deployment.id in manager.removed  # its container/network were cleaned up too
    rows = db_session.query(Deployment).filter_by(project_id=project.id).all()
    assert [(row.id, row.status) for row in rows] == [(deployment.id, DeploymentStatus.FAILED)]


def test_an_exception_after_activation_does_not_fail_or_tear_down_the_active_deployment(
    db_session, project, monkeypatch
):
    """Whatever raises once the row is ACTIVE and committed, the outer handler
    must not rewrite it to FAILED or remove the container of a service that
    is routed and serving."""
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)

    def boom(db, build, manager):
        raise RuntimeError("simulated failure after activation")

    monkeypatch.setattr("app.services.deploy._teardown_preview_on_promotion", boom)
    manager = FakeDeployManager()

    result = execute_deployment(
        db_session, deployment, manager=manager, git_provider=FakeMergeProvider()
    )

    assert result.status == DeploymentStatus.ACTIVE
    assert result.failure_reason is None
    assert manager.removed == []


# -- removals that did not happen are not silent ----------------------------


class StubbornDeployManager(FakeDeployManager):
    """`remove_service` fails (returns False, the way `DeployManager` reports
    a container or network it could not remove) for the listed deployments,
    or raises for the ones in `raising`."""

    def __init__(self, *args, stubborn=(), raising=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.stubborn = set(stubborn)
        self.raising = set(raising)

    def remove_service(self, deployment_id):
        self.removed.append(deployment_id)
        if deployment_id in self.raising:
            raise RuntimeError("simulated Docker failure")
        return deployment_id not in self.stubborn


def _audit_for(db, action, deployment_id):
    return (
        db.query(AuditLog)
        .filter_by(action=action, target=f"deployment:{deployment_id}")
        .all()
    )


def test_stop_records_a_container_that_could_not_be_removed(db_session, project, caplog):
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    manager = StubbornDeployManager()
    execute_deployment(db_session, deployment, manager=manager, git_provider=FakeMergeProvider())
    manager.stubborn.add(deployment.id)

    with caplog.at_level("ERROR", logger="app.deploy"):
        stopped = stop_active_deployment(
            db_session, project=project, actor_id=project.owner_id, manager=manager
        )
    db_session.commit()

    assert stopped.status == DeploymentStatus.STOPPED
    (entry,) = _audit_for(db_session, "deployment.stopped", deployment.id)
    assert entry.metadata_json["container_removed"] is False
    errors = [r for r in caplog.records if r.levelname == "ERROR" and deployment.id in r.getMessage()]
    assert errors, "a removal that did not happen must be an ERROR in the log"


def test_a_clean_stop_does_not_claim_a_failed_removal(db_session, project):
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    manager = StubbornDeployManager()
    execute_deployment(db_session, deployment, manager=manager, git_provider=FakeMergeProvider())

    stop_active_deployment(db_session, project=project, actor_id=project.owner_id, manager=manager)
    db_session.commit()

    (entry,) = _audit_for(db_session, "deployment.stopped", deployment.id)
    assert "container_removed" not in entry.metadata_json


def test_a_superseded_container_that_could_not_be_removed_is_recorded(db_session, project, caplog):
    manager = StubbornDeployManager()
    build1 = _make_build(db_session, project, build_id="bld-1", diff_ref="b1#1")
    first = _activate_build(db_session, project, build1, manager)
    manager.stubborn.add(first.id)
    build2 = _make_build(db_session, project, build_id="bld-2", diff_ref="b2#2")

    with caplog.at_level("ERROR", logger="app.deploy"):
        second = _activate_build(db_session, project, build2, manager)

    db_session.refresh(first)
    assert second.status == DeploymentStatus.ACTIVE  # the new deployment is not failed by it
    assert first.status == DeploymentStatus.SUPERSEDED
    (entry,) = _audit_for(db_session, "deployment.container_removal_failed", first.id)
    assert entry.metadata_json == {
        "project_id": project.id,
        "superseded_by": second.id,
        "container_removed": False,
    }
    assert any(r.levelname == "ERROR" and first.id in r.getMessage() for r in caplog.records)


def test_a_superseded_removal_that_raises_neither_fails_the_new_deployment_nor_goes_unrecorded(
    db_session, project
):
    manager = StubbornDeployManager()
    build1 = _make_build(db_session, project, build_id="bld-1", diff_ref="b1#1")
    first = _activate_build(db_session, project, build1, manager)
    manager.raising.add(first.id)
    build2 = _make_build(db_session, project, build_id="bld-2", diff_ref="b2#2")

    second = _activate_build(db_session, project, build2, manager)

    assert second.status == DeploymentStatus.ACTIVE
    assert len(_audit_for(db_session, "deployment.container_removal_failed", first.id)) == 1


def test_a_clean_supersede_writes_no_removal_failure(db_session, project):
    manager = StubbornDeployManager()
    build1 = _make_build(db_session, project, build_id="bld-1", diff_ref="b1#1")
    first = _activate_build(db_session, project, build1, manager)
    build2 = _make_build(db_session, project, build_id="bld-2", diff_ref="b2#2")
    _activate_build(db_session, project, build2, manager)

    assert _audit_for(db_session, "deployment.container_removal_failed", first.id) == []


def test_a_failed_deployment_whose_cleanup_was_incomplete_says_so(db_session, project):
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    manager = StubbornDeployManager(healthy=False)
    manager.stubborn.add(deployment.id)

    execute_deployment(db_session, deployment, manager=manager, git_provider=FakeMergeProvider())

    db_session.refresh(deployment)
    assert deployment.failure_reason == DeploymentFailureReason.HEALTH_TIMEOUT
    (entry,) = _audit_for(db_session, "deployment.failed", deployment.id)
    assert entry.metadata_json["reason"] == "HEALTH_TIMEOUT"
    assert entry.metadata_json["container_removed"] is False


def test_a_failed_deployment_with_complete_cleanup_does_not_claim_otherwise(db_session, project):
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    manager = StubbornDeployManager(healthy=False)

    execute_deployment(db_session, deployment, manager=manager, git_provider=FakeMergeProvider())

    (entry,) = _audit_for(db_session, "deployment.failed", deployment.id)
    assert "container_removed" not in entry.metadata_json
    assert deployment.id in manager.removed


# -- the stale sweep removes what a dead worker left -------------------------


def _stale_and_fresh_rows(db, project, seeded_ids):
    build = _make_build(db, project)
    other = Project(
        name="deploy-unit-fresh-project",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db.add(other)
    db.flush()
    other_build = _make_build(db, other, build_id="bld-fresh")
    stuck = _deploying_row(
        db, project, build, started_at=datetime.now(timezone.utc) - timedelta(minutes=90)
    )
    fresh = _deploying_row(db, other, other_build, started_at=datetime.now(timezone.utc))
    return stuck, fresh


def test_the_stale_sweep_removes_the_container_and_network_of_each_row_it_fails(
    db_session, project, seeded_ids
):
    stuck, fresh = _stale_and_fresh_rows(db_session, project, seeded_ids)
    manager = StubbornDeployManager()

    sweep_stale_deployments(db_session, max_duration_minutes=20, manager=manager)

    assert manager.removed == [stuck.id]  # not the fresh, still-running one
    db_session.refresh(stuck)
    assert stuck.status == DeploymentStatus.FAILED
    (entry,) = _audit_for(db_session, "deployment.failed", stuck.id)
    assert "container_removed" not in entry.metadata_json


def test_the_stale_sweep_still_fails_the_row_when_the_removal_does_not_work(
    db_session, project, seeded_ids
):
    stuck, _fresh = _stale_and_fresh_rows(db_session, project, seeded_ids)
    manager = StubbornDeployManager(stubborn=[stuck.id])

    sweep_stale_deployments(db_session, max_duration_minutes=20, manager=manager)

    db_session.refresh(stuck)
    assert stuck.status == DeploymentStatus.FAILED
    (entry,) = _audit_for(db_session, "deployment.failed", stuck.id)
    assert entry.metadata_json["container_removed"] is False


def test_the_stale_sweep_builds_a_manager_only_when_something_is_stale(
    db_session, project, seeded_ids
):
    built: list[int] = []

    def factory():
        built.append(1)
        return StubbornDeployManager()

    sweep_stale_deployments(db_session, max_duration_minutes=20, manager_factory=factory)
    assert built == []  # a quiet tick constructs no Docker client

    stuck, _fresh = _stale_and_fresh_rows(db_session, project, seeded_ids)
    sweep_stale_deployments(db_session, max_duration_minutes=20, manager_factory=factory)
    assert built == [1]
    db_session.refresh(stuck)
    assert stuck.status == DeploymentStatus.FAILED


def test_the_stale_sweep_still_fails_the_row_when_no_manager_can_be_built(
    db_session, project, seeded_ids
):
    stuck, _fresh = _stale_and_fresh_rows(db_session, project, seeded_ids)

    def factory():
        raise RuntimeError("Docker unreachable")

    sweep_stale_deployments(db_session, max_duration_minutes=20, manager_factory=factory)

    db_session.refresh(stuck)
    assert stuck.status == DeploymentStatus.FAILED
    (entry,) = _audit_for(db_session, "deployment.failed", stuck.id)
    assert entry.metadata_json["container_removed"] is False
