"""Integration tests for `SandboxBuildExecutor` against a real
Docker Engine: the full claim -> sandbox -> agent-command -> harvest ->
terminal-state path, using the test-only stub agent sandbox image
(docker/agent-sandbox-stub) instead of the real Claude Agent SDK — no real
Anthropic API key anywhere in this file or the images it builds against.

Requires `forge-llm-proxy:latest` and `forge-agent-sandbox-stub:latest` to
be built locally:
    docker build -t forge-llm-proxy:latest -f docker/llm-proxy/Dockerfile .
    docker build -t forge-agent-sandbox-stub:latest docker/agent-sandbox-stub
(from `backend/`). Skips cleanly if either image is missing, or if Docker
itself is unavailable.

the stub engine WRITES FILES into the sandbox (marker, an
edit, a hostile `.git/hooks/pre-commit`, ... and, on a directive in the request
text, a secret / a tampered Dockerfile / links), so these tests follow real
agent output through the real runner, adapter, executor, a local bare repo, the
image build and the policy gate. Rebuild the stub image after changing it.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("docker")

import docker  # noqa: E402
from docker.errors import DockerException, ImageNotFound  # noqa: E402

from app.config import settings  # noqa: E402
from app.models.build import Build  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.services.agent_adapter import AgentAdapter  # noqa: E402
from app.services.git_provider import PullRequestRef, RepoRef  # noqa: E402
from app.services.sandbox_build_executor import SandboxBuildExecutor  # noqa: E402
from app.services.sandbox_runner import SandboxRunner  # noqa: E402
from app.services.status import BuildFailureReason, ProjectStatus  # noqa: E402
from tests.test_publish import LocalBareRepoProvider, _init_bare_repo  # noqa: E402

PROXY_IMAGE = "forge-llm-proxy:latest"
STUB_AGENT_IMAGE = "forge-agent-sandbox-stub:latest"


class FakeGitProvider:
    """Test double: no real git host. Exercises the executor's
    publish wiring (repo-per-project + branch/PR -> diff_ref) without any
    network call — `publish.py`'s own git mechanics (init/commit) still run
    for real against the harvested workspace; only the host-facing push/PR
    calls are faked here. Provider-level HTTP behavior against a stubbed
    upstream is covered separately by test_github_provider.py."""

    def __init__(self) -> None:
        self.pushed: list[tuple[str, str]] = []  # (branch, local_path)
        self.opened_prs: list[dict] = []
        # Publish fetches the base branch's tip, so the "host" has a real
        # repository to fetch from, with an initial commit on `main`. Only push and
        # PR remain fake.
        self._remote_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._remote = Path(self._remote_dir.name) / "remote.git"
        _init_bare_repo(self._remote)

    def create_repo(self, *, name: str) -> RepoRef:
        return RepoRef(full_name=f"fake-org/{name}", clone_url=str(self._remote), default_branch="main")

    def push_branch(self, *, repo: RepoRef, branch: str, local_path: str) -> None:
        self.pushed.append((branch, local_path))

    def open_pr(self, *, repo: RepoRef, branch: str, base_branch: str, title: str, body: str) -> PullRequestRef:
        self.opened_prs.append(
            {"repo": repo.full_name, "branch": branch, "base": base_branch, "title": title, "body": body}
        )
        return PullRequestRef(number=len(self.opened_prs), url=f"fake://pr/{len(self.opened_prs)}", state="open")

    def resolve_ref(self, *, repo: RepoRef, branch: str) -> str:
        return "0" * 40

    def get_pr_state(self, *, repo: RepoRef, pr_number: int) -> PullRequestRef:
        return PullRequestRef(number=pr_number, url=f"fake://pr/{pr_number}", state="open")


def _docker_available() -> bool:
    try:
        docker.from_env().ping()
        return True
    except DockerException:
        return False


def _images_available() -> bool:
    if not _docker_available():
        return False
    client = docker.from_env()
    try:
        client.images.get(PROXY_IMAGE)
        client.images.get(STUB_AGENT_IMAGE)
        return True
    except (ImageNotFound, DockerException):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _images_available(), reason="required test images not built locally"),
]


@pytest.fixture()
def docker_client():
    return docker.from_env()


@pytest.fixture(autouse=True)
def _stub_agent_sandbox_image(monkeypatch):
    """Point the sandbox-backed executor at the stub agent image (no real
    Claude Agent SDK/API key) instead of the real pinned
    `agent_sandbox_image` for the duration of each test."""
    monkeypatch.setattr(settings, "agent_sandbox_image", STUB_AGENT_IMAGE)
    monkeypatch.setattr(settings, "anthropic_api_key", "fake-key-not-real")
    monkeypatch.setattr(settings, "anthropic_upstream_base_url", "http://example.invalid")


def _make_project(db_session, seeded_ids, name: str = "it-project") -> Project:
    project = Project(
        name=name,
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.commit()
    return project


def _make_build(
    db_session,
    project: Project,
    build_id: str = "build-it-1",
    *,
    nl_request: str = "Build a small to-do list service.",
    build_type: str = "create",
) -> Build:
    """A build with the change request that carries its request -- the executor
 refuses a build that has none. `nl_request` may carry a stub-engine
    directive such as `[stub:secret]` (docker/agent-sandbox-stub)."""
    from app.models.change_request import ChangeRequest

    build = Build(id=build_id, project_id=project.id, type=build_type, engine="anthropic")
    db_session.add(build)
    db_session.flush()
    db_session.add(
        ChangeRequest(
            project_id=project.id,
            requester_id=project.owner_id,
            nl_request=nl_request,
            build_id=build.id,
        )
    )
    db_session.commit()
    return build


def test_full_path_succeeds_with_stub_agent(docker_client, db_session, seeded_ids):
    from app.models.audit import AuditLog
    from app.services.preview_manager import PreviewManager

    provider = FakeGitProvider()
    executor = SandboxBuildExecutor(docker_client=docker_client, git_provider=provider)
    project = _make_project(db_session, seeded_ids)
    build = _make_build(db_session, project, "build-full-success")

    try:
        result = executor.execute(build)

        assert result.ok is True
        assert result.reason is None
        # the agent's own text, handed on exactly as the adapter returned it.
        assert result.summary is None
        assert result.agent_summary is not None
        assert "fake agent completed" in result.agent_summary
        # Usage recorded onto the build even on success.
        # The figures are the PROXY's alone (ADR 0014 §Amendment 2026-10-01): the stub engine
        # never calls the proxy, so what was measured is exactly zero. The stub SDK's own
        # self-reported 333 tokens / 0.0123 USD is written inside L3 and must NOT appear
        # here -- it used to, via the executor's engine fallback.
        assert build.cost_tokens == 0
        assert build.cost_usd == 0.0
        # the real proxy container served its (empty: the stub makes no model call) call
        # record over the new admin route, and the build's trace holds it, its diff and its log.
        from app.models.build_trace import BuildTrace

        stored = db_session.query(BuildTrace).filter_by(build_id=build.id).one()
        assert (stored.calls_state, stored.diff_state) == ("captured", "computed")
        assert stored.calls["steps"] == [] and stored.calls["calls_total"] == 0
        assert stored.diff["files"], "the stub engine writes files, so the diff is not empty"
        assert stored.log_tail is not None
        # publish step recorded diff_ref + image_ref, and pushed/opened a PR.
        assert build.diff_ref == f"forge-build-{build.id}#1"
        assert build.image_ref == f"forge-build-{project.id}-{build.id}"
        assert len(provider.pushed) == 1
        assert len(provider.opened_prs) == 1
        assert project.repo_url == f"fake-org/{project.id}"
        # preview started from the assembled image; signed URL +
        # expiry + token hash (never the raw token) recorded on the build.
        assert build.preview_url is not None
        assert build.preview_url.startswith(
            f"/api/projects/{project.id}/preview/{build.id}?token="
        )
        assert build.preview_expires_at is not None
        assert build.preview_token_hash is not None
        assert build.preview_token_hash not in build.preview_url  # hash != raw token
        # Issuance audit-logged, token never in the log entry.
        db_session.commit()
        audit_rows = (
            db_session.query(AuditLog)
            .filter(AuditLog.action == "preview.issued", AuditLog.target == f"build:{build.id}")
            .all()
        )
        assert len(audit_rows) == 1
        assert "token" not in (audit_rows[0].metadata_json or {})

        # the policy gate ran right after publish/preview and
        # produced a verdict without touching build status or the preview.
        from app.models.policy_verdict import PolicyCheckResult, PolicyVerdict

        verdict = db_session.query(PolicyVerdict).filter_by(build_id=build.id).one()
        assert verdict.risk_tier in ("low", "medium", "high")
        checks = db_session.query(PolicyCheckResult).filter_by(verdict_id=verdict.id).all()
        assert len(checks) == 6
        gate_audit_rows = (
            db_session.query(AuditLog)
            .filter(AuditLog.action == "policy_gate.verdict", AuditLog.target == f"build:{build.id}")
            .all()
        )
        assert len(gate_audit_rows) == 1

        # the verdict also created exactly one ApprovalRequest for
        # this build, linked to the
        # verdict, with the project owner as the recorded requester.
        from app.models.approval import ApprovalRequest, ApprovalStatus

        requests = db_session.query(ApprovalRequest).filter_by(build_id=build.id).all()
        assert len(requests) == 1
        assert requests[0].policy_verdict_id == verdict.id
        assert requests[0].requester_id == project.owner_id
        assert requests[0].status in (ApprovalStatus.AUTO_APPROVED, ApprovalStatus.PENDING)
        # Tier and routing agree: low <-> AUTO_APPROVED, medium/high <-> PENDING.
        if verdict.risk_tier == "low":
            assert requests[0].status == ApprovalStatus.AUTO_APPROVED
        else:
            assert requests[0].status == ApprovalStatus.PENDING
    finally:
        PreviewManager(client=docker_client).stop_preview(build.id)
        if build.image_ref:
            docker_client.images.get(build.image_ref).remove(force=True)


def test_policy_gate_flag_does_not_change_build_status_or_block_preview(docker_client, db_session, seeded_ids):
    """Pipeline-hook acceptance criterion: a flagged verdict is not a
    failed build — the build stays SUCCEEDED and keeps its preview even when the
    gate flags something.

    the secret is planted BY THE AGENT, inside the sandbox (the stub
    engine writes `app/leaked.py` on the `[stub:secret]` directive), and travels
    harvest -> sanitise -> gate. It used to be planted by monkeypatching the
    host-side `_stage_workspace`, which put it in the staged directory -- the
    directory the executor wrongly published -- so the test passed without the
    gate ever seeing agent output. Nothing here touches the host's staging."""
    provider = FakeGitProvider()
    executor = SandboxBuildExecutor(docker_client=docker_client, git_provider=provider)
    project = _make_project(db_session, seeded_ids, name="flagged-project")
    build = _make_build(db_session, project, "build-flagged", nl_request="Build a to-do list. [stub:secret]")

    from app.services.preview_manager import PreviewManager

    try:
        result = executor.execute(build)

        assert result.ok is True  # gate flag never fails the build
        assert build.preview_url is not None  # preview unaffected

        from app.models.policy_verdict import PolicyVerdict

        verdict = db_session.query(PolicyVerdict).filter_by(build_id=build.id).one()
        assert verdict.risk_tier == "high"

        from app.worker import run_one_build
        from app.services.status import BuildStatus

        # Confirm at the worker level too: SUCCEEDED, not FAILED.
        build.status = BuildStatus.RUNNING
        db_session.commit()

        class _AlreadyRanExecutor:
            def execute(self, build):
                return result

        run_one_build(db_session, build, _AlreadyRanExecutor())
        db_session.refresh(build)
        assert build.status == BuildStatus.SUCCEEDED
    finally:
        PreviewManager(client=docker_client).stop_preview(build.id)
        if build.image_ref:
            docker_client.images.get(build.image_ref).remove(force=True)


