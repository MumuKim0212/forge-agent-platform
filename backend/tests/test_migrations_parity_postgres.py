"""the migration chain on a REAL Postgres, which is what production runs.

`test_migrations_parity.py` runs the same two checks on SQLite only, so every
Postgres-specific branch in the chain -- `postgresql_using` on a column type change, a partial
index's `postgresql_where`, the native `ALTER`s that `op.batch_alter_table` takes on Postgres
instead of a table rebuild -- never executed anywhere. This file runs them:

  P1  `alembic upgrade head` on an empty Postgres database, then `compare_metadata` against
      `Base.metadata` is empty.
  P2  one revision at a time up, recording the reflected schema; one at a time down, each step
      landing on the schema recorded at its parent; back up to head.

plus the indexes added by the one-approve revision, as Postgres itself renders them.

The check bodies are `tests/migration_parity_support.py`'s, shared with the SQLite test.
One throwaway `postgres:18-alpine` per module (`real_service_postgres_admin_dsn`, removed with
`v=True`) and one scratch database per test on it (`scratch_postgres_url`), dropped afterwards.
Skips cleanly (not fails) when Docker is unavailable.
"""

from __future__ import annotations

import pytest

pytest.importorskip("docker")
pytest.importorskip("psycopg")

import docker  # noqa: E402
from alembic import command  # noqa: E402
from docker.errors import DockerException  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

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

# Index name -> (table, columns); none of these is unique. Spelled out here, not read back from
# the models, so renaming one in the migration AND the model together still goes red.
T089_INDEXES = {
    "ix_audit_log_target": ("audit_log", ["target"]),
    "ix_audit_log_ts": ("audit_log", ["ts"]),
    "ix_builds_project_id": ("builds", ["project_id"]),
    "ix_builds_status_created_at": ("builds", ["status", "created_at"]),
    "ix_builds_ended_at": ("builds", ["ended_at"]),
    "ix_approval_requests_project_id": ("approval_requests", ["project_id"]),
    "ix_change_requests_project_id": ("change_requests", ["project_id"]),
    "ix_deployments_project_id": ("deployments", ["project_id"]),
    "ix_eval_cases_project_id": ("eval_cases", ["project_id"]),
}
APPROVE_INDEX = "uq_approval_decisions_one_approve_per_actor"
PARENT = "b3f6a1d94c28"  # the head before the one-approve revision
ONE_APPROVE_REVISION = "d2c8f5a71e39"  # adds APPROVE_INDEX
INDEXES_REVISION = "a9e4b6c3d175"  # adds T089_INDEXES


def test_p1_upgrade_head_builds_exactly_the_models_on_postgres(scratch_postgres_url):
    parity.assert_p1_head_builds_exactly_the_models(scratch_postgres_url)


def test_p2_every_downgrade_works_and_the_chain_rebuilds_on_postgres(scratch_postgres_url):
    parity.assert_p2_every_downgrade_works_and_the_chain_rebuilds(scratch_postgres_url)


def test_p3_the_constraint_indexes_exist_at_head_as_postgres_renders_them(scratch_postgres_url):
    command.upgrade(parity.config(scratch_postgres_url), "head")

    engine = create_engine(scratch_postgres_url)
    try:
        with engine.connect() as conn:
            defs = {
                r.indexname: r.indexdef
                for r in conn.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"))
            }
    finally:
        engine.dispose()

    for name, (table, columns) in T089_INDEXES.items():
        assert name in defs, f"{name} is missing at head"
        assert f" ON public.{table} " in defs[name]
        assert f"({', '.join(columns)})" in defs[name], defs[name]
        assert "UNIQUE" not in defs[name]

    # The one-approve-per-actor index is a PARTIAL UNIQUE index: it must carry its predicate, or
    # it would forbid an approver rejecting after approving (which the console offers).
    # (The predicate is read from after `WHERE`: the index's own NAME contains "approve", so a
    # substring check on the whole definition passes for any predicate.)
    ddl = defs[APPROVE_INDEX]
    assert "UNIQUE" in ddl and "(approval_request_id, actor_id)" in ddl
    assert " WHERE " in ddl, ddl
    predicate = ddl.split(" WHERE ", 1)[1]
    assert "decision" in predicate and "'approve'" in predicate and "'reject'" not in predicate, predicate


def test_p4_stepping_down_past_the_constraint_revisions_removes_exactly_their_indexes(scratch_postgres_url):
    cfg = parity.config(scratch_postgres_url)
    command.upgrade(cfg, "head")

    def index_names() -> set[str]:
        engine = create_engine(scratch_postgres_url)
        try:
            with engine.connect() as conn:
                return {
                    r.indexname
                    for r in conn.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"))
                }
        finally:
            engine.dispose()

    assert set(T089_INDEXES) <= index_names()

    command.downgrade(cfg, ONE_APPROVE_REVISION)  # undoes the secondary-index revision only
    names = index_names()
    assert set(T089_INDEXES).isdisjoint(names)
    assert APPROVE_INDEX in names

    command.downgrade(cfg, PARENT)  # undoes the one-approve-per-actor revision
    assert APPROVE_INDEX not in index_names()

    command.upgrade(cfg, INDEXES_REVISION)
    assert (set(T089_INDEXES) | {APPROVE_INDEX}) <= index_names()
