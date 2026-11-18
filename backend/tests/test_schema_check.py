"""the API server and the worker refuse to start unless the platform
DB is exactly at the Alembic head, and nothing in `app/` creates tables.

Every database here is a `tmp_path` SQLite file (never `:memory:`, never the
dev DB, never ambient `.env`). The lifespan is driven directly with
`asyncio.run` -- `TestClient` is never entered.
"""

import ast
import asyncio
import shutil
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import app.main as app_main
import app.worker as worker
from app.config import settings
from app.models.base import Base
from app.models.template import Template
from app.schema_check import MIGRATIONS_DIR, SchemaNotAtHeadError, alembic_config, assert_schema_at_head

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _url(db_file: Path) -> str:
    return f"sqlite:///{db_file.as_posix()}"


@pytest.fixture()
def make_engine():
    made = []

    def _make(db_file: Path):
        engine = create_engine(_url(db_file), connect_args={"check_same_thread": False})
        made.append(engine)
        return engine

    yield _make
    for engine in made:
        engine.dispose()


def _script() -> ScriptDirectory:
    # Deliberately NOT `app.schema_check.read_head`: the expected revision comes
    # from alembic directly, so a wrong head in the check cannot agree with itself.
    return ScriptDirectory.from_config(alembic_config("sqlite://"))


def _head() -> str:
    (head,) = _script().get_heads()
    return head


def _parent_of_head() -> str:
    parent = _script().get_revision(_head()).down_revision
    assert isinstance(parent, str)
    return parent


def _migrated(db_file: Path, revision: str = "head") -> None:
    command.upgrade(alembic_config(_url(db_file)), revision)


def _unstamped_with_tables(db_file: Path, make_engine):
    """The way the bug arises: an older server's `create_all` on a fresh file."""
    engine = make_engine(db_file)
    Base.metadata.create_all(bind=engine)
    return engine


def _message(engine, **kw) -> str:
    with pytest.raises(SchemaNotAtHeadError) as excinfo:
        assert_schema_at_head(engine, **kw)
    return str(excinfo.value)


# --- (b) the check, one test per state -------------------------------------


def test_b1_at_head_passes(tmp_path, make_engine):
    db_file = tmp_path / "head.db"
    _migrated(db_file)

    assert_schema_at_head(make_engine(db_file))


def test_b2_unstamped_with_tables_names_the_cause_and_the_dev_remedy(tmp_path, make_engine):
    engine = _unstamped_with_tables(tmp_path / "unstamped.db", make_engine)

    msg = _message(engine)

    assert "UNSTAMPED DATABASE" in msg
    assert "created outside migrations" in msg
    assert "discard the DB file and re-run dev.ps1" in msg
    assert "python -m app.demo_data" in msg
    assert "never stamp blindly" in msg
    assert msg.endswith("There is no override flag.")


def test_b2b_an_empty_alembic_version_table_with_tables_is_also_unstamped(tmp_path, make_engine):
    db_file = tmp_path / "emptyversion.db"
    _migrated(db_file)
    engine = make_engine(db_file)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM alembic_version"))

    assert "UNSTAMPED DATABASE" in _message(engine)


def test_b3_empty_database_says_run_upgrade(tmp_path, make_engine):
    engine = make_engine(tmp_path / "empty.db")

    msg = _message(engine)

    assert "EMPTY DATABASE" in msg
    assert "no tables" in msg
    assert "alembic upgrade head" in msg
    assert "UNSTAMPED" not in msg
    assert msg.endswith("There is no override flag.")


def test_b4_behind_names_both_current_and_head(tmp_path, make_engine):
    db_file = tmp_path / "behind.db"
    parent = _parent_of_head()
    _migrated(db_file, parent)

    msg = _message(make_engine(db_file))

    assert "SCHEMA AT ANOTHER REVISION" in msg
    assert f"is at {parent}" in msg
    assert f"migration head is {_head()}" in msg
    assert msg.endswith("There is no override flag.")


