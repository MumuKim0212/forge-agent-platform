"""The SQL syntax check (ADR 0047 §Amendment 2026-11-25): what PostgreSQL's own grammar rejects in
the SQL a generated service executes, found without a database.

Pure unit tests over a temporary workspace. The first test is the incident: the code a build wrote
for `GET /bookings`, with the unquoted `end` column, in the two shapes it was written in (a string
passed to `text(...)` and a string assigned to a name that is then passed to it).
"""

from __future__ import annotations

import pytest

from app.services import sql_syntax_check as chk
from app.services.sql_syntax_check import (
    UNPRINTABLE_PATH,
    check_sql_syntax,
    clean_findings,
    has_sql_findings,
)

INCIDENT_APP = '''
from sqlalchemy import text
from db import SessionLocal


@app.post("/bookings")
def create_booking(room: str, start: str, end: str) -> dict:
    """Create a booking. Select a room and a time."""
    create_table_sql = (
        "CREATE TABLE IF NOT EXISTS bookings ("
        "id SERIAL PRIMARY KEY,"
        "room VARCHAR NOT NULL,"
        "start VARCHAR NOT NULL,"
        "end VARCHAR NOT NULL"
        ")"
    )
    with SessionLocal() as session:
        session.execute(text(create_table_sql))
        session.execute(
            text("INSERT INTO bookings (room, start, end) VALUES (:room, :start, :end) RETURNING id"),
            {"room": room, "start": start, "end": end},
        )
        rows = session.execute(text("SELECT id, room, start, end FROM bookings ORDER BY id")).all()
'''


def _write(root, relative: str, source: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def _check(tmp_path, source: str, relative: str = "app/app.py"):
    _write(tmp_path, relative, source)
    return check_sql_syntax(str(tmp_path))


def test_the_incident_code_is_rejected_at_each_statement_with_the_offending_word(tmp_path):
    findings = _check(tmp_path, INCIDENT_APP)

    # the CREATE TABLE (reached through the name it is assigned to), the INSERT and the SELECT:
    # three findings, each at the line of the argument passed to `text`.
    assert [f["error"] for f in findings] == ['syntax error at or near "end"'] * 3
    assert {f["path"] for f in findings} == {"app/app.py"}
    assert all(isinstance(f["line"], int) and f["line"] >= 1 for f in findings)
    assert len({f["line"] for f in findings}) == 3


def test_the_same_statements_with_the_word_quoted_are_clean(tmp_path):
    fixed = INCIDENT_APP.replace("end VARCHAR", '\\"end\\" VARCHAR').replace(", end)", ', \\"end\\")')
    fixed = fixed.replace("start, end FROM", 'start, \\"end\\" FROM')
    findings = _check(tmp_path, fixed)

    assert findings == []


def test_a_renamed_column_is_clean(tmp_path):
    source = (
        "from sqlalchemy import text\n"
        'def f(session):\n    session.execute(text("INSERT INTO b (room, ends_at) VALUES (:room, :e)"), {})\n'
    )
    assert _check(tmp_path, source) == []


def test_a_docstring_or_a_label_that_reads_like_sql_is_never_parsed(tmp_path):
    source = (
        '"""Select one or more rooms from the list, please."""\n'
        "LABEL = 'Select the room, then press the button to create the booking'\n"
        "def f():\n"
        '    """Create a booking from the form. Update it later."""\n'
        '    print("Select a room from")\n'
    )
    assert _check(tmp_path, source) == []


def test_text_only_counts_in_a_file_that_imports_sqlalchemy(tmp_path):
    source = 'def text(s):\n    return s\n\nlabel = text("hello world")\n'
    assert _check(tmp_path, source) == []


def test_execute_and_op_execute_and_exec_driver_sql_are_read(tmp_path):
    source = (
        "from alembic import op\n"
        "def upgrade(conn, cur):\n"
        '    op.execute("ALTER TABLE b ADD COLUMN end VARCHAR")\n'
        '    conn.exec_driver_sql("INSERT INTO t (user) VALUES (1)")\n'
        '    cur.execute("CREATE TABLE t (order int)")\n'
        '    cur.executemany("SELECT * FROM t WHERE a = %s", [(1,)])\n'
    )
    findings = _check(tmp_path, source, "app/migrations/versions/x.py")

    assert [f["error"] for f in findings] == [
        'syntax error at or near "end"',
        'syntax error at or near "user"',
        'syntax error at or near "order"',
    ]
    assert {f["path"] for f in findings} == {"app/migrations/versions/x.py"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t WHERE a = :a AND b::int = :b",  # named bind parameters and a cast
        "SELECT * FROM t WHERE a = %(a)s AND b = %s",  # psycopg placeholders
        "SELECT * FROM t WHERE at > '09:00' AND note = 'a:b'",  # colons that are not parameters
        "INSERT INTO t (a) VALUES (:a) RETURNING id",
        "SELECT 1; SELECT 2",
    ],
)
def test_valid_postgres_with_the_usual_placeholders_is_clean(tmp_path, sql):
    source = f"from sqlalchemy import text\ndef f(s):\n    s.execute(text({sql!r}))\n"
    assert _check(tmp_path, source) == []