def test_a_build_without_the_secret_directive_is_not_flagged_high(docker_client, db_session, seeded_ids):
    """The control for the test above: the same stub engine and request, minus
    the directive. If the stub's default output (the marker, the edit, the hostile
    `.git`) already tripped the gate, the test above would prove nothing."""
    executor = SandboxBuildExecutor(
        docker_client=docker_client, git_provider=FakeGitProvider(), preview_manager=FakePreviewManager()
    )
    project = _make_project(db_session, seeded_ids, name="unflagged-project")
    build = _make_build(db_session, project, "build-unflagged")

    try:
        result = executor.execute(build)

        assert result.ok is True
        from app.models.policy_verdict import PolicyVerdict

        verdict = db_session.query(PolicyVerdict).filter_by(build_id=build.id).one()
        assert verdict.risk_tier != "high"
    finally:
        if build.image_ref:
            docker_client.images.get(build.image_ref).remove(force=True)


def test_usage_recorded_even_on_agent_failure(docker_client, db_session, seeded_ids):
    """Wire a fake AgentAdapter reporting failure to prove usage is still
    written onto the build ('including on failure')."""

    class FailingAdapter:
        def run(self, adapter_input):
            from app.services.agent_adapter import AgentAdapterResult

            return AgentAdapterResult(
                ok=False,
                reason=BuildFailureReason.AGENT_ERROR,
                summary="agent exited nonzero",
                usage_tokens=50,
                usage_cost_usd=0.01,
            )

    executor = SandboxBuildExecutor(
        docker_client=docker_client, adapter=FailingAdapter(), git_provider=FakeGitProvider()
    )
    project = _make_project(db_session, seeded_ids)
    build = _make_build(db_session, project, "build-failure")

    result = executor.execute(build)

    assert result.ok is False
    assert result.reason == BuildFailureReason.AGENT_ERROR
    # Even on failure, usage is recorded -- from the proxy's accounting alone. No call
    # went through the proxy in this fake-adapter test, so the measured figure is 0 / 0.0:
    # the field is set (not NULL, which would mean "not measured") and the adapter's
    # self-reported 50 tokens / 0.01 USD is NOT what was written.
    assert build.cost_tokens == 0
    assert build.cost_usd == 0.0
    # Publish is never reached on an agent-level failure.
    assert build.diff_ref is None
    assert build.image_ref is None


