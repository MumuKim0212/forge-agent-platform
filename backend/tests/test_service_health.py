"""the worker's active checks of every ACTIVE service.

Every container here is a fake; no Docker is touched. A fake that must hang waits on an event it
gives up on after `HANG_CEILING_SECONDS`, so code without the bound under test fails an
elapsed-time assertion instead of hanging the run. Timeouts are set per test from literals.
"""

from __future__ import annotations

import signal
import threading
import time

import pytest
from docker.errors import APIError, NotFound
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.worker as worker
from app.config import settings
from app.models.base import Base
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.models.service_health_check import ServiceHealthCheck
from app.models.template import Template
from app.models.user import Team, User
from app.seed import seed
from app.services import project_activity, service_health
from app.services.service_health import ServiceCheckRunner, run_service_checks
from app.services.status import BuildStatus, ProjectStatus
from app.services.worker_lease import WorkerLease
from app.services.worker_shutdown import SHUTDOWN
from tests.instance_scope import TEST_INSTANCE_ID

HANG_CEILING_SECONDS = 6.0

# Captured at import, before the suite's autouse fixture replaces it with a stub (conftest).
REAL_START_SERVICE_CHECKS = worker._start_service_checks
OK = (0, b"200\napplication/json\n{}")


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'svc.db').as_posix()}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False)
    with maker() as db:
        seed(db)
    yield maker
    engine.dispose()


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "service_check_failure_threshold", 3)
    monkeypatch.setattr(settings, "service_check_exec_timeout_seconds", 10.0)
    monkeypatch.setattr(settings, "service_check_cycle_timeout_seconds", 120.0)
    monkeypatch.setattr(settings, "service_check_interval_seconds", 60.0)


def _activate(factory, name: str = "svc", status: str = DeploymentStatus.ACTIVE) -> tuple[str, str]:
    """(deployment_id, project_id) of a new deployment in `status`."""
    with factory() as db:
        user = db.scalars(select(User)).first()
        project = Project(
            name=name,
            owner_id=user.id,
            team_id=db.scalars(select(Team)).first().id,
            template_id=db.scalars(select(Template)).first().id,
            status=ProjectStatus.SUBMITTED,
        )
        db.add(project)
        db.flush()
        build = Build(project_id=project.id, type="create", status=BuildStatus.SUCCEEDED)
        db.add(build)
        db.flush()
        deployment = Deployment(
            project_id=project.id, build_id=build.id, confirmed_by_id=user.id, status=status
        )
        db.add(deployment)
        db.commit()
        return deployment.id, project.id


class FakeContainer:
    def __init__(
        self,
        deployment_id,
        *,
        status="running",
        restarts=0,
        instance=TEST_INSTANCE_ID,
        role="deploy",
        exec_result=OK,
        on_exec=None,
    ):
        self.labels = {"forge.instance": instance, "forge.role": role, "forge.deployment_id": deployment_id}
        self.status = status
        self.attrs = {"RestartCount": restarts}
        self.exec_result = exec_result
        self.on_exec = on_exec
        self.exec_calls = 0
        self.reload_error = None

    def reload(self):
        if self.reload_error is not None:
            raise self.reload_error

    def exec_run(self, cmd):
        self.exec_calls += 1
        if self.on_exec is not None:
            return self.on_exec()
        return self.exec_result


class FakeContainers:
    def __init__(self, containers, error=None, on_list=None):
        self.containers, self.error, self.on_list = containers, error, on_list
        self.list_calls: list[dict] = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.on_list is not None:
            self.on_list()
        if self.error is not None:
            raise self.error
        return list(self.containers)

    def get(self, *args, **kwargs):  # the check selects by label, never by name
        raise AssertionError("the service check looked a container up by name")


class FakeClient:
    def __init__(self, containers=(), error=None, on_list=None):
        self.containers = FakeContainers(containers, error, on_list)