def test_a_statement_built_at_run_time_is_not_measured(tmp_path):
    source = (
        "from sqlalchemy import text\n"
        "def f(s, table, cols):\n"
        '    s.execute(text(f"SELECT end FROM {table}"))\n'
        '    s.execute(text("SELECT end FROM " + table))\n'
        '    s.execute(text("SELECT %s FROM t" % cols))\n'
    )
    assert _check(tmp_path, source) == []


def test_a_name_assigned_two_different_strings_is_not_measured(tmp_path):
    source = (
        "from sqlalchemy import text\n"
        "def a(s):\n"
        '    q = "SELECT end FROM t"\n'
        "    s.execute(text(q))\n"
        "def b(s):\n"
        '    q = "SELECT id FROM t"\n'
        "    s.execute(text(q))\n"
    )
    assert _check(tmp_path, source) == []


def test_a_message_is_a_platform_sentence_never_the_parsers_own_text(tmp_path):
    odd = "from sqlalchemy import text\ndef f(s):\n    s.execute(text(\"SELECT 'unterminated FROM t\"))\n"
    assert [f["error"] for f in _check(tmp_path / "one", odd)] == ["syntax error"]

    odd2 = 'from sqlalchemy import text\ndef f(s):\n    s.execute(text("SELECT FROM WHERE"))\n'
    errors = [f["error"] for f in _check(tmp_path / "two", odd2)]
    assert errors and all(e.startswith("syntax error") and len(e) < 60 for e in errors)

    at_end = 'from sqlalchemy import text\ndef f(s):\n    s.execute(text("SELECT * FROM"))\n'
    assert [f["error"] for f in _check(tmp_path / "three", at_end)] == ["syntax error at end of input"]


def test_a_token_that_is_not_a_plain_identifier_is_left_out_of_the_sentence():
    assert chk._error_text('syntax error at or near "end"') == 'syntax error at or near "end"'
    assert chk._error_text('syntax error at or near "x; DROP"') == "syntax error"
    assert chk._error_text('syntax error at or near "' + "a" * 33 + '"') == "syntax error"
    assert chk._error_text("something else entirely, at index 3") == "syntax error"


def test_files_the_agent_does_not_own_or_cannot_be_parsed_are_skipped(tmp_path):
    bad = "from sqlalchemy import text\ndef f(s):\n    s.execute(text('SELECT end FROM t'))\n"
    _write(tmp_path, ".forge/agent_runner.py", bad)
    _write(tmp_path, "node_modules/x/y.py", bad)
    _write(tmp_path, "app/__pycache__/z.py", bad)
    _write(tmp_path, "app/broken.py", "def (:\n")
    _write(tmp_path, "app/data.txt", bad)

    assert check_sql_syntax(str(tmp_path)) == []


def test_a_path_with_unprintable_characters_is_not_shown(tmp_path):
    findings = _check(tmp_path, INCIDENT_APP, "app/we ird name.py")

    assert findings and {f["path"] for f in findings} == {UNPRINTABLE_PATH}


def test_findings_are_capped_and_in_file_then_line_order(tmp_path):
    many = "from sqlalchemy import text\ndef f(s):\n" + "".join(
        f'    s.execute(text("SELECT end FROM t{i}"))\n' for i in range(25)
    )
    findings = _check(tmp_path, many)

    assert len(findings) == chk.MAX_FINDINGS
    assert [f["line"] for f in findings] == sorted(f["line"] for f in findings)


def test_a_missing_parser_is_not_measured_not_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(chk, "_PGLAST", False)

    assert _check(tmp_path, INCIDENT_APP) is None


def test_findings_are_in_line_order_even_when_the_statements_are_nested_differently(tmp_path):
    source = (
        "from sqlalchemy import text\n"
        "def outer(s):\n"
        "    def inner(t):\n"
        '        t.execute(text("SELECT end FROM a"))\n'  # line 4, nested deeper
        '    s.execute(text("SELECT end FROM b"))\n'  # line 5
        '    s.execute(text("SELECT end FROM c"))\n'  # line 6
        'top = text("SELECT end FROM d")\n'  # line 7, module level
    )
    findings = _check(tmp_path, source)

    assert [f["line"] for f in findings] == [4, 5, 6, 7]


def test_an_empty_workspace_is_measured_and_clean_but_a_missing_one_is_not_measured(tmp_path):
    assert check_sql_syntax(str(tmp_path)) == []
    assert check_sql_syntax(str(tmp_path / "does-not-exist")) is None


def test_only_well_formed_findings_trigger_or_survive():
    good = {"path": "app/app.py", "line": 12, "error": 'syntax error at or near "end"'}
    assert has_sql_findings([good]) is True
    assert clean_findings([good, "x", None, {"path": "a b", "line": 1, "error": "syntax error"}]) == [good]
    for bad in (
        None,
        [],
        "syntax error",
        {"path": "app/app.py"},
        [{"path": "app/app.py", "line": 0, "error": "syntax error"}],
        [{"path": "app/app.py", "line": True, "error": "syntax error"}],
        [{"path": "app/app.py", "line": 3, "error": 'syntax error at or near "end", at index 24'}],
        [{"path": "../../etc/pass wd", "line": 3, "error": "syntax error"}],
    ):
        assert has_sql_findings(bad) is False