def _sandbox_network_ids(docker_client) -> set[str]:
    """The per-build sandbox networks that exist right now (`forge-sandbox-net-...`)."""
    return {n.id for n in docker_client.networks.list() if n.name.startswith(settings.sandbox_network_name)}


def test_engine_unreachable_when_proxy_image_missing(docker_client, db_session, seeded_ids):
    """Proxy container fails to start (missing image) -> ENGINE_UNREACHABLE,
    and no container/network is leaked. The network half is asserted
    now -- this test said "no container/network" and checked containers only,
    so the leaked sandbox network of a failed proxy start passed it."""
    from app.services.proxy_container import ProxyContainer

    original_start = ProxyContainer.start

    def _start_with_missing_image(self, **kwargs):
        kwargs["image"] = "forge-llm-proxy-does-not-exist:latest"
        kwargs["startup_timeout_seconds"] = 3.0
        return original_start(self, **kwargs)

    import app.services.sandbox_build_executor as executor_module

    orig = executor_module.ProxyContainer.start
    executor_module.ProxyContainer.start = _start_with_missing_image
    try:
        executor = SandboxBuildExecutor(docker_client=docker_client, git_provider=FakeGitProvider())
        project = _make_project(db_session, seeded_ids)
        build = _make_build(db_session, project, "build-engine-unreachable")

        before_ids = {c.id for c in docker_client.containers.list(all=True)}
        before_networks = _sandbox_network_ids(docker_client)
        result = executor.execute(build)
        after_ids = {c.id for c in docker_client.containers.list(all=True)}
        after_networks = _sandbox_network_ids(docker_client)

        assert result.ok is False
        assert result.reason == BuildFailureReason.ENGINE_UNREACHABLE
        assert after_ids - before_ids == set()  # no leaked containers
        assert after_networks - before_networks == set()  # no leaked networks
    finally:
        executor_module.ProxyContainer.start = orig


def test_a_proxy_that_starts_and_then_dies_leaves_no_container_and_no_network(
    docker_client, db_session, seeded_ids, monkeypatch
):
    """the proxy container starts and exits at once (an `alpine` image whose
    command ends), so the failure surfaces from the first `exec_run` against it as a Docker 409
    `APIError` -- NOT a `ProxyStartError`. `execute` caught only `ProxyStartError`, so this one
    escaped with the per-build sandbox network still on the host. Now: ENGINE_UNREACHABLE (the proxy
    never became healthy) and neither a container nor a network of the attempt survives."""
    from docker.errors import APIError

    import app.services.proxy_container as proxy_container_module
    import app.services.sandbox_build_executor as executor_module
    from app.services.proxy_container import ProxyContainer, ProxyStartError

    # `start` refuses an image that does not advertise what the worker needs of a proxy, and
    # `alpine` is not one. This test is about the teardown after a container that exits, not about the
    # image check (`test_proxy_container_integration.py` covers that), so it asks for nothing.
    monkeypatch.setattr(proxy_container_module, "REQUIRED_PROXY_CAPABILITIES", ())
    try:
        docker_client.images.get("alpine:3.19")
    except ImageNotFound:
        try:
            docker_client.images.pull("alpine:3.19")  # CI starts without it
        except DockerException:
            pytest.skip("alpine:3.19 is not present locally and could not be pulled")
    except DockerException:
        pytest.skip("alpine:3.19 is not present locally")
    original_start = ProxyContainer.start

    def _start_a_container_that_exits(self, **kwargs):
        kwargs["image"] = "alpine:3.19"
        kwargs["startup_timeout_seconds"] = 3.0
        return original_start(self, **kwargs)

    # Characterise the failure first, directly: it must be the shape the old handler missed.
    network = docker_client.networks.create("forge-sandbox-net-caseae-characterise", driver="bridge", internal=True)
    try:
        with pytest.raises(Exception) as raised:
            _start_a_container_that_exits(
                ProxyContainer(client=docker_client), real_api_key="k", sandbox_network=network
            )
        assert isinstance(raised.value, APIError) and not isinstance(raised.value, ProxyStartError)
        assert raised.value.status_code == 409
    finally:
        network.remove()

    orig = executor_module.ProxyContainer.start
    executor_module.ProxyContainer.start = _start_a_container_that_exits
    try:
        executor = SandboxBuildExecutor(docker_client=docker_client, git_provider=FakeGitProvider())
        project = _make_project(db_session, seeded_ids)
        build = _make_build(db_session, project, "build-proxy-dies")

        before_ids = {c.id for c in docker_client.containers.list(all=True)}
        before_networks = _sandbox_network_ids(docker_client)
        result = executor.execute(build)
        after_ids = {c.id for c in docker_client.containers.list(all=True)}
        after_networks = _sandbox_network_ids(docker_client)

        assert result.ok is False
        assert result.reason == BuildFailureReason.ENGINE_UNREACHABLE
        assert result.summary == "the LLM proxy container failed to start; see the worker log"
        assert after_ids - before_ids == set()
        assert after_networks - before_networks == set()
    finally:
        executor_module.ProxyContainer.start = orig