def _hang():
    release = threading.Event()

    def _on_exec():
        release.wait(HANG_CEILING_SECONDS)
        return OK

    return release, _on_exec


def _row(factory, deployment_id) -> ServiceHealthCheck | None:
    with factory() as db:
        row = db.get(ServiceHealthCheck, deployment_id)
        if row is not None:
            db.expunge(row)
        return row


def _run(factory, client, **kwargs):
    with factory() as db:
        return run_service_checks(db, client=client, **kwargs)


# -- one failure kind each, each distinguishable in the persisted row -------------------------------


def test_a_passing_service_is_healthy_and_the_first_check_sets_the_restart_baseline(factory):
    dep, _ = _activate(factory)
    assert _run(factory, FakeClient([FakeContainer(dep, restarts=4)])) == 1
    row = _row(factory, dep)
    assert (row.state, row.last_result, row.consecutive_failures) == ("HEALTHY", "PASS", 0)
    assert (row.last_failure_kind, row.failing_since, row.last_restart_count) == (None, None, 4)
    assert row.last_passed_at is not None


def test_a_missing_container_is_container_missing(factory):
    dep, _ = _activate(factory)
    _run(factory, FakeClient([]))
    row = _row(factory, dep)
    assert (row.last_result, row.last_failure_kind, row.consecutive_failures) == ("FAIL", "CONTAINER_MISSING", 1)
    assert row.failing_since is not None


def test_a_container_gone_between_listing_and_inspecting_is_container_missing(factory):
    dep, _ = _activate(factory)
    container = FakeContainer(dep)
    container.reload_error = NotFound("gone")
    _run(factory, FakeClient([container]))
    assert _row(factory, dep).last_failure_kind == "CONTAINER_MISSING"
    assert container.exec_calls == 0


@pytest.mark.parametrize("status", ["exited", "restarting", "created", "paused"])
def test_a_container_that_is_not_running_is_not_running_and_is_not_exec_ed(factory, status):
    dep, _ = _activate(factory)
    container = FakeContainer(dep, status=status)
    _run(factory, FakeClient([container]))
    assert _row(factory, dep).last_failure_kind == "NOT_RUNNING"
    assert container.exec_calls == 0


def test_a_restart_count_that_went_up_is_restarted_once_and_the_next_pass_recovers(factory):
    dep, _ = _activate(factory)
    container = FakeContainer(dep, restarts=2)
    _run(factory, FakeClient([container]))
    container.attrs["RestartCount"] = 3
    _run(factory, FakeClient([container]))
    row = _row(factory, dep)
    assert (row.last_result, row.last_failure_kind, row.last_restart_count) == ("FAIL", "RESTARTED", 3)
    _run(factory, FakeClient([container]))  # still 3: the baseline moved, so this is not a second failure
    row = _row(factory, dep)
    assert (row.last_result, row.consecutive_failures, row.last_failure_kind) == ("PASS", 0, "RESTARTED")


@pytest.mark.parametrize(
    "result",
    [(1, b"URLError"), (0, b"503\n\n"), (0, b""), (0, b"500\n\nboom")],
)
def test_a_healthz_that_does_not_say_200_is_healthz_failed(factory, result):
    dep, _ = _activate(factory)
    _run(factory, FakeClient([FakeContainer(dep, exec_result=result)]))
    assert _row(factory, dep).last_failure_kind == "HEALTHZ_FAILED"


def test_an_exec_that_raises_a_docker_error_is_healthz_failed(factory):
    dep, _ = _activate(factory)

    def boom():
        raise APIError("exec refused")

    _run(factory, FakeClient([FakeContainer(dep, on_exec=boom)]))
    assert _row(factory, dep).last_failure_kind == "HEALTHZ_FAILED"


