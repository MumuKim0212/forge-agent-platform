"""the build sandbox's disk cap and the
bounded harvest, against a REAL Docker Engine. Marked `integration`; skipped cleanly without Docker.

What each test is named for, and why it can reach it:

- The **harvest bounds** are driven by what a hostile build can actually make: a sparse file (apparent
  size far above what it occupies), more entries than the ceiling. Each is run twice, once on the
  generic image (python, so the in-sandbox pre-flight answers) and once on alpine (no python, so the
  pre-flight is silent and ONLY the streamed byte bound and the entry check hold), because the
  pre-flight is advisory and the boundary is the layer under it.
- The **disk watchdog** is driven by bytes written OUTSIDE `/workspace` (`/tmp`): the harvest never
  archives those, so only the watchdog on the writable layer can see them. A test that wrote its
  excess into `/workspace` would be passed by the harvest bound and prove nothing about the watchdog.
- "Never held in memory" is measured (tracemalloc peak while a 96 MiB workspace is harvested), not
  just asserted from the source.

On this dev host (Docker Desktop, Docker 28.4, containerd snapshotter) these were also used to measure
what the engine does. The limits are passed per run (`SandboxRunConfig
.disk_limit_mb`) and kept small so no test needs gigabytes of real disk.
"""

from __future__ import annotations

import os
import tempfile
import time
import tracemalloc

import pytest

pytest.importorskip("docker")

import docker  # noqa: E402
from docker.errors import DockerException, ImageNotFound  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import sandbox_runner as sr  # noqa: E402
from app.services.sandbox_runner import SandboxRunConfig, SandboxRunner  # noqa: E402
from app.services.status import BuildFailureReason  # noqa: E402

MIB = 1024 * 1024
ALPINE = "alpine:3.19"  # no python: the harvest's in-sandbox pre-flight cannot answer


def _docker_available() -> bool:
    try:
        docker.from_env().ping()
        return True
    except DockerException:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_available(), reason="Docker Engine not available"),
]


