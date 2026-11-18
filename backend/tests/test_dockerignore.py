"""`backend/.dockerignore` keeps secrets and state out of every image
build context. `backend/` is the build context of the llm-proxy image, and it holds `.env` (the real
LLM key), the platform DB, every service's repo (`local-git/`) and the service-database dumps.

No daemon is needed: the file's rules are applied to the paths below with Docker's own matching
semantics (patterns anchored at the context root, `**` crossing directories, `*` and `?` not
crossing a separator, a pattern matching a path or any parent of it, the last matching rule winning,
`!` re-including). The list is the repo's actual `backend/` tree plus names that exist only on an
installed host. The context a real build sent was also listed once with a probe image."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
IGNORE_FILE = BACKEND / ".dockerignore"

# Names that exist on an installed host (or a dev box) but not in a fresh checkout.
MUST_BE_EXCLUDED = [
    ".env",
    ".env.production",
    ".env.example",
    ".env.example.ko",
    "nested/dir/.env",
    "forge_dev.db",
    "forge_dev.db-wal",
    "forge_dev.db-shm",
    "tests/leftover.db",
    "platform.sqlite3",
    "local-git/p1/refs/heads/main",
    "backups/service-data/svc_x.sql",
    ".venv/Scripts/python.exe",
    "app/__pycache__/main.cpython-314.pyc",
    "app/services/__pycache__/x.pyc",
    "uvicorn.log",
    "worker.err.log",
    ".pytest_cache/v/cache/lastfailed",
]
MUST_BE_INCLUDED = [
    "constraints.txt",
    "app/main.py",
    "app/proxy_server.py",
    "docker/llm-proxy/Dockerfile",
    "pyproject.toml",
]


def _rules() -> list[tuple[bool, str]]:
    rules = []
    for raw in IGNORE_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        rules.append((negated, line[1:] if negated else line))
    return rules


def _regex(pattern: str) -> re.Pattern[str]:
    pattern = pattern.strip("/")
    out, i = "", 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif ch == "*":
            out += "[^/]*"
            i += 1
        elif ch == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(ch)
            i += 1
    return re.compile(out + r"\Z")


def excluded(path: str) -> bool:
    """Whether a build context built under these rules would leave `path` out."""
    parts = path.split("/")
    # a pattern matches the path itself or any ancestor directory of it
    candidates = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    verdict = False
    for negated, pattern in _rules():
        regex = _regex(pattern)
        if any(regex.match(c) for c in candidates):
            verdict = not negated
    return verdict


def _tree() -> list[str]:
    found = []
    for path in BACKEND.rglob("*"):
        if path.is_file() and ".venv" not in path.parts and ".git" not in path.parts:
            found.append(path.relative_to(BACKEND).as_posix())
    return found


@pytest.mark.parametrize("path", MUST_BE_EXCLUDED)
def test_secrets_and_state_are_excluded(path):
    assert excluded(path), f"{path} would be sent to the daemon in a backend/ build context"


@pytest.mark.parametrize("path", MUST_BE_INCLUDED)
def test_what_the_images_build_from_is_not_excluded(path):
    assert not excluded(path), f"{path} is needed by an image build and the ignore rules drop it"


def test_no_rule_re_includes_anything():
    assert [pattern for negated, pattern in _rules() if negated] == []


def test_nothing_in_the_real_tree_that_is_an_env_file_or_database_is_in_the_context():
    leaks = [
        p
        for p in _tree()
        if (Path(p).name == ".env" or Path(p).name.startswith(".env.") or Path(p).suffix == ".db") and not excluded(p)
    ]
    assert leaks == []


def test_the_matcher_can_tell_included_from_excluded():
    """The matcher is test code, so prove it is not simply answering True: a file the rules do not
    name stays in."""
    assert excluded("app/main.py") is False
