"""Integration tests for the L3 sandbox runner against a real Docker
Engine. Marked `integration`; skipped cleanly (not failed) when Docker is
unavailable, so plain `pytest` stays green everywhere while still proving
the real invariants (egress deny, round-trip staging/harvest, timeout,
resource caps) on a machine with Docker running.

These pull `settings.sandbox_image` (default `python:3.11-slim`) if not
already cached locally — first run may be slower.
"""

from __future__ import annotations

import os
import tempfile

import pytest

pytest.importorskip("docker")

import docker  # noqa: E402
from docker.errors import DockerException  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.sandbox_runner import SandboxRunConfig, SandboxRunner  # noqa: E402
from app.services.status import BuildFailureReason  # noqa: E402


def _docker_available() -> bool:
    try:
        client = docker.from_env()
        client.ping()
        return True
    except DockerException:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_available(), reason="Docker Engine not available"),
]


@pytest.fixture(autouse=True)
def _keep_harvests_out_of_the_host_temp_dir(tmp_path_factory, monkeypatch):
    """every real `SandboxRunner.run` here harvests into a fresh
    `forge-harvest-*` directory that no test removes. Pointing the temp root at a
    pytest-owned directory keeps them out of the host's real temp dir, where they
    had accumulated by the thousand, one per test run."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("systemp")))


@pytest.fixture()
def runner():
    return SandboxRunner()


@pytest.fixture()
def workspace_dir(tmp_path):
    (tmp_path / "input.txt").write_text("hello from host\n")
    return str(tmp_path)


def test_simple_command_succeeds(runner, workspace_dir):
    config = SandboxRunConfig(command=["echo", "hello from sandbox"], workspace_dir=workspace_dir)

    result = runner.run(config)

    assert result.ok is True
    assert result.exit_code == 0
    assert "hello from sandbox" in result.logs


def test_nonzero_exit_reports_agent_error(runner, workspace_dir):
    config = SandboxRunConfig(command=["sh", "-c", "exit 3"], workspace_dir=workspace_dir)

    result = runner.run(config)

    assert result.ok is False
    assert result.reason == BuildFailureReason.AGENT_ERROR
    assert result.exit_code == 3


def test_staging_and_harvest_round_trip(runner, workspace_dir):
    """Airlock: local dir staged in, command mutates the workspace, mutated
    workspace harvested back out to a new local directory."""
    config = SandboxRunConfig(
        command=["sh", "-c", "cat input.txt > output.txt && echo appended >> output.txt"],
        workspace_dir=workspace_dir,
    )

    result = runner.run(config)

    assert result.ok is True
    assert result.harvested_workspace_dir is not None
    harvested_file = os.path.join(result.harvested_workspace_dir, "output.txt")
    assert os.path.isfile(harvested_file)
    with open(harvested_file, encoding="utf-8") as f:
        content = f.read()
    assert "hello from host" in content
    assert "appended" in content


def test_no_external_egress_from_sandbox(runner, workspace_dir):
    """Invariant 3: default-deny egress. An in-container attempt to reach an
    external host must fail — proves the isolated internal network has no
    external route."""
    config = SandboxRunConfig(
        command=[
            "sh",
            "-c",
            "python -c \"import socket; socket.setdefaulttimeout(3); "
            "socket.create_connection(('1.1.1.1', 443))\"",
        ],
        workspace_dir=workspace_dir,
        timeout_seconds=15,
    )

    result = runner.run(config)

    assert result.ok is False
    assert result.reason == BuildFailureReason.AGENT_ERROR
    assert result.exit_code != 0


def test_wall_clock_timeout_is_killed_and_reported(runner, workspace_dir):
    config = SandboxRunConfig(
        command=["sleep", "30"],
        workspace_dir=workspace_dir,
        timeout_seconds=2,
    )

    result = runner.run(config)

    assert result.ok is False
    assert result.reason == BuildFailureReason.TIMEOUT


def test_memory_limit_oom_kills_reports_resource_limit(runner, workspace_dir):
    config = SandboxRunConfig(
        command=[
            "python",
            "-c",
            "x = bytearray(500 * 1024 * 1024)\nwhile True:\n    x += bytearray(50 * 1024 * 1024)",
        ],
        workspace_dir=workspace_dir,
        memory_limit_mb=64,
        timeout_seconds=30,
    )

    result = runner.run(config)

    assert result.ok is False
    assert result.reason == BuildFailureReason.RESOURCE_LIMIT


def test_container_runs_as_non_root(runner, workspace_dir):
    config = SandboxRunConfig(command=["whoami"], workspace_dir=workspace_dir)

    result = runner.run(config)

    assert result.ok is True
    assert "root" not in result.logs.strip().lower()


def test_container_is_removed_after_run(runner, workspace_dir):
    config = SandboxRunConfig(command=["echo", "hi"], workspace_dir=workspace_dir)
    client = docker.from_env()
    before_ids = {c.id for c in client.containers.list(all=True)}

    runner.run(config)

    after_ids = {c.id for c in client.containers.list(all=True)}
    assert after_ids - before_ids == set()  # no leftover container from this run


def test_per_run_network_is_removed_after_run(runner, workspace_dir):
    config = SandboxRunConfig(command=["echo", "hi"], workspace_dir=workspace_dir)
    client = docker.from_env()
    before_names = {n.name for n in client.networks.list()}

    runner.run(config)

    after_names = {n.name for n in client.networks.list()}
    assert after_names - before_names == set()  # no leftover sandbox network


def test_two_concurrent_sandboxes_cannot_reach_each_other(workspace_dir):
    """Derived rule: 'No inter-sandbox traffic.'
    Each run gets its own isolated network, so a container from one run
    cannot address a container from a concurrently-running other run."""
    client = docker.from_env()
    runner_a = SandboxRunner(client=client)

    # Provision run A's container directly (bypassing the full run()) so it
    # stays alive while we start run B and probe reachability from inside B.
    network_a = runner_a._create_run_network()
    container_a = runner_a._provision(
        SandboxRunConfig(command=["sleep", "60"], workspace_dir=workspace_dir),
        network_a.name,
    )
    try:
        container_a.reload()
        ip_a = container_a.attrs["NetworkSettings"]["Networks"][network_a.name]["IPAddress"]

        runner_b = SandboxRunner(client=client)
        config_b = SandboxRunConfig(
            command=["sh", "-c", f"ping -c1 -W2 {ip_a} && echo REACHED || echo UNREACHABLE"],
            workspace_dir=workspace_dir,
        )
        result_b = runner_b.run(config_b)

        assert result_b.ok is True  # ping's own exit status is masked by the sh fallback
        assert "UNREACHABLE" in result_b.logs
        assert "REACHED" not in result_b.logs
    finally:
        container_a.remove(v=True, force=True)
        network_a.remove()


def test_no_docker_socket_reachable_from_inside_sandbox(runner, workspace_dir):
    """Invariant 2 (behavioral proof): the Docker socket path does not exist
    inside the sandbox, so the agent cannot reach the Docker Engine even
    indirectly."""
    config = SandboxRunConfig(
        command=["sh", "-c", "test -S /var/run/docker.sock && echo FOUND || echo ABSENT"],
        workspace_dir=workspace_dir,
    )

    result = runner.run(config)

    assert result.ok is True
    assert "ABSENT" in result.logs
    assert "FOUND" not in result.logs


def test_staged_subdirectories_are_owned_and_writable_by_the_sandbox_user(
    runner, tmp_path, monkeypatch
):
    """Docker extracts a `put_archive` tar keeping each entry's header
    uid/gid and mode, so on a Linux host (the host user's uid, dirs 0755) a
    subdirectory the platform staged (`.forge/`, where `agent_runner.py`
    writes `result.json`) is not writable by SANDBOX_USER unless ownership is
    handed over *after* the extract. Docker Desktop on Windows yields 0:0 /
    0777 and hides this, so the Linux shape is forced here rather than
    assumed: every tar entry is rewritten to a 0755 dir / 0644 file owned by
    uid 1000.

    Asserts on what the container observed (owner, mode, and a real write as
    the sandbox user), not on how the code gets there."""
    import tarfile

    real_add = tarfile.TarFile.add

    def linux_shaped_add(self, name, arcname=None, recursive=True, *, filter=None):
        def force(info):
            info.mode = 0o755 if info.isdir() else 0o644
            info.uid = info.gid = 1000
            info.uname = info.gname = ""
            return info

        return real_add(self, name, arcname, recursive, filter=force)

    monkeypatch.setattr(tarfile.TarFile, "add", linux_shaped_add)

    deep = tmp_path / ".forge" / "deeper"
    deep.mkdir(parents=True)
    (tmp_path / ".forge" / "task_input.json").write_text("{}")
    (deep / "nested.txt").write_text("x")

    config = SandboxRunConfig(
        command=[
            "sh",
            "-c",
            "stat -c 'STAT %n %u:%g %a' /workspace /workspace/.forge "
            "/workspace/.forge/deeper /workspace/.forge/task_input.json "
            "/workspace/.forge/deeper/nested.txt "
            "&& echo '{}' > /workspace/.forge/result.json "
            "&& touch /workspace/.forge/deeper/probe "
            "&& echo 'x' >> /workspace/.forge/deeper/nested.txt",
        ],
        workspace_dir=str(tmp_path),
    )

    result = runner.run(config)

    assert result.ok is True, result.logs
    for path in (
        "/workspace",
        "/workspace/.forge",
        "/workspace/.forge/deeper",
        "/workspace/.forge/task_input.json",
        "/workspace/.forge/deeper/nested.txt",
    ):
        assert f"STAT {path} 65534:65534" in result.logs, result.logs
    # The Linux shape really reached the container (the mode survived), so the
    # ownership above is what made the directories writable, not a 0777 mode.
    assert "STAT /workspace/.forge 65534:65534 755" in result.logs
    assert os.path.isfile(
        os.path.join(result.harvested_workspace_dir, ".forge", "result.json")
    )
