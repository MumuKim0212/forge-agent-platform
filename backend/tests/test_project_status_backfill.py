"""the one-time
backfill of `projects.status` from history (migration `d91c3e7a5b28`).

Runs the real migration over fixture rows of each kind. The database is built
to the parent revision, rows are inserted with raw SQL against the schema as it
was, and only then is `upgrade head` run -- so what is asserted is what the
migration does to rows that already exist, not what the application writes now.
`tmp_path` SQLite files only (alembic needs a file that survives connections);
nothing here reads the ambient `.env` or touches the dev DB.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.schema_check import alembic_option_value

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
PARENT = "a74e5c2b8d31"  # the head before the backfill revision
BACKFILL = "d91c3e7a5b28"
TS = "2026-01-01 00:00:00"


def _config(db_file: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", alembic_option_value(str(MIGRATIONS_DIR)))
    cfg.set_main_option("sqlalchemy.url", alembic_option_value(f"sqlite:///{db_file.as_posix()}"))
    return cfg


def _project(conn, pid: str, status: str = "SUBMITTED") -> None:
    conn.execute(
        text(
            "INSERT INTO projects (id, name, owner_id, team_id, template_id, status, last_active_at, created_at) "
            "VALUES (:id, :id, 'u', 't', 'tpl', :status, :ts, :ts)"
        ),
        {"id": pid, "status": status, "ts": TS},
    )


def _build(conn, bid: str, pid: str, status: str, *, preview_url: str | None = None) -> None:
    conn.execute(
        text(
            "INSERT INTO builds (id, project_id, type, status, created_at, preview_url) "
            "VALUES (:id, :p, 'create', :status, :ts, :url)"
        ),
        {"id": bid, "p": pid, "status": status, "ts": TS, "url": preview_url},
    )


def _approval(conn, aid: str, pid: str, bid: str, status: str) -> None:
    conn.execute(
        text(
            "INSERT INTO approval_requests (id, build_id, project_id, requester_id, risk_tier, status, "
            "required_approvals, created_at) VALUES (:id, :b, :p, 'u', 'medium', :status, 1, :ts)"
        ),
        {"id": aid, "b": bid, "p": pid, "status": status, "ts": TS},
    )


def _deployment(conn, did: str, pid: str, bid: str, status: str) -> None:
    conn.execute(
        text(
            "INSERT INTO deployments (id, project_id, build_id, confirmed_by_id, status, created_at) "
            "VALUES (:id, :p, :b, 'u', :status, :ts)"
        ),
        {"id": did, "p": pid, "b": bid, "status": status, "ts": TS},
    )


def _audit(conn, action: str, target: str, metadata: dict | None = None) -> None:
    conn.execute(
        text(
            "INSERT INTO audit_log (id, actor_id, action, target, metadata_json, ts) "
            "VALUES (:id, NULL, :action, :target, :meta, :ts)"
        ),
        {
            "id": f"{action}:{target}",
            "action": action,
            "target": target,
            "meta": json.dumps(metadata) if metadata is not None else None,
            "ts": TS,
        },
    )


def _seed_rows(conn) -> None:
    """One project per kind of history. The id says what it is."""
    # Nothing at all, and only the never-queued slice-1 stub.
    _project(conn, "p-no-builds")
    _project(conn, "p-stub-build")
    _build(conn, "b-stub", "p-stub-build", "NOT_IMPLEMENTED")

    # A build exists.
    _project(conn, "p-queued")
    _build(conn, "b-queued", "p-queued", "QUEUED")
    _project(conn, "p-failed-build")
    _build(conn, "b-failed", "p-failed-build", "FAILED")
    _project(conn, "p-succeeded-no-preview")
    _build(conn, "b-nopreview", "p-succeeded-no-preview", "SUCCEEDED")

    # A preview existed: still live, or reaped (columns cleared; only the audit entry remains).
    _project(conn, "p-preview-live")
    _build(conn, "b-live", "p-preview-live", "SUCCEEDED", preview_url="http://preview.test/x")
    _project(conn, "p-preview-reaped")
    _build(conn, "b-reaped", "p-preview-reaped", "SUCCEEDED")
    _audit(conn, "preview.issued", "build:b-reaped", {"project_id": "p-preview-reaped"})
    # A preview was issued for a build that did NOT succeed: not "preview ready".
    _project(conn, "p-preview-on-failed")
    _build(conn, "b-pf", "p-preview-on-failed", "FAILED", preview_url="http://preview.test/y")

    # An approval is waiting; a decided one is not evidence of waiting.
    _project(conn, "p-pending")
    _build(conn, "b-pending", "p-pending", "SUCCEEDED", preview_url="http://preview.test/z")
    _approval(conn, "a-pending", "p-pending", "b-pending", "PENDING")
    _project(conn, "p-decided-approval")
    _build(conn, "b-decided", "p-decided-approval", "SUCCEEDED")
    _approval(conn, "a-decided", "p-decided-approval", "b-decided", "APPROVED")
    _build(conn, "b-decided2", "p-decided-approval", "SUCCEEDED")
    _approval(conn, "a-superseded", "p-decided-approval", "b-decided2", "SUPERSEDED")

    # Deployed at some point: ACTIVE, STOPPED and SUPERSEDED all prove it.
    for suffix, dep_status in (("active", "ACTIVE"), ("stopped", "STOPPED"), ("superseded", "SUPERSEDED")):
        pid = f"p-deployed-{suffix}"
        _project(conn, pid)
        _build(conn, f"b-{suffix}", pid, "SUCCEEDED")
        _deployment(conn, f"d-{suffix}", pid, f"b-{suffix}", dep_status)

    # A deployment that never became ACTIVE does not make a project ACTIVE.
    _project(conn, "p-never-activated")
    _build(conn, "b-na", "p-never-activated", "SUCCEEDED", preview_url="http://preview.test/n")
    _deployment(conn, "d-na-failed", "p-never-activated", "b-na", "FAILED")
    _deployment(conn, "d-na-deploying", "p-never-activated", "b-na", "DEPLOYING")

    # Evidence belongs to ITS project: a deployed neighbour changes nothing here.
    _project(conn, "p-neighbour-of-deployed")

    # Rows that must not move.
    _project(conn, "p-archived", status="ARCHIVED")
    _build(conn, "b-arch", "p-archived", "SUCCEEDED")
    _deployment(conn, "d-arch", "p-archived", "b-arch", "STOPPED")
    _audit(conn, "project.archived", "project:p-archived", {"previous_status": "SUBMITTED"})
    _project(conn, "p-already-ahead", status="AWAITING_APPROVAL")
    _build(conn, "b-ahead", "p-already-ahead", "QUEUED")
    _project(conn, "p-already-active", status="ACTIVE")


EXPECTED = {
    "p-no-builds": "SUBMITTED",
    "p-stub-build": "SUBMITTED",
    "p-queued": "BUILD_PENDING",
    "p-failed-build": "BUILD_PENDING",
    "p-succeeded-no-preview": "BUILD_PENDING",
    "p-preview-live": "PREVIEW_READY",
    "p-preview-reaped": "PREVIEW_READY",
    "p-preview-on-failed": "BUILD_PENDING",
    "p-pending": "AWAITING_APPROVAL",
    "p-decided-approval": "BUILD_PENDING",
    "p-deployed-active": "ACTIVE",
    "p-deployed-stopped": "ACTIVE",
    "p-deployed-superseded": "ACTIVE",
    "p-never-activated": "PREVIEW_READY",
    "p-neighbour-of-deployed": "SUBMITTED",
    "p-archived": "ARCHIVED",
    "p-already-ahead": "AWAITING_APPROVAL",
    "p-already-active": "ACTIVE",
}


@pytest.fixture()
def migrated(tmp_path):
    db_file = tmp_path / "backfill.db"
    cfg = _config(db_file)
    command.upgrade(cfg, PARENT)
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    with engine.begin() as conn:
        _seed_rows(conn)
    command.upgrade(cfg, "head")
    try:
        yield cfg, engine
    finally:
        engine.dispose()


def _statuses(engine) -> dict[str, str]:
    with engine.connect() as conn:
        return {pid: status for pid, status in conn.execute(text("SELECT id, status FROM projects"))}


def test_the_backfill_revision_sits_on_its_parent_and_the_chain_still_reaches_it():
    """It was "is the head" until a revision was added on top of it; what matters now is that it
    is still one revision after `PARENT` and is an ancestor of the one head (so `upgrade head`
    runs it, which the tests below depend on)."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_config(Path("unused.db")))
    (head,) = script.get_heads()
    assert script.get_revision(BACKFILL).down_revision == PARENT
    assert BACKFILL in {rev.revision for rev in script.iterate_revisions(head, PARENT)}