def test_a_healthz_that_never_returns_is_healthz_timeout_within_the_bound(factory, monkeypatch):
    monkeypatch.setattr(settings, "service_check_exec_timeout_seconds", 0.4)
    dep, _ = _activate(factory)
    release, on_exec = _hang()
    started = time.monotonic()
    try:
        _run(factory, FakeClient([FakeContainer(dep, on_exec=on_exec)]))
        elapsed = time.monotonic() - started
    finally:
        release.set()
    row = _row(factory, dep)
    assert (row.last_result, row.last_failure_kind) == ("FAIL", "HEALTHZ_TIMEOUT")
    assert elapsed < 2.0, f"one hung exec held the pass for {elapsed:.1f}s"


def test_the_five_kinds_are_five_different_stored_values(factory):
    kinds = {
        service_health.FailureKind.CONTAINER_MISSING,
        service_health.FailureKind.NOT_RUNNING,
        service_health.FailureKind.RESTARTED,
        service_health.FailureKind.HEALTHZ_FAILED,
        service_health.FailureKind.HEALTHZ_TIMEOUT,
    }
    assert kinds == {"CONTAINER_MISSING", "NOT_RUNNING", "RESTARTED", "HEALTHZ_FAILED", "HEALTHZ_TIMEOUT"}


# -- threshold, recovery, transitions ---------------------------------------------------------------


def _audit(factory):
    with factory() as db:
        return [(row.action, row.target, row.metadata_json) for row in db.scalars(select(AuditLog).where(AuditLog.action.like("service_health.%")).order_by(AuditLog.ts))]


def test_unhealthy_only_after_n_consecutive_failures_and_healthy_again_after_one_pass(factory):
    dep, project_id = _activate(factory)
    bad = FakeContainer(dep, exec_result=(0, b"503\n\n"))
    good = FakeContainer(dep)
    for expected_failures in (1, 2):
        _run(factory, FakeClient([bad]))
        row = _row(factory, dep)
        assert (row.state, row.consecutive_failures) == ("HEALTHY", expected_failures)
    assert _audit(factory) == []

    _run(factory, FakeClient([bad]))
    row = _row(factory, dep)
    assert (row.state, row.consecutive_failures) == ("UNHEALTHY", 3)
    assert _audit(factory) == [
        (
            "service_health.unhealthy",
            f"project:{project_id}",
            {"deployment_id": dep, "failure_kind": "HEALTHZ_FAILED", "consecutive_failures": 3},
        )
    ]

    _run(factory, FakeClient([bad]))  # a fourth failure is not a second transition
    assert len(_audit(factory)) == 1

    _run(factory, FakeClient([good]))
    row = _row(factory, dep)
    assert (row.state, row.consecutive_failures, row.failing_since) == ("HEALTHY", 0, None)
    assert row.last_failure_kind == "HEALTHZ_FAILED"  # the last failure stays visible
    assert [a[0] for a in _audit(factory)] == ["service_health.unhealthy", "service_health.recovered"]


def test_a_pass_in_the_middle_resets_the_streak(factory):
    dep, _ = _activate(factory)
    bad, good = FakeContainer(dep, exec_result=(1, b"")), FakeContainer(dep)
    for container in (bad, bad, good, bad, bad):
        _run(factory, FakeClient([container]))
    row = _row(factory, dep)
    assert (row.state, row.consecutive_failures) == ("HEALTHY", 2)


def test_failing_since_is_the_start_of_the_streak_not_the_last_check(factory):
    dep, _ = _activate(factory)
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
    bad = FakeContainer(dep, exec_result=(1, b""))
    _run(factory, FakeClient([bad]), now=t0)
    _run(factory, FakeClient([bad]), now=t0 + timedelta(minutes=5))
    row = _row(factory, dep)
    assert row.failing_since.replace(tzinfo=timezone.utc) == t0
    assert row.last_checked_at.replace(tzinfo=timezone.utc) == t0 + timedelta(minutes=5)


