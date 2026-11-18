"""Per-project serialization: migration
`e9b4d27a6c13` adds the partial unique index that lets the database refuse a
second `DEPLOYING` row for one project.

Runs the real migration over fixture rows. The database is built to the parent
revision, rows are inserted with raw SQL against the schema as it was, and only
then is the migration applied -- so what is asserted is what it does to rows
that already exist, in particular to a project that ALREADY holds two or more
`DEPLOYING` rows (possible before the index: two confirms racing past the
check), for which a bare `CREATE UNIQUE INDEX` would fail and leave
`alembic upgrade` half done.

`tmp_path` SQLite files only (alembic needs a file that survives connections);
nothing here reads the ambient `.env` or touches the dev DB. The chain-wide
properties (model == migrations, every downgrade restores the previous schema)
are `tests/test_migrations_parity.py`'s; this file owns the data behaviour and
the index's shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.schema_check import alembic_option_value

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
PARENT = "c4a8e2f61d37"  # the head before the DEPLOYING index
REVISION = "e9b4d27a6c13"
INDEX = "uq_deployments_one_deploying_per_project"
TS = "2026-01-01 00:00:00.000000"


def _config(db_file: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", alembic_option_value(str(MIGRATIONS_DIR)))
    cfg.set_main_option("sqlalchemy.url", alembic_option_value(f"sqlite:///{db_file.as_posix()}"))
    return cfg


def _project(conn, pid: str) -> None:
    conn.execute(
        text(
            "INSERT INTO projects (id, name, owner_id, team_id, template_id, status, kind, last_active_at, created_at) "
            "VALUES (:id, :id, 'u', 't', 'tpl', 'SUBMITTED', 'normal', :ts, :ts)"
        ),
        {"id": pid, "ts": TS},
    )


def _deployment(
    conn,
    did: str,
    pid: str,
    status: str,
    *,
    created: str = TS,
    started: str | None = None,
) -> None:
    conn.execute(
        text(
            "INSERT INTO deployments (id, project_id, build_id, confirmed_by_id, status, created_at, started_at) "
            "VALUES (:id, :p, 'b', 'u', :status, :created, :started)"
        ),
        {"id": did, "p": pid, "status": status, "created": created, "started": started},
    )


def _seed_rows(conn) -> None:
    """One project per shape. The id says what it is."""
    # Two DEPLOYING rows: the OLDER one was never claimed, the newer one was.
    # The claimed row is the one a worker may be executing, so it is kept.
    _project(conn, "p-claimed-newer")
    _deployment(conn, "d-unclaimed-older", "p-claimed-newer", "DEPLOYING", created="2026-01-01 10:00:00.000000")
    _deployment(
        conn,
        "d-claimed-newer",
        "p-claimed-newer",
        "DEPLOYING",
        created="2026-01-01 11:00:00.000000",
        started="2026-01-01 11:00:05.000000",
    )

    # Three unclaimed rows: the oldest is kept (the queue is oldest-first).
    _project(conn, "p-three")
    _deployment(conn, "d-three-2", "p-three", "DEPLOYING", created="2026-01-01 10:01:00.000000")
    _deployment(conn, "d-three-1", "p-three", "DEPLOYING", created="2026-01-01 10:00:00.000000")
    _deployment(conn, "d-three-3", "p-three", "DEPLOYING", created="2026-01-01 10:02:00.000000")

    # Identical timestamps: the id breaks the tie, so the outcome is deterministic.
    _project(conn, "p-tied")
    _deployment(conn, "d-tied-b", "p-tied", "DEPLOYING", created="2026-01-01 10:00:00.000000")
    _deployment(conn, "d-tied-a", "p-tied", "DEPLOYING", created="2026-01-01 10:00:00.000000")

    # Rows that need no change: one DEPLOYING alone, and a project whose
    # DEPLOYING row sits beside an ACTIVE one and finished ones (a redeploy).
    _project(conn, "p-single")
    _deployment(conn, "d-single", "p-single", "DEPLOYING")
    _project(conn, "p-redeploy")
    _deployment(conn, "d-redeploy-active", "p-redeploy", "ACTIVE")
    _deployment(conn, "d-redeploy-deploying", "p-redeploy", "DEPLOYING")
    _deployment(conn, "d-redeploy-old-1", "p-redeploy", "SUPERSEDED")
    _deployment(conn, "d-redeploy-old-2", "p-redeploy", "SUPERSEDED")
    _deployment(conn, "d-redeploy-failed", "p-redeploy", "FAILED")


def _statuses(engine) -> dict[str, tuple[str, str | None, bool]]:
    with engine.connect() as conn:
        return {
            row.id: (row.status, row.failure_reason, row.ended_at is not None)
            for row in conn.execute(text("SELECT id, status, failure_reason, ended_at FROM deployments"))
        }


def _failed_audit(engine) -> dict[str, dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT target, actor_id, metadata_json FROM audit_log WHERE action = 'deployment.failed'")
        )
        return {row.target: {"actor_id": row.actor_id, "meta": json.loads(row.metadata_json)} for row in rows}


@pytest.fixture()
def db_at_parent(tmp_path):
    db_file = tmp_path / "casead.db"
    cfg = _config(db_file)
    command.upgrade(cfg, PARENT)
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    with engine.begin() as conn:
        _seed_rows(conn)
    yield cfg, engine
    engine.dispose()


def test_the_parent_revision_is_what_this_test_thinks_it_is(db_at_parent):
    """Guards the fixture: the rows above are inserted against THIS schema, and
    it must not yet have the index (else the seeded duplicates could not exist)."""
    _cfg, engine = db_at_parent
    assert INDEX not in {i["name"] for i in inspect(engine).get_indexes("deployments")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == PARENT


def test_upgrade_keeps_one_deploying_row_per_project_and_fails_the_rest(db_at_parent):
    cfg, engine = db_at_parent

    command.upgrade(cfg, REVISION)

    after = _statuses(engine)
    # The claimed row beats an older unclaimed one.
    assert after["d-claimed-newer"][0] == "DEPLOYING"
    assert after["d-unclaimed-older"] == ("FAILED", "INFRA_ERROR", True)
    # Otherwise the oldest is kept.
    assert after["d-three-1"][0] == "DEPLOYING"
    assert after["d-three-2"] == ("FAILED", "INFRA_ERROR", True)
    assert after["d-three-3"] == ("FAILED", "INFRA_ERROR", True)
    # A tie on created_at is broken by id.
    assert after["d-tied-a"][0] == "DEPLOYING"
    assert after["d-tied-b"] == ("FAILED", "INFRA_ERROR", True)
    # Nothing else is touched: a lone DEPLOYING row, and every row of a redeploy in flight.
    assert after["d-single"][0] == "DEPLOYING"
    assert after["d-redeploy-active"][0] == "ACTIVE"
    assert after["d-redeploy-deploying"][0] == "DEPLOYING"
    assert after["d-redeploy-old-1"][0] == "SUPERSEDED"
    assert after["d-redeploy-old-2"][0] == "SUPERSEDED"
    assert after["d-redeploy-failed"] == ("FAILED", None, False)

    # No project is left with two DEPLOYING rows.
    with engine.connect() as conn:
        per_project = conn.execute(
            text("SELECT project_id, COUNT(*) FROM deployments WHERE status = 'DEPLOYING' GROUP BY project_id")
        ).fetchall()
    assert all(count == 1 for _project_id, count in per_project)


def test_each_demoted_row_gets_a_timeline_visible_audit_entry_and_no_other_row_does(db_at_parent):
    cfg, engine = db_at_parent

    command.upgrade(cfg, REVISION)

    audit = _failed_audit(engine)
    assert set(audit) == {
        "deployment:d-unclaimed-older",
        "deployment:d-three-2",
        "deployment:d-three-3",
        "deployment:d-tied-b",
    }
    entry = audit["deployment:d-unclaimed-older"]
    assert entry["actor_id"] is None
    assert entry["meta"]["project_id"] == "p-claimed-newer"
    assert entry["meta"]["build_id"] == "b"
    assert entry["meta"]["reason"] == "INFRA_ERROR"
    assert "e9b4d27a6c13" in entry["meta"]["cause"]


def test_the_upgrade_creates_a_partial_unique_index_the_database_enforces(db_at_parent):
    cfg, engine = db_at_parent
    command.upgrade(cfg, REVISION)

    (index,) = [i for i in inspect(engine).get_indexes("deployments") if i["name"] == INDEX]
    assert bool(index["unique"]) is True
    assert index["column_names"] == ["project_id"]
    with engine.connect() as conn:
        ddl = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = :n"), {"n": INDEX}).scalar()
    assert "WHERE status = 'DEPLOYING'" in ddl  # partial, not a blanket unique on project_id

    # Enforced: a second DEPLOYING row for a project that has one is refused ...
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            _deployment(conn, "d-second", "p-single", "DEPLOYING", created="2026-01-02 00:00:00.000000")
    # ... while every shape that is not "second DEPLOYING" is still fine.
    with engine.begin() as conn:
        _deployment(conn, "d-more-superseded", "p-single", "SUPERSEDED")
        _deployment(conn, "d-more-failed", "p-single", "FAILED")
        _project(conn, "p-new")
        _deployment(conn, "d-new", "p-new", "DEPLOYING")


def test_downgrade_drops_the_index_and_leaves_the_demoted_rows_failed(db_at_parent):
    cfg, engine = db_at_parent
    command.upgrade(cfg, REVISION)

    command.downgrade(cfg, PARENT)

    assert INDEX not in {i["name"] for i in inspect(engine).get_indexes("deployments")}
    after = _statuses(engine)
    assert after["d-unclaimed-older"] == ("FAILED", "INFRA_ERROR", True)  # not re-opened
    # The rule is gone again, so a second DEPLOYING row can be written ...
    with engine.begin() as conn:
        _deployment(conn, "d-second", "p-single", "DEPLOYING", created="2026-01-02 00:00:00.000000")
    # ... and upgrading again demotes it, rather than failing on it.
    command.upgrade(cfg, REVISION)
    assert _statuses(engine)["d-second"] == ("FAILED", "INFRA_ERROR", True)
    assert _statuses(engine)["d-single"][0] == "DEPLOYING"
    assert INDEX in {i["name"] for i in inspect(engine).get_indexes("deployments")}


def test_upgrading_a_database_with_no_duplicates_writes_no_audit_entries(tmp_path):
    db_file = tmp_path / "casead-clean.db"
    cfg = _config(db_file)
    command.upgrade(cfg, PARENT)
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        with engine.begin() as conn:
            _project(conn, "p-only")
            _deployment(conn, "d-only", "p-only", "DEPLOYING")

        command.upgrade(cfg, REVISION)

        assert _failed_audit(engine) == {}
        assert _statuses(engine)["d-only"][0] == "DEPLOYING"
    finally:
        engine.dispose()