def test_a_shutdown_raised_while_the_agent_runs_still_tears_down_the_proxy_and_the_network(
    docker_client, db_session, seeded_ids
):
    """ADR 0037 §Decision 6: a SIGTERM reaches the worker while a build's agent is running;
    the handler's `WorkerShutdown` is a `SystemExit`, which the executor's `except Exception` around
    the adapter does not hold, so it unwinds through `execute`'s `finally`. That `finally` is the
    build's "normal failure teardown": the proxy container (it holds the real API key), the per-build
    network and the temp trees go, and the exception still reaches the worker, which fails the row."""
    from app.services.worker_shutdown import WorkerShutdown

    class _InterruptedAdapter:
        def run(self, adapter_input):
            raise WorkerShutdown(15)

    executor = SandboxBuildExecutor(
        docker_client=docker_client, adapter=_InterruptedAdapter(), git_provider=FakeGitProvider()
    )
    project = _make_project(db_session, seeded_ids)
    build = _make_build(db_session, project, "build-shutdown-mid-run")

    before_ids = {c.id for c in docker_client.containers.list(all=True)}
    before_networks = _sandbox_network_ids(docker_client)
    with pytest.raises(WorkerShutdown):
        executor.execute(build)
    after_ids = {c.id for c in docker_client.containers.list(all=True)}
    after_networks = _sandbox_network_ids(docker_client)

    assert after_ids - before_ids == set()  # the proxy container is gone
    assert after_networks - before_networks == set()  # and so is the network


def test_an_agent_that_writes_a_sparse_bomb_fails_the_build_resource_limit_with_the_platforms_own_detail(
    docker_client, db_session, seeded_ids, monkeypatch
):
    """The whole path with the real stub engine: the agent leaves a 3 GiB SPARSE file in the
    workspace (cheap in the sandbox, 3 GiB in the workspace archive), against a 2 GiB cap. A layer of
    the runner refuses it, the adapter hands on the runner's sentence, and the build fails RESOURCE_LIMIT with it as
    its detail -- the agent's own words nowhere in it -- and nothing is published, assembled or left on
    the host. (The stub image must be rebuilt for the `[stub:sparse]` directive.)"""
    monkeypatch.setattr(settings, "sandbox_disk_limit_mb", 2048)
    provider = FakeGitProvider()
    executor = SandboxBuildExecutor(docker_client=docker_client, git_provider=provider)
    project = _make_project(db_session, seeded_ids)
    build = _make_build(db_session, project, "build-sparse-bomb", nl_request="Build a service. [stub:sparse]")
    before_ids = {c.id for c in docker_client.containers.list(all=True)}
    before_networks = _sandbox_network_ids(docker_client)

    result = executor.execute(build)

    assert result.ok is False
    assert result.reason == BuildFailureReason.RESOURCE_LIMIT
    # Whichever layer sees the file first, by engine: the harvest bound (the containerd snapshotter's
    # `SizeRw` ignores a sparse file) or the disk watchdog (overlay2's `SizeRw` counts its apparent size).
    assert result.summary in (
        "the agent run in the sandbox ended unsuccessfully (RESOURCE_LIMIT): "
        "the build's output is larger than the 2048 MiB limit; its output is in the worker log",
        "the agent run in the sandbox ended unsuccessfully (RESOURCE_LIMIT): "
        "the sandbox's files grew past the 2048 MiB disk limit; its output is in the worker log",
    )
    assert "fake agent completed" not in result.summary  # the agent's text is L3's, never the detail
    assert provider.pushed == [] and provider.opened_prs == []
    assert build.diff_ref is None and build.image_ref is None
    assert {c.id for c in docker_client.containers.list(all=True)} - before_ids == set()
    assert _sandbox_network_ids(docker_client) - before_networks == set()


class FakePreviewManager:
    """Test double: records the preview-start call without touching
    Docker. Used where the test's assertions are about the *build* path's
    resource accounting — a real preview container deliberately outlives
    `execute()` (that's its whole point, ADR 0016), so it would show up as
    a false positive in leak accounting. Real preview lifecycle/teardown is
    covered by tests/test_preview_manager_integration.py."""

    def __init__(self) -> None:
        self.started: list[str] = []

    def start_preview(self, *, project_id: str, build):
        from datetime import datetime, timedelta, timezone

        from app.services.preview_manager import PreviewIssued, hash_token

        self.started.append(build.id)
        token = "fake-token"
        return PreviewIssued(
            url=f"/api/projects/{project_id}/preview/{build.id}?token={token}",
            token=token,
            token_hash=hash_token(token),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=60),
        )


def test_no_containers_or_networks_leak_after_run(docker_client, db_session, seeded_ids):
    executor = SandboxBuildExecutor(
        docker_client=docker_client,
        git_provider=FakeGitProvider(),
        preview_manager=FakePreviewManager(),
    )
    project = _make_project(db_session, seeded_ids)
    build = _make_build(db_session, project, "build-no-leak")

    before_containers = {c.id for c in docker_client.containers.list(all=True)}
    before_networks = {n.id for n in docker_client.networks.list()}

    result = executor.execute(build)

    after_containers = {c.id for c in docker_client.containers.list(all=True)}
    after_networks = {n.id for n in docker_client.networks.list()}

    assert after_containers - before_containers == set()
    assert after_networks - before_networks == set()

    if result.ok and build.image_ref:
        docker_client.images.get(build.image_ref).remove(force=True)


def test_worker_claim_to_terminal_state_end_to_end(docker_client, db_session, seeded_ids):
    """The literal acceptance-criteria path: a QUEUED build in the DB is
    claimed by the worker loop, runs through the real sandbox + proxy +
    stub agent, and lands in a terminal state with usage persisted."""
    from app.services.status import BuildStatus
    from app.worker import run_iteration

    project = _make_project(db_session, seeded_ids, name="e2e-project")
    build = Build(project_id=project.id, type="create", status=BuildStatus.QUEUED)
    db_session.add(build)
    db_session.flush()
    from app.models.change_request import ChangeRequest

    db_session.add(
        ChangeRequest(
            project_id=project.id,
            requester_id=project.owner_id,
            nl_request="Build a small to-do list service.",
            build_id=build.id,
        )
    )
    db_session.commit()

    executor = SandboxBuildExecutor(
        docker_client=docker_client,
        git_provider=FakeGitProvider(),
        preview_manager=FakePreviewManager(),
    )
    ran = run_iteration(db_session, executor)

    assert ran is not None
    db_session.refresh(build)
    assert build.status == BuildStatus.SUCCEEDED
    assert build.ended_at is not None
    # Measured by the proxy: the stub engine makes no proxy call, so 0 / 0.0 (not the stub
    # SDK's self-reported 333 tokens / 0.0123 USD, and not NULL, which means "not measured").
    assert build.cost_tokens == 0
    assert build.cost_usd == 0.0
    assert build.diff_ref is not None
    assert build.image_ref is not None

    docker_client.images.get(build.image_ref).remove(force=True)


