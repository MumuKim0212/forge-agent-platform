import logging
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db_url import normalise_database_url

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
# a bare `postgresql://` means the installed psycopg v3 driver.
engine = create_engine(normalise_database_url(settings.database_url), connect_args=connect_args)


if settings.database_url.startswith("sqlite"):
    # In-memory SQLite (used by the test fixture, `sqlite:///:memory:` or
    # `sqlite://`) has no file to apply WAL to and errors if asked; only
    # file-backed dev DBs get WAL.
    _is_file_backed_sqlite = ":memory:" not in settings.database_url

    # scoped to THIS module's own `engine`, not the `Engine` CLASS.
    # `event.listens_for(Engine, "connect")` (no specific engine) subscribes
    # process-wide, so in dev mode (SQLite platform DB) it would ALSO fire
    # this SQLite-only PRAGMA against the shared-Postgres admin connection
    # `services/service_data.py` opens in the SAME process — a real
    # production defect, not only a test artifact (it surfaced first via
    # tests/conftest.py's identical mistake, fixed alongside this one).
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        if _is_file_backed_sqlite:
            # the worker (separate process) and the API both hold
            # connections to the same dev SQLite file; WAL lets readers and
            # a single writer coexist without "database is locked" errors.
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


logger = logging.getLogger("app.db")


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def release_connection(db) -> None:
    """ends the read-only transaction a handler's first query began, so its pooled
    connection goes back to the pool NOW instead of when the request ends. For a handler that
    reads, then waits on something slow (the proxy routes' `docker exec`), then needs the session
    again or not at all: the session opens a new transaction by itself on its next use, and
    anything it loaded is re-read from the database when touched again.

    Never raises. It is housekeeping on the way to the real work, and a connection too broken to
    roll back is one `get_db`'s own `close()` meets again at the end of the request; the work in
    between must not fail because of it (`test_service_proxy_still_serves_traffic_when_the_activity_
    commit_and_rollback_both_fail` is that case). Not for a handler with uncommitted changes: it
    rolls them back."""
    try:
        db.rollback()
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning("could not return a database connection to the pool early", exc_info=True)