def test_each_kind_of_history_lands_on_its_latest_evidenced_stage(migrated):
    _cfg, engine = migrated

    assert _statuses(engine) == EXPECTED


def test_a_never_deployed_project_is_never_marked_active(migrated):
    """The mutation this names: a backfill that reads "has a deployment row" (or anything
    looser than "was once ACTIVE") as ACTIVE. Every project here with no ACTIVE/SUPERSEDED/
    STOPPED deployment must come out short of ACTIVE."""
    _cfg, engine = migrated
    deployed = {"p-deployed-active", "p-deployed-stopped", "p-deployed-superseded", "p-already-active"}

    never_deployed = {pid: s for pid, s in _statuses(engine).items() if pid not in deployed}

    assert "ACTIVE" not in never_deployed.values()


def test_every_moved_project_gets_one_audit_entry_and_no_other_does(migrated):
    _cfg, engine = migrated
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT actor_id, target, metadata_json FROM audit_log WHERE action = 'project.status_changed'")
        ).fetchall()

    entries = {target: (actor, json.loads(meta)) for actor, target, meta in rows}
    assert len(rows) == len(entries)  # one per project
    moved = {pid for pid, status in EXPECTED.items() if status not in ("SUBMITTED", "ARCHIVED")} - {
        "p-already-ahead",
        "p-already-active",
    }
    assert set(entries) == {f"project:{pid}" for pid in moved}
    actor, meta = entries["project:p-deployed-stopped"]
    assert actor is None
    assert meta == {"old": "SUBMITTED", "new": "ACTIVE", "cause": "backfill"}
    assert entries["project:p-pending"][1] == {"old": "SUBMITTED", "new": "AWAITING_APPROVAL", "cause": "backfill"}