def test_worker_selects_sandbox_executor_only_when_both_settings_set(monkeypatch):
    from app.worker import _select_executor
    from app.services.build_executor import PlaceholderBuildExecutor

    monkeypatch.setattr(settings, "use_sandbox_executor", False)
    monkeypatch.setattr(settings, "anthropic_api_key", "some-key")
    assert isinstance(_select_executor(), PlaceholderBuildExecutor)

    monkeypatch.setattr(settings, "use_sandbox_executor", True)
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    assert isinstance(_select_executor(), PlaceholderBuildExecutor)

    monkeypatch.setattr(settings, "use_sandbox_executor", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "some-key")
    assert isinstance(_select_executor(), SandboxBuildExecutor)


# --- ADR 0032: what is published is what the agent built ---------------------


def _git(bare, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=bare, check=True, capture_output=True).stdout


def _pushed_sha(bare, build) -> str:
    return _git(bare, "rev-parse", f"refs/heads/forge-build-{build.id}").decode().strip()


def _pushed_paths(bare, sha: str) -> list[str]:
    return [p for p in _git(bare, "ls-tree", "-r", "--name-only", sha).decode().splitlines() if p]


def _pushed_file(bare, sha: str, path: str) -> bytes:
    return _git(bare, "show", f"{sha}:{path}")


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


@pytest.fixture()
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    _init_bare_repo(bare)
    return bare


@pytest.fixture()
def hook_marker(tmp_path, monkeypatch):
    """Where the stub's hostile `.git/hooks/pre-commit` would write if host git
    ever RAN it. The variable is set in this (host) process only; the sandbox
    never sees it, so inside L3 the hook is inert."""
    marker = tmp_path / "hook-ran-on-the-host"
    monkeypatch.setenv("FORGE_STUB_HOOK_MARKER", str(marker))
    return marker


def _remove_image(docker_client, build) -> None:
    if build.image_ref:
        try:
            docker_client.images.get(build.image_ref).remove(force=True)
        except DockerException:
            pass


def _executor(docker_client, bare_repo) -> SandboxBuildExecutor:
    return SandboxBuildExecutor(
        docker_client=docker_client,
        git_provider=LocalBareRepoProvider(bare_repo),
        preview_manager=FakePreviewManager(),
    )


def test_the_pushed_commit_and_the_image_are_the_agent_s_output(
    docker_client, db_session, seeded_ids, bare_repo, hook_marker
):
    """Before ADR 0032 the commit and the image were the staged
    template; the stub's marker and edit were harvested and abandoned."""
    executor = _executor(docker_client, bare_repo)
    project = _make_project(db_session, seeded_ids, name="agent-output-project")
    build = _make_build(db_session, project, "build-agent-output")
    with open(os.path.join(settings.template_skeleton_dir, "app", "app.py"), "rb") as f:
        template_original = _lf(f.read())

    try:
        result = executor.execute(build)
        assert result.ok is True, result.summary
        sha = _pushed_sha(bare_repo, build)
        paths = _pushed_paths(bare_repo, sha)

        # (c1) the stub's marker file and its edit are in the pushed commit.
        assert "app/agent_marker.txt" in paths
        assert _lf(_pushed_file(bare_repo, sha, "app/agent_marker.txt")) == b"AGENT-MARKER written by the stub engine\n"
        edited = _lf(_pushed_file(bare_repo, sha, "app/app.py"))
        assert edited.startswith(template_original)  # it IS the template file...
        assert edited.endswith(b"\n# edited by the stub agent\n")  # ...with the agent's edit
        assert edited != template_original
        # Ordinary agent files are carried, including names that merely resemble
        # the platform's: only the root .forge and any .git are not.
        assert ".forge-decoy.txt" in paths
        assert "vendor/lib/code.py" in paths

        # (c2) no .forge/ entry, none of the platform's channel files, no .git
        # entry from the agent. (Git itself cannot track a path named .git, so that
        # half is a sanity check; the proof that host git never read the agent's
        # .git is (c4) and the mutation recorded in the task file.)
        assert not any(p == ".forge" or p.startswith(".forge/") for p in paths), paths
        assert not any(os.path.basename(p) in ("task_input.json", "agent_runner.py", "result.json") for p in paths), paths
        assert not any(".git" in p.split("/") for p in paths), paths
        assert b"160000" not in _git(bare_repo, "ls-tree", "-r", sha)  # no gitlink either
        # The commit was made in the platform's OWN repository (publish's `git init`
        # + `git config user.name`), not in one the agent supplied.
        assert _git(bare_repo, "log", "-1", "--format=%an <%ae>", sha).decode().strip() == "Forge Bot <forge-bot@forge.local>"

        # (c3) the built image contains the marker.
        assert build.image_ref
        output = docker_client.containers.run(build.image_ref, ["cat", "/app/agent_marker.txt"], remove=True)
        assert output == b"AGENT-MARKER written by the stub engine\n"

        # (c4) the stub's hook never ran on the host.
        assert not hook_marker.exists(), "host git ran a hook that came out of the sandbox"
    finally:
        _remove_image(docker_client, build)


