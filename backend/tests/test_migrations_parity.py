"""the migration chain, not `create_all`, is what builds the
platform schema -- so a test builds it that way and compares.

Replaces the "upgrade / downgrade verified by hand on a scratch SQLite" line
every migration task used to carry. Runs on plain `pytest` (no Docker), against
`tmp_path` SQLite files: alembic needs a real file that survives across
connections, so never `:memory:`.

The URL is pinned through the Alembic `Config` (migrations/env.py honours an
explicitly supplied URL before it falls back to `settings.database_url`), so
nothing here reads the ambient `.env` or touches the dev DB.
"""

from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from tests import migration_parity_support as parity

MIGRATIONS_DIR = parity.MIGRATIONS_DIR


def _url(db_file: Path) -> str:
    return parity.sqlite_url(db_file)


def _config(db_file: Path):
    return parity.config(_url(db_file))


def _head() -> str:
    return parity.head()


def _diff(db_file: Path) -> list:
    return parity.diff(_url(db_file))


def test_p1_upgrade_head_builds_exactly_the_models(tmp_path):
    parity.assert_p1_head_builds_exactly_the_models(_url(tmp_path / "p1.db"))


def test_p2_every_downgrade_works_and_the_chain_rebuilds(tmp_path):
    """The check lives in `tests/migration_parity_support.py` so the Postgres twin
    (`test_migrations_parity_postgres.py`) runs the identical one."""
    parity.assert_p2_every_downgrade_works_and_the_chain_rebuilds(_url(tmp_path / "p2.db"))


def test_p3_the_chain_has_exactly_one_head():
    script = ScriptDirectory.from_config(_config(Path("unused.db")))

    heads = script.get_heads()

    assert len(heads) == 1, f"the migration chain is branched: {heads}"


def test_the_measured_change_columns_round_trip_with_an_existing_build_row(tmp_path):
    """`builds.changed_files` / `builds.protected_path_drift` are added NULL ("not
    measured") to a row that predates them, survive an upgrade, are dropped by the
    downgrade with the rest of the row intact, and come back on re-upgrade."""
    parent = "b53e9d2a4c18"  # the head before this revision
    db_file = tmp_path / "caseab.db"
    cfg = _config(db_file)
    command.upgrade(cfg, parent)
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO builds (id, project_id, type, status, created_at, failure_detail) "
                    "VALUES ('b-old', 'p-1', 'create', 'SUCCEEDED', '2026-01-01 00:00:00', 'kept')"
                )
            )

        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT status, failure_detail, changed_files, protected_path_drift FROM builds WHERE id='b-old'")
            ).one()
        assert row == ("SUCCEEDED", "kept", None, None)

        with engine.begin() as conn:
            conn.execute(
                text("UPDATE builds SET changed_files = :c, protected_path_drift = :d WHERE id='b-old'"),
                {"c": '{"entries": [], "totals": {"added": 0, "modified": 0, "deleted": 0}, "truncated": false}', "d": '["Dockerfile"]'},
            )

        command.downgrade(cfg, parent)
        insp = inspect(engine)
        assert {"changed_files", "protected_path_drift"}.isdisjoint(c["name"] for c in insp.get_columns("builds"))
        with engine.connect() as conn:
            row = conn.execute(text("SELECT status, failure_detail FROM builds WHERE id='b-old'")).one()
        assert row == ("SUCCEEDED", "kept")

        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT status, changed_files, protected_path_drift FROM builds WHERE id='b-old'")
            ).one()
        assert row == ("SUCCEEDED", None, None)  # the dropped values are gone; the row is not
    finally:
        engine.dispose()
    assert _diff(db_file) == []


def test_repair_routes_is_added_null_to_an_existing_build_row_and_dropped_by_the_downgrade(tmp_path):
    """`builds.repair_routes` is NULL on a row that predates it (never backfilled, never an empty
    list), takes a stored list, is dropped by the downgrade with the rest of the row intact, and is
    NULL again on re-upgrade."""
    parent = "d5b9f3a72e18"  # the head before this revision
    db_file = tmp_path / "repair_routes.db"
    cfg = _config(db_file)
    command.upgrade(cfg, parent)
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO builds (id, project_id, type, status, created_at, repair_attempts) "
                    "VALUES ('b-old', 'p-1', 'create', 'SUCCEEDED', '2026-01-01 00:00:00', 2)"
                )
            )

        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            row = conn.execute(text("SELECT status, repair_attempts, repair_routes FROM builds WHERE id='b-old'")).one()
        assert row == ("SUCCEEDED", 2, None)

        with engine.begin() as conn:
            conn.execute(
                text("UPDATE builds SET repair_routes = :r WHERE id='b-old'"),
                {"r": '[{"attempt": 1, "routes": [{"path": "/boom", "status": 500, "outcome": "server_error"}]}]'},
            )

        command.downgrade(cfg, parent)
        assert "repair_routes" not in {c["name"] for c in inspect(engine).get_columns("builds")}
        with engine.connect() as conn:
            row = conn.execute(text("SELECT status, repair_attempts FROM builds WHERE id='b-old'")).one()
        assert row == ("SUCCEEDED", 2)

        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            row = conn.execute(text("SELECT status, repair_attempts, repair_routes FROM builds WHERE id='b-old'")).one()
        assert row == ("SUCCEEDED", 2, None)  # the dropped value is gone; the row is not
    finally:
        engine.dispose()
    assert _diff(db_file) == []
