"""Against a REAL Postgres (a throwaway `postgres:18-alpine`, `real_service_postgres_admin_dsn`):
the approval lock is `SELECT ... FOR UPDATE` here, which SQLite ignores, and the schema is the one
`alembic upgrade head` builds, as production gets it.

What is made real, and how
--------------------------
Two sessions are two connections to the same database, each in its own thread where one has to
wait for the other. The interleaving is not left to timing:

  1. session A runs `decide` (or a supersede) and does NOT commit, so it holds the request row's
     lock with its work pending;
  2. session B starts its own decision in a thread;
  3. the test watches `pg_stat_activity` from a third connection until B's backend reports
     `wait_event_type = 'Lock'` -- Postgres itself saying B is waiting on a lock -- and fails
     (rather than waits forever) if B finishes first, which is what a missing lock looks like;
  4. only then does A commit; B resumes, re-reads the request, and is refused.

Every wait is bounded (a `lock_timeout` on B's connection and a deadline on each poll), so a
broken lock fails the test instead of hanging the suite. Also here: the data scenario of
migration `d2c8f5a71e39` on Postgres (the datetime and JSON types, the FK enforcement and the
partial index as Postgres renders them differ from SQLite's).

Skips cleanly (not fails) when Docker is unavailable.
"""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("docker")
pytest.importorskip("psycopg")

import docker  # noqa: E402
from alembic import command  # noqa: E402
from docker.errors import DockerException  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models.approval import ApprovalRequest, ApprovalStatus  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.seed import seed  # noqa: E402
from app.services.approval import InvalidDecisionError, decide, supersede_older_pending_requests  # noqa: E402
from tests import approval_decision_migration_support as scenario  # noqa: E402
from tests import migration_parity_support as parity  # noqa: E402
from tests.approval_support import approve, decision_rows, new_request, newer_build, roles  # noqa: E402


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


# -- the migration's data scenario, on Postgres ------------------------------


def test_duplicates_are_removed_audited_and_the_index_enforces_one_approve_per_actor_on_postgres(
    scratch_postgres_url,
):
    scenario.run_data_scenario(scratch_postgres_url)


def test_a_postgres_database_without_duplicates_is_not_touched(scratch_postgres_url):
    scenario.run_clean_database_scenario(scratch_postgres_url)


# -- concurrent deciders ------------------------------------------------------

B_APP = "caseag-session-b"


class _Worker(threading.Thread):
    """Runs `fn(session)` on its own session (its own connection, named so the monitor can find
    it) and records what it returned or raised."""

    def __init__(self, url: str, fn):
        super().__init__(daemon=True)
        self.engine = create_engine(
            url,
            # lock_timeout: a lock that is never released fails this thread after 20 s instead
            # of hanging the suite.
            connect_args={"application_name": B_APP, "options": "-c lock_timeout=20000"},
        )
        self.fn = fn
        self.finished = threading.Event()
        self.result = None
        self.error: BaseException | None = None

    def run(self):
        try:
            with Session(self.engine) as session:
                try:
                    self.result = self.fn(session)
                    session.commit()
                except BaseException as exc:  # noqa: BLE001 - recorded for the test to assert on
                    session.rollback()
                    self.error = exc
        finally:
            self.finished.set()

    def stop(self):
        self.join(timeout=30)
        self.engine.dispose()


class _Pg:
    def __init__(self, url: str):
        self.url = url
        self.engines = []
        self.monitor_engine = create_engine(url)

    def session(self) -> Session:
        engine = create_engine(self.url)
        self.engines.append(engine)
        return Session(engine)

    def wait_until_blocked(self, worker: _Worker, *, seconds: float = 15.0) -> None:
        """Returns once Postgres reports `worker`'s backend waiting on a lock. Fails if the worker
        finished first (it was not held up: no lock), or the deadline passes."""
        deadline = time.monotonic() + seconds
        # AUTOCOMMIT: `pg_stat_activity` is read once per TRANSACTION and cached, so polling it
        # inside one transaction keeps showing the first answer (the worker not yet waiting).
        with self.monitor_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            while time.monotonic() < deadline:
                if worker.finished.is_set():
                    raise AssertionError(
                        f"the second session finished (result={worker.result!r}, error={worker.error!r}) "
                        "while the first still held the request: nothing held it back"
                    )
                waiting = conn.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE application_name = :app AND wait_event_type = 'Lock'"
                    ),
                    {"app": B_APP},
                ).scalar_one()
                if waiting:
                    return
                time.sleep(0.05)
        raise AssertionError("the second session was never seen waiting on a lock")

    def close(self):
        for engine in self.engines:
            engine.dispose()
        self.monitor_engine.dispose()


@pytest.fixture()
def pg(scratch_postgres_url):
    """An empty database brought to head by `alembic upgrade`, with the seeded users."""
    command.upgrade(parity.config(scratch_postgres_url), "head")
    engine = create_engine(scratch_postgres_url)
    with Session(engine) as db:
        seed(db, is_prod=False)
    engine.dispose()
    handle = _Pg(scratch_postgres_url)
    yield handle
    handle.close()


def _final(pg: _Pg, request_id: str):
    with pg.session() as db:
        request = db.get(ApprovalRequest, request_id)
        return request.status, decision_rows(db, request_id), decision_rows(db, request_id, "approve")