def test_state_survives_a_worker_restart_a_restart_is_not_a_recovery(factory):
    dep, _ = _activate(factory)
    bad = FakeContainer(dep, exec_result=(1, b""))
    _run(factory, FakeClient([bad]))
    _run(factory, FakeClient([bad]))
    # A new worker: new runner, new in-flight dict, new sessions -- only the database is shared.
    runner = ServiceCheckRunner(factory, client_factory=lambda: FakeClient([bad]))
    runner.run_cycle()
    row = _row(factory, dep)
    assert (row.state, row.consecutive_failures) == ("UNHEALTHY", 3)


def test_the_restart_baseline_survives_a_worker_restart(factory):
    dep, _ = _activate(factory)
    container = FakeContainer(dep, restarts=1)
    _run(factory, FakeClient([container]))
    container.attrs["RestartCount"] = 2
    ServiceCheckRunner(factory, client_factory=lambda: FakeClient([container])).run_cycle()
    assert _row(factory, dep).last_failure_kind == "RESTARTED"


def test_a_deployment_that_is_no_longer_active_loses_its_row(factory):
    dep, _ = _activate(factory)
    _run(factory, FakeClient([FakeContainer(dep)]))
    with factory() as db:
        db.get(Deployment, dep).status = DeploymentStatus.SUPERSEDED
        db.commit()
    assert _run(factory, FakeClient([FakeContainer(dep)])) == 0
    assert _row(factory, dep) is None


def test_only_active_deployments_are_checked(factory):
    active, _ = _activate(factory, "a")
    stopped, _ = _activate(factory, "b", status=DeploymentStatus.STOPPED)
    _run(factory, FakeClient([FakeContainer(active), FakeContainer(stopped)]))
    assert _row(factory, active) is not None
    assert _row(factory, stopped) is None


# -- selection by labels ----------------------------------------------------------------------------


def test_containers_are_selected_by_this_instance_and_the_deploy_role_and_never_by_name(factory):
    dep, _ = _activate(factory)
    other_instance = FakeContainer(dep, instance="another-installation")
    wrong_role = FakeContainer(dep, role="service-redis")
    client = FakeClient([other_instance, wrong_role])
    _run(factory, client)
    # Neither impostor is "the" container, so the service reads as missing and nothing was exec-ed.
    assert _row(factory, dep).last_failure_kind == "CONTAINER_MISSING"
    assert other_instance.exec_calls == wrong_role.exec_calls == 0
    (call,) = client.containers.list_calls
    assert call["filters"] == {"label": [f"forge.instance={TEST_INSTANCE_ID}", "forge.role=deploy"]}
    assert call["all"] is True


def test_a_container_is_matched_to_its_deployment_by_label_not_by_a_name_that_looks_right(factory):
    dep, _ = _activate(factory)
    right = FakeContainer(dep)
    right.name = f"forge-deploy-{dep}"
    impostor = FakeContainer("some-other-deployment")
    impostor.name = f"forge-deploy-{dep}"
    _run(factory, FakeClient([impostor, right]))
    assert (right.exec_calls, impostor.exec_calls) == (1, 0)


# -- inconclusive checks ----------------------------------------------------------------------------


def test_docker_unreachable_is_unknown_not_a_failure_and_not_a_pass(factory):
    dep, _ = _activate(factory)
    _run(factory, FakeClient([FakeContainer(dep, exec_result=(1, b""))]))  # one real failure first
    _run(factory, FakeClient(error=RuntimeError("daemon down")))
    row = _row(factory, dep)
    assert (row.last_result, row.consecutive_failures, row.state) == ("UNKNOWN", 1, "HEALTHY")
    assert row.last_passed_at is None


def test_a_client_that_cannot_be_built_is_unknown(factory):
    dep, _ = _activate(factory)

    def no_docker():
        raise RuntimeError("no socket")

    with factory() as db:
        run_service_checks(db, client_factory=no_docker)
    assert _row(factory, dep).last_result == "UNKNOWN"


def test_a_docker_error_while_inspecting_is_unknown(factory):
    dep, _ = _activate(factory)
    container = FakeContainer(dep)
    container.reload_error = APIError("500 from the daemon")
    _run(factory, FakeClient([container]))
    row = _row(factory, dep)
    assert (row.last_result, row.consecutive_failures) == ("UNKNOWN", 0)


