"""Unit tests for the L3 sandbox runner using a fake Docker client.

These assert the invariant-relevant parameters passed to
`containers.create` / `exec_create` without touching a real Docker Engine,
so they run everywhere (no `integration` marker, no skip).

Two more groups sit at the bottom: the configurable container runtime
(`Settings.build_container_runtime` / `SandboxRunConfig.container_runtime`)
and — in the same file on purpose — the assertions that pin that setting's
blast radius to L3 build containers only, so someone changing the setting
sees both halves of its contract in one place.
"""

from __future__ import annotations

import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import sandbox_runner as sr
from app.services.sandbox_runner import SandboxRunConfig, SandboxRunner
from app.services.status import BuildFailureReason
from tests.engine_list_shim import ContainerListViaContainers
from tests.fake_admin_dsn import FAKE_ADMIN_DSN


class FakeExecStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __iter__(self):
        return iter(self._chunks)


class FakeRunningExecStream:
    """a command that is still running. The iterator waits until the container is
    killed, or until `end_after` seconds have passed (the command "finishing by itself");
    a safety limit keeps a broken test from hanging."""

    def __init__(self, container_id, end_after=None):
        self._container_id = container_id
        self._end_after = end_after

    def __iter__(self):
        started = time.monotonic()
        limit = self._end_after if self._end_after is not None else 10.0
        while time.monotonic() - started < limit:
            if FakeContainer.instances[self._container_id].killed:
                break
            time.sleep(0.005)
        return iter(())


