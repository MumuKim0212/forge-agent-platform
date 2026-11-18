"""ADR 0038: what the worker does with its lease, and what activation does when the row is
no longer its own.

* (a) a second worker against a live lease refuses before any recovery, reconcile or claim, exits 1
  and says who holds it (the real `worker.main()` and the real `_acquire_worker_lease`);
* (d) a worker that loses the lease stops claiming and exits without reaping -- at each of the
  re-check sites (before the sweeps, before the reconcile pass, before a claim) and through `main()`;
* (e) a stop signal releases the lease, and so does any other way out of `main()`.
(f), `deploy._activate` as a compare-and-set, is `test_deploy_activate_cas.py`.

The lease runs on a real SQLite file (two sessions are two connections, as the loop and the
heartbeat thread are); the fake clock for its stale window is `test_worker_lease.py`'s business.
Nothing here waits in real time, and no Docker is involved.
"""

from __future__ import annotations

import signal

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

import app.worker as worker
from app.models.base import Base
from app.models.build import Build
from app.models.project import Project
from app.models.template import Template
from app.models.user import Team, User
from app.models.worker_lease import WorkerLease as WorkerLeaseRow
from app.seed import seed
from app.services.build_executor import ExecutionResult
from app.services.status import BuildStatus, ProjectStatus
from app.services.worker_lease import LeaseLost, WorkerLease
from app.services.worker_shutdown import SHUTDOWN

# Captured at import, before the suite's autouse fixture replaces it with a stub: the tests below
# drive the REAL acquisition.
REAL_ACQUIRE = worker._acquire_worker_lease


@pytest.fixture()
def lease_db(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'worker.db').as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    with factory() as db:
        seed(db)
    yield factory
    engine.dispose()


def _queue_build(factory, name: str) -> str:
    with factory() as db:
        row = Project(
            name=name,
            owner_id=db.scalars(select(User)).first().id,
            team_id=db.scalars(select(Team)).first().id,
            template_id=db.scalars(select(Template)).first().id,
            status=ProjectStatus.SUBMITTED,
        )
        db.add(row)
        db.commit()
        build = Build(project_id=row.id, type="create", status=BuildStatus.QUEUED)
        db.add(build)
        db.commit()
        return build.id


def _status(factory, build_id: str) -> str:
    with factory() as db:
        return db.get(Build, build_id).status


def _lease_row(factory):
    with factory() as db:
        row = db.scalars(select(WorkerLeaseRow)).first()
        db.expunge(row)
        return row


def _take_over(factory, holder: str = "intruder") -> None:
    """Another worker takes the lease (what a stale take-over writes)."""
    with factory() as db:
        db.execute(update(WorkerLeaseRow).values(holder_id=holder, holder_host="other-host", holder_pid=777))
        db.commit()


class Spy:
    """A reconciler that only counts."""

    def __init__(self) -> None:
        self.calls = 0

    def reconcile(self, db):
        self.calls += 1
        return type("Report", (), {"unavailable": False})()

    def destroy_build_objects(self, *args, **kwargs):
        return None


class Executor:
    uses_engine = False

    def __init__(self, on_call=None) -> None:
        self.calls = 0
        self._on_call = on_call

    def execute(self, build):
        self.calls += 1
        if self._on_call is not None:
            self._on_call(self.calls)
        return ExecutionResult(ok=True)


def _drive_main(monkeypatch, factory, executor, reconciler=None, *, sleep=None):
    """The real `main()` with the REAL lease acquisition and the real `run_iteration` /
    `_recover_at_start`, over the file database. Only what needs a schema check, Docker or a
    network is replaced."""
    monkeypatch.setattr(worker, "assert_schema_at_head", lambda engine: None)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(worker, "_bind_instance_id", lambda: "forge-test-lease-worker")
    monkeypatch.setattr(worker, "_select_executor", lambda: executor)
    monkeypatch.setattr(worker, "_new_reconciler", lambda: reconciler)
    monkeypatch.setattr(worker, "_acquire_worker_lease", REAL_ACQUIRE)
    if sleep is not None:
        real_class = WorkerLease
        monkeypatch.setattr(worker, "WorkerLease", lambda session_factory: real_class(session_factory, sleep=sleep))
    idle_ticks: list[float] = []

    def idle(seconds):
        idle_ticks.append(seconds)
        if len(idle_ticks) > 25:  # a loop that spins with nothing to do is a test bug, not a pass
            raise AssertionError("the worker loop kept ticking; it should have stopped or run out of work")

    monkeypatch.setattr(worker.time, "sleep", idle)