def test_a_hung_container_list_is_unknown_within_the_bound(factory, monkeypatch):
    monkeypatch.setattr(settings, "service_check_exec_timeout_seconds", 0.4)
    dep, _ = _activate(factory)
    release = threading.Event()
    started = time.monotonic()
    try:
        _run(factory, FakeClient(on_list=lambda: release.wait(HANG_CEILING_SECONDS)))
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert _row(factory, dep).last_result == "UNKNOWN"
    assert elapsed < 2.0, f"a hung list held the pass for {elapsed:.1f}s"


def test_the_pass_never_raises_even_when_the_database_does(factory):
    class BrokenSession:
        def execute(self, *a, **k):
            raise RuntimeError("db gone")

        def rollback(self):
            raise RuntimeError("and the rollback too")

    assert run_service_checks(BrokenSession(), client=FakeClient()) == 0  # type: ignore[arg-type]


# -- bounds -----------------------------------------------------------------------------------------


def test_the_pass_stops_starting_services_once_its_own_bound_is_spent(factory, monkeypatch):
    monkeypatch.setattr(settings, "service_check_exec_timeout_seconds", 0.3)
    monkeypatch.setattr(settings, "service_check_cycle_timeout_seconds", 0.5)
    deps = [_activate(factory, f"s{i}")[0] for i in range(4)]
    release, on_exec = _hang()
    containers = [FakeContainer(d, on_exec=on_exec) for d in deps]
    started = time.monotonic()
    try:
        checked = _run(factory, FakeClient(containers))
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert checked == 2, "the third hung service must not have been started after the bound"
    assert elapsed < 2.5, f"four hung services held the pass for {elapsed:.1f}s"
    assert sum(c.exec_calls for c in containers) == 2


def test_a_service_whose_last_check_is_still_hung_is_not_exec_ed_again(factory, monkeypatch):
    monkeypatch.setattr(settings, "service_check_exec_timeout_seconds", 0.3)
    dep, _ = _activate(factory)
    release, on_exec = _hang()
    container = FakeContainer(dep, on_exec=on_exec)
    in_flight: dict = {}
    try:
        _run(factory, FakeClient([container]), in_flight=in_flight)
        _run(factory, FakeClient([container]), in_flight=in_flight)
        _run(factory, FakeClient([container]), in_flight=in_flight)
    finally:
        release.set()
    assert container.exec_calls == 1, "a hung exec was stacked with another"
    assert _row(factory, dep).consecutive_failures == 3  # still failing each pass, as it is still hung


def test_a_hung_service_does_not_stop_the_next_one_from_being_checked(factory, monkeypatch):
    monkeypatch.setattr(settings, "service_check_exec_timeout_seconds", 0.3)
    hung, _ = _activate(factory, "a-hung")
    fine, _ = _activate(factory, "b-fine")
    release, on_exec = _hang()
    try:
        _run(factory, FakeClient([FakeContainer(hung, on_exec=on_exec), FakeContainer(fine)]))
    finally:
        release.set()
    assert _row(factory, fine).last_result == "PASS"
    assert _row(factory, hung).last_failure_kind == "HEALTHZ_TIMEOUT"


# -- observe only -----------------------------------------------------------------------------------