def test_b5_unknown_revision_names_it_and_the_head(tmp_path, make_engine):
    db_file = tmp_path / "unknown.db"
    _migrated(db_file)
    engine = make_engine(db_file)
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = 'deadbeef0000'"))

    msg = _message(engine)

    assert "SCHEMA AT ANOTHER REVISION" in msg
    assert "is at deadbeef0000" in msg
    assert f"migration head is {_head()}" in msg


def test_b6_more_than_one_row_is_not_at_head_even_if_one_is_the_head(tmp_path, make_engine):
    db_file = tmp_path / "tworows.db"
    _migrated(db_file)
    engine = make_engine(db_file)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('deadbeef0000')"))

    msg = _message(engine)

    assert "SCHEMA AT ANOTHER REVISION" in msg
    assert "deadbeef0000" in msg
    assert _head() in msg


def test_b7_the_three_message_families_are_pairwise_distinct(tmp_path, make_engine):
    unstamped = _message(_unstamped_with_tables(tmp_path / "u.db", make_engine))
    empty = _message(make_engine(tmp_path / "e.db"))
    behind_file = tmp_path / "b.db"
    _migrated(behind_file, _parent_of_head())
    elsewhere = _message(make_engine(behind_file))

    msgs = [unstamped, empty, elsewhere]
    assert len({*msgs}) == 3
    # Each family's marker appears in its own message and in neither other one.
    markers = ["UNSTAMPED DATABASE", "EMPTY DATABASE", "SCHEMA AT ANOTHER REVISION"]
    for i, msg in enumerate(msgs):
        for j, marker in enumerate(markers):
            assert (marker in msg) == (i == j), f"message {i} / marker {j}"
    # The destructive remedy belongs to the unstamped message alone.
    assert "discard the DB file" in unstamped
    assert "discard the DB file" not in empty
    assert "discard the DB file" not in elsewhere


