"""ADR 0038 §Decision 1: the lease's conditional `UPDATE`s on a REAL Postgres (a throwaway
`postgres:18-alpine`, `scratch_postgres_url`), over the schema `alembic upgrade head` builds.

The ADR claims it "works the same on SQLite and Postgres". Postgres differs where it matters here:
`timestamptz` comes back timezone-aware, a failed insert aborts the transaction, and the row count of
an `UPDATE` is what says who won. The same four behaviours as `test_worker_lease.py` (take, refuse a
live holder, take over a stale one, release), with a controllable clock and no real waits.

Skips cleanly (not fails) when Docker is unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("docker")
pytest.importorskip("psycopg")

import docker  # noqa: E402
from alembic import command  # noqa: E402
from docker.errors import DockerException  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.models.worker_lease import WorkerLease as WorkerLeaseRow  # noqa: E402
from app.services.worker_lease import LeaseHeld, LeaseLost, WorkerLease  # noqa: E402
from tests import migration_parity_support as parity  # noqa: E402


def _docker_available() -> bool:
    try:
        docker.from_env().ping()
        return True
    except DockerException:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_available(), reason="Docker Engine unavailable"),
]


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture()
def factory(scratch_postgres_url):
    command.upgrade(parity.config(scratch_postgres_url), "head")
    engine = create_engine(scratch_postgres_url)
    yield sessionmaker(bind=engine, autoflush=False)
    engine.dispose()


def _row(factory):
    with factory() as db:
        row = db.scalars(select(WorkerLeaseRow)).first()
        db.expunge(row)
        return row


def _never_sleep(seconds):
    raise AssertionError("waited where it should have acted at once")


def test_take_release_and_retake_on_postgres(factory):
    clock = _Clock()
    first = WorkerLease(factory, clock=clock, sleep=_never_sleep, host="host-a", pid=11)

    first.acquire()
    taken = _row(factory)
    assert (taken.holder_id, taken.holder_host, taken.holder_pid) == (first.holder_id, "host-a", 11)
    assert first.renew() is True
    first.require()
    assert first.release() is True
    assert _row(factory).holder_id is None

    second = WorkerLease(factory, clock=clock, sleep=_never_sleep, host="host-b", pid=22)
    second.acquire()
    assert _row(factory).holder_id == second.holder_id
    assert first.renew() is False  # the row is no longer first's


def test_a_live_holder_is_refused_and_a_stale_one_is_taken_over_on_postgres(factory):
    clock = _Clock()
    holder = WorkerLease(factory, clock=clock, sleep=lambda s: None, host="host-a", pid=11)
    holder.acquire()

    def holder_beats(seconds):
        clock.advance(4)
        assert holder.renew() is True

    newcomer = WorkerLease(factory, clock=clock, sleep=holder_beats, host="host-b", pid=22)
    with pytest.raises(LeaseHeld) as refused:
        newcomer.acquire()
    assert (refused.value.host, refused.value.pid) == ("host-a", 11)

    clock.advance(61)  # the holder has gone silent
    survivor = WorkerLease(factory, clock=clock, sleep=_never_sleep, host="host-c", pid=33)
    survivor.acquire()
    assert _row(factory).holder_id == survivor.holder_id
    with pytest.raises(LeaseLost):  # and the old holder, asked, finds it is not its own any more
        holder.require()


def test_a_second_row_cannot_exist_on_postgres(factory):
    from sqlalchemy.exc import IntegrityError

    clock = _Clock()
    WorkerLease(factory, clock=clock, host="h", pid=1).acquire()
    with factory() as db:
        db.add(WorkerLeaseRow(id="another", acquired_at=clock(), heartbeat_at=clock()))
        with pytest.raises(IntegrityError):
            db.commit()
