"""The SQL syntax check (ADR 0047 §Amendment 2026-11-25).

Parses, with PostgreSQL's own parser (`pglast`, libpg_query), the SQL strings a generated service
hands to the database layer, and reports the ones PostgreSQL would reject as a syntax error. It
needs no database and no network: it is the one measurement of the SQL that the build sandbox
(which has no database, ADR 0023) can be given.

What it reads. Only strings that are EXECUTED: the first positional argument of a call to `text`,
`execute`, `executemany` or `exec_driver_sql` (`sa.text(...)`, `session.execute(...)`,
`op.execute(...)`, `cursor.execute(...)`), either written in place or a module- or function-level
name that is assigned a constant string and passed there. Nothing is guessed from what a string
looks like, so a docstring or a label that starts with "Select" is never parsed. A string the
check cannot resolve to a constant (an f-string with expressions, a value built at run time) is
skipped: a statement built at run time is not measured, and "not measured" never counts as clean
or as a failure.

What it cannot see: a table or column that does not exist, a type error, a constraint, anything
that needs the database. A clean result is "PostgreSQL's grammar accepts these statements", not
"these statements work".

What it returns. For each rejected statement: the file (relative, posix), the line, and `error`,
a platform-chosen sentence from a fixed vocabulary (`syntax error`, `syntax error at end of input`,
or `syntax error at or near "<token>"` where the token is a plain identifier of at most 32
characters). The parser's own message is never passed on: it can quote text the agent wrote, and
this fact reaches the repair prompt (ADR 0047 §3).
"""

from __future__ import annotations

import ast
import logging
import os
import re
from typing import Iterator

logger = logging.getLogger(__name__)

try:  # a declared dependency; if it cannot be imported the check reports "not measured", never "clean"
    from pglast import parse_sql
    from pglast.parser import ParseError

    _PGLAST = True
except ImportError:  # pragma: no cover - exercised by monkeypatching `_PGLAST`
    _PGLAST = False

MAX_FINDINGS = 10
MAX_FILES = 300
MAX_FILE_BYTES = 1024 * 1024
MAX_SQL_CHARS = 20_000
_SKIP_DIRS = frozenset({".forge", ".git", "node_modules", "__pycache__", ".venv", "venv"})
_EXECUTING_CALLS = frozenset({"text", "execute", "executemany", "exec_driver_sql"})

_SAFE_PATH = re.compile(r"[A-Za-z0-9_./-]{1,200}")
_SAFE_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,31}")
_NEAR = re.compile(r'^syntax error at or near "(?P<token>[^"]*)"')
_AT_END = re.compile(r"^syntax error at end of input")
UNPRINTABLE_PATH = "(path not shown)"

# Bind parameters in the three notations a Python service uses; each becomes `$1`, which the
# grammar accepts wherever a value can stand. `::` (a cast) and `:` inside a time like '09:00'
# are not parameters: a parameter name starts with a letter or underscore and is not preceded by
# a word character or another colon.
_NAMED_PARAM = re.compile(r"(?<![\w:]):[A-Za-z_]\w*")
_PYFORMAT_NAMED = re.compile(r"%\([A-Za-z_]\w*\)s")
_PYFORMAT_POSITIONAL = re.compile(r"%s")


def _normalise(sql: str) -> str:
    sql = _PYFORMAT_NAMED.sub("$1", sql)
    sql = _PYFORMAT_POSITIONAL.sub("$1", sql)
    return _NAMED_PARAM.sub("$1", sql)


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _constant_text(node: ast.AST, names: dict[str, str | None]) -> str | None:
    """The string an expression is made of when that is known without running anything: a string
    constant, a `+` of such, an f-string with no expressions, or a name assigned only such strings.
    None for everything else."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _constant_text(node.left, names), _constant_text(node.right, names)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for part in node.values:
            if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                return None
            parts.append(part.value)
        return "".join(parts)
    if isinstance(node, ast.Name):
        return names.get(node.id)
    return None


def _assigned_names(tree: ast.AST) -> dict[str, str | None]:
    """Every simple `NAME = <expression>` in the file, resolved to its constant string, or None
    when the name is ever assigned something that is not (or is assigned more than once, to
    different things): such a name is not measured. A name assigned the same string twice keeps it."""
    resolved: dict[str, str | None] = {}

    def record(name: str, value: str | None) -> None:
        resolved[name] = None if name in resolved and resolved[name] != value else value

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            record(node.targets[0].id, _constant_text(node.value, {}))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            record(node.target.id, _constant_text(node.value, {}))
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            resolved[node.target.id] = None
    return resolved


def _imports_sqlalchemy(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "sqlalchemy" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "sqlalchemy":
            return True
    return False


def _executed_statements(tree: ast.AST) -> Iterator[tuple[int, str]]:
    """(line, SQL text) of every executed string that resolves to a constant. A call to `text` only
    counts in a file that imports SQLAlchemy: the name is too common to read as SQL otherwise."""
    names = _assigned_names(tree)
    text_is_sql = _imports_sqlalchemy(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        called = _call_name(node)
        if called not in _EXECUTING_CALLS or (called == "text" and not text_is_sql):
            continue
        sql = _constant_text(node.args[0], names)
        if sql is not None and sql.strip():
            yield node.args[0].lineno, sql


def _error_text(message: str) -> str:
    """The platform's own sentence for a parser message; see the module docstring."""
    if _AT_END.match(message):
        return "syntax error at end of input"
    near = _NEAR.match(message)
    if near and _SAFE_TOKEN.fullmatch(near.group("token")):
        return f'syntax error at or near "{near.group("token")}"'
    return "syntax error"