class FakeArchiveResponse:
    """What `client.api._get(..., stream=True)` returns for the archive request:
    `iter_content` over the container's archive chunks, and a `closed` flag the tests read."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False
        self.chunks_read = 0

    def iter_content(self, chunk_size):
        for chunk in self._chunks:
            self.chunks_read += 1
            yield chunk

    def close(self):
        self.closed = True


class FakeContainer:
    instances: dict[str, "FakeContainer"] = {}  # the archive request names a container by id

    def __init__(self, create_kwargs):
        self.create_kwargs = create_kwargs
        self.id = "fake-container-id"
        FakeContainer.instances[self.id] = self
        self.started = False
        self.removed = False
        self.remove_force = None
        self.remove_v = None
        self.attrs = {"State": {"OOMKilled": False}}
        self.put_archive_calls = []
        self.exec_run_calls = []

    def start(self):
        self.started = True
        self.attrs["State"]["Running"] = True

    def exec_run(self, cmd, user=None):
        self.exec_run_calls.append({"cmd": cmd, "user": user})
        return 0, b""

    def put_archive(self, path, data):
        self.put_archive_calls.append((path, data))

    def get_archive(self, path):
        import io
        import tarfile

        # Mirrors real Docker Engine behavior: get_archive(dir) returns the
        # requested directory as a single top-level tar entry, not its
        # contents at the tar root.
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            info = tarfile.TarInfo(name="workspace/harvested.txt")
            payload = b"harvested"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        tar_stream.seek(0)
        return iter([tar_stream.read()]), {}

    def reload(self):
        pass

    killed = False

    def kill(self):
        self.killed = True
        self.attrs["State"]["Running"] = False  # the run reads the state back to confirm the stop

    def remove(self, v=False, force=False):
        self.removed = True
        self.remove_force = force
        self.remove_v = v


class FakeNetwork:
    def __init__(self, name):
        self.name = name
        self.removed = False
        self.connected: list[str] = []

    def remove(self):
        self.removed = True

    def connect(self, container):
        # `DeployManager._connect_data_services` attaches the shared
        # Postgres/Redis containers via `network.connect(container)`. Only
        # `FakeRunClient`'s tests (below) ever reach this — the plain
        # `SandboxRunner` tests above never attach a second container to
        # their own per-run network — so recording the id/name is enough,
        # no test currently asserts against it.
        self.connected.append(getattr(container, "id", container))


class FakeNetworks:
    def __init__(self):
        self.created = []
        self.last: FakeNetwork | None = None

    def get(self, name):
        import docker

        raise docker.errors.NotFound("no such network")

    def create(self, name, driver=None, internal=None, labels=None):
        # `DeployManager.start_service` labels the network it creates
        # (`forge.deployment_id`), so the fake accepts what Docker does.
        self.created.append({"name": name, "driver": driver, "internal": internal})
        network = FakeNetwork(name)
        self.last = network
        return network


class FakeContainersCollection:
    def __init__(self):
        self.create_calls = []
        self._last: FakeContainer | None = None

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        container = FakeContainer(kwargs)
        self._last = container
        return container


class FakeExecApi(ContainerListViaContainers):
    """Fake for `client.api.exec_create` / `exec_start` / `exec_inspect`, and
    for the writable-layer size read, `client.api.containers(size=True, ...)`.

    `size_rw` is what that read answers: an int (every poll), a callable taking the
    1-based poll number, an Exception instance (raised), or `None` (the row has no
    `SizeRw` at all, which is what an engine that does not report it looks like)."""

    def __init__(self, exit_code=0, stream_chunks=(b"hello\n",), size_rw=0, running=False, end_after=None):
        self._exit_code = exit_code
        self._stream_chunks = stream_chunks
        self._size_rw = size_rw
        self._running = running  # the command does not finish until killed (or `end_after`)
        self._end_after = end_after
        self.size_polls = 0
        self.exec_create_calls = []
        self._exec_container_id = None

    # The archive request (`SandboxRunner._archive_chunks`) uses docker-py's low-level helpers.
    last_archive_response: FakeArchiveResponse | None = None

    def _url(self, pattern, *args):
        return pattern.format(*args)

    def _get(self, url, params=None, stream=False, headers=None, **kwargs):
        if url == "/containers/json":  # the size read (`list_containers_bounded`)
            return self._list_get(url, params, kwargs.get("timeout"))
        container_id = url.split("/")[2]
        chunks, _ = FakeContainer.instances[container_id].get_archive(params["path"])
        self.last_archive_response = FakeArchiveResponse(chunks)
        return self.last_archive_response

    def _raise_for_status(self, response):
        pass

    def containers(self, all=False, size=False, filters=None, **kwargs):
        self.size_polls += 1
        container_id = filters["id"][0]
        value = self._size_rw(self.size_polls) if callable(self._size_rw) else self._size_rw
        if isinstance(value, BaseException):
            raise value
        row = {"Id": container_id}
        if value is not None:
            row["SizeRootFs"] = 141_250_560  # an engine that computes sizes says so...
            if value != 0:
                row["SizeRw"] = value  # ...and omits SizeRw for an empty layer (measured on overlay2)
        return [row]

    def exec_create(self, container_id, cmd, workdir=None, user=None):
        self.exec_create_calls.append(
            {"container_id": container_id, "cmd": cmd, "workdir": workdir, "user": user}
        )
        self._exec_container_id = container_id
        return {"Id": "fake-exec-id"}

    def exec_start(self, exec_id, stream=True, demux=False):
        if self._running:
            return FakeRunningExecStream(self._exec_container_id, end_after=self._end_after)
        return FakeExecStream(self._stream_chunks)

    def exec_inspect(self, exec_id):
        return {"ExitCode": self._exit_code}


class FakeDockerClient:
    def __init__(self, exit_code=0, stream_chunks=(b"hello\n",), size_rw=0, running=False, end_after=None):
        self.networks = FakeNetworks()
        self.containers = FakeContainersCollection()
        self.api = FakeExecApi(
            exit_code=exit_code,
            stream_chunks=stream_chunks,
            size_rw=size_rw,
            running=running,
            end_after=end_after,
        )


@pytest.fixture(autouse=True)
def system_temp(tmp_path_factory, monkeypatch):
    """every `run()` here harvests into a fresh temp directory that nobody in
    these tests removes. Pointing the temp root at a pytest-owned directory keeps
    them out of the host's real temp dir (where they accumulated, thousands of them,
    one per test run), and lets the harvest tests below say exactly what a harvest
    leaves behind."""
    root = tmp_path_factory.mktemp("systemp")
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


@pytest.fixture()
def workspace_dir(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')")
    return str(tmp_path)


def _make_config(workspace_dir, command=("echo", "hi")):
    return SandboxRunConfig(command=list(command), workspace_dir=workspace_dir)


def test_create_params_have_no_docker_socket_mount(workspace_dir):
    """Invariant 2: no Docker socket, no host path other than the tar
    airlock (no bind-mount `volumes` at all)."""
    fake_client = FakeDockerClient()
    runner = SandboxRunner(client=fake_client)

    runner.run(_make_config(workspace_dir))

    assert len(fake_client.containers.create_calls) == 1
    create_kwargs = fake_client.containers.create_calls[0]
    volumes = create_kwargs.get("volumes")
    assert not volumes  # empty dict/None — no bind mounts at all
    for value in (volumes or {}).values():
        assert "/var/run/docker.sock" not in str(value)


def test_create_params_not_privileged(workspace_dir):
    fake_client = FakeDockerClient()
    runner = SandboxRunner(client=fake_client)

    runner.run(_make_config(workspace_dir))

    create_kwargs = fake_client.containers.create_calls[0]
    assert create_kwargs.get("privileged") is False


def test_create_params_drop_all_capabilities_and_no_new_privileges(workspace_dir):
    """All capabilities dropped except CHOWN, which is required only for the
    root workspace-setup step (see sandbox_runner.SANDBOX_USER docstring)
    and is not exercisable by the non-root executed command."""
    fake_client = FakeDockerClient()
    runner = SandboxRunner(client=fake_client)

    runner.run(_make_config(workspace_dir))

    create_kwargs = fake_client.containers.create_calls[0]
    assert create_kwargs.get("cap_drop") == ["ALL"]
    assert create_kwargs.get("cap_add") == ["CHOWN"]
    assert "no-new-privileges" in create_kwargs.get("security_opt", [])


def test_create_params_attach_only_isolated_internal_network(workspace_dir):
    """Invariant 3: attaches only to a fresh, isolated, internal=True
    network created per run (not one shared network — see
    SandboxRunner._create_run_network for why sharing would let two
    concurrent sandboxes reach each other)."""
    fake_client = FakeDockerClient()
    runner = SandboxRunner(client=fake_client)

    runner.run(_make_config(workspace_dir))

    create_kwargs = fake_client.containers.create_calls[0]
    assert len(fake_client.networks.created) == 1
    created_network = fake_client.networks.created[0]
    assert created_network["name"].startswith(settings.sandbox_network_name)
    assert created_network["driver"] == "bridge"
    assert created_network["internal"] is True
    assert create_kwargs.get("network") == created_network["name"]


def test_network_is_destroyed_unconditionally_on_success(workspace_dir):
    fake_client = FakeDockerClient()
    runner = SandboxRunner(client=fake_client)

    runner.run(_make_config(workspace_dir))

    assert fake_client.networks.last is not None
    assert fake_client.networks.last.removed is True


def test_create_params_apply_resource_caps_from_settings(workspace_dir):
    """Invariant 4: memory/CPU/pids caps sourced from Settings."""
    fake_client = FakeDockerClient()
    runner = SandboxRunner(client=fake_client)

    runner.run(_make_config(workspace_dir))

    create_kwargs = fake_client.containers.create_calls[0]
    assert create_kwargs.get("mem_limit") == f"{settings.sandbox_memory_limit_mb}m"
    assert create_kwargs.get("nano_cpus") == int(settings.sandbox_cpu_limit_cores * 1_000_000_000)
    assert create_kwargs.get("pids_limit") == settings.sandbox_pids_limit


def test_execute_runs_command_as_non_root_user(workspace_dir):
    fake_client = FakeDockerClient()
    runner = SandboxRunner(client=fake_client)

    runner.run(_make_config(workspace_dir, command=["echo", "hi"]))

    assert len(fake_client.api.exec_create_calls) == 1
    exec_call = fake_client.api.exec_create_calls[0]
    assert exec_call["user"] == sr.SANDBOX_USER
    assert exec_call["cmd"] == ["echo", "hi"]


def test_container_is_destroyed_unconditionally_on_success(workspace_dir):
    fake_client = FakeDockerClient(exit_code=0)
    runner = SandboxRunner(client=fake_client)

    result = runner.run(_make_config(workspace_dir))

    assert result.ok is True
    container = fake_client.containers._last
    assert container.removed is True
    assert container.remove_force is True
    assert container.remove_v is True  # anonymous volumes go with the container


def test_container_is_destroyed_unconditionally_on_nonzero_exit(workspace_dir):
    fake_client = FakeDockerClient(exit_code=1)
    runner = SandboxRunner(client=fake_client)

    result = runner.run(_make_config(workspace_dir))

    assert result.ok is False
    assert result.reason == BuildFailureReason.AGENT_ERROR
    container = fake_client.containers._last
    assert container.removed is True


def test_container_is_destroyed_when_staging_raises(workspace_dir):
    fake_client = FakeDockerClient()

    class ExplodingContainer(FakeContainer):
        def exec_run(self, cmd, user=None):
            raise RuntimeError("boom during workspace setup")

    original_create = fake_client.containers.create

    def create_and_explode(**kwargs):
        fake_client.containers.create_calls.append(kwargs)
        container = ExplodingContainer(kwargs)
        fake_client.containers._last = container
        return container

    fake_client.containers.create = create_and_explode
    runner = SandboxRunner(client=fake_client)

    result = runner.run(_make_config(workspace_dir))

    assert result.ok is False
    assert result.reason == BuildFailureReason.INFRA_ERROR
    container = fake_client.containers._last
    assert container.removed is True


def test_nonzero_exit_reports_agent_error(workspace_dir):
    fake_client = FakeDockerClient(exit_code=42)
    runner = SandboxRunner(client=fake_client)

    result = runner.run(_make_config(workspace_dir))

    assert result.ok is False
    assert result.reason == BuildFailureReason.AGENT_ERROR
    assert result.exit_code == 42


def test_logs_are_size_capped():
    fake_client = FakeDockerClient(stream_chunks=[b"x" * 10_000_000])
    runner = SandboxRunner(client=fake_client)

    import tempfile

    with tempfile.TemporaryDirectory() as workspace:
        result = runner.run(_make_config(workspace))

    assert len(result.logs.encode("utf-8")) <= settings.sandbox_log_max_bytes + len(
        "\n...[log output truncated]"
    )
    assert "truncated" in result.logs


# -- configurable container runtime -------------------------------
#
# ADR 0003 §Residual risk names "make the sandbox runtime a configuration
# value" as the trigger for revisiting L3 isolation at all. The runtime
# strings below are literals on both sides on purpose: nothing is really
# started against the fake client, so the value only has to travel intact.


def test_unset_runtime_passes_no_runtime_argument_at_all(workspace_dir):
    """Unconfigured must reproduce today's behaviour exactly: the `runtime`
    key is ABSENT — not `None`, and not the literal "runc". On a host whose
    `default-runtime` is not runc, sending "runc" would silently change which
    runtime a build container gets, so "unspecified" has to stay
    unspecified."""
    fake_client = FakeDockerClient()
    runner = SandboxRunner(client=fake_client)

    runner.run(_make_config(workspace_dir))

    create_kwargs = fake_client.containers.create_calls[0]
    assert "runtime" not in create_kwargs


def test_settings_runtime_is_passed_through_verbatim(workspace_dir, monkeypatch):
    monkeypatch.setattr(settings, "build_container_runtime", "runsc")
    fake_client = FakeDockerClient()
    runner = SandboxRunner(client=fake_client)

    runner.run(_make_config(workspace_dir))

    create_kwargs = fake_client.containers.create_calls[0]
    assert create_kwargs["runtime"] == "runsc"


def test_per_run_config_runtime_applies_without_any_setting(workspace_dir):
    """The per-run override follows the same unset-defaults-from-Settings
    pattern as image/memory_limit_mb/cpu_limit_cores — so a one-off
    measurement needs no global config change."""
    fake_client = FakeDockerClient()
    runner = SandboxRunner(client=fake_client)

    runner.run(
        SandboxRunConfig(
            command=["echo", "hi"], workspace_dir=workspace_dir, container_runtime="runsc"
        )
    )

    create_kwargs = fake_client.containers.create_calls[0]
    assert create_kwargs["runtime"] == "runsc"


def test_per_run_config_runtime_overrides_the_setting(workspace_dir, monkeypatch):
    monkeypatch.setattr(settings, "build_container_runtime", "runsc")
    fake_client = FakeDockerClient()
    runner = SandboxRunner(client=fake_client)

    runner.run(
        SandboxRunConfig(
            command=["echo", "hi"], workspace_dir=workspace_dir, container_runtime="kata"
        )
    )

    create_kwargs = fake_client.containers.create_calls[0]
    assert create_kwargs["runtime"] == "kata"


def test_selecting_a_runtime_weakens_no_adr_0003_invariant(workspace_dir, monkeypatch):
    """Selecting a runtime is a runtime-only change: all four invariants are
    still enforced, in the same places, on whatever runtime is chosen."""
    monkeypatch.setattr(settings, "build_container_runtime", "runsc")
    fake_client = FakeDockerClient()
    runner = SandboxRunner(client=fake_client)

    runner.run(_make_config(workspace_dir))

    create_kwargs = fake_client.containers.create_calls[0]
    assert create_kwargs["runtime"] == "runsc"
    # 2: no Docker socket / no host path, not privileged.
    assert not create_kwargs.get("volumes")
    assert create_kwargs.get("privileged") is False
    assert create_kwargs.get("cap_drop") == ["ALL"]
    assert "no-new-privileges" in create_kwargs.get("security_opt", [])
    # 3: still its own per-run default-deny network.
    assert fake_client.networks.created[0]["internal"] is True
    assert create_kwargs.get("network") == fake_client.networks.created[0]["name"]
    # 4: caps still applied, command still run as nobody.
    assert create_kwargs.get("mem_limit") == f"{settings.sandbox_memory_limit_mb}m"
    assert create_kwargs.get("pids_limit") == settings.sandbox_pids_limit
    assert fake_client.api.exec_create_calls[0]["user"] == sr.SANDBOX_USER
    # 1: single-use — container and its network destroyed regardless.
    assert fake_client.containers._last.removed is True
    assert fake_client.networks.last.removed is True


# -- the setting's blast radius: L3 build containers only ----------
#
# ADR 0003 §Ordering hardens the BUILD container first, deliberately: it is
# the one that executes code no artifact records and no check inspects. An L4
# service container runs an image a human confirmed, and a preview runs the
# byte-identical image promotion puts into L4 — promoting a hostile image onto
# a stronger runtime is not what this seam is for.
#
# Both of those call sites DO read the `sandbox_*` resource caps, so "every
# sandbox_* value applies wherever the others do" is exactly the misreading
# that must fail loudly. A grep for the setting name cannot catch it (the
# failure mode is a later edit, not today's code), so these drive the real
# DeployManager / PreviewManager against a fake client with the setting turned
# ON and assert the runtime never reaches Docker.


class FakeRunContainer:
    def __init__(self, create_kwargs):
        self.create_kwargs = create_kwargs
        self.id = "fake-run-container-id"
        self.labels = dict(create_kwargs.get("labels") or {})
        self.status = "running"

    def remove(self, v=False, force=False):
        pass


class FakeRunContainers:
    """Fake for the `containers.run`-based managers. `list`/`get` cover the
    idempotent teardown probes that run before a start.

    `known_containers` lets a test make this fake answer for a
    SPECIFIC, deliberately-chosen name (e.g. the shared Postgres/Redis
    containers `DeployManager._connect_data_services` looks up) without
    changing `get()`'s behaviour for every OTHER name — those must keep
    raising `NotFound` so the pre-existing idempotent-teardown-probe tests
    are unaffected."""

    def __init__(self, known_containers: dict | None = None):
        self.run_calls = []
        self._known = dict(known_containers or {})

    def run(self, **kwargs):
        self.run_calls.append(kwargs)
        return FakeRunContainer(kwargs)

    def list(self, all=False, filters=None):
        return []

    def get(self, name):
        import docker

        if name in self._known:
            return self._known[name]
        raise docker.errors.NotFound(f"no such container: {name}")


class FakeRunClient:
    def __init__(self, known_containers: dict | None = None):
        self.containers = FakeRunContainers(known_containers)
        self.networks = FakeNetworks()


def test_l4_deploy_container_never_receives_the_build_runtime(monkeypatch):
    """`service_postgres_admin_dsn` is set EXPLICITLY here,
    never inherited from whatever `backend/.env` happens to hold on the
    machine running the suite — `DeployManager.start_service` now attaches
    the shared Postgres/Redis to the deployment's network
    (`_connect_data_services`) whenever `service_postgres_configured` is
    true, and that must not be ambient. `FakeRunClient` is told about both
    container names so the attach genuinely succeeds against the fake
    (rather than the unrelated `runtime`-leak assertion below depending on
    data services being silently skipped)."""
    from app.services.deploy_manager import DeployManager
    from app.services.service_data import redis_container_name

    monkeypatch.setattr(settings, "build_container_runtime", "runsc")
    monkeypatch.setattr(settings, "preview_container_runtime", "runsc")  # neither seam reaches L4
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    project_id = "proj-1"
    fake_client = FakeRunClient(
        known_containers={
            settings.service_postgres_container_name: FakeRunContainer({}),
            redis_container_name(project_id): FakeRunContainer({}),
        }
    )

    DeployManager(client=fake_client).start_service(
        project_id=project_id,
        deployment_id="dep-1",
        build_id="bld-1",
        image_ref="forge-build-img",
        env={},
    )

    assert len(fake_client.containers.run_calls) == 1
    run_kwargs = fake_client.containers.run_calls[0]
    assert "runtime" not in run_kwargs
    # Not a vacuous assertion: this same call really does pick up the
    # `sandbox_*` caps, which is the whole reason the runtime setting had to
    # be named so it cannot be mistaken for another one of them.
    assert run_kwargs["mem_limit"] == f"{settings.sandbox_memory_limit_mb}m"


def _start_preview(monkeypatch, *, build_runtime, preview_runtime):
    """`service_postgres_admin_dsn` is OFF here (never ambient): this is about the runtime
    argument, orthogonal to the preview data services, and `FakeRunClient` has no real Postgres."""
    from types import SimpleNamespace

    from app.services.preview_manager import PreviewManager

    monkeypatch.setattr(settings, "build_container_runtime", build_runtime)
    monkeypatch.setattr(settings, "preview_container_runtime", preview_runtime)
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)
    fake_client = FakeRunClient()

    PreviewManager(client=fake_client).start_preview(
        project_id="proj-1", build=SimpleNamespace(id="bld-1", image_ref="forge-build-img")
    )

    assert len(fake_client.containers.run_calls) == 1
    return fake_client.containers.run_calls[0]


def test_l3_preview_container_defaults_to_the_build_runtime(monkeypatch):
    """Changed on purpose (previews were once pinned OFF the seam): ADR 0046 §Decision 2."""
    run_kwargs = _start_preview(monkeypatch, build_runtime="runsc", preview_runtime=None)

    assert run_kwargs["runtime"] == "runsc"
    assert run_kwargs["mem_limit"] == f"{settings.sandbox_memory_limit_mb}m"


def test_l3_preview_container_runs_as_nobody(monkeypatch):
    """ADR 0046 §Decision 5. The deploy half is pinned as it is TODAY, so moving L4 to a user
    is a deliberate, visible change of this assertion."""
    run_kwargs = _start_preview(monkeypatch, build_runtime=None, preview_runtime=None)

    assert run_kwargs["user"] == "65534:65534"
    assert run_kwargs["cap_drop"] == ["ALL"]  # the non-root user is on top of the dropped capabilities


def test_l3_preview_runtime_of_its_own_wins_over_the_build_runtime(monkeypatch):
    run_kwargs = _start_preview(monkeypatch, build_runtime="runsc", preview_runtime="kata")

    assert run_kwargs["runtime"] == "kata"


def test_l3_preview_runtime_set_alone_does_not_touch_the_build(monkeypatch, workspace_dir):
    """`PREVIEW_CONTAINER_RUNTIME` is for previews only: a build container created with it set sends no key."""
    monkeypatch.setattr(settings, "build_container_runtime", None)
    monkeypatch.setattr(settings, "preview_container_runtime", "runsc")
    fake_client = FakeDockerClient()

    SandboxRunner(client=fake_client).run(_make_config(workspace_dir))

    assert "runtime" not in fake_client.containers.create_calls[0]


def test_l3_preview_container_with_no_runtime_configured_sends_no_runtime_key(monkeypatch):
    """The rule from before, still: unset emits no key at all (not None, not "runc")."""
    run_kwargs = _start_preview(monkeypatch, build_runtime=None, preview_runtime=None)

    assert "runtime" not in run_kwargs


def test_l3_preview_blank_runtime_settings_count_as_unset(monkeypatch):
    run_kwargs = _start_preview(monkeypatch, build_runtime="  ", preview_runtime="")

    assert "runtime" not in run_kwargs


# -- cgroup v2 OOM signal: `_cgroup_oom_kill_observed` -------------
#
# This method is the second, more reliable OOM signal (see its
# docstring in sandbox_runner.py for the measured evidence) but left its
# decision logic without a direct pin — only the Docker-engine-dependent
# integration test exercised it, and that test still passes most of the
# time even when this method is broken, because `State.OOMKilled` (checked
# first, unaffected here) also succeeds most of the time on its own. These
# tests call `_cgroup_oom_kill_observed` directly against a fake container,
# so the decision logic is pinned deterministically with no real container
# and no Docker Engine involved (runs in the engine-off suite).
#
# The `memory.events` payloads below are realistic cgroup v2 fixtures with
# several counters in kernel-doc order (low, high, max, oom, oom_kill,
# oom_group_kill) — not a one-line fixture built just for this test. The
# false-case fixture keeps `low`/`high`/`max` and the adjacent `oom`
# counter (a literal prefix of `oom_kill`, and the single most plausible
# line for a wrong-line parser bug to read instead) nonzero while
# `oom_kill` itself is 0 (`oom_group_kill` stays 0 too — it sits after
# `oom_kill`, not adjacent to it, so it adds nothing to the trap). So
# `oom` and `oom_kill` deliberately disagree in sign, and a parser that
# reads the wrong line, or treats "any nonzero counter" as the signal,
# disagrees with the correct answer and fails these tests instead of
# passing by coincidence. Neither the cgroup path nor the counter key is
# a named constant in sandbox_runner.py (both are literals inline in
# `_cgroup_oom_kill_observed`), so there is nothing to import here without
# re-typing them — these bytes are this test's own literals.

REALISTIC_MEMORY_EVENTS_WITH_OOM_KILL = (
    b"low 0\n"
    b"high 0\n"
    b"max 12\n"
    b"oom 1\n"
    b"oom_kill 2\n"
    b"oom_group_kill 0\n"
)

REALISTIC_MEMORY_EVENTS_WITHOUT_OOM_KILL = (
    b"low 3\n"
    b"high 1\n"
    b"max 7\n"
    b"oom 1\n"
    b"oom_kill 0\n"
    b"oom_group_kill 0\n"
)

MEMORY_EVENTS_MISSING_OOM_KILL_LINE = b"low 0\nhigh 0\nmax 0\noom 0\n"

MEMORY_EVENTS_MALFORMED_OOM_KILL_VALUE = (
    b"low 0\n"
    b"high 0\n"
    b"max 0\n"
    b"oom 0\n"
    b"oom_kill not-a-number\n"
    b"oom_group_kill 0\n"
)


HEX_ID = "9f2c" * 16  # a Docker container id is 64 hex characters


class HostCgroupContainer(FakeContainer):
    """a container whose cgroup is read from the HOST, so `exec_run` is a trap: any exec at
    all fails the test that triggered it (the read must not touch the container)."""

    def __init__(self, *, container_id: str = HEX_ID, cgroup_parent: str | None = None):
        super().__init__(create_kwargs={})
        self.id = container_id
        self.attrs = {"State": {"OOMKilled": False}, "HostConfig": {"CgroupParent": cgroup_parent or ""}}

    def exec_run(self, cmd, user=None):
        self.exec_run_calls.append({"cmd": cmd, "user": user})
        raise AssertionError(f"the OOM read must not exec into the container: {cmd!r}")


@pytest.fixture
def cgroup_root(tmp_path, monkeypatch):
    """A stand-in for the host's `/sys/fs/cgroup`, empty until a test writes a file under it."""
    from app.services import sandbox_runner

    root = tmp_path / "cgroup"
    root.mkdir()
    monkeypatch.setattr(sandbox_runner, "HOST_CGROUP_ROOT", str(root))
    return root