@pytest.fixture(autouse=True)
def _keep_harvests_out_of_the_host_temp_dir(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("systemp")
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


@pytest.fixture()
def system_temp(_keep_harvests_out_of_the_host_temp_dir):
    return _keep_harvests_out_of_the_host_temp_dir


@pytest.fixture()
def client():
    return docker.from_env()


@pytest.fixture()
def workspace_dir(tmp_path_factory):
    path = tmp_path_factory.mktemp("ws")
    (path / "input.txt").write_text("hello from host\n")
    return str(path)


@pytest.fixture()
def no_leaks(client):
    """Containers and sandbox networks must be the same set after the test as before it."""

    def snapshot():
        return (
            {c.id for c in client.containers.list(all=True)},
            {n.id for n in client.networks.list() if n.name.startswith(settings.sandbox_network_name)},
        )

    before = snapshot()
    yield
    after = snapshot()
    assert after[0] - before[0] == set(), "a sandbox container was left behind"
    assert after[1] - before[1] == set(), "a sandbox network was left behind"


def _image_or_skip(client, image: str) -> str:
    """`image`, pulled when absent (CI starts without alpine); skipped only if it cannot be had."""
    try:
        client.images.get(image)
    except ImageNotFound:
        try:
            client.images.pull(image)
        except DockerException:
            pytest.skip(f"{image} is not present locally and could not be pulled")
    except DockerException:
        pytest.skip(f"{image} is not present locally")
    return image


def _run(client, command, workspace_dir, *, image=None, disk_limit_mb=64, timeout_seconds=60):
    return SandboxRunner(client=client).run(
        SandboxRunConfig(
            command=command,
            workspace_dir=workspace_dir,
            image=image,
            disk_limit_mb=disk_limit_mb,
            timeout_seconds=timeout_seconds,
        )
    )


# --- a sparse file larger than the cap -------------------------------------------------------------


@pytest.fixture()
def blind_watchdog(monkeypatch):
    """The disk watchdog reports an empty layer, so only the HARVEST can answer. Needed because what
    `SizeRw` counts for a sparse file depends on the engine (measured: the containerd snapshotter counts
    what it occupies, so a sparse file adds nothing; overlay2 counts its apparent size, so the watchdog
    catches it itself), and a test of the harvest bound must not depend on which host it runs on."""
    monkeypatch.setattr(SandboxRunner, "_writable_layer_bytes", lambda self, container: 0)


@pytest.mark.parametrize("image", [None, ALPINE], ids=["preflight-answers", "preflight-silent"])
def test_a_sparse_file_larger_than_the_cap_is_resource_limit_and_leaves_nothing(
    client, workspace_dir, system_temp, no_leaks, blind_watchdog, image
):
    """`truncate -s 1G` costs the sandbox nothing, but the workspace archive carries the file's
    apparent size. With the pre-flight silent (alpine) this is the streamed byte bound alone, read
    from a real daemon, and the spy proves the stream really was read, and read no further than the cap."""
    if image:
        _image_or_skip(client, image)
    pulled: list[int] = []
    original = SandboxRunner._archive_chunks

    def spy(self, container):
        inner = original(self, container)

        def counting():
            try:
                for chunk in inner:
                    pulled.append(len(chunk))
                    yield chunk
            finally:
                inner.close()

        return counting()

    SandboxRunner._archive_chunks = spy
    try:
        result = _run(client, ["sh", "-c", "truncate -s 1G /workspace/big"], workspace_dir, image=image)
    finally:
        SandboxRunner._archive_chunks = original

    assert result.ok is False
    assert result.reason == BuildFailureReason.RESOURCE_LIMIT
    assert result.detail == "the build's output is larger than the 64 MiB limit"
    assert result.harvested_workspace_dir is None
    assert os.listdir(system_temp) == []  # no partial archive, no scratch tree
    if image == ALPINE:
        assert pulled, "the pre-flight was meant to be silent here, so the archive stream must have been read"
        assert sum(pulled) <= 64 * MIB + 2 * MIB  # the cap, plus the chunk that crossed it: never the 1 GiB


def test_a_sparse_bomb_is_stopped_by_whichever_layer_the_engine_lets_see_it(
    client, workspace_dir, system_temp, no_leaks
):
    """No isolation: the real watchdog and the real harvest, on whatever engine this is. On the
    containerd snapshotter (Docker Desktop) `SizeRw` ignores a sparse file and the harvest bound answers; on
    overlay2 `SizeRw` counts its apparent size and the watchdog answers first. Both are RESOURCE_LIMIT,
    each with its own platform sentence, and nothing is left behind -- the property that holds everywhere."""
    result = _run(client, ["sh", "-c", "truncate -s 1G /workspace/big"], workspace_dir)

    assert result.reason == BuildFailureReason.RESOURCE_LIMIT
    assert result.detail in (
        "the build's output is larger than the 64 MiB limit",
        "the sandbox's files grew past the 64 MiB disk limit",
    )
    assert result.harvested_workspace_dir is None
    assert os.listdir(system_temp) == []


# --- more entries than the ceiling ------------------------------------------------------------------


@pytest.mark.parametrize("image", [None, ALPINE], ids=["preflight-answers", "preflight-silent"])
def test_a_workspace_over_the_entry_ceiling_is_resource_limit(
    client, workspace_dir, system_temp, no_leaks, monkeypatch, image
):
    if image:
        _image_or_skip(client, image)
    monkeypatch.setattr(sr, "HARVEST_MAX_ENTRIES", 100)
    command = [
        "sh",
        "-c",
        "mkdir /workspace/d; i=0; while [ $i -lt 300 ]; do : > /workspace/d/f$i; i=$((i+1)); done",
    ]

    result = _run(client, command, workspace_dir, image=image)

    assert result.reason == BuildFailureReason.RESOURCE_LIMIT
    assert result.detail == "the build's output has more than 100 files and directories"
    assert result.harvested_workspace_dir is None
    assert os.listdir(system_temp) == []


# --- the disk watchdog ----------------------------------------------------------------------------------


def test_bytes_written_outside_the_workspace_are_stopped_by_the_watchdog_while_the_build_runs(
    client, workspace_dir, system_temp, no_leaks, monkeypatch
):
    """200 MiB into /tmp against a 64 MiB cap, then a 60-second sleep. The harvest never archives
    /tmp, so only the watchdog on the writable layer can see this; and it must stop the build WHILE it
    sleeps, not after it finishes."""
    monkeypatch.setattr(settings, "sandbox_disk_poll_interval_seconds", 0.5)
    started = time.monotonic()

    result = _run(
        client,
        ["sh", "-c", "dd if=/dev/zero of=/tmp/junk bs=1M count=200 2>/dev/null; sleep 60"],
        workspace_dir,
        timeout_seconds=120,
    )

    assert result.ok is False
    assert result.reason == BuildFailureReason.RESOURCE_LIMIT
    assert result.detail == "the sandbox's files grew past the 64 MiB disk limit"
    assert result.harvested_workspace_dir is None
    assert time.monotonic() - started < 30  # killed by the watchdog, not run out its sleep
    assert os.listdir(system_temp) == []


def test_a_burst_that_finishes_inside_one_poll_interval_is_caught_by_the_read_after_exit(
    client, workspace_dir, system_temp, no_leaks, monkeypatch
):
    """The interval is 60 s and the command exits at once, so the in-loop poll never fires; only the
    read after the command exits can see 200 MiB in /tmp."""
    monkeypatch.setattr(settings, "sandbox_disk_poll_interval_seconds", 60.0)

    result = _run(client, ["sh", "-c", "dd if=/dev/zero of=/tmp/junk bs=1M count=200 2>/dev/null"], workspace_dir)

    assert result.reason == BuildFailureReason.RESOURCE_LIMIT
    assert result.detail == "the sandbox's files grew past the 64 MiB disk limit"
    assert result.harvested_workspace_dir is None


def test_a_build_inside_the_cap_runs_to_completion_and_its_files_come_back(
    client, workspace_dir, system_temp, no_leaks, monkeypatch
):
    """The non-vacuity half: real writes, in the workspace and outside it, under the cap."""
    monkeypatch.setattr(settings, "sandbox_disk_poll_interval_seconds", 0.2)

    result = _run(
        client,
        [
            "sh",
            "-c",
            "dd if=/dev/zero of=/workspace/out.bin bs=1M count=8 2>/dev/null; "
            "dd if=/dev/zero of=/tmp/scratch bs=1M count=8 2>/dev/null; sleep 1",
        ],
        workspace_dir,
    )

    assert result.ok is True, (result.reason, result.detail, result.logs)
    assert os.path.getsize(os.path.join(result.harvested_workspace_dir, "out.bin")) == 8 * MIB
    assert os.path.isfile(os.path.join(result.harvested_workspace_dir, "input.txt"))


def test_the_engine_ignores_storage_opt_on_this_host_which_is_why_it_is_not_the_cap(client):
    """The measurement behind the design, kept as an executable note. Where the engine enforces
    `storage_opt` (Linux overlay2 on xfs with pquota) this test has nothing to show and skips; where it
    accepts the option and ignores it (Docker Desktop, measured 2026-10-17: 300 MiB written under a
    100M limit), the platform cannot rest the cap on it, and this is the proof that the claim in
    `sandbox_runner._provision` was measured, not assumed."""
    try:
        container = client.containers.create(
            image=settings.sandbox_image, command=["sleep", "30"], storage_opt={"size": "100M"}
        )
    except DockerException as exc:
        pytest.skip(f"this engine refuses storage_opt outright ({type(exc).__name__})")
    try:
        container.start()
        exit_code, _ = container.exec_run(["sh", "-c", "dd if=/dev/zero of=/big bs=1M count=300 2>/dev/null"])
        if exit_code != 0:
            pytest.skip("this engine enforces storage_opt: the write beyond 100M failed")
        container.reload()
        assert container.attrs["HostConfig"]["StorageOpt"] == {"size": "100M"}  # accepted, and recorded...
        assert SandboxRunner(client=client)._writable_layer_bytes(container) > 100 * MIB  # ...and not enforced
    finally:
        container.remove(force=True, v=True)


# --- nothing the build left running survives the command ---------------------------------------------

# A FINITE background writer: 1 MiB every 0.2 s for 60 rounds, 60 MiB in all, which is larger than the
# 32 MiB cap this test runs under. Left alone it would pass the cap some six seconds after the command
# exited, with nothing watching. It is finite on purpose (PLAN: a bounded test must stay bounded under
# mutation): with the stop removed it writes until the run's own teardown removes the container, a few
# seconds, never to its 60 MiB. `nohup` and the redirections let the exec stream end while it runs on.
_BACKGROUND_WRITER = (
    "nohup sh -c 'i=0; while [ $i -lt 60 ]; do "
    "dd if=/dev/zero of=/tmp/bg$i bs=1M count=1 2>/dev/null; i=$((i+1)); sleep 0.2; done' "
    ">/dev/null 2>&1 & sleep 1"
)


def test_a_process_the_build_left_running_is_stopped_before_the_harvest_and_adds_no_bytes_after_it(
    client, workspace_dir, system_temp, no_leaks, monkeypatch
):
    """Measured on the real engine, from inside the runner's own seams: when the harvest starts the container is
    not running; its writable layer is the same size then as at the harvest's start and again 1.5 s later, just
    before the container is removed (a writer still alive adds ~7 MiB in that time); and the build is
    an ordinary success whose tree comes back. Non-vacuity: the writer really ran (at least 1 MiB was in the
    layer when the harvest started). Mutation: remove `_stop_processes` from `_execute` and `running` is True and
    the two later sizes differ from the first."""
    monkeypatch.setattr(settings, "sandbox_disk_poll_interval_seconds", 0.2)
    seen: dict = {}

    class Spy(SandboxRunner):
        def _harvest_workspace(self, container, **kwargs):
            container.reload()
            seen["running_at_harvest"] = container.attrs["State"]["Running"]
            seen["bytes_at_harvest"] = self._writable_layer_bytes(container)
            return super()._harvest_workspace(container, **kwargs)

        def _destroy(self, container):
            seen["bytes_before_destroy"] = self._writable_layer_bytes(container)
            time.sleep(1.5)
            seen["bytes_later"] = self._writable_layer_bytes(container)
            container.reload()
            seen["running_before_destroy"] = container.attrs["State"]["Running"]
            super()._destroy(container)

    result = Spy(client=client).run(
        SandboxRunConfig(
            command=["sh", "-c", _BACKGROUND_WRITER],
            workspace_dir=workspace_dir,
            disk_limit_mb=32,
            timeout_seconds=60,
        )
    )

    assert result.ok is True, (result.reason, result.detail, result.logs)
    assert os.path.isfile(os.path.join(result.harvested_workspace_dir, "input.txt"))  # the same tree comes back
    assert seen["bytes_at_harvest"] >= 1 * MIB  # the writer really ran before the command ended
    assert seen["bytes_at_harvest"] < 32 * MIB
    assert seen["running_at_harvest"] is False
    assert seen["running_before_destroy"] is False
    assert seen["bytes_before_destroy"] == seen["bytes_at_harvest"]
    assert seen["bytes_later"] == seen["bytes_at_harvest"]


# --- the size read's bound holds on a real engine's transport ----------------------------------------


def test_a_size_read_that_the_engine_does_not_answer_in_time_is_given_up_on_at_the_bound(client, monkeypatch):
    """The bound is only worth having if this transport honours it (measured 2026-10-28 on Docker Desktop's named
    pipe: a 0.3 s bound on a 0.7 s read, 400,000 empty files in the layer, raised `ReadTimeout` after 0.31 s). A
    bound so small no engine answers inside it makes the same read come back None quickly; with the real bound
    the same call answers. Non-vacuity is that second call: it is the same container, read right after."""
    container = client.containers.create(image=settings.sandbox_image, command=["sleep", "30"])
    try:
        container.start()
        runner = SandboxRunner(client=client)
        assert runner._writable_layer_bytes(container) is not None  # answers within the real bound

        monkeypatch.setattr(sr, "SIZE_READ_TIMEOUT_SECONDS", 0.0001)
        started = time.monotonic()
        used = runner._writable_layer_bytes(container)
        elapsed = time.monotonic() - started
    finally:
        SandboxRunner(client=client)._destroy(container)  # the runner's own removal (force, with its volumes)

    assert used is None
    assert elapsed < 2.0


# --- never held in memory ------------------------------------------------------------------------------------


def test_harvesting_a_96_mib_workspace_never_holds_the_archive_in_memory(client, workspace_dir, system_temp, no_leaks):
    """Measured, not asserted: the peak of Python's own allocations while a real 96 MiB workspace is
    staged, run, archived by the daemon, streamed and extracted. A harvest that read the stream into a
    buffer first holds the whole archive at once (more than 96 MiB); streamed, the peak is a few chunks."""
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        result = _run(
            client,
            ["sh", "-c", "dd if=/dev/zero of=/workspace/blob bs=1M count=96 2>/dev/null"],
            workspace_dir,
            disk_limit_mb=512,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.ok is True, (result.reason, result.detail)
    assert os.path.getsize(os.path.join(result.harvested_workspace_dir, "blob")) == 96 * MIB
    assert peak < 48 * MIB, f"peak Python allocation {peak / MIB:.1f} MiB while harvesting a 96 MiB workspace"


# --- does the cap hold on this host? ---------------------------------------------------------------------------


def test_the_startup_probe_finds_no_gap_on_this_host(client):
    assert SandboxRunner(client=client).probe_disk_cap() is None
