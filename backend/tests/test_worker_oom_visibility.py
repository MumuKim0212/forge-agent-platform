"""the worker says at start when the post-run OOM read cannot see the engine's cgroups.

No Docker: a fake engine answers `info()`, and `HOST_CGROUP_ROOT` points at a tmp directory."""

from __future__ import annotations

import pytest

import app.worker as worker
from app.services import sandbox_runner


class _Engine:
    def __init__(self, info=None, error: Exception | None = None):
        self._info = info
        self._error = error

    def info(self):
        if self._error is not None:
            raise self._error
        return self._info


class _SandboxExecutor:
    starts_previews = True


def _banner(executor, engine, caplog):
    with caplog.at_level("INFO", logger="app.worker"):
        worker._log_oom_visibility_startup_banner(executor, engine)
    return [(r.levelname, r.getMessage()) for r in caplog.records if "OOM visibility" in r.getMessage()]


@pytest.fixture
def cgroup_root(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_runner, "HOST_CGROUP_ROOT", str(tmp_path))
    return tmp_path


def test_a_visible_cgroup_parent_on_cgroup_v2_logs_nothing(cgroup_root, caplog):
    (cgroup_root / "system.slice").mkdir()

    assert _banner(_SandboxExecutor(), _Engine({"CgroupVersion": "2", "CgroupDriver": "systemd"}), caplog) == []


def test_the_cgroupfs_driver_is_looked_for_under_docker(cgroup_root, caplog):
    (cgroup_root / "docker").mkdir()

    assert _banner(_SandboxExecutor(), _Engine({"CgroupVersion": "2", "CgroupDriver": "cgroupfs"}), caplog) == []


def test_an_invisible_cgroup_tree_warns_that_an_oom_is_reported_as_an_agent_error(cgroup_root, caplog):
    # Docker Desktop: the engine's cgroups are in a VM, the worker's host has no such directory.
    (line,) = _banner(_SandboxExecutor(), _Engine({"CgroupVersion": "2", "CgroupDriver": "cgroupfs"}), caplog)

    assert line[0] == "WARNING"
    assert "AGENT_ERROR" in line[1]
    assert "RESOURCE_LIMIT" in line[1]


def test_cgroup_v1_warns_even_when_the_directory_exists(cgroup_root, caplog):
    (cgroup_root / "system.slice").mkdir()

    (line,) = _banner(_SandboxExecutor(), _Engine({"CgroupVersion": "1", "CgroupDriver": "systemd"}), caplog)

    assert line[0] == "WARNING"
    assert "AGENT_ERROR" in line[1]


def test_an_engine_that_cannot_be_asked_warns_too(cgroup_root, caplog):
    (line,) = _banner(_SandboxExecutor(), _Engine(error=RuntimeError("down")), caplog)

    assert line[0] == "WARNING"
    assert "AGENT_ERROR" in line[1]


def test_an_executor_that_runs_no_sandbox_does_not_ask_the_engine(cgroup_root, caplog):
    class _Placeholder:
        pass

    assert _banner(_Placeholder(), _Engine(error=AssertionError("must not be asked")), caplog) == []