def test_the_stub_s_hook_does_fire_when_host_git_commits_in_a_tree_that_holds_it(tmp_path, hook_marker):
    """The positive control for (c4): the marker mechanism works on THIS host. The
    stub's hook, installed in a real repository, fires on a plain `git commit`
    here, so `not hook_marker.exists()` above is a measurement and not a hook
    that could never have run (no `sh`, a path git ignores, ...)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fake_claude_agent_sdk_for_hook",
        os.path.join(os.path.dirname(__file__), "..", "docker", "agent-sandbox-stub", "fake_claude_agent_sdk.py"),
    )
    stub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stub)

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(stub.HOOK, newline="\n")
    hook.chmod(0o755)
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=n", "commit", "--allow-empty", "-m", "x"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    assert hook_marker.exists()


def test_a_tampered_protected_path_is_reported_as_drift_and_restored_on_a_modify_build(
    docker_client, db_session, seeded_ids, bare_repo
):
    """drift measured on real agent output.

    A modify build is used because its staged workspace is the confirmed ref,
    which already holds the pinned Dockerfile and compose.yaml, so drift can only
    come from the agent. (Previously a create build could not be used: it stages
    only app/, so the two protected files were always 'missing' and always
    reported. Drift is now measured against the build's own input, and
    `test_a_create_build_reports_no_drift_...` below is that measurement.)"""
    executor = _executor(docker_client, bare_repo)
    project = _make_project(db_session, seeded_ids, name="drift-project")
    built: list[Build] = []
    with open(os.path.join(settings.template_skeleton_dir, "Dockerfile"), "rb") as f:
        pinned_dockerfile = _lf(f.read())

    try:
        # The base: a create build whose branch becomes the confirmed ref.
        create = _make_build(db_session, project, "build-drift-base")
        built.append(create)
        assert executor.execute(create).ok is True
        project.confirmed_ref = _pushed_sha(bare_repo, create)
        db_session.commit()

        # Control: a modify build whose agent leaves the protected paths alone.
        clean = _make_build(db_session, project, "build-drift-clean", build_type="modify", nl_request="Tweak it.")
        built.append(clean)
        clean_result = executor.execute(clean)
        assert clean_result.ok is True, clean_result.summary
        assert clean_result.agent_summary is not None
        assert clean.protected_path_drift == []

        # The agent overwrites the Dockerfile.
        tampered = _make_build(
            db_session, project, "build-drift-tampered", build_type="modify", nl_request="Tweak it. [stub:protected]"
        )
        built.append(tampered)
        result = executor.execute(tampered)
        assert result.ok is True, result.summary
        # the drift is a measured fact on the build, no longer a sentence appended to the agent's text.
        assert "Protected-path drift" not in result.agent_summary
        assert tampered.protected_path_drift == ["Dockerfile"]

        sha = _pushed_sha(bare_repo, tampered)
        committed = _lf(_pushed_file(bare_repo, sha, "Dockerfile"))
        assert committed == pinned_dockerfile
        assert b"AGENT TAMPERED" not in committed
        assert "(drift dropped: Dockerfile)" in _git(bare_repo, "log", "-1", "--format=%s", sha).decode()
        # And the agent's real change still landed.
        assert "app/agent_marker.txt" in _pushed_paths(bare_repo, sha)
    finally:
        for b in built:
            _remove_image(docker_client, b)


def test_a_hard_link_in_the_agent_s_output_fails_the_build_and_publishes_nothing(
    docker_client, db_session, seeded_ids, bare_repo
):
    """what reaches the sanitiser from a real harvest. Docker's archive
    stores the second name of a hard link as a link entry; `tarfile` extracts it
    with os.link, so both names arrive with st_nlink == 2 on every platform."""
    executor = _executor(docker_client, bare_repo)
    project = _make_project(db_session, seeded_ids, name="hardlink-project")
    build = _make_build(db_session, project, "build-hardlink", nl_request="Build it. [stub:hardlink]")

    try:
        result = executor.execute(build)

        assert result.ok is False
        assert result.reason == BuildFailureReason.HARVEST_ERROR
        assert result.summary == (
            "the agent's output contains entries that cannot be published "
            "(2 hard link(s)); links and special files are not allowed"
        )
        assert build.diff_ref is None and build.image_ref is None
        assert _branches(bare_repo) == []  # nothing was pushed
    finally:
        _remove_image(docker_client, build)


def test_an_in_tree_symlink_in_the_agent_s_output(docker_client, db_session, seeded_ids, bare_repo):
    """an in-tree symlink survives the runner's `data` filter on Linux and
    must be refused by the sanitiser; on Windows without the symlink privilege
    `tarfile` has already turned it into a copy of its target, so there is no link
    left to refuse. The expectation is therefore per platform, and says so: this
    measures what the host does, it does not assume it."""
    executor = _executor(docker_client, bare_repo)
    project = _make_project(db_session, seeded_ids, name="symlink-project")
    build = _make_build(db_session, project, "build-symlink", nl_request="Build it. [stub:symlink]")

    try:
        result = executor.execute(build)

        if os.name == "nt":
            assert result.ok is True, result.summary
            sha = _pushed_sha(bare_repo, build)
            assert _pushed_file(bare_repo, sha, "app/link.txt") == _pushed_file(bare_repo, sha, "app/agent_marker.txt")
        else:
            assert result.ok is False
            assert result.reason == BuildFailureReason.HARVEST_ERROR
            assert "1 symbolic link(s)" in result.summary
            assert _branches(bare_repo) == []
    finally:
        _remove_image(docker_client, build)


def test_a_symlink_escaping_the_tree_fails_in_the_runner_s_harvest_and_publishes_nothing(
    docker_client, db_session, seeded_ids, bare_repo
):
    """a symlink pointing outside the extraction root is refused by
    `tarfile`'s `data` filter, so the runner's harvest fails first (no directory is
    produced, none is leaked) and the sanitiser is never reached."""
    executor = _executor(docker_client, bare_repo)
    project = _make_project(db_session, seeded_ids, name="escape-project")
    build = _make_build(db_session, project, "build-escape", nl_request="Build it. [stub:escape]")

    result = executor.execute(build)

    assert result.ok is False
    assert result.reason == BuildFailureReason.HARVEST_ERROR
    assert build.diff_ref is None and build.image_ref is None
    assert _branches(bare_repo) == []


def test_a_complete_case_variant_repository_from_the_sandbox_never_reaches_host_git(
    docker_client, db_session, seeded_ids, bare_repo, hook_marker
):
    """Engine-up: the stub writes a COMPLETE repository under `.GIT`
    (plus its default lower-case `.git`) with a hostile `pre-commit` and a config
    naming another author. On the NTFS dev host `.GIT` IS `.git`; this is the shape
    that ran the hook on the host. The host marker is
    absent and the pushed commit is authored by Forge Bot."""
    executor = _executor(docker_client, bare_repo)
    project = _make_project(db_session, seeded_ids, name="caserepo-project")
    build = _make_build(db_session, project, "build-caserepo", nl_request="Build it. [stub:caserepo]")

    try:
        result = executor.execute(build)

        assert result.ok is True, result.summary
        assert not hook_marker.exists(), "host git ran a hook that came out of the sandbox"
        sha = _pushed_sha(bare_repo, build)
        assert _git(bare_repo, "log", "-1", "--format=%an <%ae>", sha).decode().strip() == "Forge Bot <forge-bot@forge.local>"
        paths = _pushed_paths(bare_repo, sha)
        assert "app/agent_marker.txt" in paths
        assert not any(p.split("/")[0].casefold() == ".git" for p in paths), paths
    finally:
        _remove_image(docker_client, build)


# --- ADR 0009 §Amendment 2026-10-05: protected paths by build type -----------


def _confirmed_base(executor, db_session, project, bare_repo, built: list[Build], build_id: str) -> None:
    """A create build whose pushed branch becomes the project's confirmed ref."""
    base = _make_build(db_session, project, build_id)
    built.append(base)
    assert executor.execute(base).ok is True
    project.confirmed_ref = _pushed_sha(bare_repo, base)
    db_session.commit()


