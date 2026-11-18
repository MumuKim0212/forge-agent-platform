# Not protected -- this is the kind of file the agent is expected to
# replace/extend inside the template shape (template = structure,
# not feature). Ships as part of the data contract seed.
#
# Database engine for the generated service (ADR 0023 §Data
# services / §Schema migrations).
#
# Reads `DATABASE_URL` from the environment by that exact name -- the
# platform issues it as part of the service env at first deploy
# ("the template contract defines the
# variable names; generated code consumes them by name only") and never
# chooses a driver for it: the value arrives as a bare `postgresql://` DSN
# on purpose. This module picks psycopg (v3) and
# rewrites the scheme itself, exactly as the platform does for its own
# admin connection (`app/services/service_data.py` on the platform side --
# a pattern to imitate, not a dependency: nothing here imports from the
# platform's own `app/` package).
#
# Fails at IMPORT time -- no fallback, no SQLite, no in-memory mode -- when
# `DATABASE_URL` is absent or does not parse as a URL. A generated service
# must never start against a database it does not have.
# `alembic/env.py` (this template's own migrations, see
# `migrations/env.py`) calls `resolve_database_url()` directly rather than
# importing the module-level `DATABASE_URL` constant below, but the two
# resolve identically -- one function, one rule, read from two places.

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import sessionmaker

# SQLAlchemy's bare `postgresql://` scheme resolves to psycopg2 by default,
# which this template does not install -- psycopg (v3) is the driver this
# template picks, so every DATABASE_URL is rewritten
# onto this drivername explicitly rather than depending on that default.
_DRIVER = "postgresql+psycopg"


class DatabaseConfigurationError(RuntimeError):
    """Raised when DATABASE_URL is missing, blank, or unparseable. No
    silent fallback exists for this condition -- see module docstring."""


def resolve_database_url() -> str:
    raw = os.environ.get("DATABASE_URL")
    if raw is None or not raw.strip():
        raise DatabaseConfigurationError(
            "DATABASE_URL is not set. The platform issues this value as "
            "part of the service env at first deploy -- a generated service cannot start "
            "without its own database."
        )
    try:
        url = make_url(raw)
    except ArgumentError as exc:
        raise DatabaseConfigurationError(
            f"DATABASE_URL could not be parsed: {exc}"
        ) from exc
    # NOT str(url) -- SQLAlchemy's URL.__str__ redacts the password as
    # "***" (a safety default for printing/logging a URL). render_as_
    # string(hide_password=False) is the only way to get a connectable
    # DSN back out; confirmed by hand against a real Postgres container
    # -- str(url) silently produced a URL that failed
    # `psycopg.OperationalError: ... password authentication failed`
    # against the CORRECT password, because the string handed to the
    # driver was the literal 3 characters "***".
    return url.set(drivername=_DRIVER).render_as_string(hide_password=False)


DATABASE_URL = resolve_database_url()
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