# -- (a) a second worker refuses -----------------------------------------------------------------


def test_a_second_worker_refuses_before_any_recovery_reconcile_or_claim_and_exits_1(
    lease_db, monkeypatch, caplog
):
    queued = _queue_build(lease_db, "refused")
    holder = WorkerLease(lease_db, host="build-host-7", pid=4242)
    holder.acquire()
    touched: list[str] = []
    executor = Executor()
    _drive_main(
        monkeypatch,
        lease_db,
        executor,
        reconciler=Spy(),
        sleep=lambda seconds: holder.renew(),  # the live holder beats while the newcomer watches
    )
    monkeypatch.setattr(worker, "_select_executor", lambda: touched.append("executor") or executor)
    monkeypatch.setattr(worker, "_recover_at_start", lambda *a, **k: touched.append("recover"))
    monkeypatch.setattr(worker, "run_iteration", lambda *a, **k: touched.append("iteration"))

    with caplog.at_level("ERROR", logger="app.worker"):
        with pytest.raises(SystemExit) as exit_info:
            worker.main()

    assert exit_info.value.code == 1
    assert touched == []  # not even the executor and its probes were built
    assert _status(lease_db, queued) == BuildStatus.QUEUED
    assert _lease_row(lease_db).holder_id == holder.holder_id  # nothing taken, nothing released
    (refusal,) = [r.getMessage() for r in caplog.records if "refuses to start" in r.getMessage()]
    assert "build-host-7" in refusal and "4242" in refusal and "heartbeat" in refusal


# -- (d) a worker that loses the lease stops --------------------------------------------------------


def test_a_worker_that_loses_the_lease_between_builds_claims_nothing_more_and_exits_1(lease_db, monkeypatch):
    first = _queue_build(lease_db, "ran-under-the-lease")
    second = _queue_build(lease_db, "never-claimed")
    reconciler = Spy()
    executor = Executor(on_call=lambda n: _take_over(lease_db))  # another worker takes it mid-build
    _drive_main(monkeypatch, lease_db, executor, reconciler)

    with pytest.raises(SystemExit) as exit_info:
        worker.main()

    assert exit_info.value.code == 1
    assert executor.calls == 1
    assert _status(lease_db, first) == BuildStatus.SUCCEEDED  # it was ours when it was claimed
    assert _status(lease_db, second) == BuildStatus.QUEUED  # and nothing was claimed after the loss
    assert reconciler.calls == 1  # the start-up pass only; no reaping once it was lost
    assert _lease_row(lease_db).holder_id == "intruder"  # and `main` did not free a lease that is not its own


def test_the_same_two_builds_both_run_when_the_lease_is_never_lost(lease_db, monkeypatch):
    """The control for the test above: without the take-over, the second build IS claimed."""
    first = _queue_build(lease_db, "one")
    second = _queue_build(lease_db, "two")

    def stop_after_second(n):
        if n == 2:
            signal.raise_signal(signal.SIGTERM)

    executor = Executor(on_call=stop_after_second)
    _drive_main(monkeypatch, lease_db, executor, Spy())

    worker.main()

    assert executor.calls == 2
    assert _status(lease_db, first) == BuildStatus.SUCCEEDED


class LosesOnCall(WorkerLease):
    """A real lease that is taken over by another worker at its Nth `require()`."""

    def __init__(self, factory, lose_on_call: int) -> None:
        super().__init__(factory, host="h", pid=1)
        self._factory = factory
        self.lose_on_call = lose_on_call
        self.required = 0

    def require(self) -> None:
        self.required += 1
        if self.required == self.lose_on_call:
            _take_over(self._factory)
        super().require()


def test_a_lease_lost_just_before_the_reconcile_pass_stops_the_tick_before_it_reaps_or_claims(lease_db):
    """The reconcile pass is the third check of a tick (before the build sweep, before the
    stale-deployment sweep, before the pass): lost there, the pass does not run and nothing after it
    does either."""
    queued = _queue_build(lease_db, "unclaimed")
    lease = LosesOnCall(lease_db, lose_on_call=3)
    lease.acquire()
    reconciler = Spy()

    with lease_db() as db:
        with pytest.raises(LeaseLost):
            worker.run_iteration(db, Executor(), reconciler=reconciler, reconcile_state={}, lease=lease)

    assert lease.required == 3
    assert reconciler.calls == 0
    assert _status(lease_db, queued) == BuildStatus.QUEUED


