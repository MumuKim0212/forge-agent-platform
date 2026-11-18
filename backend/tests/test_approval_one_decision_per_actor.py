"""An approval count is a
count of DISTINCT approvers, and the database enforces it.

Three layers, each tested on its own so that removing one leaves a red test naming it:

  1. the constraint -- `uq_approval_decisions_one_approve_per_actor`, a partial unique index on
     `approval_decisions (approval_request_id, actor_id) WHERE decision = 'approve'`;
  2. the tally -- `decide` counts distinct actors, not rows;
  3. the lock -- `decide` takes the request row's lock and re-reads the request under it, so
     approve, reject and the worker's supersede cannot overwrite one another.

The in-memory `db_session` fixture covers 1 and 2. 3 needs two real connections, so those tests
use a file-backed SQLite database (WAL, as `app/db.py` configures it) and two sessions, with the
interleaving forced at a known point INSIDE `decide` (a hook on the step that follows the lock)
instead of hoped for by timing. The same interleavings against a real Postgres, where the lock is
`SELECT ... FOR UPDATE`, are `tests/test_approval_postgres.py`'s.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from app.models.approval import ApprovalDecision, ApprovalRequest, ApprovalStatus
from app.models.base import Base
from app.models.project import Project
from app.seed import seed
from app.services import approval
from app.services.approval import InvalidDecisionError, decide, supersede_older_pending_requests
from tests.approval_support import approve, decision_rows, new_request, newer_build, roles

INDEX = "uq_approval_decisions_one_approve_per_actor"


# ---------------------------------------------------------------------------
# 1. The constraint
# ---------------------------------------------------------------------------


def test_the_database_refuses_a_second_approve_row_by_one_actor(db_session):
    request_id = new_request(db_session, required=2)
    approver = roles(db_session)["approver"]
    db_session.add(ApprovalDecision(approval_request_id=request_id, actor_id=approver, decision="approve"))
    db_session.commit()

    db_session.add(ApprovalDecision(approval_request_id=request_id, actor_id=approver, decision="approve"))
    with pytest.raises(IntegrityError) as raised:
        db_session.flush()
    db_session.rollback()

    assert "approval_decisions" in str(raised.value)
    assert decision_rows(db_session, request_id, "approve") == 1


def test_the_constraint_is_per_actor_per_request_and_for_approvals_only(db_session):
    """What it must NOT forbid: another actor's approval of the same request, the same actor's
    approval of another request, and an approver rejecting after approving (the console offers
    that, and `REJECTED` is terminal, so it cannot repeat)."""
    first, second = new_request(db_session, required=2), new_request(db_session, required=2)
    ids = roles(db_session)

    db_session.add(ApprovalDecision(approval_request_id=first, actor_id=ids["approver"], decision="approve"))
    db_session.add(ApprovalDecision(approval_request_id=first, actor_id=ids["admin"], decision="approve"))
    db_session.add(ApprovalDecision(approval_request_id=second, actor_id=ids["approver"], decision="approve"))
    db_session.add(
        ApprovalDecision(approval_request_id=first, actor_id=ids["approver"], decision="reject", comment="no")
    )
    db_session.commit()  # none of these is refused

    assert decision_rows(db_session, first) == 3
    assert decision_rows(db_session, second) == 1


def test_the_index_is_a_partial_unique_index_in_the_models_schema(db_session):
    """Pins the shape the migration must also produce (`test_migrations_parity.py` compares the
    two): unique, on exactly these columns, limited to approvals."""
    rows = db_session.execute(text("SELECT sql FROM sqlite_master WHERE name = :n"), {"n": INDEX}).all()

    assert len(rows) == 1, f"{INDEX} is not in the schema the models build"
    ddl = " ".join(rows[0][0].split())
    assert "UNIQUE INDEX" in ddl
    assert "(approval_request_id, actor_id)" in ddl
    assert "WHERE decision = 'approve'" in ddl


# ---------------------------------------------------------------------------
# 2. The tally
# ---------------------------------------------------------------------------


def test_one_actor_approving_twice_in_a_row_leaves_a_two_person_request_pending(db_session):
    request_id = new_request(db_session, required=2)
    approver = roles(db_session)["approver"]

    approve(db_session, request_id, approver)
    db_session.commit()
    with pytest.raises(InvalidDecisionError, match="already approved"):
        approve(db_session, request_id, approver)
    db_session.commit()

    request = db_session.get(ApprovalRequest, request_id)
    assert request.status == ApprovalStatus.PENDING
    assert decision_rows(db_session, request_id, "approve") == 1


def test_two_distinct_actors_satisfy_a_two_person_request(db_session):
    request_id = new_request(db_session, required=2)
    ids = roles(db_session)

    approve(db_session, request_id, ids["approver"])
    db_session.commit()
    assert db_session.get(ApprovalRequest, request_id).status == ApprovalStatus.PENDING

    approve(db_session, request_id, ids["admin"])
    db_session.commit()

    assert db_session.get(ApprovalRequest, request_id).status == ApprovalStatus.APPROVED


def test_the_tally_counts_distinct_actors_not_rows_even_without_the_constraint(db_session):
    """The tally must stand on its own, because it is what runs against a database where the
    constraint is missing (the window before the migration; a restore from a backup that
    predates it). Duplicate rows are planted with the index dropped: the same actor holds two
    approve rows on a request that needs three. One further, distinct approver brings the
    distinct count to two, not three, so the request must stay PENDING; counting rows would
    reach three and approve it."""
    request_id = new_request(db_session, required=3)
    ids = roles(db_session)
    db_session.execute(text(f"DROP INDEX {INDEX}"))
    for _ in range(2):
        db_session.add(ApprovalDecision(approval_request_id=request_id, actor_id=ids["approver"], decision="approve"))
    db_session.commit()
    assert decision_rows(db_session, request_id, "approve") == 2  # the planted duplicates

    approve(db_session, request_id, ids["admin"])
    db_session.commit()

    assert decision_rows(db_session, request_id, "approve") == 3
    assert db_session.get(ApprovalRequest, request_id).status == ApprovalStatus.PENDING


# ---------------------------------------------------------------------------
# 3. The lock (two real connections: a file-backed SQLite database)
# ---------------------------------------------------------------------------


@pytest.fixture()
def sessions(tmp_path):
    """A factory of independent sessions on one file-backed SQLite database, each its own
    connection with a 0.3 s busy timeout (a blocked writer fails fast instead of waiting 5 s).
    WAL and foreign keys on, as `app/db.py` sets them. The schema comes from the models and the
    seeded users from `seed`; the first session is closed after setup."""
    url = f"sqlite:///{(tmp_path / 'race.db').as_posix()}"
    engines = []
    open_sessions = []

    def make():
        engine = create_engine(url, connect_args={"timeout": 0.3, "check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        engines.append(engine)
        session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
        open_sessions.append(session)
        return session

    setup = make()
    Base.metadata.create_all(setup.get_bind())
    seed(setup)
    setup.close()
    yield make
    for session in open_sessions:
        session.close()
    for engine in engines:
        engine.dispose()


def _in_flight(monkeypatch, during):
    """Runs `during()` ONCE, at the moment the first `decide` call has taken its lock, has asked
    whether this actor already approved, and is about to insert -- the window a read-then-insert
    check leaves open. The first call's answer is computed BEFORE `during()` runs, so it is the
    answer the race would have given (not yet aware of anything `during()` commits). Calls made
    from inside `during()` (a second `decide`) pass straight through to the real check."""
    real = approval._has_approved
    fired = []

    def hooked(db, *, request_id, actor_id):
        answer = real(db, request_id=request_id, actor_id=actor_id)
        if not fired:
            fired.append(True)
            during()
        return answer

    monkeypatch.setattr(approval, "_has_approved", hooked)
    return fired


def test_a_second_decider_cannot_pass_while_the_first_holds_the_request(sessions, monkeypatch):
    """On SQLite the lock is the database's one writer: `decide` makes a no-op write first, so
    a second decider fails at its lock step instead of reading a PENDING status that is about
    to be out of date. With the first still in flight, the second is refused; once the first
    has committed, the second is refused for a different reason -- the request is already
    APPROVED -- and its decision row never exists. Both succeeding is the lost update."""
    a, b = sessions(), sessions()
    request_id = new_request(a, required=1)
    ids = roles(a)
    blocked: list[Exception] = []

    def second_decider_while_first_is_in_flight():
        try:
            approve(b, request_id, ids["admin"])
        except OperationalError as exc:
            blocked.append(exc)
            b.rollback()

    fired = _in_flight(monkeypatch, second_decider_while_first_is_in_flight)

    approve(a, request_id, ids["approver"])
    a.commit()

    assert fired, "the hook never ran: the test did not reach the window it names"
    assert len(blocked) == 1 and "locked" in str(blocked[0]), "the second decider was not held out"
    with pytest.raises(InvalidDecisionError, match="already APPROVED"):
        approve(b, request_id, ids["admin"])
    b.rollback()
    verify = sessions()
    assert decision_rows(verify, request_id) == 1
    assert verify.get(ApprovalRequest, request_id).status == ApprovalStatus.APPROVED


def test_a_reject_that_lost_to_an_approve_is_refused_not_applied_on_top(sessions, monkeypatch):
    a, b = sessions(), sessions()
    request_id = new_request(a, required=1)
    ids = roles(a)

    approve(a, request_id, ids["approver"])
    a.commit()

    with pytest.raises(InvalidDecisionError, match="already APPROVED"):
        approve(b, request_id, ids["admin"], decision="reject", comment="too late")
    b.rollback()

    verify = sessions()
    assert decision_rows(verify, request_id, "reject") == 0
    assert verify.get(ApprovalRequest, request_id).status == ApprovalStatus.APPROVED


def test_a_request_object_loaded_before_the_other_decider_committed_is_re_read_under_the_lock(sessions):
    """The router loads the request, then calls `decide`; anything that committed in between is
    invisible to the object in memory. The lock step must therefore re-read the row, or it
    only waits for a writer and then acts on a status it no longer is."""
    a, b = sessions(), sessions()
    request_id = new_request(a, required=1)
    ids = roles(a)
    stale = b.get(ApprovalRequest, request_id)
    assert stale.status == ApprovalStatus.PENDING

    approve(a, request_id, ids["approver"])
    a.commit()
    assert stale.status == ApprovalStatus.PENDING  # still the old value in B's memory

    with pytest.raises(InvalidDecisionError, match="already APPROVED"):
        decide(
            b, stale, actor_id=ids["admin"], project_owner_id=ids["owner"], decision="approve", comment=None
        )
    b.rollback()
    assert decision_rows(sessions(), request_id) == 1


def test_a_supersede_cannot_overwrite_an_approval_that_holds_the_request(sessions, monkeypatch):
    """The worker's supersede is a write too. While a decider holds the request it cannot
    start; and once the approval has committed, it matches nothing, because it supersedes
    only requests that are still PENDING at the moment of its own update."""
    a, b = sessions(), sessions()
    request_id = new_request(a, required=1)
    ids = roles(a)
    project_id = a.get(ApprovalRequest, request_id).project_id
    newer = newer_build(a, project_id)
    blocked: list[Exception] = []

    def supersede_while_the_approval_is_in_flight():
        try:
            supersede_older_pending_requests(b, project_id=project_id, newer_build_id=newer)
        except OperationalError as exc:
            blocked.append(exc)
            b.rollback()

    fired = _in_flight(monkeypatch, supersede_while_the_approval_is_in_flight)

    approve(a, request_id, ids["approver"])
    a.commit()

    assert fired and len(blocked) == 1, "the supersede was not held out by the approval in flight"
    superseded = supersede_older_pending_requests(b, project_id=project_id, newer_build_id=newer)
    b.commit()
    assert superseded == []
    verify = sessions()
    request = verify.get(ApprovalRequest, request_id)
    assert request.status == ApprovalStatus.APPROVED
    assert request.superseded_by_build_id is None


def test_a_decision_after_a_committed_supersede_is_refused(sessions):
    a, b = sessions(), sessions()
    request_id = new_request(a, required=1)
    ids = roles(a)
    project_id = a.get(ApprovalRequest, request_id).project_id
    newer = newer_build(a, project_id)
    stale = b.get(ApprovalRequest, request_id)

    superseded = supersede_older_pending_requests(a, project_id=project_id, newer_build_id=newer)
    a.commit()
    assert [r.id for r in superseded] == [request_id]

    with pytest.raises(InvalidDecisionError, match="already SUPERSEDED"):
        decide(
            b, stale, actor_id=ids["approver"], project_owner_id=ids["owner"], decision="approve", comment=None
        )
    b.rollback()
    assert decision_rows(sessions(), request_id) == 0


def test_a_simulated_race_is_refused_by_the_constraint_alone(sessions, monkeypatch):
    """The lock is taken out of play and the duplicate check is made to run before the other
    approval commits, as it would have in the race: one actor, two approvals, the second
    committing in the gap. The second insert must be refused by the DATABASE, reported as the
    same 'already approved' (so the API's 400), and the request must stay PENDING with exactly
    one approve row."""
    monkeypatch.setattr(approval, "_lock_request", lambda db, request: None)
    a, b = sessions(), sessions()
    request_id = new_request(a, required=2)
    approver = roles(a)["approver"]

    # The check passes for A, then B's approval by the same actor commits, then A inserts.
    fired = _in_flight(monkeypatch, lambda: (approve(b, request_id, approver), b.commit()))

    with pytest.raises(InvalidDecisionError, match="already approved"):
        approve(a, request_id, approver)

    assert fired
    verify = sessions()
    assert decision_rows(verify, request_id, "approve") == 1
    assert verify.get(ApprovalRequest, request_id).status == ApprovalStatus.PENDING
