"""migration `e9b4d27a6c13` (one DEPLOYING deployment per project) on a
real Postgres -- the engine production runs, which `tests/test_deploying_index_
migration.py` (SQLite) cannot speak for.

What differs between the two and is exercised here: Postgres enforces the
foreign keys the seeded rows must satisfy, returns `created_at` as a
`timestamptz` rather than text (the migration sorts on it), binds the audit
entry's JSON natively, and renders the index's predicate itself. The whole
revision chain runs up to the parent first, so a migration that only works on
SQLite shows up here.

Uses the shared `real_service_postgres_admin_dsn` fixture (a throwaway
`postgres:18-alpine`, one per module) and a scratch database on it, dropped
afterwards. Skips cleanly when Docker is unavailable.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("docker")
pytest.importorskip("psycopg")

import docker  # noqa: E402
import psycopg  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from docker.errors import DockerException  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models.build import Build  # noqa: E402
from app.models.deployment import Deployment  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.models.user import User  # noqa: E402
from app.seed import seed  # noqa: E402

from app.schema_check import alembic_option_value  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
PARENT = "c4a8e2f61d37"
REVISION = "e9b4d27a6c13"
INDEX = "uq_deployments_one_deploying_per_project"


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


@pytest.fixture()
def scratch_database_url(real_service_postgres_admin_dsn):
    """A fresh, empty database on the throwaway Postgres, as an SQLAlchemy URL."""
    name = f"deploying_index_{uuid.uuid4().hex[:10]}"
    admin = psycopg.connect(real_service_postgres_admin_dsn, autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
        url = make_url(real_service_postgres_admin_dsn).set(drivername="postgresql+psycopg", database=name)
        yield url.render_as_string(hide_password=False)
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", alembic_option_value(str(MIGRATIONS_DIR)))
    cfg.set_main_option("sqlalchemy.url", alembic_option_value(url))
    return cfg


def _ts(minute: int) -> datetime:
    return datetime(2026, 1, 1, 10, minute, tzinfo=timezone.utc)


def test_the_migration_demotes_duplicates_and_adds_the_index_on_postgres(scratch_database_url):
    cfg = _config(scratch_database_url)
    command.upgrade(cfg, PARENT)

    engine = create_engine(scratch_database_url)
    try:
        # The ORM inserts below use the CURRENT `Build` model against a schema at PARENT, and its
        # INSERT names every `builds` column the model has. The ones added after PARENT are added here
        # the way their own migrations add them (`preview_start_failure`,
        # `repair_attempts`).
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN preview_start_failure VARCHAR")
            conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN repair_attempts INTEGER NOT NULL DEFAULT 0")
            conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN repair_routes JSON")
        with Session(engine) as db:
            seed(db, is_prod=False)
            owner = db.query(User).first()
            projects = {}
            for pid in ("dup", "single", "redeploy"):
                project = Project(
                    name=pid, owner_id=owner.id, team_id=owner.team_id, template_id=_template_id(db)
                )
                db.add(project)
                db.flush()
                build = Build(project_id=project.id, type="create", status="SUCCEEDED")
                db.add(build)
                db.flush()
                projects[pid] = (project, build)

            def row(key, status, minute, *, started=None):
                project, build = projects[key]
                return _insert_deployment(
                    db,
                    project_id=project.id,
                    build_id=build.id,
                    confirmed_by_id=owner.id,
                    status=status,
                    started_at=started,
                    created_at=_ts(minute),
                )

            # The OLDER row was never claimed, the NEWER one was: the claimed one is kept.
            unclaimed_older = row("dup", "DEPLOYING", 0)
            claimed_newer = row("dup", "DEPLOYING", 5, started=_ts(6))
            third = row("dup", "DEPLOYING", 9)
            single = row("single", "DEPLOYING", 0)
            redeploy_active = row("redeploy", "ACTIVE", 0)
            redeploy_deploying = row("redeploy", "DEPLOYING", 1)
            # Read before the commit: after it the instances are expired, and touching `.id` would
            # make the ORM re-SELECT every column of the CURRENT `Build` model from the database as
            # it was at PARENT (a column added since, e.g. `smoke_checks`, does not exist).
            single_project_id = projects["single"][0].id
            single_build_id = projects["single"][1].id
            dup_project_id = projects["dup"][0].id
            db.commit()

        command.upgrade(cfg, REVISION)

        with engine.connect() as conn:
            status = {
                r.id: (r.status, r.failure_reason, r.ended_at is not None)
                for r in conn.execute(text("SELECT id, status, failure_reason, ended_at FROM deployments"))
            }
            assert status[claimed_newer][0] == "DEPLOYING"
            assert status[unclaimed_older] == ("FAILED", "INFRA_ERROR", True)
            assert status[third] == ("FAILED", "INFRA_ERROR", True)
            assert status[single][0] == "DEPLOYING"
            assert status[redeploy_active][0] == "ACTIVE"
            assert status[redeploy_deploying][0] == "DEPLOYING"

            audit = {
                r.target: r.metadata_json
                for r in conn.execute(
                    text("SELECT target, metadata_json FROM audit_log WHERE action = 'deployment.failed'")
                )
            }
            assert set(audit) == {f"deployment:{unclaimed_older}", f"deployment:{third}"}
            entry = audit[f"deployment:{unclaimed_older}"]
            assert entry["project_id"] == dup_project_id
            assert entry["reason"] == "INFRA_ERROR"
            assert REVISION in entry["cause"]

            # A partial unique index, as Postgres renders it.
            ddl = conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": INDEX}
            ).scalar_one()
            assert "UNIQUE" in ddl and "(project_id)" in ddl and "DEPLOYING" in ddl and "WHERE" in ddl

        # Enforced: a second DEPLOYING row for a project that has one is refused ...
        with pytest.raises(IntegrityError):
            with Session(engine) as db:
                _insert_deployment(
                    db,
                    project_id=single_project_id,
                    build_id=single_build_id,
                    confirmed_by_id=_owner_id(engine),
                    status="DEPLOYING",
                )
                db.commit()
        # ... and a finished row beside it is not.
        with Session(engine) as db:
            _insert_deployment(
                db,
                project_id=single_project_id,
                build_id=single_build_id,
                confirmed_by_id=_owner_id(engine),
                status="SUPERSEDED",
            )
            db.commit()

        command.downgrade(cfg, PARENT)
        assert INDEX not in {i["name"] for i in inspect(engine).get_indexes("deployments")}
        command.upgrade(cfg, REVISION)
        assert INDEX in {i["name"] for i in inspect(engine).get_indexes("deployments")}
    finally:
        engine.dispose()


def _insert_deployment(db, *, created_at=None, **values) -> str:
    """One `deployments` row by a Core INSERT of only the named columns. The ORM would list every
    column of the CURRENT `Deployment` model, and a column added since this revision (e.g.
    `traffic_counter_seen`) does not exist in a database at PARENT. The index under test is
    exercised exactly as before: same rows, same statuses, same constraint violation."""
    row_id = str(uuid.uuid4())
    db.execute(
        Deployment.__table__.insert().values(
            id=row_id, created_at=created_at or datetime.now(timezone.utc), **values
        )
    )
    db.flush()
    return row_id


def _template_id(db) -> str:
    from app.models.template import Template

    return db.query(Template).first().id


def _owner_id(engine) -> str:
    with Session(engine) as db:
        return db.query(User).first().id
