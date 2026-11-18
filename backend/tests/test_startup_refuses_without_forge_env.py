"""the real entry points refuse to start without `FORGE_ENV`
.

`test_config_fail_safe.py` proves `Settings` refuses. This proves the two
processes an operator actually starts do, because they build one: the API server
(`uvicorn app.main:app`, as `dev.ps1` runs it) and the worker (`python -m
app.worker`). Each is a fresh interpreter, because the refusal happens at import
time -- `app.config` builds the process-wide `settings` -- and the test process
already has it imported with `FORGE_ENV` set.

The child's working directory is an empty temp directory, so no `.env` is found
(the suite never reads the developer's), and its environment is this process's with
`FORGE_ENV` removed or set deliberately. `PYTHONPATH` carries `backend/`, because
the cwd is not `backend/`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
TIMEOUT_SECONDS = 120


def _child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"FORGE_ENV", "AUTH_MODE", "CORS_ORIGINS"}}
    env["PYTHONPATH"] = str(BACKEND_DIR)
    env.update(extra)
    return env


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=TIMEOUT_SECONDS
    )


def test_the_control_imports_cleanly_when_forge_env_is_set(tmp_path):
    """So the refusals below are about `FORGE_ENV` and not about a child that cannot
    import the app at all (a bad cwd or `PYTHONPATH` would also exit non-zero)."""
    result = _run(["-c", "import app.main, app.worker"], tmp_path, _child_env(FORGE_ENV="dev"))
    assert result.returncode == 0, result.stderr


def test_the_api_server_refuses_to_start_without_forge_env(tmp_path):
    """The exact command `dev.ps1` runs. uvicorn imports `app.main`, which imports
    `app.config`; the named error ends the process before anything listens."""
    result = _run(
        ["-m", "uvicorn", "app.main:app", "--port", "8099"], tmp_path, _child_env()
    )
    assert result.returncode != 0
    assert "ForgeEnvNotSetError" in result.stderr
    assert "FORGE_ENV=dev" in result.stderr and "FORGE_ENV=prod" in result.stderr
    assert "Uvicorn running" not in result.stderr  # it never got as far as serving


def test_the_worker_refuses_to_start_without_forge_env(tmp_path):
    result = _run(["-m", "app.worker"], tmp_path, _child_env())
    assert result.returncode != 0
    assert "ForgeEnvNotSetError" in result.stderr
    assert "worker starting" not in result.stderr  # not even the startup banner


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "spaces"])
def test_a_blank_forge_env_is_refused_by_the_worker_too(tmp_path, blank):
    result = _run(["-m", "app.worker"], tmp_path, _child_env(FORGE_ENV=blank))
    assert result.returncode != 0
    assert "ForgeEnvNotSetError" in result.stderr


def test_the_refusal_is_not_the_dev_default_in_disguise(tmp_path):
    """With `FORGE_ENV=prod` and nothing else the child fails on a DIFFERENT, named
    prod condition (no `DATABASE_URL`) -- proof that the variable is read, and that
    prod's own guards still sit behind it."""
    result = _run(["-c", "import app.main"], tmp_path, _child_env(FORGE_ENV="prod"))
    assert result.returncode != 0
    assert "ProdDatabaseUrlMissingError" in result.stderr
    assert "ForgeEnvNotSetError" not in result.stderr
