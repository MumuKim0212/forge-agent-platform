"""A percent-encoded database password must not break `alembic upgrade head`
nor be printed by it.

Alembic's `Config` is a `configparser`; an unescaped `%` in a value raises
`ValueError: invalid interpolation syntax in '<the whole URL>'`. A password containing
`@ : / # ? %` has to be percent-encoded, so on such an install the migration failed and the
message carried the password. Every test here uses a MARKER, never a real value, and the
failure paths are written so that a red run does not print the marker either (the exception
is reduced to its type name before any assert sees it).
"""

import ast
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.config import settings
from app.schema_check import MIGRATIONS_DIR, alembic_option_value

BACKEND_DIR = Path(__file__).resolve().parent.parent
MARKER = "PWMARKER9F3A"
HELPER = "alembic_option_value"
_PRUNED = {".venv", "node_modules", "__pycache__", ".git", "worktrees"}


def _run_env_py(monkeypatch, url: str, *, offline: bool) -> str | None:
    """Drive `migrations/env.py` (no URL on the Config, so it falls back to settings) and return
    the exception TYPE NAME if it raised -- never the exception, whose text is the URL."""
    monkeypatch.setattr(settings, "database_url", url)
    cfg = Config()
    cfg.set_main_option("script_location", alembic_option_value(str(MIGRATIONS_DIR)))
    failure = None
    try:
        if offline:
            # Only the first revision: later ones move data and need a live connection.
            first = ScriptDirectory.from_config(cfg).get_bases()[0]
            command.upgrade(cfg, f"base:{first}", sql=True)
        else:
            command.upgrade(cfg, "head")
    except Exception as exc:  # noqa: BLE001 - reduced to a type name on purpose
        failure = type(exc).__name__
    return failure


def test_env_py_runs_with_a_percent_encoded_postgres_password_and_prints_no_marker(monkeypatch, capsys):
    # Offline (`--sql`): env.py builds its dialect from the URL and emits SQL, with no server.
    # (Offline cannot run the whole chain -- a data-moving revision needs a connection.)
    url = f"postgresql://forge:p%40{MARKER}@db.invalid:5432/forge"

    failure = _run_env_py(monkeypatch, url, offline=True)
    captured = capsys.readouterr()

    assert failure is None
    assert MARKER not in captured.out
    assert MARKER not in captured.err
    assert "CREATE TABLE" in captured.out  # it really ran the chain


def test_env_py_migrates_a_sqlite_file_whose_url_contains_a_percent(monkeypatch, tmp_path, capsys):
    db_file = tmp_path / f"p%40{MARKER}.db"
    url = f"sqlite:///{db_file.as_posix()}"

    failure = _run_env_py(monkeypatch, url, offline=False)
    captured = capsys.readouterr()

    assert failure is None
    # SQLAlchemy does not percent-decode a sqlite path, but this test does not rely on that:
    # it asks only that the migration created a database file in the directory.
    assert [p for p in tmp_path.iterdir() if MARKER in p.name]
    assert MARKER not in captured.out
    assert MARKER not in captured.err


def test_the_helper_hands_the_original_value_back_through_a_config():
    original = f"postgresql://forge:p%40{MARKER}%25x@db.invalid/forge"
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", alembic_option_value(original))

    assert cfg.get_main_option("sqlalchemy.url") == original


# -- the guard: every `set_main_option` in the tree goes through the helper ---------------------


def _is_escaped(value: ast.expr, *, template: bool) -> bool:
    """The value argument must BE a call to the helper -- not a variable, which could hold anything.
    A generated service cannot import the platform's helper, so inside `templates/` the same effect
    spelled inline, `<expr>.replace("%", "%%")`, is accepted."""
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    if isinstance(func, ast.Name) and func.id == HELPER:
        return True
    if isinstance(func, ast.Attribute) and func.attr == HELPER:
        return True
    if template and isinstance(func, ast.Attribute) and func.attr == "replace":
        literals = [a.value for a in value.args if isinstance(a, ast.Constant)]
        return literals == ["%", "%%"]
    return False


def _unescaped_calls(source: str, *, template: bool) -> list[int]:
    bad = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"set_main_option", "set_section_option"}
        ):
            value = node.args[-1] if node.args else None
            if value is None or not _is_escaped(value, template=template):
                bad.append(node.lineno)
    return bad


def _python_files() -> list[Path]:
    return sorted(
        p for p in BACKEND_DIR.rglob("*.py") if not (_PRUNED & set(p.relative_to(BACKEND_DIR).parts))
    )


def test_every_set_main_option_call_goes_through_the_escape_helper():
    this_file = Path(__file__).resolve()
    found = 0
    offenders = []
    for path in _python_files():
        if path.resolve() == this_file:
            continue  # its own self-test snippets are deliberately wrong
        source = path.read_text(encoding="utf-8")
        if "set_main_option" not in source and "set_section_option" not in source:
            continue
        relative = path.relative_to(BACKEND_DIR)
        is_template = relative.parts[0] == "templates"
        calls = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr in {"set_main_option", "set_section_option"}]
        found += len(calls)
        offenders += [f"{relative.as_posix()}:{line}" for line in _unescaped_calls(source, template=is_template)]

    assert found >= 8, "the scan found too few set_main_option calls -- it is not looking where it should"
    assert offenders == []


@pytest.mark.parametrize(
    "snippet",
    [
        'cfg.set_main_option("sqlalchemy.url", url)',
        'cfg.set_main_option("sqlalchemy.url", settings.database_url)',
        'cfg.set_main_option("sqlalchemy.url", safe)',  # a variable could hold anything
        'cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))',  # inline: templates only
    ],
)
def test_the_scan_flags_an_unescaped_call(snippet):
    assert _unescaped_calls(snippet, template=False) == [1]


def test_the_scan_accepts_the_helper_and_the_template_inline_form():
    assert _unescaped_calls('cfg.set_main_option("k", alembic_option_value(url))', template=False) == []
    assert _unescaped_calls('cfg.set_main_option("k", mod.alembic_option_value(url))', template=False) == []
    assert _unescaped_calls('cfg.set_main_option("k", url.replace("%", "%%"))', template=True) == []
    assert _unescaped_calls('cfg.set_main_option("k", url)', template=True) == [1]