def _write_events(root, relative_dir, payload: bytes):
    directory = root / relative_dir
    directory.mkdir(parents=True)
    (directory / "memory.events").write_bytes(payload)


def _cgroup_oom_kill_observed(container) -> bool:
    return SandboxRunner(client=FakeDockerClient())._cgroup_oom_kill_observed(container)


def test_cgroup_oom_kill_observed_true_when_oom_kill_counter_nonzero(cgroup_root):
    _write_events(cgroup_root, f"system.slice/docker-{HEX_ID}.scope", REALISTIC_MEMORY_EVENTS_WITH_OOM_KILL)
    container = HostCgroupContainer()

    assert _cgroup_oom_kill_observed(container) is True
    assert container.exec_run_calls == []  # read from the host: nothing was exec'd (the trap would have raised)


def test_the_cgroupfs_driver_path_is_read_too(cgroup_root):
    _write_events(cgroup_root, f"docker/{HEX_ID}", REALISTIC_MEMORY_EVENTS_WITH_OOM_KILL)

    assert _cgroup_oom_kill_observed(HostCgroupContainer()) is True


def test_a_configured_cgroup_parent_is_where_the_file_is_looked_for(cgroup_root):
    """`HostConfig.CgroupParent` moves the container's cgroup; a file at the default place is not its."""
    _write_events(cgroup_root, f"system.slice/docker-{HEX_ID}.scope", REALISTIC_MEMORY_EVENTS_WITH_OOM_KILL)
    _write_events(cgroup_root, f"forge.slice/docker-{HEX_ID}.scope", REALISTIC_MEMORY_EVENTS_WITHOUT_OOM_KILL)

    assert _cgroup_oom_kill_observed(HostCgroupContainer(cgroup_parent="forge.slice")) is False


