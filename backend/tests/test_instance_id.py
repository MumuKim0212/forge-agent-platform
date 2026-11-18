"""ADR 0037 §Decision 2: the installation's id.

It is stored in the platform database (`platform_instance`, one row), generated the first
time it is asked for and read ever after, so it is stable for one installation and different
for another. The worker binds it at start; the test suite pins its own per run
(`tests/instance_scope.py`).

`tmp_path` SQLite files only (the migration needs a file that survives connections); nothing
here reads the ambient `.env` or touches the dev DB. Model == migration for the whole chain is
`test_migrations_parity.py`'s; this file owns the id's behaviour and the table's shape.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.platform_instance import PlatformInstance
from app.services import platform_labels
from app.services.instance_id import bind_instance_id, get_or_create_instance_id
from tests.instance_scope import TEST_INSTANCE_ID

from app.schema_check import alembic_option_value

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
PARENT = "e9b4d27a6c13"
REVISION = "b3f6a1d94c28"


def _sessions(tmp_path: Path, name: str):
    engine = create_engine(f"sqlite:///{(tmp_path / name).as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False), engine


def test_the_id_is_created_on_first_use_and_is_the_same_ever_after(tmp_path):
    make_session, engine = _sessions(tmp_path, "a.db")
    with make_session() as db:
        first = get_or_create_instance_id(db)
    with make_session() as db:
        second = get_or_create_instance_id(db)
        third = get_or_create_instance_id(db)
        rows = db.query(PlatformInstance).count()
    engine.dispose()

    assert first == second == third
    assert rows == 1


def test_the_stored_id_is_a_bare_uuid_hex_never_derived_from_the_host(tmp_path):
    make_session, engine = _sessions(tmp_path, "a.db")
    with make_session() as db:
        value = get_or_create_instance_id(db)
    engine.dispose()

    assert re.fullmatch(r"[0-9a-f]{32}", value)


def test_two_installations_get_two_ids(tmp_path):
    """A second checkout on the same daemon has its own database, so its own id, with nothing to
    configure."""
    make_a, engine_a = _sessions(tmp_path, "a.db")
    make_b, engine_b = _sessions(tmp_path, "b.db")
    with make_a() as db:
        a = get_or_create_instance_id(db)
    with make_b() as db:
        b = get_or_create_instance_id(db)
    engine_a.dispose()
    engine_b.dispose()

    assert a != b


def test_a_process_that_loses_the_creation_race_reads_the_winners_id(tmp_path, monkeypatch):
    """Two processes both find no row; one inserts first. The loser's insert hits the singleton
    primary key, it rolls back and returns what the winner stored."""
    make_session, engine = _sessions(tmp_path, "race.db")
    real_get = None

    with make_session() as winner:
        winner_value = get_or_create_instance_id(winner)

    with make_session() as loser:
        real_get = loser.get
        calls = {"n": 0}

        def blind_first_get(entity, ident, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # "no row yet", as the loser saw it before the winner committed
            return real_get(entity, ident, *args, **kwargs)

        monkeypatch.setattr(loser, "get", blind_first_get)
        value = get_or_create_instance_id(loser)
    engine.dispose()

    assert value == winner_value
    assert calls["n"] == 2  # it did go through the insert-refused path, and then re-read


def test_a_second_row_cannot_exist(tmp_path):
    make_session, engine = _sessions(tmp_path, "single.db")
    with make_session() as db:
        get_or_create_instance_id(db)
    with engine.begin() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text("INSERT INTO platform_instance (id, instance_id, created_at) VALUES ('other', 'x', '2026-01-01')")
            )
    engine.dispose()


def test_binding_hands_the_stored_id_to_the_label_helper(tmp_path, monkeypatch):
    make_session, engine = _sessions(tmp_path, "bind.db")
    monkeypatch.setattr(platform_labels, "_instance_id", None)
    with make_session() as db:
        bound = bind_instance_id(db)
        stored = get_or_create_instance_id(db)
    engine.dispose()

    assert bound == stored
    assert platform_labels.instance_id() == stored
    assert platform_labels.platform_labels("sandbox")["forge.instance"] == stored


def test_the_test_runs_id_can_never_be_a_stored_one():
    """The reason the suite's reconciler cannot reap the dev host's live objects and a dev
    worker's cannot reap a test's: a stored id is 32 hex characters; the suite's is not."""
    assert not re.fullmatch(r"[0-9a-f]{32}", TEST_INSTANCE_ID)
    assert TEST_INSTANCE_ID.startswith("forge-test-")


def test_the_suite_pins_its_own_id_before_every_test():
    assert platform_labels.instance_id() == TEST_INSTANCE_ID


def test_a_test_that_rebinds_the_id_is_undone_for_the_next_one():
    """`worker.main` binds the id it reads from the database; the autouse per-test pin undoes
    that, so no later test labels or reconciles under an id it did not choose. First half:
    rebind inside a test."""
    platform_labels.set_instance_id("bound-by-a-test")
    assert platform_labels.instance_id() == "bound-by-a-test"


def test_the_pin_was_restored_after_the_test_that_rebound_it():
    """Second half. Relies on running right after the one above (file order); on its own it is
    the same assertion as `test_the_suite_pins_its_own_id_before_every_test`."""
    assert platform_labels.instance_id() == TEST_INSTANCE_ID


# -- the migration -----------------------------------------------------------


def _config(db_file: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", alembic_option_value(str(MIGRATIONS_DIR)))
    cfg.set_main_option("sqlalchemy.url", alembic_option_value(f"sqlite:///{db_file.as_posix()}"))
    return cfg


def test_the_migration_creates_the_singleton_table_and_the_downgrade_drops_it(tmp_path):
    db_file = tmp_path / "mig.db"
    cfg = _config(db_file)
    command.upgrade(cfg, PARENT)
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    assert "platform_instance" not in inspect(engine).get_table_names()

    command.upgrade(cfg, REVISION)
    inspector = inspect(engine)
    assert "platform_instance" in inspector.get_table_names()
    assert {c["name"] for c in inspector.get_columns("platform_instance")} == {"id", "instance_id", "created_at"}
    # The singleton is enforced by the database: a second id is refused.
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO platform_instance (id, instance_id, created_at) VALUES ('platform', 'x', '2026-01-01')")
        )
        with pytest.raises(IntegrityError):
            conn.execute(
                text("INSERT INTO platform_instance (id, instance_id, created_at) VALUES ('second', 'y', '2026-01-01')")
            )

    command.downgrade(cfg, PARENT)
    assert "platform_instance" not in inspect(engine).get_table_names()
    engine.dispose()