def test_the_check_is_not_activity_changes_no_status_and_touches_no_container(factory, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the service check recorded activity")

    monkeypatch.setattr(project_activity, "record_traffic_activity", forbidden)
    monkeypatch.setattr(project_activity, "record_activity", forbidden, raising=False)
    dep, project_id = _activate(factory)
    with factory() as db:
        before_active = db.get(Project, project_id).last_active_at
    container = FakeContainer(dep, exec_result=(1, b""))
    for name in ("restart", "stop", "kill", "remove"):
        setattr(container, name, forbidden)
    for _ in range(4):
        _run(factory, FakeClient([container]))
    with factory() as db:
        assert db.get(Project, project_id).last_active_at == before_active
        assert db.get(Deployment, dep).status == DeploymentStatus.ACTIVE
    assert _row(factory, dep).state == "UNHEALTHY"


def test_the_module_calls_no_traefik_or_activity_code():
    import ast
    from pathlib import Path

    source = Path(service_health.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert not {m for m in imported if "traefik" in m or "activity" in m}


# -- not starved by another sweep -------------------------------------------------------------------


def test_the_checks_keep_their_interval_while_a_sweep_in_the_worker_loop_is_slow(factory, monkeypatch):
    """A backup sweep whose DSN timed out held the loop's next step for
    about a minute. Drives the real `_serve`; its loop's one tick blocks for 1.5 s (the slow
    sweep), and the checker, on its own thread at a 0.1 s interval, must run several passes in it."""
    monkeypatch.setattr(settings, "service_check_interval_seconds", 0.1)
    dep, _ = _activate(factory)
    pass_times: list[float] = []
    client = FakeClient([FakeContainer(dep)], on_list=lambda: pass_times.append(time.monotonic()))
    monkeypatch.setattr(service_health, "_default_client", lambda: client)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(worker, "_start_service_checks", REAL_START_SERVICE_CHECKS)
    monkeypatch.setattr(worker, "_log_backup_startup_banner", lambda: None)
    monkeypatch.setattr(worker, "_log_disk_cap_startup_banner", lambda e: None)
    monkeypatch.setattr(worker, "_log_proxy_image_startup_banner", lambda e: None)
    monkeypatch.setattr(worker, "_log_oom_visibility_startup_banner", lambda e: None)
    monkeypatch.setattr(worker, "_select_executor", lambda: object())
    monkeypatch.setattr(worker, "_new_reconciler", lambda: None)
    monkeypatch.setattr(worker, "_recover_at_start", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_start_preview_disk_watch", lambda executor: None)
    window: dict = {}

    def slow_tick(executor, **kwargs):
        window["start"] = time.monotonic()
        time.sleep(1.5)  # the slow sweep, in the thread that runs the loop
        window["end"] = time.monotonic()
        SHUTDOWN.handle(signal.SIGTERM)
        return object()

    monkeypatch.setattr(worker, "_run_loop_tick", slow_tick)

    worker._serve(WorkerLease(factory))  # the real type: a fake's shape once drifted from it

    during = [t for t in pass_times if window["start"] <= t <= window["end"]]
    assert len(during) >= 5, f"only {len(during)} check passes ran during the 1.5 s slow tick"
    assert _row(factory, dep).last_result == "PASS"
    assert [t.name for t in threading.enumerate() if t.name == "forge-service-check"] == []


def test_the_runner_stops_with_the_worker_and_survives_a_failing_pass(factory, monkeypatch):
    monkeypatch.setattr(settings, "service_check_interval_seconds", 0.05)
    dep, _ = _activate(factory)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("docker.from_env failed")
        return FakeClient([FakeContainer(dep)])

    runner = ServiceCheckRunner(factory, client_factory=flaky)
    runner.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (_row(factory, dep) is None or _row(factory, dep).last_result != "PASS"):
        time.sleep(0.02)
    assert runner.alive
    runner.stop()
    assert not runner.alive
    assert _row(factory, dep).last_result == "PASS"  # the first pass was UNKNOWN, a later one recovered it


def test_a_runner_whose_worker_lost_the_lease_runs_no_pass(factory):
    dep, _ = _activate(factory)
    lease = WorkerLease(factory)  # the real type; `lost` is a property on it
    lease._lost.set()
    runner = ServiceCheckRunner(factory, client_factory=lambda: FakeClient([FakeContainer(dep)]), lease=lease)
    assert runner.run_cycle() == 0
    assert _row(factory, dep) is None


def test_a_runner_whose_worker_holds_the_lease_runs_its_pass(factory):
    """The regression: `self._lease.lost()` raised TypeError on a real lease, outside the cycle's
    `try`, and ended the checker thread on its first cycle."""
    dep, _ = _activate(factory)
    lease = WorkerLease(factory)
    assert lease.lost is False
    runner = ServiceCheckRunner(factory, client_factory=lambda: FakeClient([FakeContainer(dep)]), lease=lease)
    assert runner.run_cycle() == 1
    assert _row(factory, dep).last_result == "PASS"


def test_a_lost_lease_is_a_property_on_the_real_type():
    import inspect

    assert isinstance(inspect.getattr_static(WorkerLease, "lost"), property)


def test_a_cycle_that_raises_does_not_end_the_checker(factory, monkeypatch):
    monkeypatch.setattr(settings, "service_check_interval_seconds", 0.05)
    dep, _ = _activate(factory)

    class FlakyLease:
        """Raises on the first read, like a lease read that failed; fine afterwards."""

        reads = 0

        @property
        def lost(self):
            FlakyLease.reads += 1
            if FlakyLease.reads == 1:
                raise RuntimeError("lease read failed")
            return False

    runner = ServiceCheckRunner(factory, client_factory=lambda: FakeClient([FakeContainer(dep)]), lease=FlakyLease())
    runner.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (_row(factory, dep) is None):
            time.sleep(0.02)
        assert runner.alive, "the first failed cycle ended the checker thread"
    finally:
        runner.stop()
    assert _row(factory, dep).last_result == "PASS"
    assert FlakyLease.reads >= 2


def test_the_loop_survives_a_run_cycle_that_raises_out_of_it(factory, monkeypatch):
    monkeypatch.setattr(settings, "service_check_interval_seconds", 0.05)
    runner = ServiceCheckRunner(factory)
    calls = {"n": 0}

    def run_cycle():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("escaped")
        return 0

    monkeypatch.setattr(runner, "run_cycle", run_cycle)
    runner.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and calls["n"] < 3:
            time.sleep(0.02)
        assert runner.alive and calls["n"] >= 3
    finally:
        runner.stop()


# -- rotation and liveness --------------------------------------------------------------------------


def test_a_pass_cut_short_by_its_bound_is_followed_by_one_that_starts_where_it_stopped(factory, monkeypatch):
    monkeypatch.setattr(settings, "service_check_exec_timeout_seconds", 0.3)
    monkeypatch.setattr(settings, "service_check_cycle_timeout_seconds", 0.4)
    deps = sorted(_activate(factory, f"s{i}")[0] for i in range(4))
    release, on_exec = _hang()
    containers = [FakeContainer(d, on_exec=on_exec) for d in deps]
    rotation: dict = {}
    reached_first: set = set()
    reached_after: set = set()
    first = 0
    try:
        # A fresh in-flight dict per pass: the no-stacking shortcut (a still-hung service answers
        # at once) would otherwise let the tail through on its own, and this test is about order.
        # A pass reaches only the services its bound allows (two, a little less on a slow machine),
        # so a few passes are allowed; without rotation the same head is re-checked forever.
        for number in range(4):
            checked = _run(factory, FakeClient(containers), in_flight={}, rotation=rotation)
            reached_after = {d for d in deps if _row(factory, d) is not None}
            if number == 0:
                first, reached_first = checked, set(reached_after)
            if reached_after == set(deps):
                break
    finally:
        release.set()
    assert first < 4 and reached_first == set(deps[:first])
    assert reached_after == set(deps), "the tail never got a check: hung services at the head starved it"


def test_every_pass_records_when_it_started_and_finished_even_with_nothing_to_check(factory):
    from app.models.service_checker_status import ServiceCheckerStatus

    _run(factory, FakeClient())
    with factory() as db:
        row = db.get(ServiceCheckerStatus, "singleton")
    assert row.last_pass_started_at is not None and row.last_pass_finished_at is not None