def test_cgroup_oom_kill_observed_false_when_oom_kill_counter_zero(cgroup_root):
    """`low`/`high`/`max` and the adjacent `oom` counter -- a literal
    prefix of `oom_kill` and the single most plausible line for a
    wrong-line parser bug to read instead -- are nonzero while `oom_kill`
    itself is 0 (`oom_group_kill` stays 0 too, since it isn't adjacent to
    `oom_kill`). A parser that reads the wrong line, or treats "any
    nonzero counter" as the signal rather than reading `oom_kill`
    specifically, disagrees with the correct answer and fails here."""
    _write_events(cgroup_root, f"system.slice/docker-{HEX_ID}.scope", REALISTIC_MEMORY_EVENTS_WITHOUT_OOM_KILL)

    assert _cgroup_oom_kill_observed(HostCgroupContainer()) is False


def test_cgroup_oom_kill_observed_false_when_oom_kill_counter_absent(cgroup_root):
    _write_events(cgroup_root, f"system.slice/docker-{HEX_ID}.scope", MEMORY_EVENTS_MISSING_OOM_KILL_LINE)

    assert _cgroup_oom_kill_observed(HostCgroupContainer()) is False


def test_cgroup_oom_kill_observed_false_when_oom_kill_value_malformed(cgroup_root):
    _write_events(cgroup_root, f"system.slice/docker-{HEX_ID}.scope", MEMORY_EVENTS_MALFORMED_OOM_KILL_VALUE)

    assert _cgroup_oom_kill_observed(HostCgroupContainer()) is False


def test_cgroup_oom_kill_observed_false_when_the_host_has_no_such_file(cgroup_root):
    """Docker Desktop (the engine is in a VM), cgroup v1, a container already gone: no file on this
    host, so no additional signal -- `State.OOMKilled` alone decides -- and never an exception."""
    assert _cgroup_oom_kill_observed(HostCgroupContainer()) is False


def test_cgroup_oom_kill_observed_false_when_the_file_cannot_be_read(cgroup_root, monkeypatch):
    """Every failure mode is claimed to degrade to False, and the caller in `_execute` never
    wraps this call in its own try/except: pin an unreadable file (an OSError) rather than trust it."""
    import builtins

    _write_events(cgroup_root, f"system.slice/docker-{HEX_ID}.scope", REALISTIC_MEMORY_EVENTS_WITH_OOM_KILL)
    real_open = builtins.open

    def refusing_open(path, *args, **kwargs):
        if str(path).endswith("memory.events"):
            raise PermissionError("denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", refusing_open)

    assert _cgroup_oom_kill_observed(HostCgroupContainer()) is False


@pytest.mark.parametrize("bad_id", ["../../etc", "a/b", "", ".hidden", "a\\b", "x..y"])
def test_a_container_id_that_could_steer_the_path_reads_nothing(cgroup_root, bad_id):
    """The id is joined into a host path. Files the id would reach with a separator or `..` exist
    here, so a missing check would read one and answer True."""
    (cgroup_root / "etc").mkdir()
    (cgroup_root / "etc" / "memory.events").write_bytes(REALISTIC_MEMORY_EVENTS_WITH_OOM_KILL)
    _write_events(cgroup_root, "docker/a", REALISTIC_MEMORY_EVENTS_WITH_OOM_KILL)

    assert _cgroup_oom_kill_observed(HostCgroupContainer(container_id=bad_id)) is False


def test_no_exec_runs_in_a_sandbox_after_the_agent_step_starts(workspace_dir):
    """ADR 0046 §Decision 6: the only root execs are the two staging steps, before the agent
    command; everything exec'd after (the pre-flight walk) runs as the sandbox user, and the OOM read
    is not an exec at all."""
    fake_client = _client_with_container(FakeContainer)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir))

    assert result.ok is True
    calls = fake_client.containers._last.exec_run_calls
    root_calls = [c["cmd"] for c in calls if c["user"] is None]
    assert [cmd[-1] for cmd in root_calls] == [
        "mkdir -p /workspace",
        "chown -R 65534:65534 /workspace",
    ]
    assert [c["user"] for c in calls[:2]] == [None, None]  # the root execs are the FIRST two
    assert all(c["user"] == "65534:65534" for c in calls[2:])
    assert not any("memory.events" in " ".join(c["cmd"]) for c in calls)


