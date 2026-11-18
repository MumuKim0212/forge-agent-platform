"""the one place the migration-parity checks are spelled, so the SQLite
test (`test_migrations_parity.py`) and the Postgres one (`test_migrations_parity_postgres.py`)
cannot drift apart. Both call these with a database URL; neither reads the ambient `.env`.

Why a Postgres twin exists at all: production is Postgres, the parity test ran on SQLite
only, and the `postgresql_using` / `postgresql_where` branches in the chain never executed
anywhere. What a URL-parameterised helper cannot hide is the dialect: a
migration that only works on SQLite fails in the Postgres run, not in this file.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app.models.base import Base
from app.schema_check import alembic_option_value
from app.models import (  # noqa: F401  (registers every model on Base.metadata)
    approval,
    audit,
    build,
    change_request,
    deployment,
    policy_verdict,
    project,
    template,
    user,
)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def sqlite_url(db_file: Path) -> str:
    return f"sqlite:///{db_file.as_posix()}"


def config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", alembic_option_value(str(MIGRATIONS_DIR)))
    cfg.set_main_option("sqlalchemy.url", alembic_option_value(url))
    return cfg


def head() -> str:
    heads = ScriptDirectory.from_config(config("sqlite://")).get_heads()
    assert len(heads) == 1
    return heads[0]


def chain(cfg: Config) -> list[str]:
    """Every revision, base -> head."""
    script = ScriptDirectory.from_config(cfg)
    return [r.revision for r in reversed(list(script.walk_revisions()))]


def diff(url: str) -> list:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return compare_metadata(MigrationContext.configure(conn), Base.metadata)
    finally:
        engine.dispose()


def stamped_revisions(url: str) -> list[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return [r[0] for r in conn.execute(text("SELECT version_num FROM alembic_version"))]
    finally:
        engine.dispose()


def snapshot(url: str) -> dict:
    """The structure of every table but the version table, as plain data: it must be equal for
    'migrated up to R' and 'migrated up past R, then down to R'. Reflected, not read from
    `sqlite_master` text, because SQLite batch operations rewrite a table's DDL without changing
    what it is. An index's dialect options are part of it (a partial index's predicate),
    so a downgrade that leaves a plain index where a partial one was is not 'the same schema'."""
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        out = {}
        for table in sorted(set(insp.get_table_names()) - {"alembic_version"}):
            out[table] = {
                "columns": sorted(
                    (c["name"], str(c["type"]), c["nullable"], str(c["default"]))
                    for c in insp.get_columns(table)
                ),
                "pk": sorted(insp.get_pk_constraint(table)["constrained_columns"]),
                "fks": sorted(
                    (tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]))
                    for f in insp.get_foreign_keys(table)
                ),
                "indexes": sorted(
                    (
                        i["name"],
                        tuple(i["column_names"]),
                        bool(i["unique"]),
                        repr(sorted((k, str(v)) for k, v in (i.get("dialect_options") or {}).items())),
                    )
                    for i in insp.get_indexes(table)
                ),
                "uniques": sorted(
                    tuple(u["column_names"]) for u in insp.get_unique_constraints(table)
                ),
            }
        return out
    finally:
        engine.dispose()


def assert_p1_head_builds_exactly_the_models(url: str) -> None:
    command.upgrade(config(url), "head")

    drift = diff(url)

    # The failure message IS the drift: print it, don't make anyone re-derive it.
    assert drift == [], f"migrations and Base.metadata disagree: {drift!r}"


def assert_p2_every_downgrade_works_and_the_chain_rebuilds(url: str) -> None:
    """Upgrade one revision at a time recording the schema at each, then downgrade one at a
    time and require each step to land on exactly the schema recorded at that parent. Going
    straight head -> base would pass even with a no-op `downgrade()`, because the initial
    migration drops whole tables and takes a leftover column with it (planted and watched
    pass)."""
    cfg = config(url)
    revisions = chain(cfg)
    assert revisions[-1] == head()

    at = {"base": {}}
    for rev in revisions:
        command.upgrade(cfg, rev)
        at[rev] = snapshot(url)

    parents = ["base"] + revisions[:-1]
    for rev, parent in zip(reversed(revisions), reversed(parents)):
        command.downgrade(cfg, "-1")
        assert snapshot(url) == at[parent], f"downgrading {rev} did not restore the schema at {parent}"

    assert snapshot(url) == {}
    command.upgrade(cfg, "head")

    assert stamped_revisions(url) == [head()]
    assert snapshot(url) == at[revisions[-1]]
    drift = diff(url)
    assert drift == [], f"after upgrade->downgrade->upgrade, migrations and Base.metadata disagree: {drift!r}"