def _branches(bare_repo) -> list[str]:
    """The branches publish pushed: every branch but `main`, which the repository starts with."""
    names = [b.strip().lstrip("* ") for b in _git(bare_repo, "branch", "--list").decode().splitlines() if b.strip()]
    return [n for n in names if n != "main"]


def test_a_modify_build_that_edits_a_migration_in_the_sandbox_is_refused_and_nothing_is_pushed(
    docker_client, db_session, seeded_ids, bare_repo
):
    """the stub edits the template's existing migration INSIDE the sandbox;
    the change travels harvest -> sanitise and is caught against the confirmed ref the
    build was staged from. The bare repo holds the base build's branch and no other."""
    executor = _executor(docker_client, bare_repo)
    project = _make_project(db_session, seeded_ids, name="schema-refused-project")
    built: list[Build] = []
    try:
        _confirmed_base(executor, db_session, project, bare_repo, built, "build-schema-base")
        base_branches = _branches(bare_repo)
        assert base_branches == ["forge-build-build-schema-base"]

        refused = _make_build(
            db_session, project, "build-schema-edit", build_type="modify",
            nl_request="Tweak it. [stub:migration-edit]",
        )
        built.append(refused)
        result = executor.execute(refused)

        assert result.ok is False
        assert result.reason == BuildFailureReason.AGENT_ERROR
        assert result.summary == (
            "the agent changed the database schema on a self-modify build "
            "(1 file(s) under app/migrations/, app/alembic.ini); a schema change is not "
            "allowed on a self-modify build and must take the developer path"
        )
        assert "d41f9c7a2b3e" not in result.summary  # no file name
        assert refused.diff_ref is None and refused.image_ref is None
        assert _branches(bare_repo) == base_branches  # no branch for the refused build
    finally:
        for b in built:
            _remove_image(docker_client, b)


def test_a_modify_build_that_leaves_the_schema_alone_is_published(
    docker_client, db_session, seeded_ids, bare_repo
):
    """The control for the test above: same base, same stub, no migration directive."""
    executor = _executor(docker_client, bare_repo)
    project = _make_project(db_session, seeded_ids, name="schema-clean-project")
    built: list[Build] = []
    try:
        _confirmed_base(executor, db_session, project, bare_repo, built, "build-clean-base")
        clean = _make_build(db_session, project, "build-clean-modify", build_type="modify", nl_request="Tweak it.")
        built.append(clean)

        result = executor.execute(clean)

        assert result.ok is True, result.summary
        assert f"forge-build-{clean.id}" in " ".join(_branches(bare_repo))
    finally:
        for b in built:
            _remove_image(docker_client, b)


def test_a_create_build_that_adds_a_migration_publishes_it(docker_client, db_session, seeded_ids, bare_repo):
    """Create builds add forward-only migrations freely (ADR 0023)."""
    executor = _executor(docker_client, bare_repo)
    project = _make_project(db_session, seeded_ids, name="create-migration-project")
    build = _make_build(db_session, project, "build-create-migration", nl_request="Build it. [stub:migration-add]")

    try:
        result = executor.execute(build)

        assert result.ok is True, result.summary
        sha = _pushed_sha(bare_repo, build)
        paths = _pushed_paths(bare_repo, sha)
        assert "app/migrations/versions/e5f6a7b8c9d0_stub_add_note.py" in paths
        assert "app/migrations/versions/d41f9c7a2b3e_create_items_table.py" in paths
        added = _lf(_pushed_file(bare_repo, sha, "app/migrations/versions/e5f6a7b8c9d0_stub_add_note.py"))
        assert b'down_revision = "d41f9c7a2b3e"' in added
    finally:
        _remove_image(docker_client, build)


def test_a_create_build_reports_no_drift_when_the_agent_did_not_touch_the_manifests(
    docker_client, db_session, seeded_ids, bare_repo
):
    """Measured: EVERY create build reported `Dockerfile,
    compose.yaml` as drift, because its input (the template's app/) never contained
    them. Measured against the input, an untouched create build reports none -- and
    still publishes the pinned copies."""
    executor = _executor(docker_client, bare_repo)
    project = _make_project(db_session, seeded_ids, name="create-nodrift-project")
    build = _make_build(db_session, project, "build-create-nodrift")
    pinned = {}
    for name in ("Dockerfile", "compose.yaml"):
        with open(os.path.join(settings.template_skeleton_dir, name), "rb") as f:
            pinned[name] = _lf(f.read())

    try:
        result = executor.execute(build)

        assert result.ok is True, result.summary
        assert build.protected_path_drift == []  # the drift is a measured fact, not a sentence in the summary
        assert "Protected-path drift" not in (result.agent_summary or "")
        sha = _pushed_sha(bare_repo, build)
        assert "(drift dropped" not in _git(bare_repo, "log", "-1", "--format=%s", sha).decode()
        for name, expected in pinned.items():
            assert _lf(_pushed_file(bare_repo, sha, name)) == expected
    finally:
        _remove_image(docker_client, build)


def test_a_create_build_whose_agent_adds_a_dockerfile_reports_that_drift_and_publishes_the_pinned_one(
    docker_client, db_session, seeded_ids, bare_repo
):
    """The other side of (d3), so the test above is a measurement: the same create
    build with the agent writing a Dockerfile DOES report it, and only it."""
    executor = _executor(docker_client, bare_repo)
    project = _make_project(db_session, seeded_ids, name="create-drift-project")
    build = _make_build(db_session, project, "build-create-drift", nl_request="Build it. [stub:protected]")
    with open(os.path.join(settings.template_skeleton_dir, "Dockerfile"), "rb") as f:
        pinned = _lf(f.read())

    try:
        result = executor.execute(build)

        assert result.ok is True, result.summary
        assert build.protected_path_drift == ["Dockerfile"]
        assert "Protected-path drift" not in (result.agent_summary or "")
        sha = _pushed_sha(bare_repo, build)
        assert _lf(_pushed_file(bare_repo, sha, "Dockerfile")) == pinned
        assert "(drift dropped: Dockerfile)" in _git(bare_repo, "log", "-1", "--format=%s", sha).decode()
    finally:
        _remove_image(docker_client, build)


