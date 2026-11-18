"""the data scenario for migration `d2c8f5a71e39` (one approve decision per actor per
approval request), written once and run on both engines -- `test_approval_decision_index_migration.py`
(SQLite) and `test_approval_postgres.py` (a real Postgres).

The database is built to the PARENT revision, rows are inserted into the schema as it was, and
only then is the migration applied, so what is asserted is what the migration does to rows that
already exist: in particular a request on which one actor holds several approve rows (possible
before the index: two clicks racing past the read-then-insert check), for which a bare
`CREATE UNIQUE INDEX` would fail and leave `alembic upgrade` half done.

The decision rows get ids that sort OPPOSITE to their creation times, so a migration that kept
"the lowest id" instead of "the earliest" is caught.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from alembic import command
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.approval import ApprovalDecision, ApprovalRequest
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.project import Project
from app.models.template import Template
from app.models.user import Team, User
from tests import migration_parity_support as parity

PARENT = "b3f6a1d94c28"  # the head before this revision
REVISION = "d2c8f5a71e39"
INDEX = "uq_approval_decisions_one_approve_per_actor"
ACTION = "approval_decision.duplicate_removed"

T0 = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def seed_rows(engine) -> dict:
    """Four requests; returns the ids the assertions name. See the module docstring."""
    with Session(engine) as db:
        team = Team(name="caseag team")
        db.add(team)
        db.flush()
        users = {}
        for key in ("owner", "alice", "bob"):
            users[key] = User(team_id=team.id, name=key, email=f"{key}@caseag.example", role="requester")
            db.add(users[key])
        template = Template(name="caseag tpl", stack="x", dockerfile_ref="d", deploy_config_ref="c", version="1")
        db.add(template)
        db.flush()
        project = Project(name="caseag", owner_id=users["owner"].id, team_id=team.id, template_id=template.id)
        db.add(project)
        db.flush()

        def request(key: str, *, status: str, required: int) -> str:
            build = Build(project_id=project.id, type="create", status="SUCCEEDED")
            db.add(build)
            db.flush()
            row = ApprovalRequest(
                id=f"req-{key}",
                build_id=build.id,
                project_id=project.id,
                requester_id=users["owner"].id,
                risk_tier="high",
                status=status,
                required_approvals=required,
            )
            db.add(row)
            db.flush()
            return row.id

        def decision(did: str, rid: str, actor: str, kind: str, minute: int, comment: str | None = None) -> None:
            row = ApprovalDecision(
                id=did, approval_request_id=rid, actor_id=users[actor].id, decision=kind, comment=comment
            )
            row.created_at = _at(minute)
            db.add(row)
            db.flush()

        # R1 pending, needs two: alice approved twice (the EARLIER row has the HIGHER id), bob once.
        r1 = request("pending-dup", status="PENDING", required=2)
        decision("z-alice-first", r1, "alice", "approve", 0, comment="looks fine")
        decision("a-alice-second", r1, "alice", "approve", 5, comment="clicked twice")
        decision("m-bob", r1, "bob", "approve", 7)
        # R2 APPROVED on the strength of ONE person approving three times (needs two): the
        # inflated tally the new rule forbids, left in place and flagged.
        r2 = request("inflated", status="APPROVED", required=2)
        decision("r2-1", r2, "alice", "approve", 0)
        decision("r2-2", r2, "alice", "approve", 1)
        decision("r2-3", r2, "alice", "approve", 2)
        # R3: approve then reject by one actor, and another's approval: nothing to remove.
        r3 = request("mixed", status="REJECTED", required=2)
        decision("r3-approve", r3, "alice", "approve", 0)
        decision("r3-reject", r3, "alice", "reject", 1, comment="changed my mind")
        decision("r3-bob", r3, "bob", "approve", 2)
        # R4: clean.
        r4 = request("clean", status="APPROVED", required=1)
        decision("r4-alice", r4, "alice", "approve", 0)
        db.commit()
        return {"alice": users["alice"].id, "bob": users["bob"].id, "requests": (r1, r2, r3, r4)}


def run_data_scenario(url: str) -> None:
    """Asserts the whole behaviour of the revision against `url` (an empty database)."""
    cfg = parity.config(url)
    command.upgrade(cfg, PARENT)
    engine = create_engine(url)
    try:
        # the seed inserts through the ORM `Build`, whose columns are the model's today; this
        # revision predates `builds.preview_start_failure`, so add it as a later migration would.
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN preview_start_failure VARCHAR")
            conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN repair_attempts INTEGER NOT NULL DEFAULT 0")
            conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN repair_routes JSON")
        ids = seed_rows(engine)
        r1, r2, r3, r4 = ids["requests"]

        command.upgrade(cfg, REVISION)

        with Session(engine) as db:
            remaining = {d.id for d in db.query(ApprovalDecision)}
            # Earliest kept per (request, actor); the later duplicates are gone.
            assert remaining == {
                "z-alice-first", "m-bob",  # R1
                "r2-1",  # R2
                "r3-approve", "r3-reject", "r3-bob",  # R3: the reject and both approvals survive
                "r4-alice",  # R4
            }
            kept = db.get(ApprovalDecision, "z-alice-first")
            assert kept.comment == "looks fine" and kept.actor_id == ids["alice"]

            # No request changes status: un-approving something that may already be deployed is
            # not a migration's call. The inflated one is FLAGGED instead (below).
            assert {r.id: r.status for r in db.query(ApprovalRequest)} == {
                r1: "PENDING", r2: "APPROVED", r3: "REJECTED", r4: "APPROVED",
            }

            entries = db.query(AuditLog).filter(AuditLog.action == ACTION).all()
            by_removed = {e.metadata_json["removed_decision_id"]: e for e in entries}
            assert set(by_removed) == {"a-alice-second", "r2-2", "r2-3"}
            first = by_removed["a-alice-second"]
            assert first.actor_id is None  # the migration is not a user
            assert first.target == f"approval_request:{r1}"
            assert first.metadata_json["kept_decision_id"] == "z-alice-first"
            assert first.metadata_json["actor_id"] == ids["alice"]
            assert first.metadata_json["comment"] == "clicked twice"  # the removed row's content is preserved
            assert first.metadata_json["request_status"] == "PENDING"
            assert first.metadata_json["required_approvals"] == 2
            assert first.metadata_json["distinct_approvers"] == 2
            assert REVISION in first.metadata_json["cause"]
            inflated = by_removed["r2-3"].metadata_json
            assert inflated["request_status"] == "APPROVED"
            assert inflated["distinct_approvers"] == 1 < inflated["required_approvals"]
            assert db.query(AuditLog).count() == 3  # nothing else wrote an audit row

        # The index exists, is unique, and only covers approvals.
        indexes = {i["name"]: i for i in inspect(engine).get_indexes("approval_decisions")}
        assert indexes[INDEX]["unique"] and indexes[INDEX]["column_names"] == ["approval_request_id", "actor_id"]
        with Session(engine) as db:
            db.add(ApprovalDecision(approval_request_id=r4, actor_id=ids["alice"], decision="approve"))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
            else:
                raise AssertionError("a second approve row by one actor was accepted after the migration")
            db.add(ApprovalDecision(approval_request_id=r4, actor_id=ids["alice"], decision="reject", comment="x"))
            db.commit()  # a reject beside the approve is not covered

        # Down drops the index only; the removed rows are not restored (their content is in the
        # audit entries). Up again finds nothing to remove and adds no audit rows.
        command.downgrade(cfg, PARENT)
        assert INDEX not in {i["name"] for i in inspect(engine).get_indexes("approval_decisions")}
        with Session(engine) as db:
            assert db.query(ApprovalDecision).count() == 8  # 7 survivors + the reject added above
            assert db.query(AuditLog).filter(AuditLog.action == ACTION).count() == 3
        command.upgrade(cfg, REVISION)
        with Session(engine) as db:
            assert db.query(AuditLog).filter(AuditLog.action == ACTION).count() == 3
        assert INDEX in {i["name"] for i in inspect(engine).get_indexes("approval_decisions")}
    finally:
        engine.dispose()


def run_clean_database_scenario(url: str) -> None:
    """A database with no duplicates: the revision changes no row and writes no audit entry."""
    cfg = parity.config(url)
    command.upgrade(cfg, PARENT)
    engine = create_engine(url)
    try:
        command.upgrade(cfg, REVISION)
        with Session(engine) as db:
            assert db.query(AuditLog).count() == 0
            assert db.query(ApprovalDecision).count() == 0
        assert INDEX in {i["name"] for i in inspect(engine).get_indexes("approval_decisions")}
    finally:
        engine.dispose()