# --- a harvest leaves exactly one directory, whose top level is the workspace ---


def test_the_harvest_is_one_directory_holding_the_workspace_at_its_top_level(workspace_dir, system_temp):
    """The tar nests the workspace under `workspace/`. That nesting is resolved here,
    once, and the returned directory is the ONLY thing the harvest leaves in the temp
    dir: its owner removes it and nothing else remains. (Earlier the nested path
    was returned and its `forge-harvest-*` parent was left behind for every build.)"""
    runner = SandboxRunner(client=FakeDockerClient())

    result = runner.run(_make_config(workspace_dir))

    assert result.ok is True
    harvested = result.harvested_workspace_dir
    assert os.path.basename(harvested).startswith("forge-harvest-")
    assert os.listdir(harvested) == ["harvested.txt"]  # contents at the top level, not under workspace/
    assert os.listdir(system_temp) == [os.path.basename(harvested)]  # no scratch or parent left


def test_a_failed_harvest_leaves_nothing_behind(workspace_dir, system_temp):
    class NoArchiveContainer(FakeContainer):
        def get_archive(self, path):
            raise RuntimeError("archive stream broke")

    fake_client = FakeDockerClient()
    fake_client.containers.create = lambda **kw: NoArchiveContainer(kw)
    runner = SandboxRunner(client=fake_client)

    result = runner.run(_make_config(workspace_dir))

    assert result.ok is False
    assert result.reason == BuildFailureReason.HARVEST_ERROR
    assert result.harvested_workspace_dir is None
    assert os.listdir(system_temp) == []


def test_a_tar_without_the_workspace_nesting_is_still_one_directory(workspace_dir, system_temp):
    import io
    import tarfile

    class FlatContainer(FakeContainer):
        def get_archive(self, path):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                info = tarfile.TarInfo(name="flat.txt")
                info.size = 3
                tar.addfile(info, io.BytesIO(b"abc"))
            return iter([buf.getvalue()]), {}

    fake_client = FakeDockerClient()
    fake_client.containers.create = lambda **kw: FlatContainer(kw)

    result = SandboxRunner(client=fake_client).run(_make_config(workspace_dir))

    assert os.listdir(result.harvested_workspace_dir) == ["flat.txt"]
    assert os.listdir(system_temp) == [os.path.basename(result.harvested_workspace_dir)]


# --- the disk cap (a watchdog on the writable layer) and the bounded harvest ----------
#
# The fake client answers `containers(size=True)` with `size_rw` (an int, a callable of the poll
# number, an exception, or None for "the engine did not say"). `running=True` makes the command
# a still-running one that only ends when the container is killed (or after `end_after`), so the
# in-loop watchdog can be exercised; without it the command has already finished by the first
# poll, which is what exercises the read after exit.

MIB = 1024 * 1024


@pytest.fixture()
def fast_poll(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_disk_poll_interval_seconds", 0.02)


def _cfg(workspace_dir, *, disk_limit_mb=64, timeout_seconds=None):
    return SandboxRunConfig(
        command=["echo", "hi"],
        workspace_dir=workspace_dir,
        disk_limit_mb=disk_limit_mb,
        timeout_seconds=timeout_seconds,
    )


def _archive_of(members):
    """`get_archive` for a FakeContainer subclass: `members` is (name, bytes) pairs."""
    import io
    import tarfile

    def get_archive(self, path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, data in members:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return iter([buf.getvalue()]), {}

    return get_archive


def _client_with_container(container_cls, **client_kwargs):
    fake_client = FakeDockerClient(**client_kwargs)

    def create(**kw):
        fake_client.containers.create_calls.append(kw)
        container = container_cls(kw)
        fake_client.containers._last = container
        return container

    fake_client.containers.create = create
    return fake_client


def test_the_cap_comes_from_settings_unless_the_run_overrides_it(workspace_dir, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_disk_limit_mb", 7)
    runner = SandboxRunner(client=FakeDockerClient())

    assert runner._disk_limit_bytes(SandboxRunConfig(command=["x"], workspace_dir=workspace_dir)) == 7 * 1024 * 1024
    assert (
        runner._disk_limit_bytes(SandboxRunConfig(command=["x"], workspace_dir=workspace_dir, disk_limit_mb=3))
        == 3 * 1024 * 1024
    )


def test_no_storage_opt_is_sent_to_the_engine(workspace_dir):
    """Measured on this host: `storage_opt={'size': '100M'}` is accepted, recorded in
    `HostConfig.StorageOpt`, and not enforced (300 MiB was written). It is also refused outright
    by a Linux engine without xfs `pquota`. So it is neither the cap nor safe to send blind;
    the cap is the watchdog (`_execute`) and the harvest bound."""
    fake_client = FakeDockerClient()

    SandboxRunner(client=fake_client).run(_cfg(workspace_dir))

    assert "storage_opt" not in fake_client.containers.create_calls[0]


def test_a_writable_layer_over_the_cap_kills_the_running_build_and_fails_it_resource_limit(
    workspace_dir, fast_poll, system_temp
):
    fake_client = FakeDockerClient(size_rw=lambda n: 0 if n < 3 else 100 * MIB, running=True)

    started = time.monotonic()
    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir, disk_limit_mb=64))
    elapsed = time.monotonic() - started

    container = fake_client.containers._last
    # Stopped by the in-loop poll, while the command was still running: the fake command only ends by
    # itself after 10 s, and the read after exit would see the same over-cap layer then -- so the
    # outcome alone cannot tell the watchdog from the read after exit; the clock can.
    assert elapsed < 3
    assert result.ok is False
    assert result.reason == BuildFailureReason.RESOURCE_LIMIT
    assert result.detail == "the sandbox's files grew past the 64 MiB disk limit"
    assert container.killed is True  # stopped while it was still running, not after it finished
    assert container.removed is True
    assert fake_client.api.last_archive_response is None  # no harvest of an over-cap tree
    assert result.harvested_workspace_dir is None
    assert os.listdir(system_temp) == []


def test_a_build_that_writes_its_excess_and_exits_inside_one_interval_is_caught_by_the_read_after_exit(
    workspace_dir, monkeypatch, system_temp
):
    """The command has finished before the loop's first poll (a long interval, an immediate
    exit), so only the read after the command exits can see the layer. Without it this build
    would be harvested and reported ok."""
    monkeypatch.setattr(settings, "sandbox_disk_poll_interval_seconds", 30.0)
    fake_client = FakeDockerClient(size_rw=100 * MIB)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir, disk_limit_mb=64))

    assert result.ok is False
    assert result.reason == BuildFailureReason.RESOURCE_LIMIT
    assert fake_client.api.size_polls == 1  # exactly the read after exit
    assert fake_client.api.last_archive_response is None
    assert os.listdir(system_temp) == []


def test_the_cap_is_inclusive_one_byte_over_is_not(workspace_dir):
    at_cap = FakeDockerClient(size_rw=64 * MIB)
    over = FakeDockerClient(size_rw=64 * MIB + 1)

    ok = SandboxRunner(client=at_cap).run(_cfg(workspace_dir, disk_limit_mb=64))
    refused = SandboxRunner(client=over).run(_cfg(workspace_dir, disk_limit_mb=64))

    assert ok.ok is True
    assert refused.reason == BuildFailureReason.RESOURCE_LIMIT


def test_a_build_under_the_cap_is_a_normal_run_and_is_harvested(workspace_dir, fast_poll):
    fake_client = FakeDockerClient(size_rw=10 * MIB, running=True, end_after=0.2)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir, disk_limit_mb=64))

    assert result.ok is True and result.detail is None
    assert fake_client.api.size_polls >= 2  # it really was polled while running, and read after exit
    # a normal exit is followed by the stop of every process (the container is `killed` once the
    # command has ended; that it is NOT killed before is `test_the_sandbox_is_stopped_after_...` below).
    assert fake_client.containers._last.killed is True
    assert os.listdir(result.harvested_workspace_dir) == ["harvested.txt"]