def test_bytecode_the_sandbox_wrote_never_refuses_a_modify_build_nor_reaches_the_commit(
    docker_client, db_session, seeded_ids, bare_repo
):
    """ADR 0032 §Amendment 2026-10-05, engine-up: the stub writes a `cpython-311.pyc` and a
    source-less `evil.pyc` under `app/migrations/`. The build is not refused and neither
    file is in the pushed commit."""
    executor = _executor(docker_client, bare_repo)
    project = _make_project(db_session, seeded_ids, name="bytecode-project")
    built: list[Build] = []
    try:
        _confirmed_base(executor, db_session, project, bare_repo, built, "build-bytecode-base")
        modify = _make_build(
            db_session, project, "build-bytecode-modify", build_type="modify", nl_request="Tweak it. [stub:bytecode]"
        )
        built.append(modify)

        result = executor.execute(modify)

        assert result.ok is True, result.summary
        paths = _pushed_paths(bare_repo, _pushed_sha(bare_repo, modify))
        assert "app/migrations/versions/d41f9c7a2b3e_create_items_table.py" in paths
        assert not [p for p in paths if "__pycache__" in p or p.endswith((".pyc", ".pyo"))], paths
    finally:
        for b in built:
            _remove_image(docker_client, b)


def test_no_compiled_bytecode_reaches_a_create_build_s_first_commit(docker_client, db_session, seeded_ids, bare_repo):
    """The host's template directory may hold `__pycache__` (importing it writes some); the
    create staging leaves it out and the first commit holds none."""
    executor = _executor(docker_client, bare_repo)
    project = _make_project(db_session, seeded_ids, name="first-commit-project")
    build = _make_build(db_session, project, "build-first-commit")

    try:
        assert executor.execute(build).ok is True
        paths = _pushed_paths(bare_repo, _pushed_sha(bare_repo, build))
        assert "app/app.py" in paths
        assert not [p for p in paths if "__pycache__" in p or p.endswith((".pyc", ".pyo"))], paths
    finally:
        _remove_image(docker_client, build)


# --- a route that answers 500 is repaired inside the build ---------------------------------
#
# A test-local template (a stdlib server, no database) whose `GET /flaky` answers with the status in
# `app/flaky_status.txt`. The stub engine writes "500" there on a first attempt and "200" when its
# prompt carries the platform's `<measured-failures>` block (`[stub:flaky-route]`,
# docker/agent-sandbox-stub): the stub is stateless between runs, so the repair prompt's own marker is
# how the two attempts differ. Rebuild the stub image after changing it.

FLAKY_SERVER = '''
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DOC = {"openapi": "3.1.0", "paths": {"/ok": {"get": {}}, "/flaky": {"get": {}}}}


def flaky_status():
    try:
        with open(os.path.join(HERE, "flaky_status.txt")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 200


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/openapi.json":
            self._send(200, json.dumps(DOC))
        elif self.path == "/ok":
            self._send(200, "{}")
        elif self.path == "/flaky":
            self._send(flaky_status(), "{}")
        else:
            self._send(404, "{}")

    def log_message(self, *args):
        pass


def main():
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
'''


def _flaky_skeleton(root: Path) -> Path:
    skeleton = root / "flaky-template"
    (skeleton / "app").mkdir(parents=True)
    (skeleton / "Dockerfile").write_text(
        'FROM python:3.11-slim\nWORKDIR /app\nCOPY app/ /app/\nCMD ["python", "server.py", "server:main"]\n'
    )
    (skeleton / "app" / "server.py").write_text(FLAKY_SERVER.replace("PORT", str(settings.preview_container_port)))
    return skeleton


def test_a_route_that_answers_500_is_repaired_inside_the_build_and_one_attempt_ships(
    docker_client, db_session, seeded_ids, monkeypatch, tmp_path
):
    from datetime import datetime, timezone

    from app.services.preview_manager import PREVIEW_CONTAINER_PREFIX, PreviewManager

    monkeypatch.setattr(settings, "template_skeleton_dir", str(_flaky_skeleton(tmp_path)))
    monkeypatch.setattr(settings, "build_repair_max_attempts", 1)
    provider = FakeGitProvider()
    executor = SandboxBuildExecutor(docker_client=docker_client, git_provider=provider)
    project = _make_project(db_session, seeded_ids, "repair-it-project")
    build = _make_build(db_session, project, "build-repair-it", nl_request="Build it. [stub:flaky-route]")
    build.started_at = datetime.now(timezone.utc)  # the loop measures the wall-clock from here
    db_session.commit()
    first_tag = f"forge-build-{project.id}-{build.id}"
    repaired_tag = first_tag + "-r1"
    before_networks = {n.id for n in docker_client.networks.list()}

    try:
        result = executor.execute(build)

        assert result.ok is True, result.summary
        # The first attempt's smoke check measured /flaky at 500 (that is what started the repair);
        # the shipped attempt's measurement is clean.
        assert build.repair_attempts == 1
        assert build.smoke_checks == {
            "routes": [
                {"path": "/flaky", "status": 200, "outcome": "ok"},
                {"path": "/ok", "status": 200, "outcome": "ok"},
            ],
            "skipped": 0,
            "truncated": False,
        }
        # One branch and PR, from the repaired attempt.
        assert len(provider.pushed) == 1 and len(provider.opened_prs) == 1
        assert build.diff_ref == f"forge-build-{build.id}#1"
        # One live preview, running the repaired image; one image left, the repaired one.
        previews = docker_client.containers.list(all=True, filters={"name": f"{PREVIEW_CONTAINER_PREFIX}-{build.id}"})
        assert len(previews) == 1 and previews[0].status == "running"
        assert build.image_ref == repaired_tag
        assert previews[0].attrs["Config"]["Image"] == repaired_tag
        code, shown = previews[0].exec_run(["cat", "/app/flaky_status.txt"])
        assert code == 0 and shown.decode().strip() == "200"
        docker_client.images.get(repaired_tag)
        with pytest.raises(ImageNotFound):
            docker_client.images.get(first_tag)
        # The sandbox network of the build, shared by both attempts, is gone; only the preview's remains.
        left = [n for n in docker_client.networks.list() if n.id not in before_networks]
        assert all("preview" in n.name for n in left), [n.name for n in left]
    finally:
        PreviewManager(client=docker_client).stop_preview(build.id)
        for tag in (first_tag, repaired_tag):
            try:
                docker_client.images.get(tag).remove(force=True)
            except (ImageNotFound, DockerException):
                pass
