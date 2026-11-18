"""Canaries for `conftest._suite_db_guard`.

The guard exists so that no test can reach the platform owner's `forge_dev.db`.
A guard nobody can see working is a belief, so these tests reach the risk the
guard names: an alembic call with no URL, and the engine/session aliases that
`app.db` builds from the ambient `.env` at import time.
"""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

import app.db as app_db
import app.demo_data as app_demo_data
import app.main as app_main
import app.seed as app_seed
import app.worker as app_worker
from app.config import settings
from app.schema_check import alembic_option_value

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
SENTINEL_NAME = "must-not-be-used.db"


def _database_url_names_dev_db() -> bool:
    """Whether the ambient URL mentions the developer's dev database, as a bare boolean.

    Same reason as `tests/test_dev_instance_guard.py::_admin_dsn_is_unset` and `test_config.py`: a failing
    `assert` prints its operands, and `settings.database_url` is a credential field (it carries the
    password when the dev URL is a Postgres one). So no assertion in this file has the URL, or anything
    sliced from it, as an operand; a regression says that it did and names no value."""
    return "forge_dev" in settings.database_url


def _sentinel_path() -> Path:
    prefix = "sqlite:///"
    url = settings.database_url
    has_sqlite_form = url.startswith(prefix)
    assert has_sqlite_form, "the ambient database URL is not a sqlite:/// URL"
    return Path(url[len(prefix):])


@pytest.fixture(scope="module")
def view_at_module_fixture_time():
    """What a MODULE-scoped fixture sees. The traefik module's
    `backend_server_port` runs `alembic upgrade` at exactly this point, which
    pytest reaches before any function-scoped autouse fixture, so only the
    session-scoped guard can protect it. Strings only: nothing here touches a
    database."""
    return {
        "url": settings.database_url,
        "engines": {m.__name__: str(m.engine.url) for m in (app_db, app_main, app_worker)},
        "sessions": {
            m.__name__: str(m.SessionLocal.kw["bind"].url)
            for m in (app_db, app_main, app_worker, app_demo_data, app_seed)
        },
    }


def test_the_guard_is_already_in_force_when_a_module_scoped_fixture_runs(view_at_module_fixture_time):
    view = view_at_module_fixture_time
    url_is_the_sentinel = SENTINEL_NAME in view["url"] and "forge_dev" not in view["url"]
    assert url_is_the_sentinel, "the ambient database URL was not the throwaway file at module-fixture time"
    for where, url in {**view["engines"], **view["sessions"]}.items():
        alias_is_the_sentinel = SENTINEL_NAME in url and "forge_dev" not in url
        assert alias_is_the_sentinel, f"{where} was not pointed at the throwaway file at module-fixture time"


def test_the_ambient_database_url_is_the_throwaway_file_not_the_dev_db():
    sentinel = _sentinel_path()
    name_is_the_sentinel = sentinel.name == SENTINEL_NAME
    names_dev_db = _database_url_names_dev_db()
    already_exists = sentinel.exists()  # nothing has touched it yet in this test
    assert name_is_the_sentinel, "the ambient database URL does not name the throwaway file"
    assert not names_dev_db, "the ambient database URL names the dev database"
    assert not already_exists, "the throwaway file already exists before this test touched anything"


def test_alembic_with_no_url_builds_the_sentinel_file(tmp_path):
    cfg = Config()  # script_location only: no sqlalchemy.url, so env.py must fall back
    cfg.set_main_option("script_location", alembic_option_value(str(MIGRATIONS_DIR)))
    sentinel = _sentinel_path()
    # Before the call, not after: a failed assertion afterwards would come too late.
    name_is_the_sentinel = sentinel.name == SENTINEL_NAME
    names_dev_db = _database_url_names_dev_db()
    already_exists = sentinel.exists()
    assert name_is_the_sentinel and not names_dev_db, "alembic would not be pointed at the throwaway file"
    assert not already_exists, "the throwaway file already exists before the upgrade"

    command.upgrade(cfg, "head")

    built = sentinel.exists()
    assert built, "alembic with no URL did not build the throwaway file"
    with sqlite3.connect(sentinel) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"alembic_version", "users", "builds"} <= tables


def test_every_engine_and_session_alias_points_at_the_sentinel():
    for module in (app_db, app_main, app_worker):
        assert Path(module.engine.url.database).name == SENTINEL_NAME, module.__name__
    for module in (app_db, app_main, app_worker, app_demo_data, app_seed):
        bound = module.SessionLocal.kw["bind"]
        assert Path(bound.url.database).name == SENTINEL_NAME, module.__name__
