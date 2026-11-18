"""ADR 0037 §Decision 4-6: what the worker does around the reconciler.

* at start: every CLAIMED build and deployment a dead process left in flight is failed with a
  platform-authored reason, audit-logged, before anything is claimed (`recover_orphaned_rows`);
* the reconciler runs when it is due (`run_reconcile_if_due`), and the timeout sweep destroys a
  timed-out build's objects itself;
* a stop request (SIGTERM) stops the claiming, fails the work in flight through its normal
  teardown and ends `main()` -- the handler is a `SystemExit`, because the loop
  swallows every `Exception`.

No Docker here: the Docker half of each rule is `test_reconciler_integration.py`. The signal
tests raise real signals in this process (`signal.raise_signal`, which runs a Python handler on
Windows too) against the real `worker.main()`; the process-level proof is
`test_worker_sigterm_subprocess.py`.
"""

from __future__ import annotations

import signal
from datetime import datetime, timedelta, timezone

import pytest

import app.worker as worker
from app.config import settings
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentFailureReason, DeploymentStatus
from app.models.project import Project
from app.services import deploy as deploy_module
from app.services.build_executor import ExecutionResult, PlaceholderBuildExecutor
from app.services.build_queue import sweep_timed_out_builds
from app.services.deploy import execute_deployment
from app.services.status import BuildFailureReason, BuildStatus, ProjectStatus
from app.services.worker_shutdown import SHUTDOWN, ShutdownController, WorkerShutdown, stop_signals
from tests.fake_admin_dsn import FAKE_ADMIN_DSN
from tests.test_deploy_service import FakeDeployManager, FakeMergeProvider, _confirm, _make_build


