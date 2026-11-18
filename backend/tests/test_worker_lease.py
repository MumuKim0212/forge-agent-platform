"""ADR 0038: the worker lease itself (`services/worker_lease.py`), against a real database
and a controllable clock.

Nothing here waits in real time: the lease takes its clock and its sleep as arguments, so "stale
after 60 s" is `clock.advance(61)` and a worker waiting out a dead holder "sleeps" by advancing the
same clock. The tests that start the heartbeat THREAD use a real clock and a shortened beat. What the
worker does with the lease (refusing at start, re-checking, releasing on SIGTERM) is
`test_worker_lease_worker.py`; two real processes are `test_worker_lease_processes.py`.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.services.worker_lease as lease_module
from app.models.base import Base
from app.models.worker_lease import WorkerLease as WorkerLeaseRow
from app.services.worker_lease import LeaseHeld, LeaseLost, WorkerLease


class Clock:
    """A clock the test moves. Starts at a fixed instant, never reads the wall clock."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def session_factory(tmp_path):
    """A file-backed SQLite database with the schema, and a factory of independent sessions over
    it (a real pool: two sessions are two connections, as the loop and the heartbeat thread are)."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'lease.db').as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False)
    engine.dispose()


def make_lease(session_factory, clock, *, host="host-a", pid=100, on_sleep=None) -> WorkerLease:
    def sleep(seconds: float) -> None:
        clock.advance(seconds)
        if on_sleep is not None:
            on_sleep(seconds)

    return WorkerLease(session_factory, clock=clock, sleep=sleep, host=host, pid=pid)


def row_of(session_factory) -> WorkerLeaseRow | None:
    with session_factory() as db:
        row = db.scalars(select(WorkerLeaseRow)).first()
        if row is not None:
            db.expunge(row)
        return row


def never_sleep(seconds: float):
    raise AssertionError(f"the lease waited ({seconds}s) where it should have acted at once")


# -- taking it ---------------------------------------------------------------------------------


def test_the_first_worker_creates_the_row_and_holds_it(session_factory, clock):
    lease = make_lease(session_factory, clock, host="host-a", pid=4242)

    lease.acquire()

    row = row_of(session_factory)
    assert row.id == "platform"
    assert (row.holder_id, row.holder_host, row.holder_pid) == (lease.holder_id, "host-a", 4242)


def test_a_second_row_cannot_exist(session_factory, clock):
    make_lease(session_factory, clock).acquire()

    with session_factory() as db:
        db.add(
            WorkerLeaseRow(
                id="another", holder_id="x", acquired_at=clock(), heartbeat_at=clock()
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_a_released_lease_is_taken_at_once_without_waiting(session_factory, clock):
    first = make_lease(session_factory, clock, host="host-a")
    first.acquire()
    assert first.release() is True

    second = WorkerLease(session_factory, clock=clock, sleep=never_sleep, host="host-b", pid=7)
    second.acquire()

    assert row_of(session_factory).holder_id == second.holder_id


def test_a_live_lease_makes_the_second_worker_refuse_with_the_holders_host_pid_and_heartbeat_age(
    session_factory, clock
):
    """(a) The holder renews while the newcomer watches: a heartbeat that ADVANCES is a live
    worker. The newcomer writes nothing and raises, naming the holder."""
    holder = make_lease(session_factory, clock, host="build-host-7", pid=4242)
    holder.acquire()

    def holder_beats(seconds: float) -> None:
        clock.advance(4)  # a few seconds into its beat interval...
        assert holder.renew() is True  # ...and the live holder renews

    newcomer = make_lease(session_factory, clock, host="host-b", pid=9, on_sleep=holder_beats)

    with pytest.raises(LeaseHeld) as refused:
        newcomer.acquire()

    message = str(refused.value)
    assert "build-host-7" in message
    assert "4242" in message
    assert "heartbeat" in message and "ago" in message
    assert refused.value.heartbeat_age_seconds == 0.0  # it had just beaten, as the clock reads it
    assert row_of(session_factory).holder_id == holder.holder_id  # nothing was taken


def test_a_lease_with_no_heartbeat_for_the_stale_window_is_taken_over_at_once(session_factory, clock):
    """(b) Stale = 60 s with no heartbeat. No wait, no sleep."""
    dead = make_lease(session_factory, clock, host="host-dead", pid=1)
    dead.acquire()
    clock.advance(61)

    survivor = WorkerLease(session_factory, clock=clock, sleep=never_sleep, host="host-b", pid=2)
    survivor.acquire()

    row = row_of(session_factory)
    assert (row.holder_id, row.holder_host, row.holder_pid) == (survivor.holder_id, "host-b", 2)


def test_a_lease_just_inside_the_stale_window_is_not_taken_over_early(session_factory, clock):
    dead = make_lease(session_factory, clock, host="host-dead")
    dead.acquire()
    clock.advance(59)
    slept: list[float] = []
    survivor = make_lease(session_factory, clock, host="host-b", on_sleep=slept.append)

    survivor.acquire()

    assert slept, "at 59 s the holder is not yet stale: the newcomer must have waited"
    assert clock() - datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc) >= timedelta(seconds=60)


def test_a_held_lease_whose_heartbeat_never_moves_is_waited_out_then_taken(session_factory, clock):
    """(c) A killed worker: fresh when we arrive, never advancing. The newcomer polls, sees the
    same heartbeat, and takes it once the 60 s since that beat have passed -- on the fake clock."""
    started = clock()
    killed = make_lease(session_factory, clock, host="host-dead", pid=1)
    killed.acquire()
    clock.advance(5)  # it died 5 s ago
    slept: list[float] = []
    survivor = make_lease(session_factory, clock, host="host-b", pid=2, on_sleep=slept.append)

    survivor.acquire()

    waited = (clock() - started).total_seconds()
    assert 60 <= waited <= 62, "taken at the stale window, not before and not long after"
    assert sum(slept) == pytest.approx(waited - 5)
    assert row_of(session_factory).holder_id == survivor.holder_id


def test_two_workers_that_find_the_lease_stale_together_cannot_both_win(session_factory, clock):
    """The take-over is one conditional UPDATE; the loser's condition no longer holds."""
    dead = make_lease(session_factory, clock)
    dead.acquire()
    clock.advance(61)
    a = WorkerLease(session_factory, clock=clock, sleep=never_sleep, host="a", pid=1)
    b = make_lease(session_factory, clock, host="b", pid=2)
    a.acquire()  # a wins the stale row

    with pytest.raises(LeaseHeld) as refused:
        # b read nothing yet: it arrives after a's take-over, sees a fresh holder, then a beats
        b._sleep = lambda s: (clock.advance(3), a.renew())[0]  # noqa: SLF001 - drive a live holder
        b.acquire()

    assert refused.value.host == "a"
    assert row_of(session_factory).holder_id == a.holder_id