def test_b8_the_head_is_read_from_the_script_directory_at_runtime(tmp_path, make_engine):
    db_file = tmp_path / "real_head.db"
    _migrated(db_file)
    real_head = _head()

    scripts = tmp_path / "migrations_copy"
    shutil.copytree(MIGRATIONS_DIR, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    (scripts / "versions" / "zz_extra_revision.py").write_text(
        'revision = "zz0000extra"\n'
        f'down_revision = "{real_head}"\n'
        "branch_labels = None\n"
        "depends_on = None\n"
        "def upgrade() -> None:\n    pass\n"
        "def downgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )
    engine = make_engine(db_file)

    assert_schema_at_head(engine)  # the real directory: at head
    msg = _message(engine, script_location=scripts)  # a directory with one more revision: behind

    assert "SCHEMA AT ANOTHER REVISION" in msg
    assert f"is at {real_head}" in msg
    assert "migration head is zz0000extra" in msg


# --- (c1) nothing in app/ creates tables -----------------------------------


def _create_all_calls(source: str) -> list[int]:
    """Line numbers of every call whose callee is named `create_all`, however
    it is reached (`Base.metadata.create_all(...)`, `metadata.create_all(...)`, a
    bare `create_all(...)`)."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
            if name == "create_all":
                lines.append(node.lineno)
    return lines


def test_c1_scanner_finds_create_all_however_it_is_called():
    # The scan below proves an absence; this proves it could have found a presence.
    assert _create_all_calls("Base.metadata.create_all(bind=engine)\n") == [1]
    assert _create_all_calls("x = 1\nmetadata.create_all(engine)\n") == [2]
    assert _create_all_calls("create_all(engine)\n") == [1]
    assert _create_all_calls("Base.metadata.drop_all(bind=engine)\n# create_all\n") == []


def test_c1_no_create_all_call_anywhere_in_app():
    offenders = []
    sources = sorted(APP_DIR.rglob("*.py"))
    assert len(sources) > 20, "the scan found no app/ sources -- wrong directory?"
    for path in sources:
        for lineno in _create_all_calls(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(APP_DIR.parent)}:{lineno}")

    assert offenders == [], f"only Alembic creates the platform schema; create_all called at {offenders}"


# --- (c2) the lifespan ------------------------------------------------------


async def _run_lifespan():
    async with app_main.lifespan(app_main.app):
        pass


@pytest.fixture()
def redirect_startup(monkeypatch, make_engine):
    """Point `app.main`'s module-global `engine`/`SessionLocal` (the names the
    lifespan reads) at a temp file, as test_deploy_traefik_integration does."""

    def _redirect(db_file: Path):
        engine = make_engine(db_file)
        monkeypatch.setattr(app_main, "engine", engine)
        monkeypatch.setattr(app_main, "SessionLocal", sessionmaker(bind=engine, autoflush=False))
        return engine

    return _redirect


def test_c2_startup_refuses_an_unstamped_db_and_does_not_seed_it(tmp_path, redirect_startup, make_engine):
    db_file = tmp_path / "unstamped_startup.db"
    engine = _unstamped_with_tables(db_file, make_engine)
    redirect_startup(db_file)

    with pytest.raises(SchemaNotAtHeadError, match="UNSTAMPED DATABASE"):
        asyncio.run(_run_lifespan())

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM templates")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM users")).scalar() == 0


def test_c2_startup_refuses_an_empty_db_and_creates_no_tables(tmp_path, redirect_startup):
    db_file = tmp_path / "empty_startup.db"
    engine = redirect_startup(db_file)

    with pytest.raises(SchemaNotAtHeadError, match="EMPTY DATABASE"):
        asyncio.run(_run_lifespan())

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'")).scalar() == 0


def test_c2_startup_on_an_at_head_db_succeeds_and_seeds(tmp_path, redirect_startup):
    db_file = tmp_path / "head_startup.db"
    _migrated(db_file)
    engine = redirect_startup(db_file)

    asyncio.run(_run_lifespan())

    with sessionmaker(bind=engine)() as db:
        assert db.query(Template).count() >= 1


def test_c2_order_is_check_then_seed_then_bootstrap_admin(tmp_path, redirect_startup, monkeypatch):
    calls: list[str] = []
    redirect_startup(tmp_path / "order.db")
    monkeypatch.setattr(app_main, "assert_schema_at_head", lambda engine: calls.append("check"))
    monkeypatch.setattr(app_main, "seed", lambda db: calls.append("seed"))
    monkeypatch.setattr(app_main, "ensure_local_bootstrap_admin", lambda db: calls.append("bootstrap"))
    monkeypatch.setattr(settings, "auth_mode", "local")

    asyncio.run(_run_lifespan())

    assert calls == ["check", "seed", "bootstrap"]


# --- (c3) the worker --------------------------------------------------------


class _LoopReached(BaseException):
    """Raised by the stubbed run_iteration so main() never actually loops.

    A `BaseException`: the worker loop now survives an `Exception`
    out of one iteration (it logs it and goes round again), so an `Exception`
    subclass would no longer get out of `main()` -- the test would loop forever."""


@pytest.fixture()
def worker_on(monkeypatch, make_engine):
    def _redirect(db_file: Path):
        engine = make_engine(db_file)
        monkeypatch.setattr(worker, "engine", engine)
        monkeypatch.setattr(worker, "SessionLocal", sessionmaker(bind=engine, autoflush=False))
        return engine

    def _stop(*args, **kwargs):
        raise _LoopReached

    monkeypatch.setattr(worker, "run_iteration", _stop)
    return _redirect


def test_c3_worker_main_refuses_an_unstamped_db_before_its_loop(tmp_path, worker_on, make_engine):
    db_file = tmp_path / "unstamped_worker.db"
    _unstamped_with_tables(db_file, make_engine)
    worker_on(db_file)

    with pytest.raises(SchemaNotAtHeadError, match="UNSTAMPED DATABASE"):
        worker.main()  # would raise _LoopReached instead if the check did not come first


def test_c3_worker_main_reaches_its_loop_on_an_at_head_db(tmp_path, worker_on):
    db_file = tmp_path / "head_worker.db"
    _migrated(db_file)
    worker_on(db_file)

    with pytest.raises(_LoopReached):
        worker.main()