@pytest.fixture(autouse=True)
def _data_services_are_fakes(monkeypatch):
    """The deploy-side tests below run `execute_deployment` against a fake manager; nothing
    here may provision a real database or Redis."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    monkeypatch.setattr(
        "app.services.deploy.service_data.provision_project_data_services",
        lambda project_id, docker_client=None: {
            "DATABASE_URL": f"postgresql://svc_{project_id}:pw@forge-service-postgres:5432/svc_{project_id}",
            "REDIS_URL": f"redis://forge-service-redis-{project_id}:6379/0",
        },
    )
    monkeypatch.setattr(
        "app.services.deploy.service_data.ensure_project_redis",
        lambda project_id, *, docker_client=None, redis_url=None: redis_url
        or f"redis://forge-service-redis-{project_id}:6379/0",
    )


@pytest.fixture()
def project(db_session, seeded_ids):
    row = Project(
        name="recovery-unit",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _other_project(db, seeded_ids, name):
    row = Project(
        name=name,
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db.add(row)
    db.commit()
    return row


def _running_build(db, project, *, started_minutes_ago=0) -> Build:
    now = datetime.now(timezone.utc)
    row = Build(
        project_id=project.id,
        type="create",
        status=BuildStatus.RUNNING,
        started_at=now - timedelta(minutes=started_minutes_ago),
        heartbeat_at=now,
    )
    db.add(row)
    db.commit()
    return row


def _deploying(db, project, build, user_id, *, claimed) -> Deployment:
    row = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=user_id,
        status=DeploymentStatus.DEPLOYING,
        started_at=datetime.now(timezone.utc) if claimed else None,
    )
    db.add(row)
    db.commit()
    return row


def _audits(db, action):
    return db.query(AuditLog).filter(AuditLog.action == action).all()


# -- worker start: orphaned rows -------------------------------------------------


def test_a_running_build_is_failed_at_start_with_the_restart_detail_and_audited(db_session, project):
    build = _running_build(db_session, project)

    builds, deployments = worker.recover_orphaned_rows(db_session)

    db_session.refresh(build)
    assert (builds, deployments) == (1, 0)
    assert build.status == BuildStatus.FAILED
    assert build.failure_reason == "INFRA_ERROR"
    assert build.failure_detail == (
        "the worker restarted while this build was running, so the build was stopped before it "
        "finished; start a new build"
    )
    assert build.ended_at is not None
    (entry,) = _audits(db_session, "build.failed")
    assert entry.target == f"build:{build.id}"
    assert entry.actor_id is None
    assert entry.metadata_json["reason"] == "INFRA_ERROR"
    assert "worker restarted" in entry.metadata_json["detail"]


def test_only_running_builds_are_failed_a_queued_or_finished_one_is_left_alone(db_session, project, seeded_ids):
    queued = Build(project_id=project.id, type="create", status=BuildStatus.QUEUED)
    done = Build(project_id=project.id, type="create", status=BuildStatus.SUCCEEDED)
    db_session.add_all([queued, done])
    db_session.commit()
    elsewhere = _other_project(db_session, seeded_ids, "recovery-other")
    running_there = _running_build(db_session, elsewhere)

    builds, _ = worker.recover_orphaned_rows(db_session)

    for row in (queued, done, running_there):
        db_session.refresh(row)
    assert builds == 1
    assert queued.status == BuildStatus.QUEUED  # the queue belongs to whoever claims it next
    assert done.status == BuildStatus.SUCCEEDED
    assert running_there.status == BuildStatus.FAILED


def test_a_claimed_deploying_deployment_is_failed_at_start_with_the_restart_cause_and_audited(
    db_session, project
):
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    deployment.started_at = datetime.now(timezone.utc)  # a worker claimed it, then died
    db_session.commit()

    _, deployments = worker.recover_orphaned_rows(db_session)

    db_session.refresh(deployment)
    assert deployments == 1
    assert deployment.status == DeploymentStatus.FAILED
    assert deployment.failure_reason == DeploymentFailureReason.INFRA_ERROR
    (entry,) = _audits(db_session, "deployment.failed")
    assert entry.target == f"deployment:{deployment.id}"
    assert entry.actor_id is None
    assert entry.metadata_json["reason"] == "INFRA_ERROR"
    assert entry.metadata_json["cause"] == "worker restarted"


def test_an_unclaimed_deploying_row_is_the_queue_and_survives_the_start_and_is_then_claimed(db_session, project):
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)  # DEPLOYING, started_at NULL: a confirm waiting for a worker

    worker.recover_orphaned_rows(db_session)

    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.DEPLOYING
    claimed = deploy_module.claim_next_deployment(db_session)
    assert claimed is not None and claimed.id == deployment.id


def test_an_active_deployment_is_not_touched_at_start(db_session, project):
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    execute_deployment(db_session, deployment, manager=FakeDeployManager(), git_provider=FakeMergeProvider())
    assert deployment.status == DeploymentStatus.ACTIVE

    assert worker.recover_orphaned_rows(db_session) == (0, 0)

    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.ACTIVE


def test_start_with_nothing_in_flight_does_nothing(db_session, project):
    assert worker.recover_orphaned_rows(db_session) == (0, 0)
    assert _audits(db_session, "build.failed") == []


def test_the_start_up_step_recovers_then_reconciles_and_sets_the_next_due_time(db_session, project, monkeypatch):
    order: list[str] = []
    build = _running_build(db_session, project)

    class SpyReconciler:
        def reconcile(self, db):
            db.refresh(build)
            order.append(f"reconcile (build is {build.status})")
            return type("R", (), {"unavailable": False})()

    monkeypatch.setattr(worker, "SessionLocal", lambda: db_session)
    state: dict = {}

    worker._recover_at_start(SpyReconciler(), state)

    assert order == ["reconcile (build is FAILED)"]  # the row was failed first, so its objects are reaped
    assert state["next_due_at"] > datetime.now(timezone.utc) + timedelta(seconds=settings.reconcile_interval_seconds - 5)


def test_a_failure_in_the_start_up_step_is_logged_and_does_not_stop_the_worker(db_session, monkeypatch, caplog):
    monkeypatch.setattr(worker, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(worker, "recover_orphaned_rows", lambda db: (_ for _ in ()).throw(RuntimeError("db down")))

    with caplog.at_level("ERROR", logger="app.worker"):
        worker._recover_at_start(object(), {})  # returns normally

    assert any("recovery of orphaned rows" in r.getMessage() for r in caplog.records)


# -- the reconciler's schedule ---------------------------------------------------------


class CountingReconciler:
    def __init__(self):
        self.passes = 0
        self.destroyed: list[str] = []

    def reconcile(self, db):
        self.passes += 1
        return type("R", (), {"unavailable": False})()

    def destroy_build_objects(self, db, build_id):
        self.destroyed.append(build_id)


def test_the_reconciler_runs_first_then_only_when_the_interval_has_passed(db_session, monkeypatch):
    monkeypatch.setattr(settings, "reconcile_interval_seconds", 60.0)
    reconciler, state = CountingReconciler(), {}
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert worker.run_reconcile_if_due(db_session, reconciler, state, now=t0) is not None
    assert worker.run_reconcile_if_due(db_session, reconciler, state, now=t0 + timedelta(seconds=59)) is None
    assert worker.run_reconcile_if_due(db_session, reconciler, state, now=t0 + timedelta(seconds=60)) is not None

    assert reconciler.passes == 2


def test_no_reconciler_or_no_state_is_handled(db_session):
    assert worker.run_reconcile_if_due(db_session, None, {}) is None
    reconciler = CountingReconciler()
    worker.run_reconcile_if_due(db_session, reconciler, None)
    worker.run_reconcile_if_due(db_session, reconciler, None)
    assert reconciler.passes == 2  # no state: always due


def test_the_interval_setting_must_be_positive():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, forge_env="dev", reconcile_interval_seconds=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, forge_env="dev", reconcile_interval_seconds=-5)
    assert Settings(_env_file=None, forge_env="dev").reconcile_interval_seconds == 60.0


# -- the timeout sweep -------------------------------------------------------------------


def test_the_timeout_sweep_destroys_the_builds_objects_before_failing_the_row(db_session, project):
    stale = _running_build(db_session, project, started_minutes_ago=90)
    seen: list[tuple] = []

    def destroyer(db, build_id):
        row = db.get(Build, build_id)
        seen.append((build_id, row.status))

    timed_out = sweep_timed_out_builds(db_session, max_duration_minutes=30, destroy_objects=destroyer)

    assert seen == [(stale.id, BuildStatus.RUNNING)]  # destroyed while still RUNNING, then failed
    assert [b.id for b in timed_out] == [stale.id]
    db_session.refresh(stale)
    assert stale.status == BuildStatus.FAILED and stale.failure_reason == BuildFailureReason.TIMEOUT


def test_a_fresh_build_is_not_destroyed_by_the_sweep(db_session, project, seeded_ids):
    fresh = _running_build(db_session, _other_project(db_session, seeded_ids, "fresh"), started_minutes_ago=1)
    seen: list[str] = []

    sweep_timed_out_builds(db_session, max_duration_minutes=30, destroy_objects=lambda db, bid: seen.append(bid))

    assert seen == []
    db_session.refresh(fresh)
    assert fresh.status == BuildStatus.RUNNING


def test_a_destroyer_that_raises_cannot_stop_the_row_being_failed(db_session, project, caplog):
    stale = _running_build(db_session, project, started_minutes_ago=90)

    def broken(db, build_id):
        raise RuntimeError("docker is down")

    with caplog.at_level("ERROR", logger="app.build_queue"):
        sweep_timed_out_builds(db_session, max_duration_minutes=30, destroy_objects=broken)

    db_session.refresh(stale)
    assert stale.status == BuildStatus.FAILED and stale.failure_reason == BuildFailureReason.TIMEOUT
    assert any("destroying build" in r.getMessage() for r in caplog.records)


def test_with_no_destroyer_the_sweep_only_fails_rows_as_before(db_session, project):
    stale = _running_build(db_session, project, started_minutes_ago=90)

    sweep_timed_out_builds(db_session, max_duration_minutes=30)

    db_session.refresh(stale)
    assert stale.status == BuildStatus.FAILED


def test_an_iteration_with_a_reconciler_hands_its_destroyer_to_the_timeout_sweep(db_session, project):
    stale = _running_build(db_session, project, started_minutes_ago=999)
    reconciler = CountingReconciler()
    not_due = {"next_due_at": datetime.now(timezone.utc) + timedelta(days=1)}  # so ONLY the sweep can act

    worker.run_iteration(
        db_session, PlaceholderBuildExecutor(), reconciler=reconciler, reconcile_state=not_due
    )

    assert reconciler.destroyed == [stale.id]
    assert reconciler.passes == 0


def test_an_iteration_runs_the_reconciler_when_it_is_due_and_not_before(db_session):
    reconciler = CountingReconciler()
    state: dict = {}

    worker.run_iteration(db_session, PlaceholderBuildExecutor(), reconciler=reconciler, reconcile_state=state)
    worker.run_iteration(db_session, PlaceholderBuildExecutor(), reconciler=reconciler, reconcile_state=state)

    assert reconciler.passes == 1


def test_an_iteration_without_a_reconciler_touches_no_docker_object(db_session, project):
    stale = _running_build(db_session, project, started_minutes_ago=999)

    worker.run_iteration(db_session, PlaceholderBuildExecutor())  # the older call shape

    db_session.refresh(stale)
    assert stale.status == BuildStatus.FAILED


# -- a stop request ------------------------------------------------------------------------


def test_the_handler_exception_is_a_system_exit_and_not_an_exception():
    """Pinned because the worker loop swallows every `Exception`: a handler that raised
    a plain exception would be logged and ignored, and the worker would carry on."""
    assert issubclass(WorkerShutdown, SystemExit)
    assert not issubclass(WorkerShutdown, Exception)
    assert WorkerShutdown(15).code == 0


def test_the_loop_swallows_an_exception_but_not_a_shutdown(db_session, monkeypatch):
    monkeypatch.setattr(worker, "SessionLocal", lambda: db_session)

    def raises_exception(db, executor, **kw):
        raise RuntimeError("one bad iteration")

    def raises_shutdown(db, executor, **kw):
        raise WorkerShutdown(15)

    monkeypatch.setattr(worker, "run_iteration", raises_exception)
    assert worker._run_loop_tick(PlaceholderBuildExecutor()) is None  # survived

    monkeypatch.setattr(worker, "run_iteration", raises_shutdown)
    with pytest.raises(WorkerShutdown):
        worker._run_loop_tick(PlaceholderBuildExecutor())


def test_a_signal_outside_the_interruptible_region_only_records_the_request():
    controller = ShutdownController()

    controller.handle(signal.SIGTERM)  # no exception: it may be in the middle of a claim or a commit

    assert controller.requested and controller.signum == signal.SIGTERM
    with pytest.raises(WorkerShutdown) as info:
        with controller.interruptible():  # work that starts after the request never starts
            pytest.fail("the body must not run")
    assert info.value.signum == signal.SIGTERM


def test_a_signal_inside_the_interruptible_region_raises_once_and_a_second_one_is_ignored():
    controller = ShutdownController()
    second_signals_that_raised: list[int] = []
    with pytest.raises(WorkerShutdown):
        with controller.interruptible():
            try:
                controller.handle(signal.SIGTERM)  # the first one raises ...
            except WorkerShutdown:
                # ... and the failure teardown it triggers is still inside the interruptible
                # region. A second signal landing in it must not cut that teardown short.
                for later in (signal.SIGTERM, signal.SIGINT):
                    try:
                        controller.handle(later)
                    except WorkerShutdown:
                        second_signals_that_raised.append(later)
                raise
    assert second_signals_that_raised == []
    assert controller.requested and controller.signum == signal.SIGTERM  # the first signal is the one recorded


def test_the_controller_installs_for_the_stop_signals_and_restores_what_it_replaced():
    controller = ShutdownController()
    before = {s: signal.getsignal(s) for s in stop_signals()}

    previous = controller.install()
    try:
        for signum in stop_signals():
            assert signal.getsignal(signum) == controller.handle
    finally:
        controller.restore(previous)

    assert {s: signal.getsignal(s) for s in stop_signals()} == before
    assert signal.SIGTERM in stop_signals() and signal.SIGINT in stop_signals()


class _InterruptingExecutor:
    """An executor that is interrupted mid-build, the way a SIGTERM would."""

    uses_engine = False

    def __init__(self, interrupt):
        self._interrupt = interrupt
        self.calls = 0

    def execute(self, build):
        self.calls += 1
        self._interrupt()
        return ExecutionResult(ok=True)


@pytest.mark.parametrize("raised", [WorkerShutdown(15), KeyboardInterrupt()])
def test_an_interrupted_build_is_failed_with_the_stopped_detail_and_the_interrupt_goes_on(
    db_session, project, raised
):
    build = _running_build(db_session, project)

    def interrupt():
        raise raised

    with pytest.raises(type(raised)):
        worker.run_one_build(db_session, build, _InterruptingExecutor(interrupt))

    db_session.refresh(build)
    assert build.status == BuildStatus.FAILED
    assert build.failure_reason == "INFRA_ERROR"
    assert build.failure_detail == (
        "the worker was asked to stop (a shutdown signal) while this build was running, so the "
        "build was stopped before it finished; start a new build"
    )
    (entry,) = _audits(db_session, "build.failed")
    assert "asked to stop" in entry.metadata_json["detail"]


def test_a_build_claimed_after_a_stop_was_requested_is_failed_and_never_started(db_session, project):
    build = _running_build(db_session, project)
    executor = _InterruptingExecutor(lambda: None)
    SHUTDOWN.handle(signal.SIGTERM)  # the request arrived while the worker was claiming

    with pytest.raises(WorkerShutdown):
        worker.run_one_build(db_session, build, executor)

    assert executor.calls == 0
    db_session.refresh(build)
    assert build.status == BuildStatus.FAILED and "asked to stop" in build.failure_detail


def test_a_failure_while_failing_the_interrupted_build_never_replaces_the_interrupt(db_session, project, monkeypatch):
    build = _running_build(db_session, project)
    monkeypatch.setattr(worker, "mark_failed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))

    def interrupt():
        raise WorkerShutdown(15)

    with pytest.raises(WorkerShutdown):
        worker.run_one_build(db_session, build, _InterruptingExecutor(interrupt))


def test_an_ordinary_executor_crash_is_still_an_infra_error_not_a_stop(db_session, project):
    build = _running_build(db_session, project)

    def crash():
        raise RuntimeError("an ordinary crash")

    result = worker.run_one_build(db_session, build, _InterruptingExecutor(crash))

    assert result.status == BuildStatus.FAILED
    assert "the build executor raised RuntimeError" in result.failure_detail


class _InterruptedManager(FakeDeployManager):
    def __init__(self, raised, **kw):
        super().__init__(**kw)
        self._raised = raised

    def wait_healthy(self, deployment_id, *, timeout_seconds=None):
        raise self._raised


@pytest.mark.parametrize("raised", [WorkerShutdown(15), KeyboardInterrupt()])
def test_an_interrupted_deployment_is_failed_its_runtime_removed_and_the_interrupt_goes_on(
    db_session, project, raised
):
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    manager = _InterruptedManager(raised)

    with pytest.raises(type(raised)):
        execute_deployment(db_session, deployment, manager=manager, git_provider=FakeMergeProvider())

    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.FAILED
    assert deployment.failure_reason == DeploymentFailureReason.INFRA_ERROR
    assert manager.removed == [deployment.id]  # its container and network, through the normal teardown
    (entry,) = _audits(db_session, "deployment.failed")
    assert entry.metadata_json["cause"] == "worker stopped"


def test_a_deployment_that_was_already_active_when_the_interrupt_came_is_left_serving(db_session, project):
    first_build = _make_build(db_session, project, build_id="bld-1", diff_ref="b1#1")
    first = _confirm(db_session, project, first_build)
    manager = FakeDeployManager()
    execute_deployment(db_session, first, manager=manager, git_provider=FakeMergeProvider())
    second_build = _make_build(db_session, project, build_id="bld-2", diff_ref="b2#2")
    second = _confirm(db_session, project, second_build)

    class InterruptedWhileRemovingTheOld(FakeDeployManager):
        def remove_service(self, deployment_id):
            if deployment_id == first.id:
                raise WorkerShutdown(15)
            return super().remove_service(deployment_id)

    manager2 = InterruptedWhileRemovingTheOld()
    with pytest.raises(WorkerShutdown):
        execute_deployment(db_session, second, manager=manager2, git_provider=FakeMergeProvider())

    db_session.refresh(second)
    assert second.status == DeploymentStatus.ACTIVE  # activated and committed: never rewritten to FAILED
    assert second.id not in manager2.removed  # and its container is not torn down under live traffic


def test_a_failure_while_failing_the_interrupted_deployment_never_replaces_the_interrupt(
    db_session, project, monkeypatch
):
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    monkeypatch.setattr(deploy_module, "fail_deployment", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))

    with pytest.raises(WorkerShutdown):
        execute_deployment(
            db_session, deployment, manager=_InterruptedManager(WorkerShutdown(15)), git_provider=FakeMergeProvider()
        )


def test_a_deployment_claimed_after_a_stop_was_requested_is_failed_and_never_started(db_session, project):
    build = _make_build(db_session, project)
    _confirm(db_session, project, build)
    deployment = deploy_module.claim_next_deployment(db_session)
    manager = FakeDeployManager()
    SHUTDOWN.handle(signal.SIGTERM)

    with pytest.raises(WorkerShutdown):
        worker.run_one_deployment(db_session, deployment, deploy_manager=manager, git_provider=FakeMergeProvider())

    assert manager.started == []
    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.FAILED
    (entry,) = _audits(db_session, "deployment.failed")
    assert entry.metadata_json["cause"] == "worker stopped"


def test_an_iteration_that_sees_a_stop_request_claims_nothing(db_session, project, seeded_ids):
    queued = Build(project_id=project.id, type="create", status=BuildStatus.QUEUED)
    db_session.add(queued)
    db_session.commit()
    deploying_project = _other_project(db_session, seeded_ids, "stop-deploy")
    confirmed = _confirm(db_session, deploying_project, _make_build(db_session, deploying_project))
    SHUTDOWN.handle(signal.SIGTERM)

    assert worker.run_iteration(db_session, PlaceholderBuildExecutor()) is None

    db_session.refresh(queued)
    db_session.refresh(confirmed)
    assert queued.status == BuildStatus.QUEUED  # not claimed
    assert confirmed.started_at is None  # nor was the deployment


# -- the real main() ---------------------------------------------------------------------


def _drive_real_main(monkeypatch, db_session, executor, *, sleep):
    """The real `worker.main()` and the real `run_iteration`, over the in-memory database, with
    the collaborators that need a schema check, Docker or a real engine replaced."""
    monkeypatch.setattr(worker, "assert_schema_at_head", lambda engine: None)
    monkeypatch.setattr(worker, "_select_executor", lambda: executor)
    monkeypatch.setattr(worker, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(worker, "_bind_instance_id", lambda: "forge-test-real-main")
    monkeypatch.setattr(worker, "_new_reconciler", lambda: None)
    monkeypatch.setattr(worker, "_recover_at_start", lambda reconciler, state, lease=None: None)
    monkeypatch.setattr(worker.time, "sleep", sleep)
    monkeypatch.setattr(db_session, "close", lambda: None)


def test_a_sigterm_during_a_build_fails_it_and_ends_main_cleanly_with_the_handlers_restored(
    db_session, project, monkeypatch
):
    build = Build(project_id=project.id, type="create", status=BuildStatus.QUEUED)
    db_session.add(build)
    db_session.commit()
    before = {s: signal.getsignal(s) for s in stop_signals()}
    executor = _InterruptingExecutor(lambda: signal.raise_signal(signal.SIGTERM))
    _drive_real_main(monkeypatch, db_session, executor, sleep=lambda s: pytest.fail("must not wait"))

    worker.main()  # returns: the stop ended it, no exception out

    db_session.refresh(build)
    assert executor.calls == 1
    assert build.status == BuildStatus.FAILED
    assert "asked to stop" in build.failure_detail
    assert {s: signal.getsignal(s) for s in stop_signals()} == before


def test_a_sigterm_while_idle_ends_main_without_claiming_again(db_session, project, monkeypatch):
    def sleeping(seconds):
        signal.raise_signal(signal.SIGTERM)  # arrives during the poll sleep
        pytest.fail("the sleep must have been interrupted")

    executor = _InterruptingExecutor(lambda: None)
    _drive_real_main(monkeypatch, db_session, executor, sleep=sleeping)

    worker.main()

    assert executor.calls == 0
    assert SHUTDOWN.requested


def test_a_signal_that_arrives_between_claims_stops_the_loop_before_the_next_claim(
    db_session, project, seeded_ids, monkeypatch
):
    first = Build(project_id=project.id, type="create", status=BuildStatus.QUEUED)
    second_project = _other_project(db_session, seeded_ids, "second")
    second = Build(project_id=second_project.id, type="create", status=BuildStatus.QUEUED)
    db_session.add_all([first, second])
    db_session.commit()
    executor = _InterruptingExecutor(lambda: None)

    # The first build runs to completion; the signal lands right after it, outside any
    # interruptible region (here: in the iteration's own return), so it only sets the request.
    real_run_iteration = worker.run_iteration

    def iteration(db, ex, **kw):
        ran = real_run_iteration(db, ex, **kw)
        signal.raise_signal(signal.SIGTERM)
        return ran

    _drive_real_main(monkeypatch, db_session, executor, sleep=lambda s: None)
    monkeypatch.setattr(worker, "run_iteration", iteration)

    worker.main()

    db_session.refresh(first)
    db_session.refresh(second)
    assert executor.calls == 1  # exactly one build ran
    assert first.status == BuildStatus.SUCCEEDED
    assert second.status == BuildStatus.QUEUED  # and nothing was claimed after the request


def test_main_binds_the_instance_id_then_recovers_then_claims_in_that_order(db_session, monkeypatch):
    order: list[str] = []
    _drive_real_main(monkeypatch, db_session, PlaceholderBuildExecutor(), sleep=lambda s: None)
    monkeypatch.setattr(worker, "_bind_instance_id", lambda: order.append("bind") or "forge-test-order")
    monkeypatch.setattr(worker, "_recover_at_start", lambda reconciler, state, lease=None: order.append("recover"))

    def one_iteration(db, executor, **kwargs):
        order.append("iteration")
        SHUTDOWN.handle(signal.SIGTERM)
        return object()

    monkeypatch.setattr(worker, "run_iteration", one_iteration)

    worker.main()

    assert order == ["bind", "recover", "iteration"]
