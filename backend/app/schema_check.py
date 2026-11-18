"""Startup check: the platform DB must be exactly at the Alembic head.

Alembic is the only thing that creates or alters the platform schema outside
the in-memory test fixture. The API server and the worker call
`assert_schema_at_head` before they do anything else and refuse to start
otherwise. The server never migrates by itself: migrating is an operator
action with a backup in front of it (before a real
install), not a side effect of a restart, and two processes starting together
would race.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# Resolved from the package location, never the working directory: uvicorn and
# the worker are started from wherever the operator happens to be.
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_VERSION_TABLE = "alembic_version"
_NO_OVERRIDE = "There is no override flag."


class SchemaNotAtHeadError(RuntimeError):
    """The platform DB is not exactly at the migration head, so the process
    refuses to start.

    One class covers several conditions, each with its own message and its own
    remedy -- they must not share a message, because the remedies differ and
    the wrong one is destructive in one direction (re-creating a database an
    operator needed) or silently permanent in the other (a wrong `alembic
    stamp`):

    - unstamped, with platform tables present: created outside migrations;
    - empty: no platform tables yet;
    - stamped at some other revision: behind, ahead, unknown, or several rows;
    - the migration chain itself has more than one head (a code defect that the
      parity test catches first).

    Every message ends "There is no override flag." There is none, in dev or in
    prod."""


def alembic_option_value(value: str) -> str:
    """`value`, made safe to hand to `Config.set_main_option`.

    Alembic's `Config` is a `configparser`, so a value goes through
    %-interpolation: a lone `%` (a percent-encoded password -- any of `@ : / #
    ? %` in one has to be encoded) raises `ValueError: invalid interpolation
    syntax in '<the whole value>'`, and that message carries the whole URL,
    password included, to stderr or a CI log. Doubling every `%` makes
    `get_main_option` hand back the original. This is the only place that
    does it, and `tests/test_alembic_options_are_escaped.py` requires every
    `set_main_option` call in the tree to go through it."""
    return value.replace("%", "%%")


def alembic_config(url: str, script_location: Path | str = MIGRATIONS_DIR) -> Config:
    """An in-memory Alembic `Config` pinned to `url`. No `alembic.ini` is read,
    so no logging is reconfigured and the working directory does not matter.
    `migrations/env.py` honours the URL set here before it falls back to
    `settings.database_url`."""
    cfg = Config()
    cfg.set_main_option("script_location", alembic_option_value(str(script_location)))
    cfg.set_main_option("sqlalchemy.url", alembic_option_value(url))
    return cfg


def read_head(script_location: Path | str = MIGRATIONS_DIR) -> str:
    """The head revision, read from the script directory at runtime -- adding a
    migration needs no edit here."""
    cfg = Config()
    cfg.set_main_option("script_location", alembic_option_value(str(script_location)))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    if len(heads) != 1:
        raise SchemaNotAtHeadError(
            f"The migration chain has {len(heads)} heads ({', '.join(sorted(heads)) or 'none'}); "
            "it must be one linear chain. This is a code defect in the migrations directory, "
            f"not a database problem. {_NO_OVERRIDE}"
        )
    return heads[0]


def assert_schema_at_head(engine: Engine, script_location: Path | str = MIGRATIONS_DIR) -> None:
    head = read_head(script_location)

    tables = set(inspect(engine).get_table_names())
    platform_tables = tables - {_VERSION_TABLE}

    revisions: list[str] = []
    if _VERSION_TABLE in tables:
        with engine.connect() as conn:
            revisions = [row[0] for row in conn.execute(text(f"SELECT version_num FROM {_VERSION_TABLE}"))]

    if not revisions:
        if platform_tables:
            raise SchemaNotAtHeadError(
                "UNSTAMPED DATABASE: the platform DB has tables but no Alembic revision recorded. "
                "It was created outside migrations -- by an older server's create_all, or by hand -- "
                "so nothing can say which schema it actually has, and `alembic upgrade head` will fail "
                "on it (tables already exist). "
                "In dev: discard the DB file and re-run dev.ps1 (it migrates a fresh one), then "
                "repopulate with `python -m app.demo_data`. "
                f"In prod: an operator may `alembic stamp` it only after verifying its schema matches "
                f"a specific revision (head is {head}); never stamp blindly. {_NO_OVERRIDE}"
            )
        raise SchemaNotAtHeadError(
            "EMPTY DATABASE: the platform DB has no tables. "
            f"Run `alembic upgrade head` (from the backend directory) to create the schema (head is {head}), "
            f"then start the process again. The server does not migrate by itself. {_NO_OVERRIDE}"
        )

    if revisions != [head]:
        current = ", ".join(sorted(revisions))
        raise SchemaNotAtHeadError(
            f"SCHEMA AT ANOTHER REVISION: the platform DB is at {current}, but the migration head is {head} "
            "(behind, ahead of this code, an unknown revision, or more than one revision row). "
            "Take a backup, then run `alembic upgrade head` if it is behind; if it is ahead or unknown, "
            f"this code is older than the database and must be updated instead. {_NO_OVERRIDE}"
        )
