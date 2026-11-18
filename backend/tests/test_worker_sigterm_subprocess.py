"""ADR 0037 §Decision 6: a real worker PROCESS, a real stop signal.

`test_worker_recovery.py` raises signals inside the test process; this starts `app.worker`'s
`main()` in a fresh interpreter against a migrated throwaway SQLite database, lets it claim a
build and start "running" it, signals it, and checks what the operator would see: the process
exits with status 0 (a requested stop is not a failure), the build it was running is FAILED
with the platform's "asked to stop" detail rather than left RUNNING, and nothing further was
claimed.

**The signal.** POSIX: SIGTERM. Windows: `os.kill(SIGTERM)` is a plain `TerminateProcess` that no
handler ever sees, so a Windows process can only be asked to stop with a console control event;
the child is started in its own process group and sent `CTRL_BREAK_EVENT` (SIGBREAK), which the
worker handles the same way. On a Windows host where that cannot be delivered (no console) the
test skips and says so, and the in-process signal tests still run.

The child runs in an empty temp directory (so it never finds the developer's `.env`) with
`FORGE_ENV=dev` and `DATABASE_URL` pointing at the temp database. Its executor is a stub that
announces itself and then waits; there is no Docker, engine or network in it.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.audit import AuditLog
from app.models.build import Build
from app.models.project import Project
from app.schema_check import alembic_config
from app.seed import seed
from app.services.status import BuildStatus, ProjectStatus

BACKEND_DIR = Path(__file__).resolve().parent.parent
IS_WINDOWS = sys.platform == "win32"

_DRIVER = """
import time
import app.worker as w
from app.services.build_executor import ExecutionResult

class Waiting:
    uses_engine = False
    def execute(self, build):
        print("EXECUTING " + build.id, flush=True)
        for _ in range(600):  # at most a minute: the test kills the child long before this
            time.sleep(0.1)
        return ExecutionResult(ok=True)

w._select_executor = lambda: Waiting()
w._new_reconciler = lambda: None   # no Docker in this process
w.main()
print("MAIN-RETURNED", flush=True)
"""


def _child_env(db_file: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"FORGE_ENV", "AUTH_MODE", "CORS_ORIGINS", "DATABASE_URL"}}
    env.update(
        PYTHONPATH=str(BACKEND_DIR),
        FORGE_ENV="dev",
        DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
        WORKER_POLL_INTERVAL_SECONDS="0.2",
        PYTHONUNBUFFERED="1",
    )
    return env


@pytest.fixture()
def database(tmp_path):
    db_file = tmp_path / "worker.db"
    url = f"sqlite:///{db_file.as_posix()}"
    command.upgrade(alembic_config(url), "head")
    engine = create_engine(url, connect_args={"check_same_thread": False})
    make_session = sessionmaker(bind=engine, autoflush=False)
    with make_session() as db:
        seed(db)
    yield db_file, make_session
    engine.dispose()


def _queue_a_build(make_session) -> str:
    from app.models.template import Template
    from app.models.user import Team, User

    with make_session() as db:
        project = Project(
            name="sigterm",
            owner_id=db.query(User).first().id,
            team_id=db.query(Team).first().id,
            template_id=db.query(Template).first().id,
            status=ProjectStatus.SUBMITTED,
        )
        db.add(project)
        db.commit()
        build = Build(project_id=project.id, type="create", status=BuildStatus.QUEUED)
        db.add(build)
        db.commit()
        return build.id


class _Child:
    def __init__(self, tmp_path: Path, db_file: Path, driver: str = _DRIVER):
        kwargs = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen(
            [sys.executable, "-c", driver],
            cwd=tmp_path,
            env=_child_env(db_file),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            **kwargs,
        )
        self.lines: list[str] = []
        self._queue: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:  # type: ignore[union-attr]
            self._queue.put(line.rstrip("\n"))
        self._queue.put(None)

    def wait_for(self, text: str, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                break
            self.lines.append(line)
            if text in line:
                return
        raise AssertionError(f"the worker never printed {text!r}; output so far:\n" + "\n".join(self.lines[-40:]))

    def stop(self) -> None:
        if IS_WINDOWS:
            try:
                os.kill(self.proc.pid, signal.CTRL_BREAK_EVENT)
            except OSError as exc:  # no console to deliver it through
                self.kill()
                pytest.skip(f"cannot deliver CTRL_BREAK_EVENT to a child here ({exc})")
        else:
            self.proc.send_signal(signal.SIGTERM)

    def finish(self, timeout: float = 60.0) -> int:
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            raise AssertionError("the worker did not exit after the stop signal; output:\n" + "\n".join(self.lines[-40:]))
        while True:  # drain what the pump already read
            try:
                line = self._queue.get(timeout=1.0)
            except queue.Empty:
                break
            if line is None:
                break
            self.lines.append(line)
        return code

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)


@pytest.fixture()
def child(tmp_path, database):
    db_file, _ = database
    started = _Child(tmp_path, db_file)
    try:
        yield started
    finally:
        started.kill()


def test_a_stop_signal_during_a_build_fails_the_build_and_the_worker_exits_zero(child, database):
    _, make_session = database
    build_id = _queue_a_build(make_session)

    child.wait_for(f"EXECUTING {build_id}")  # claimed, and inside the executor
    child.stop()
    code = child.finish()

    output = "\n".join(child.lines)
    assert code == 0, output
    assert "MAIN-RETURNED" in output  # main() returned: it ended by itself, it was not killed
    with make_session() as db:
        build = db.get(Build, build_id)
        assert build.status == BuildStatus.FAILED
        assert build.failure_reason == "INFRA_ERROR"
        assert "asked to stop" in build.failure_detail
        (entry,) = db.query(AuditLog).filter(AuditLog.action == "build.failed").all()
        assert entry.target == f"build:{build_id}"


def test_a_stop_signal_to_an_idle_worker_ends_it_with_status_zero(child, database):
    child.wait_for("worker starting")
    time.sleep(1.0)  # it is in its poll loop by now
    child.stop()
    code = child.finish()

    assert code == 0, "\n".join(child.lines)
    assert "MAIN-RETURNED" in "\n".join(child.lines)
