"""A git that hangs is killed WITH the helper processes it started.

The reconciler removes Docker objects by label; it cannot reach a host process. An earlier runner
killed `git` on a timeout and left what git had started (`git-remote-https`; on Windows git for
Windows runs a second `git.exe` under the first) running against a host that never closes the
socket. `publish.run_git_process` now ends the whole tree: `taskkill /T` on Windows, a signal to
git's own process group on POSIX (git leads its own session there).

The test is real: real git against a server that accepts and never answers
(`silent_git_remote`). It first checks, WHILE git is hung, that git has a descendant at all --
otherwise "nothing survived" would be true of a git that never started a helper -- and then that
neither git nor any descendant is alive once the call has returned. Process listing uses the
platform's own tools (PowerShell + `tasklist` on Windows, `ps` on POSIX); no extra dependency.

The POSIX branch could not be run on the Windows host this was written on; it is exercised by
the same test on a Linux runner (CI), and its logic is a single `os.killpg` on a session leader.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

from app.services import publish


def _children_of(pid: int) -> list[int]:
    if sys.platform == "win32":
        script = (
            f"Get-CimInstance Win32_Process -Filter 'ParentProcessId={pid}' | "
            "ForEach-Object { $_.ProcessId }"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=60
        ).stdout
        return [int(line) for line in out.split() if line.strip().isdigit()]
    out = subprocess.run(["ps", "-e", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=30).stdout
    return [int(a) for a, b in (line.split() for line in out.splitlines() if line.strip()) if int(b) == pid]


def _descendants_of(pid: int) -> list[int]:
    found: list[int] = []
    frontier = [pid]
    while frontier:
        nxt: list[int] = []
        for parent in frontier:
            for child in _children_of(parent):
                if child not in found:
                    found.append(child)
                    nxt.append(child)
        frontier = nxt
    return found


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=30
        ).stdout
        return str(pid) in out.split()
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=30).stdout
    return bool(state.strip()) and not state.strip().startswith("Z")  # a zombie is dead, merely not yet reaped


def test_a_timed_out_git_takes_the_processes_it_started_with_it(tmp_path, monkeypatch, silent_git_remote):
    monkeypatch.setattr(publish, "GIT_TIMEOUT_SECONDS", 8.0)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)  # before Popen is wrapped below
    git_processes: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def recording_popen(args, *a, **k):
        proc = real_popen(args, *a, **k)
        if isinstance(args, list) and args and args[0] == "git":
            git_processes.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    outcome: dict = {}

    def run_hung_fetch():
        try:
            publish.run_git_process(
                ["git", "fetch", "--quiet", silent_git_remote + "/org/p.git"],
                cwd=str(repo),
                env=publish.git_env(),
            )
            outcome["error"] = None
        except subprocess.TimeoutExpired:
            outcome["error"] = "timeout"

    worker = threading.Thread(target=run_hung_fetch, daemon=True)
    worker.start()

    # While git is hung: it has to have started something, or this test proves nothing.
    deadline = time.monotonic() + 6.0
    while not git_processes and time.monotonic() < deadline:
        time.sleep(0.1)
    assert git_processes, "git was never started"
    git_pid = git_processes[0].pid
    time.sleep(1.5)
    helpers = _descendants_of(git_pid)
    assert helpers, "git started no helper process while hung, so a surviving one could not be observed"

    worker.join(timeout=60)
    assert not worker.is_alive(), "run_git_process did not return"
    assert outcome["error"] == "timeout"

    survivors: list[int] = []
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        survivors = [pid for pid in [git_pid, *helpers] if _alive(pid)]
        if not survivors:
            break
        time.sleep(0.5)
    for pid in survivors:  # never leave them behind, whatever the assertion says
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
            subprocess.run(["kill", "-9", str(pid)], capture_output=True)
    assert survivors == [], f"processes still alive after the timeout: {survivors}"