# -- keeping it --------------------------------------------------------------------------------


def test_require_passes_while_the_lease_is_ours_and_fresh(session_factory, clock):
    lease = make_lease(session_factory, clock)
    lease.acquire()
    clock.advance(30)

    lease.require()  # returns


def test_require_raises_lease_lost_when_another_worker_holds_it(session_factory, clock):
    lease = make_lease(session_factory, clock)
    lease.acquire()
    with session_factory() as db:
        db.execute(update(WorkerLeaseRow).values(holder_id="intruder", holder_host="other", holder_pid=5))
        db.commit()

    with pytest.raises(LeaseLost) as lost:
        lease.require()

    assert lost.value.code == 1
    assert lease.lost is True
    with pytest.raises(LeaseLost):  # and it stays lost, even if the row were ours again
        lease.require()


def test_require_raises_lease_lost_when_our_own_heartbeat_has_gone_stale(session_factory, clock):
    """Nobody has taken it yet, but anybody now may: the worker does not act on it."""
    lease = make_lease(session_factory, clock)
    lease.acquire()
    clock.advance(61)

    with pytest.raises(LeaseLost, match="61s old"):
        lease.require()


def test_lease_lost_is_not_an_exception_so_the_loops_catch_all_cannot_swallow_it():
    """`worker._run_loop_tick` survives every `Exception`; a lost lease must end the process."""
    lost = LeaseLost("x")

    assert not isinstance(lost, Exception)
    assert isinstance(lost, SystemExit) and lost.code == 1