def test_two_concurrent_approvals_of_one_request_do_not_both_succeed(pg):
    with pg.session() as a:
        request_id = new_request(a, required=1)
        ids = roles(a)

        approve(a, request_id, ids["approver"])  # holds the row lock, uncommitted
        worker = _Worker(pg.url, lambda b: approve(b, request_id, ids["admin"]))
        worker.start()
        try:
            pg.wait_until_blocked(worker)
            a.commit()
            worker.finished.wait(15)
        finally:
            a.rollback()  # a no-op after the commit; releases the lock if an assertion fired first
            worker.stop()

    assert isinstance(worker.error, InvalidDecisionError) and "already APPROVED" in str(worker.error), worker.error
    assert _final(pg, request_id) == (ApprovalStatus.APPROVED, 1, 1)


@pytest.mark.parametrize("first, second", [("approve", "reject"), ("reject", "approve")])
def test_an_approve_and_a_reject_of_one_request_do_not_both_succeed(pg, first, second):
    with pg.session() as a:
        request_id = new_request(a, required=1)
        ids = roles(a)

        approve(a, request_id, ids["approver"], decision=first, comment="first" if first == "reject" else None)
        worker = _Worker(
            pg.url,
            lambda b: approve(b, request_id, ids["admin"], decision=second, comment="second" if second == "reject" else None),
        )
        worker.start()
        try:
            pg.wait_until_blocked(worker)
            a.commit()
            worker.finished.wait(15)
        finally:
            a.rollback()
            worker.stop()

    terminal = ApprovalStatus.APPROVED if first == "approve" else ApprovalStatus.REJECTED
    assert isinstance(worker.error, InvalidDecisionError) and f"already {terminal}" in str(worker.error), worker.error
    assert _final(pg, request_id) == (terminal, 1, 1 if first == "approve" else 0)


def test_one_actor_approving_in_two_sessions_at_once_cannot_satisfy_a_two_person_request(pg):
    """Two parallel clicks by ONE approver (the case the unique constraint and the lock exist
    for). The second waits for the first, then finds its own earlier approval and is refused."""
    with pg.session() as a:
        request_id = new_request(a, required=2)
        approver = roles(a)["approver"]

        approve(a, request_id, approver)
        worker = _Worker(pg.url, lambda b: approve(b, request_id, approver))
        worker.start()
        try:
            pg.wait_until_blocked(worker)
            a.commit()
            worker.finished.wait(15)
        finally:
            a.rollback()
            worker.stop()

    assert isinstance(worker.error, InvalidDecisionError) and "already approved" in str(worker.error), worker.error
    assert _final(pg, request_id) == (ApprovalStatus.PENDING, 1, 1)


def test_a_supersede_waits_for_an_approval_in_flight_and_does_not_overwrite_it(pg):
    with pg.session() as a:
        request_id = new_request(a, required=1)
        ids = roles(a)
        project_id = a.get(ApprovalRequest, request_id).project_id
        newer = newer_build(a, project_id)

        approve(a, request_id, ids["approver"])
        worker = _Worker(
            pg.url, lambda b: supersede_older_pending_requests(b, project_id=project_id, newer_build_id=newer)
        )
        worker.start()
        try:
            pg.wait_until_blocked(worker)
            a.commit()
            worker.finished.wait(15)
        finally:
            a.rollback()
            worker.stop()

    assert worker.error is None and worker.result == []  # it matched nothing: the request was no longer PENDING
    assert _final(pg, request_id) == (ApprovalStatus.APPROVED, 1, 1)
    with pg.session() as db:
        assert db.get(ApprovalRequest, request_id).superseded_by_build_id is None


def test_an_approval_waits_for_a_supersede_in_flight_and_is_refused(pg):
    with pg.session() as a:
        request_id = new_request(a, required=1)
        ids = roles(a)
        project_id = a.get(ApprovalRequest, request_id).project_id
        newer = newer_build(a, project_id)

        superseded = supersede_older_pending_requests(a, project_id=project_id, newer_build_id=newer)
        assert [r.id for r in superseded] == [request_id]
        worker = _Worker(pg.url, lambda b: approve(b, request_id, ids["approver"]))
        worker.start()
        try:
            pg.wait_until_blocked(worker)
            a.commit()
            worker.finished.wait(15)
        finally:
            a.rollback()
            worker.stop()

    assert isinstance(worker.error, InvalidDecisionError) and "already SUPERSEDED" in str(worker.error), worker.error
    assert _final(pg, request_id) == (ApprovalStatus.SUPERSEDED, 0, 0)


def test_a_request_loaded_before_the_other_decider_committed_is_re_read_under_the_lock(pg):
    with pg.session() as a, pg.session() as b:
        request_id = new_request(a, required=1)
        ids = roles(a)
        stale = b.get(ApprovalRequest, request_id)
        owner = b.get(Project, stale.project_id).owner_id
        assert stale.status == ApprovalStatus.PENDING

        approve(a, request_id, ids["approver"])
        a.commit()
        assert stale.status == ApprovalStatus.PENDING  # still the old value in B's memory

        with pytest.raises(InvalidDecisionError, match="already APPROVED"):
            decide(b, stale, actor_id=ids["admin"], project_owner_id=owner, decision="approve", comment=None)
        b.rollback()

    assert _final(pg, request_id) == (ApprovalStatus.APPROVED, 1, 1)