def test_run_reconcile_if_due_does_not_reconcile_without_the_lease(lease_db):
    lease = WorkerLease(lease_db, host="h", pid=1)
    lease.acquire()
    _take_over(lease_db)
    reconciler = Spy()

    with lease_db() as db:
        with pytest.raises(LeaseLost):
            worker.run_reconcile_if_due(db, reconciler, {}, lease=lease)

    assert reconciler.calls == 0


def test_run_reconcile_if_due_with_the_lease_reconciles(lease_db):
    lease = WorkerLease(lease_db, host="h", pid=1)
    lease.acquire()
    reconciler = Spy()

    with lease_db() as db:
        worker.run_reconcile_if_due(db, reconciler, {}, lease=lease)

    assert reconciler.calls == 1


def test_a_lease_lost_before_the_claim_claims_nothing(lease_db):
    """With no reconciler the checks are before the build sweep, before the stale-deployment sweep
    and before the claim: the third is the claim's."""
    queued = _queue_build(lease_db, "unclaimed")
    lease = LosesOnCall(lease_db, lose_on_call=3)
    lease.acquire()

    with lease_db() as db:
        with pytest.raises(LeaseLost):
            worker.run_iteration(db, Executor(), lease=lease)

    assert _status(lease_db, queued) == BuildStatus.QUEUED


def test_a_lease_lost_before_the_timeout_sweeps_sweeps_nothing(lease_db):
    """The first check of a tick is before the timeout sweep of builds: lost there, a RUNNING build
    that is long past its limit is not failed by this worker."""
    from datetime import datetime, timedelta, timezone

    build_id = _queue_build(lease_db, "overdue")
    with lease_db() as db:
        build = db.get(Build, build_id)
        build.status = BuildStatus.RUNNING
        build.started_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.commit()
    lease = LosesOnCall(lease_db, lose_on_call=1)
    lease.acquire()

    with lease_db() as db:
        with pytest.raises(LeaseLost):
            worker.run_iteration(db, Executor(), lease=lease)

    assert _status(lease_db, build_id) == BuildStatus.RUNNING


def test_start_up_recovery_does_not_run_without_the_lease(lease_db, monkeypatch):
    lease = WorkerLease(lease_db, host="h", pid=1)
    lease.acquire()
    _take_over(lease_db)
    monkeypatch.setattr(worker, "SessionLocal", lease_db)
    recovered: list[int] = []
    monkeypatch.setattr(worker, "recover_orphaned_rows", lambda db: recovered.append(1) or (0, 0))
    reconciler = Spy()

    with pytest.raises(LeaseLost):  # a SystemExit: the "recovery failed, carry on" handler does not catch it
        worker._recover_at_start(reconciler, {}, lease)

    assert recovered == []
    assert reconciler.calls == 0


# -- (e) the lease is released ----------------------------------------------------------------------


def _heartbeat_threads():
    import threading

    return [t for t in threading.enumerate() if t.name == "worker-lease-heartbeat" and t.is_alive()]


def test_a_stop_signal_releases_the_lease_and_ends_its_heartbeat(lease_db, monkeypatch):
    _queue_build(lease_db, "stopped")
    executor = Executor(on_call=lambda n: signal.raise_signal(signal.SIGTERM))
    _drive_main(monkeypatch, lease_db, executor, Spy())

    worker.main()  # a requested stop: returns, exit status 0

    assert SHUTDOWN.requested
    row = _lease_row(lease_db)
    assert (row.holder_id, row.holder_host, row.holder_pid) == (None, None, None)
    assert _heartbeat_threads() == []


def test_the_lease_is_held_while_the_worker_runs(lease_db, monkeypatch):
    _queue_build(lease_db, "running")
    seen: list = []

    def look(n):
        seen.append(_lease_row(lease_db))
        assert len(_heartbeat_threads()) == 1
        signal.raise_signal(signal.SIGTERM)

    _drive_main(monkeypatch, lease_db, Executor(on_call=look), Spy())

    worker.main()

    assert seen[0].holder_id is not None and seen[0].holder_pid is not None


class _StopLoop(BaseException):
    pass


def test_any_other_way_out_of_main_releases_the_lease_too(lease_db, monkeypatch):
    executor = Executor()
    _drive_main(monkeypatch, lease_db, executor, Spy())
    monkeypatch.setattr(worker, "run_iteration", lambda *a, **k: (_ for _ in ()).throw(_StopLoop()))

    with pytest.raises(_StopLoop):
        worker.main()

    assert _lease_row(lease_db).holder_id is None
    assert _heartbeat_threads() == []
