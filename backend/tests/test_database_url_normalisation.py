"""a bare `postgresql://` platform URL works with the installed
psycopg v3 driver. Every expected value is a literal."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy.exc import OperationalError

from app.db_url import normalise_database_url
from app.schema_check import alembic_config

BACKEND = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgresql://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
        ("postgres://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        # an explicit driver is the operator's choice, left alone
        ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ("postgresql+asyncpg://u:p@h/d", "postgresql+asyncpg://u:p@h/d"),
        # SQLite untouched, in all its shapes
        ("sqlite:///./forge_dev.db", "sqlite:///./forge_dev.db"),
        ("sqlite://", "sqlite://"),
        ("sqlite:///:memory:", "sqlite:///:memory:"),
        # the rest of the URL, a percent-encoded password included, is byte for byte the input
        ("postgresql://u:p%40ss%25@h/d?sslmode=require", "postgresql+psycopg://u:p%40ss%25@h/d?sslmode=require"),
    ],
)
def test_url_shapes(given, expected):
    result = normalise_database_url(given)
    assert result == expected


def test_a_scheme_that_merely_contains_postgresql_is_not_rewritten():
    given = "mypostgresql://u:p@h/d"
    result = normalise_database_url(given)
    assert result == "mypostgresql://u:p@h/d"


def test_the_process_engine_is_built_on_the_psycopg_driver_from_a_postgres_scheme_url():
    """`app/db.py` at import, in a fresh interpreter: the env var beats `.env`, and `create_engine`
    does not connect, so no database is needed. The `postgres://` spelling is refused by SQLAlchemy 2
    itself (`NoSuchModuleError`), so without the normalisation this import fails."""
    env = {**os.environ, "FORGE_ENV": "dev", "DATABASE_URL": "postgres://u:pw@platform-db.invalid:5432/forge"}
    done = subprocess.run(
        [sys.executable, "-c", "import app.db as d; print(d.engine.url.drivername)"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-800:]
    assert done.stdout.strip() == "postgresql+psycopg"


def test_alembics_env_normalises_the_url_it_runs_on():
    """`migrations/env.py` is run for real (`alembic current`) against an unresolvable host, with the
    `postgres://` spelling. With the normalisation the psycopg v3 driver loads and the connect fails as
    an `OperationalError`; without it SQLAlchemy cannot even load the dialect (`NoSuchModuleError`)."""
    cfg = alembic_config("postgres://u:pw@platform-db.invalid:5432/forge?connect_timeout=2")
    with pytest.raises(OperationalError):
        command.current(cfg)