def test_a_database_error_while_checking_is_an_error_not_a_lost_lease(clock):
    def broken():
        raise RuntimeError("database is down")

    lease = WorkerLease(broken, clock=clock, sleep=never_sleep, host="h", pid=1)

    with pytest.raises(RuntimeError, match="database is down"):
        lease.require()
    assert lease.lost is False  # not knowing is not losing: the step is skipped, not the worker


def test_renew_says_false_once_another_worker_holds_the_row(session_factory, clock):
    lease = make_lease(session_factory, clock)
    lease.acquire()
    assert lease.renew() is True
    with session_factory() as db:
        db.execute(update(WorkerLeaseRow).values(holder_id="intruder"))
        db.commit()

    assert lease.renew() is False


def test_the_release_frees_the_row_only_for_the_worker_that_holds_it(session_factory, clock):
    holder = make_lease(session_factory, clock)
    holder.acquire()
    bystander = make_lease(session_factory, clock)

    assert bystander.release() is False  # not its lease: nothing written
    assert row_of(session_factory).holder_id == holder.holder_id
    assert holder.release() is True
    row = row_of(session_factory)
    assert (row.holder_id, row.holder_host, row.holder_pid) == (None, None, None)


def test_every_operation_uses_a_session_of_its_own_and_closes_it(session_factory, clock):
    """The renewal thread and the worker loop never share a session because the lease never holds
    one: each call opens a fresh session from the factory and closes it before returning."""
    opened: list[object] = []
    closed: list[object] = []

    def factory():
        session = session_factory()
        opened.append(session)
        real_close = session.close
        session.close = lambda: (closed.append(session), real_close())[1]
        return session

    lease = WorkerLease(factory, clock=clock, sleep=never_sleep, host="h", pid=1)

    lease.acquire()
    lease.renew()
    lease.require()
    lease.release()

    assert len(opened) >= 4
    assert len({id(s) for s in opened}) == len(opened), "a session was reused between operations"
    assert len(closed) == len(opened), "a session outlived its operation"


# -- the heartbeat thread ----------------------------------------------------------------------


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_the_heartbeat_thread_keeps_renewing_and_release_stops_it_and_frees_the_row(session_factory, monkeypatch):
    monkeypatch.setattr(lease_module, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
    lease = WorkerLease(session_factory, host="h", pid=1)
    lease.acquire()
    first = row_of(session_factory).heartbeat_at
    before = {t.ident for t in threading.enumerate()}

    lease.start_heartbeat()
    beat_thread = next(t for t in threading.enumerate() if t.name == "worker-lease-heartbeat")
    moved = _wait_for(lambda: row_of(session_factory).heartbeat_at != first)
    released = lease.release()

    assert moved, "the thread never renewed the heartbeat"
    assert beat_thread.ident not in before
    assert released is True
    assert not beat_thread.is_alive(), "release must end the heartbeat thread before it frees the row"
    assert row_of(session_factory).holder_id is None


def test_the_heartbeat_thread_marks_the_lease_lost_when_another_worker_takes_it(session_factory, monkeypatch):
    monkeypatch.setattr(lease_module, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
    lease = WorkerLease(session_factory, host="h", pid=1)
    lease.acquire()
    lease.start_heartbeat()
    try:
        with session_factory() as db:
            db.execute(update(WorkerLeaseRow).values(holder_id="intruder"))
            db.commit()

        assert _wait_for(lambda: lease.lost), "the thread never noticed the take-over"
        with pytest.raises(LeaseLost):
            lease.require()
    finally:
        lease.release()


def test_a_failing_renewal_is_a_missed_beat_not_a_lost_lease(session_factory, monkeypatch):
    monkeypatch.setattr(lease_module, "HEARTBEAT_INTERVAL_SECONDS", 0.02)
    lease = WorkerLease(session_factory, host="h", pid=1)
    lease.acquire()
    calls: list[int] = []

    def flaky() -> bool:
        calls.append(1)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(lease, "renew", flaky)
    lease.start_heartbeat()
    try:
        assert _wait_for(lambda: len(calls) >= 3), "the thread gave up after a failed beat"
        assert lease.lost is False
    finally:
        lease.release()