def test_an_engine_that_does_not_report_the_size_fails_the_run_closed_not_uncapped(
    workspace_dir, system_temp
):
    """No `SizeRw` at all is an engine the cap cannot be read from. The run is stopped
    (INFRA_ERROR, with a platform sentence), never allowed to carry on as if it were capped."""
    fake_client = FakeDockerClient(size_rw=None)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir, disk_limit_mb=64))

    assert result.ok is False
    assert result.reason == BuildFailureReason.INFRA_ERROR
    assert "stopped reporting the sandbox's disk use" in result.detail
    assert "64 MiB disk limit could not be enforced" in result.detail
    assert fake_client.api.size_polls == 3  # the read after exit, retried before giving up
    assert fake_client.api.last_archive_response is None
    assert os.listdir(system_temp) == []


def test_five_failed_polls_in_a_row_while_running_stop_the_build(workspace_dir, fast_poll):
    from docker.errors import APIError

    fake_client = FakeDockerClient(size_rw=APIError("engine busy"), running=True)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir))

    assert result.reason == BuildFailureReason.INFRA_ERROR
    assert fake_client.containers._last.killed is True
    assert fake_client.api.size_polls == 5


def test_one_failed_poll_among_good_ones_does_not_fail_the_build(workspace_dir, fast_poll):
    from docker.errors import APIError

    def answer(n):
        return APIError("engine busy") if n == 2 else 1 * MIB

    fake_client = FakeDockerClient(size_rw=answer, running=True, end_after=0.25)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir))

    assert result.ok is True
    assert fake_client.api.size_polls >= 3


def test_the_wall_clock_timeout_still_ends_a_running_build_with_the_watchdog_in_the_loop(
    workspace_dir, fast_poll
):
    fake_client = FakeDockerClient(size_rw=1 * MIB, running=True)

    started = time.monotonic()
    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir, timeout_seconds=0.3))

    assert result.reason == BuildFailureReason.TIMEOUT
    assert fake_client.containers._last.killed is True
    assert time.monotonic() - started < 5
    assert fake_client.api.size_polls >= 2  # and the loop did poll while it waited


# -- the bounded harvest, through the runner ---------------------------------------------------


def test_the_harvest_stops_reading_at_the_cap_and_releases_the_stream(workspace_dir, system_temp):
    """The streaming guarantee, behaviourally: a 100 MiB archive is offered and no more than the
    cap's worth (+1 chunk) is pulled; the response is closed; nothing is left in the temp dir.
    A harvest that buffered the stream before looking at it would have pulled all 100. (Finite on
    purpose: with the bound removed this must fail by counting, not by filling the disk.)"""
    pulled = []
    chunk = b"\0" * MIB

    class BombContainer(FakeContainer):
        def get_archive(self, path):
            def source():
                for _ in range(100):
                    pulled.append(1)
                    yield chunk

            return source(), {}

    fake_client = _client_with_container(BombContainer)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir, disk_limit_mb=8))

    assert result.ok is False
    assert result.reason == BuildFailureReason.RESOURCE_LIMIT
    assert result.detail == "the build's output is larger than the 8 MiB limit"
    assert len(pulled) <= 8 + 1
    assert fake_client.api.last_archive_response.closed is True
    assert result.harvested_workspace_dir is None
    assert os.listdir(system_temp) == []  # no partial archive, no scratch directory
    assert fake_client.containers._last.removed is True


def test_the_harvest_code_path_does_not_hold_the_archive_in_memory():
    """Secondary to the behavioural test above (a grep can be satisfied by a rename): the
    function that turns the stream into a tree must not build an in-memory buffer of it."""
    import inspect

    source = inspect.getsource(SandboxRunner._harvest_workspace)

    assert "BytesIO" not in source
    assert "b''.join" not in source and 'b"".join' not in source


def test_a_workspace_with_more_entries_than_the_ceiling_is_refused(workspace_dir, system_temp, monkeypatch):
    monkeypatch.setattr(sr, "HARVEST_MAX_ENTRIES", 10)

    class ManyFilesContainer(FakeContainer):
        get_archive = _archive_of([(f"workspace/f{i}", b"") for i in range(30)])

    fake_client = _client_with_container(ManyFilesContainer)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir, disk_limit_mb=64))

    assert result.reason == BuildFailureReason.RESOURCE_LIMIT
    assert result.detail == "the build's output has more than 10 files and directories"
    assert result.harvested_workspace_dir is None
    assert os.listdir(system_temp) == []


def test_the_bound_protects_the_timeout_path_too(workspace_dir, system_temp, fast_poll):
    """A build that timed out is still harvested, so an over-bound tree it left behind is
    refused the same way: the limit, not TIMEOUT, is the answer, and nothing is kept."""
    chunk = b"\0" * MIB

    class BombContainer(FakeContainer):
        def get_archive(self, path):
            return iter([chunk] * 100), {}

    fake_client = _client_with_container(BombContainer, size_rw=1 * MIB, running=True)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir, disk_limit_mb=8, timeout_seconds=0.2))

    assert result.reason == BuildFailureReason.RESOURCE_LIMIT
    assert fake_client.containers._last.killed is True  # it did time out first
    assert os.listdir(system_temp) == []


def test_a_preflight_that_says_over_refuses_without_ever_asking_for_the_archive(workspace_dir, system_temp):
    """The pre-flight walk (run inside the sandbox) is advisory, but when it says OVER the
    daemon is not asked to build an archive it would never be allowed to deliver."""

    class OverContainer(FakeContainer):
        def exec_run(self, cmd, user=None):
            self.exec_run_calls.append({"cmd": cmd, "user": user})
            if cmd[:3] == ["python", "-I", "-c"]:
                return 0, b"OVER bytes\n"
            return 0, b""

    fake_client = _client_with_container(OverContainer)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir, disk_limit_mb=64))

    assert result.reason == BuildFailureReason.RESOURCE_LIMIT
    assert result.detail == "the build's output is larger than the 64 MiB limit"
    assert fake_client.api.last_archive_response is None
    (probe,) = [c for c in fake_client.containers._last.exec_run_calls if c["cmd"][:3] == ["python", "-I", "-c"]]
    assert probe["user"] == sr.SANDBOX_USER  # run as the sandbox's own user, not root
    assert probe["cmd"][4:] == [str(64 * MIB), "20000", "/workspace"]
    assert os.listdir(system_temp) == []


def test_a_preflight_that_says_over_entries_names_the_entry_limit(workspace_dir):
    class OverContainer(FakeContainer):
        def exec_run(self, cmd, user=None):
            return (0, b"OVER entries\n") if cmd[:3] == ["python", "-I", "-c"] else (0, b"")

    result = SandboxRunner(client=_client_with_container(OverContainer)).run(_cfg(workspace_dir))

    assert result.reason == BuildFailureReason.RESOURCE_LIMIT
    assert result.detail == "the build's output has more than 20000 files and directories"


@pytest.mark.parametrize(
    "probe_answer",
    [(0, b"OK\n"), (0, b""), (0, b"garbage"), (1, b"python: not found"), (0, None), RuntimeError("exec failed")],
)
def test_a_preflight_with_no_usable_answer_changes_nothing_the_archive_path_carries_on(
    workspace_dir, probe_answer
):
    class MuteContainer(FakeContainer):
        def exec_run(self, cmd, user=None):
            if cmd[:3] == ["python", "-I", "-c"]:
                if isinstance(probe_answer, Exception):
                    raise probe_answer
                return probe_answer
            return 0, b""

    fake_client = _client_with_container(MuteContainer)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir))

    assert result.ok is True
    assert fake_client.api.last_archive_response is not None  # the archive was requested and bounded on its own
    assert os.listdir(result.harvested_workspace_dir) == ["harvested.txt"]


def test_the_preflight_script_itself_counts_bytes_and_entries_and_stops_early(tmp_path):
    """The script that runs in the sandbox, run here against a real directory: it must say OK for
    a tree inside both limits and OVER for each limit, and the byte answer must be the APPARENT
    size (a sparse file), which is the whole reason it exists."""
    import subprocess
    import sys

    root = tmp_path / "workspace"
    (root / "d").mkdir(parents=True)
    (root / "a.txt").write_bytes(b"x" * 100)
    (root / "d" / "b.txt").write_bytes(b"y" * 100)
    with open(root / "sparse", "wb") as f:
        f.truncate(50 * MIB)  # apparent 50 MiB, a handful of bytes on disk

    def walk(max_bytes, max_entries):
        out = subprocess.run(
            [sys.executable, "-I", "-c", sr._EXTENT_SCRIPT, str(max_bytes), str(max_entries), str(root)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return out.strip()

    assert walk(100 * MIB, 100) == "OK"
    assert walk(10 * MIB, 100) == "OVER bytes"
    assert walk(100 * MIB, 3) == "OVER entries"


# -- teardown: nothing a destroy step raises can skip the next step -----------------------------


def test_a_container_removal_that_raises_a_non_docker_error_cannot_skip_the_network_removal(workspace_dir):
    from requests.exceptions import ReadTimeout

    class SlowRemoveContainer(FakeContainer):
        def remove(self, v=False, force=False):
            raise ReadTimeout("the daemon did not answer")

    fake_client = _client_with_container(SlowRemoveContainer)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir))

    assert result.ok is True  # the run's own result is untouched
    assert fake_client.networks.last.removed is True