def _python_files(workspace_dir: str) -> list[str]:
    found: list[str] = []
    for root, dirs, files in os.walk(workspace_dir):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in sorted(files):
            if name.endswith(".py"):
                found.append(os.path.join(root, name))
                if len(found) >= MAX_FILES:
                    return found
    return found


def check_sql_syntax(workspace_dir: str) -> list[dict] | None:
    """The SQL statements under `workspace_dir` that PostgreSQL's grammar rejects, in file and line
    order, at most `MAX_FINDINGS`, as `{"path", "line", "error"}`. `[]` = measured, nothing rejected.
    None = not measured (the parser is unavailable): the caller treats it as it treats a smoke check
    that could not run. Never raises: a file that cannot be read or parsed is skipped."""
    if not _PGLAST:
        logger.warning("sql syntax check: pglast is not installed; the check was not run")
        return None
    if not os.path.isdir(workspace_dir):
        return None  # nothing to read is "not measured", not "nothing rejected"
    findings: list[dict] = []
    for path in _python_files(workspace_dir):
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                continue
            with open(path, "rb") as handle:
                tree = ast.parse(handle.read())
        except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
            continue
        relative = os.path.relpath(path, workspace_dir).replace(os.sep, "/")
        shown = relative if _SAFE_PATH.fullmatch(relative) else UNPRINTABLE_PATH
        # `ast.walk` is breadth-first; the report is in line order.
        for line, sql in sorted(_executed_statements(tree), key=lambda item: item[0]):
            if len(sql) > MAX_SQL_CHARS:
                continue
            try:
                parse_sql(_normalise(sql))
            except ParseError as exc:
                findings.append({"path": shown, "line": line, "error": _error_text(str(exc))})
                if len(findings) >= MAX_FINDINGS:
                    return findings
            except Exception:  # noqa: BLE001 - one statement the parser chokes on is not a finding
                logger.warning("sql syntax check: the parser failed on a statement in a build's workspace")
    return findings


def has_sql_findings(fact: object) -> bool:
    """True when a stored/measured list holds at least one well-formed finding. The repair loop's
    second trigger, next to `preview_smoke.has_failing_route`. None ("not measured") and anything
    that is not a list of findings are False: nothing unmeasured ever triggers a repair."""
    return bool(clean_findings(fact))


def clean_findings(fact: object) -> list[dict]:
    """The findings of `fact` that are exactly `{path: safe str, line: int >= 1, error: one of the
    fixed sentences}`, at most `MAX_FINDINGS`. Defence in depth, as `failing_routes` re-filters a
    stored smoke fact: this list is what the repair prompt and the stored reason are made from."""
    if not isinstance(fact, list):
        return []
    kept: list[dict] = []
    for item in fact:
        if not isinstance(item, dict):
            continue
        path, line, error = item.get("path"), item.get("line"), item.get("error")
        if not (isinstance(path, str) and (_SAFE_PATH.fullmatch(path) or path == UNPRINTABLE_PATH)):
            continue
        if not (isinstance(line, int) and not isinstance(line, bool) and line >= 1):
            continue
        if not (isinstance(error, str) and _error_text(error) == error):
            continue
        kept.append({"path": path, "line": line, "error": error})
        if len(kept) >= MAX_FINDINGS:
            break
    return kept