def test_an_archived_row_and_its_recorded_previous_status_are_left_alone(migrated):
    _cfg, engine = migrated
    with engine.connect() as conn:
        meta = conn.execute(
            text("SELECT metadata_json FROM audit_log WHERE action = 'project.archived' AND target = 'project:p-archived'")
        ).scalar_one()

    assert _statuses(engine)["p-archived"] == "ARCHIVED"
    assert json.loads(meta) == {"previous_status": "SUBMITTED"}


def test_downgrade_is_a_no_op_and_re_upgrading_changes_nothing_more(migrated):
    cfg, engine = migrated
    after_upgrade = _statuses(engine)
    with engine.connect() as conn:
        audit_before = conn.execute(text("SELECT COUNT(*) FROM audit_log")).scalar_one()

    command.downgrade(cfg, PARENT)

    assert _statuses(engine) == after_upgrade  # statuses are not reset
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM audit_log")).scalar_one() == audit_before

    command.upgrade(cfg, "head")  # re-running it is idempotent: nothing is later than it already is
    assert _statuses(engine) == after_upgrade
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM audit_log")).scalar_one() == audit_before


def test_an_empty_database_migrates(tmp_path):
    db_file = tmp_path / "empty.db"

    command.upgrade(_config(db_file), "head")  # no projects: nothing to do, nothing to fail