def test_a_network_removal_that_raises_a_non_docker_error_does_not_replace_the_result(workspace_dir):
    from requests.exceptions import ConnectionError as RequestsConnectionError

    fake_client = FakeDockerClient()
    original_create = fake_client.networks.create

    def create(name, **kw):
        network = original_create(name, **kw)

        def explode():
            raise RequestsConnectionError("pipe closed")

        network.remove = explode
        return network

    fake_client.networks.create = create

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir))

    assert result.ok is True


# -- the startup probe: does the cap hold on THIS host? -------------------------------------------


def _probe(
    monkeypatch=None,
    *,
    size_rw=8 * MIB,
    exec_result=(0, b""),
    create_raises=None,
    container_cls=FakeContainer,
    preview=False,
):
    class ProbeContainer(container_cls):
        def exec_run(self, cmd, user=None):
            self.exec_run_calls.append({"cmd": cmd, "user": user})
            return exec_result

    fake_client = FakeDockerClient(size_rw=size_rw)

    def create(**kw):
        if create_raises is not None:
            raise create_raises
        fake_client.containers.create_calls.append(kw)
        container = ProbeContainer(kw)
        fake_client.containers._last = container
        return container

    fake_client.containers.create = create
    return fake_client, SandboxRunner(client=fake_client).probe_disk_cap(preview=preview)


def test_the_probe_passes_when_the_engine_reports_the_probe_write():
    fake_client, gap = _probe(size_rw=8 * MIB)

    assert gap is None
    container = fake_client.containers._last
    assert container.removed is True  # the probe container is always removed
    assert fake_client.containers.create_calls[0]["image"] == settings.sandbox_image
    (write,) = container.exec_run_calls
    assert "count=8" in write["cmd"][-1]


def test_the_probe_names_a_gap_when_the_engine_reports_no_size_at_all():
    fake_client, gap = _probe(size_rw=None)

    assert "does not report a container's writable-layer size" in gap
    assert "NOT enforced" in gap and "INFRA_ERROR" in gap
    assert fake_client.containers._last.removed is True


def test_the_probe_names_a_gap_when_the_reported_size_does_not_grow():
    _, gap = _probe(size_rw=0)

    assert "reported 0 bytes" in gap and "NOT reliably enforced" in gap


def test_the_probe_reports_a_failed_write_and_a_failed_create_as_gaps_not_passes():
    _, write_failed = _probe(exec_result=(1, b"no space"))
    _, no_engine = _probe(create_raises=RuntimeError("daemon down"))

    assert "probe write failed (exit 1)" in write_failed and "could not be verified" in write_failed
    assert "could not be verified" in no_engine and "RuntimeError" in no_engine
    assert "daemon down" not in no_engine  # the class only


def test_the_probe_runs_on_the_configured_build_runtime(monkeypatch):
    monkeypatch.setattr(settings, "build_container_runtime", "runsc")

    fake_client, gap = _probe(size_rw=8 * MIB)

    assert gap is None
    assert fake_client.containers.create_calls[0]["runtime"] == "runsc"


# the same probe, for the preview containers the cap now also covers. ------------------------


def test_the_preview_probe_runs_on_the_runtime_a_preview_gets(monkeypatch):
    """Changed on purpose (previews were once pinned OFF the seam): the preview probe carries the
    runtime previews are created on, which is the build's unless a preview runtime is set."""
    monkeypatch.setattr(settings, "build_container_runtime", "runsc")
    monkeypatch.setattr(settings, "preview_container_runtime", None)

    build_client, build_gap = _probe(size_rw=8 * MIB)
    preview_client, preview_gap = _probe(size_rw=8 * MIB, preview=True)

    assert build_gap is None and preview_gap is None
    assert build_client.containers.create_calls[0]["runtime"] == "runsc"
    assert preview_client.containers.create_calls[0]["runtime"] == "runsc"
    assert preview_client.containers.create_calls[0]["image"] == settings.sandbox_image
    assert preview_client.containers._last.removed is True  # a throwaway like the build's


def test_a_preview_runtime_of_its_own_reaches_the_preview_probe_and_never_the_build_probe(monkeypatch):
    monkeypatch.setattr(settings, "build_container_runtime", None)
    monkeypatch.setattr(settings, "preview_container_runtime", "runsc")

    build_client, _ = _probe(size_rw=8 * MIB)
    preview_client, _ = _probe(size_rw=8 * MIB, preview=True)

    assert "runtime" not in build_client.containers.create_calls[0]
    assert preview_client.containers.create_calls[0]["runtime"] == "runsc"


def test_with_no_runtime_configured_neither_probe_sends_a_runtime_key(monkeypatch):
    monkeypatch.setattr(settings, "build_container_runtime", None)
    monkeypatch.setattr(settings, "preview_container_runtime", None)

    build_client, _ = _probe(size_rw=8 * MIB)
    preview_client, _ = _probe(size_rw=8 * MIB, preview=True)

    assert "runtime" not in build_client.containers.create_calls[0]
    assert "runtime" not in preview_client.containers.create_calls[0]


def test_a_preview_probe_gap_is_worded_for_previews_and_the_build_wording_is_unchanged():
    _, no_sizes = _probe(size_rw=None, preview=True)
    _, no_growth = _probe(size_rw=0, preview=True)
    _, build_no_sizes = _probe(size_rw=None)

    # For a preview there is no harvest to fall back on, and a preview the engine cannot size is torn down.
    assert "a preview's disk cap is NOT enforced on this host" in no_sizes
    assert "only its TTL ends it" in no_sizes
    assert "previews will be torn down shortly after they start until it does" in no_sizes
    assert "harvest" not in no_sizes and "INFRA_ERROR" not in no_sizes
    assert "reported 0 bytes" in no_growth and "a preview's disk cap is NOT reliably enforced" in no_growth
    # ... while the build's sentence is the one already shipped.
    assert build_no_sizes == (
        "the container engine does not report a container's writable-layer size, so the build "
        "sandbox's disk cap is NOT enforced on this host (only the harvest's byte bound applies) "
        "and builds will be stopped as INFRA_ERROR until it does"
    )


def test_a_preview_probe_that_cannot_run_is_a_gap_not_a_pass():
    _, write_failed = _probe(exec_result=(1, b"no space"), preview=True)
    _, no_engine = _probe(create_raises=RuntimeError("daemon down"), preview=True)

    assert "probe write failed (exit 1)" in write_failed and "could not be verified" in write_failed
    assert "could not be verified" in no_engine and "RuntimeError" in no_engine
    assert "daemon down" not in no_engine


# -- what the size read does with the rows real engines return --------------------------------------


def _layer_bytes_for(rows_or_error):
    class Api(ContainerListViaContainers):
        def containers(self, **kwargs):
            if isinstance(rows_or_error, BaseException):
                raise rows_or_error
            return rows_or_error

    runner = SandboxRunner(client=SimpleNamespace(api=Api()))
    return runner._writable_layer_bytes(SimpleNamespace(id="c1"))


def test_the_size_read_understands_each_shape_an_engine_answers_with():
    # Docker Desktop (containerd snapshotter) and overlay2 with something written: both fields.
    assert _layer_bytes_for([{"Id": "c1", "SizeRw": 5_242_880, "SizeRootFs": 141_250_560}]) == 5_242_880
    # overlay2 with NOTHING written omits SizeRw (measured); the row still carries SizeRootFs, so
    # the engine did compute sizes and the layer is empty. Reading this as "the engine is blind"
    # would fail every build that has written nothing yet.
    assert _layer_bytes_for([{"Id": "c1", "SizeRootFs": 141_250_560}]) == 0
    # An engine that reports no sizes at all: neither field. THAT is blind.
    assert _layer_bytes_for([{"Id": "c1"}]) is None


@pytest.mark.parametrize(
    "rows",
    [
        [],
        None,
        [{"Id": "somebody-else", "SizeRw": 1, "SizeRootFs": 1}],
        [{"Id": "c1", "SizeRw": -1, "SizeRootFs": 1}],
        [{"Id": "c1", "SizeRw": True, "SizeRootFs": 1}],
        [{"Id": "c1", "SizeRw": "12", "SizeRootFs": 1}],
    ],
    ids=["no rows", "none", "another container", "negative", "bool", "string"],
)
def test_a_size_read_it_cannot_vouch_for_is_none_not_zero(rows):
    assert _layer_bytes_for(rows) is None


def test_a_size_read_that_raises_is_none():
    assert _layer_bytes_for(RuntimeError("engine down")) is None


# -- every process in the sandbox is stopped before anything is read out of it -------------
#
# The command exits; what it left running in the background (`nohup ... &`) does not. Earlier it
# kept writing to the writable layer, unwatched, through the final disk check and the harvest. Against
# a real engine this is `test_sandbox_runner_bounds_integration.py`'s
# `test_a_process_the_build_left_running_is_stopped_...`; here the ORDER of the calls is pinned.


class LoggedContainer(FakeContainer):
    """Every call that touches the container goes into one shared log, in the order it was made."""

    events: list[str] = []

    def exec_run(self, cmd, user=None):
        self.exec_run_calls.append({"cmd": cmd, "user": user})
        if cmd[:3] == ["python", "-I", "-c"]:
            LoggedContainer.events.append("preflight")
            return 0, b"OK\n"
        return 0, b""

    def reload(self):
        LoggedContainer.events.append("reload")

    def kill(self):
        LoggedContainer.events.append("kill")
        super().kill()

    def get_archive(self, path):
        LoggedContainer.events.append("archive")
        return super().get_archive(path)


def _logged_run(workspace_dir, **client_kwargs):
    LoggedContainer.events = []
    fake_client = _client_with_container(LoggedContainer, **client_kwargs)
    real_inspect = fake_client.api.exec_inspect
    real_containers = fake_client.api.containers

    def inspect(exec_id):
        LoggedContainer.events.append("exec-inspect")
        return real_inspect(exec_id)

    def sized(**kwargs):
        LoggedContainer.events.append("size-read")
        return real_containers(**kwargs)

    fake_client.api.exec_inspect = inspect
    fake_client.api.containers = sized
    runner = SandboxRunner(client=fake_client)

    def host_read(container):
        LoggedContainer.events.append("cgroup-oom-read")  # a host-side read, not an exec
        return False

    runner._cgroup_oom_kill_observed = host_read
    result = runner.run(_cfg(workspace_dir, disk_limit_mb=64))
    return fake_client, result, list(LoggedContainer.events)


def test_the_sandbox_is_stopped_after_the_live_reads_and_before_the_final_disk_check_and_the_harvest(
    workspace_dir,
):
    """The order is the fix: the command's exit, then what needs the LIVE container (the OOM signals,
    the pre-flight walk), then the stop, then the disk check on the frozen state, then the archive.
    Remove the stop and `kill` is missing; move it later and the size read or the archive precedes it."""
    fake_client, result, events = _logged_run(workspace_dir)

    assert result.ok is True
    after_exit = events[events.index("exec-inspect") :]
    assert [e for e in after_exit if e != "reload"] == [
        "exec-inspect",
        "cgroup-oom-read",
        "preflight",
        "kill",
        "size-read",
        "archive",
    ]
    assert fake_client.containers._last.removed is True


def test_the_final_disk_check_judges_the_stopped_sandbox_not_a_running_one(workspace_dir):
    """Over the cap on the frozen state is still `RESOURCE_LIMIT`, and the harvest is never asked for."""
    fake_client, result, events = _logged_run(workspace_dir, size_rw=100 * MIB)

    assert result.reason == BuildFailureReason.RESOURCE_LIMIT
    assert result.detail == "the sandbox's files grew past the 64 MiB disk limit"
    assert events.index("kill") < events.index("size-read")
    assert "archive" not in events
    assert fake_client.api.last_archive_response is None


def test_an_oom_kill_is_still_reported_resource_limit_because_it_is_read_before_the_stop(
    workspace_dir, tmp_path, monkeypatch
):
    """The cgroup `oom_kill` counter is read from the host cgroup directory, which exists only while the
    container is alive (it goes with the container), so the read has to come first. Here the directory
    is removed by `kill()`: a stop moved ahead of the read turns this into an ordinary successful build."""
    import shutil

    from app.services import sandbox_runner

    root = tmp_path / "cgroup"
    scope = root / "system.slice" / "docker-fake-container-id.scope"
    scope.mkdir(parents=True)
    (scope / "memory.events").write_bytes(b"low 0\nhigh 0\nmax 3\noom 1\noom_kill 1\n")
    monkeypatch.setattr(sandbox_runner, "HOST_CGROUP_ROOT", str(root))

    class OomWhileAliveContainer(FakeContainer):
        def kill(self):
            shutil.rmtree(scope)  # the container's cgroup is gone once it is stopped
            super().kill()

    fake_client = _client_with_container(OomWhileAliveContainer)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir))

    assert result.ok is False
    assert result.reason == BuildFailureReason.RESOURCE_LIMIT
    assert result.detail is None  # the OOM sentence is the code's; no disk wording leaks into it
    assert os.listdir(result.harvested_workspace_dir) == ["harvested.txt"]  # outcome unchanged: still harvested


def test_a_sandbox_that_cannot_be_confirmed_stopped_fails_the_run_closed_and_reads_nothing(workspace_dir):
    """The kill raises and the container is still running: nothing is read out of it (no size read, no
    archive), the failure is `INFRA_ERROR` with a platform sentence, and the container is still removed."""
    from docker.errors import APIError

    class UnkillableContainer(FakeContainer):
        def kill(self):
            raise APIError("500: the engine could not signal the container")

    fake_client = _client_with_container(UnkillableContainer)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir))

    assert result.ok is False
    assert result.reason == BuildFailureReason.INFRA_ERROR
    assert result.detail == (
        "the sandbox's processes could not be confirmed stopped before its files were read, so the build was stopped"
    )
    assert fake_client.api.size_polls == 0
    assert fake_client.api.last_archive_response is None
    assert result.harvested_workspace_dir is None
    assert fake_client.containers._last.removed is True


def test_a_kill_that_raises_because_the_container_is_already_stopped_is_a_stop(workspace_dir):
    """`kill` on a container that already exited answers 409 (measured). The state says it is stopped,
    so the run goes on and harvests: the answer is the container's state, not the exception."""
    from docker.errors import APIError

    class AlreadyStoppedContainer(FakeContainer):
        def kill(self):
            self.killed = True
            self.attrs["State"]["Running"] = False
            raise APIError("409 Conflict: container is not running")

    fake_client = _client_with_container(AlreadyStoppedContainer)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir))

    assert result.ok is True
    assert os.listdir(result.harvested_workspace_dir) == ["harvested.txt"]


def test_a_timed_out_build_whose_sandbox_cannot_be_confirmed_stopped_is_not_harvested(workspace_dir, fast_poll):
    from docker.errors import APIError

    class UnkillableContainer(FakeContainer):
        def kill(self):
            self.killed = True  # the fake command stops waiting, but the engine never confirms the stop
            raise APIError("500: the engine could not signal the container")

    fake_client = _client_with_container(UnkillableContainer, size_rw=1 * MIB, running=True)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir, timeout_seconds=0.2))

    assert result.reason == BuildFailureReason.INFRA_ERROR
    assert fake_client.api.last_archive_response is None


# -- the size read has a bound of its own ----------------------------------------------------


def test_the_size_read_bound_is_five_seconds_and_every_read_carries_it(workspace_dir, fast_poll):
    """Pinned once as a literal, then every read of a real run is checked against what it was given:
    the in-loop polls and the read after the stop alike. Without the bound a read waits docker-py's 60 s
    default, and five unreadable polls take five minutes."""
    assert sr.SIZE_READ_TIMEOUT_SECONDS == 5.0
    fake_client = FakeDockerClient(size_rw=1 * MIB, running=True, end_after=0.2)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir))

    assert result.ok is True
    assert len(fake_client.api.list_timeouts) >= 2  # polled while running, and read after the stop
    assert set(fake_client.api.list_timeouts) == {5.0}


def test_a_size_read_that_times_out_is_an_unreadable_poll_and_five_in_a_row_fail_the_build_closed(
    workspace_dir, fast_poll
):
    """The engine does not answer in time: `requests` raises its own `ReadTimeout`, which is not a
    Docker error and is read as "unreadable" all the same."""
    from requests.exceptions import ReadTimeout

    fake_client = FakeDockerClient(size_rw=ReadTimeout("no answer within the bound"), running=True)

    result = SandboxRunner(client=fake_client).run(_cfg(workspace_dir))

    assert result.reason == BuildFailureReason.INFRA_ERROR
    assert fake_client.api.size_polls == 5
    assert fake_client.containers._last.killed is True
