"""L3 disposable hardened sandbox runner (ADR 0003).

Provisions a fresh, single-use Linux container for one build, stages a local
workspace directory in, executes a command under resource/time limits,
harvests the workspace and logs back out, and destroys the container
unconditionally. Enforces the four ADR 0003 invariants:

1. Single-use — a fresh container per run; destroyed on every exit path
   (success, failure, timeout, exception) via try/finally.
2. No Docker socket — never mounted; no `privileged` flag either.
3. Default-deny egress — the container attaches only to a fresh,
   `internal=True` Docker network created for this run alone (and destroyed
   with it), with no route out and no other sandbox sharing it — so
   concurrent builds cannot reach each other either. This module opens no
   path out at all: the executor (`sandbox_build_executor.py`) attaches the
   LLM proxy container to that network, and that is the only thing the
   sandbox can reach. No package registry is reachable from it.
4. Memory/CPU/pids/disk caps — applied from `Settings` (mode-conditional
   defaults); the container
   runs with `no-new-privileges` and dropped capabilities. Its PID 1 and the
   staging/chown execs around the build are root; the build command itself
   always runs as the non-root `SANDBOX_USER`. The
   disk cap is a watchdog on the writable layer's size plus a harvest
   that stops reading at the same byte count; see `_execute` and
   `_harvest_workspace` for why it is not a `storage_opt`.

The container *runtime* is configurable (
`Settings.build_container_runtime`, overridable per run via
`SandboxRunConfig.container_runtime`) so a stronger runtime such as gVisor
(`--runtime=runsc`) can be measured without a code change — the seam ADR
0003 §Residual risk names as the trigger for revisiting isolation. It is a
runtime-only knob: none of the four invariants above depend on it, and all
four are still enforced here on whichever runtime is selected. Unset (the
default) passes no runtime argument at all, leaving the host's own default
runtime in charge. Scoped to the build container alone, per ADR 0003
§Ordering — L4 service containers (`deploy_manager.py`) and L3 preview
containers (`preview_manager.py`) deliberately do not read it.

This is a standalone service with its own tests. It does not
implement the `BuildExecutor` Protocol itself: `SandboxBuildExecutor`
(`sandbox_build_executor.py`) wraps it, and the worker runs that executor.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field

import docker
from docker import utils as docker_utils
from docker.errors import APIError, DockerException
from docker.models.containers import Container
from docker.types import Ulimit

from app.config import settings
from app.services.platform_labels import Role, platform_labels
from app.services.harvest import (
    HARVEST_MAX_ENTRIES,
    HarvestLimitExceeded,
    extract_bounded,
    remove_tree,
    write_stream_bounded,
)
from app.services.status import BuildFailureReason

logger = logging.getLogger("app.sandbox_runner")

_MIB = 1024 * 1024

# How many polls in a row may fail to read the container's writable-layer size
# before the run is failed closed. A single failed call is a hiccup of
# the engine's API under load; five seconds of them is a cap nobody is enforcing,
# and a build is never allowed to run on in that state without anyone saying so.
_DISK_BLIND_POLL_LIMIT = 5

# The longest ONE size read may take, in seconds. `containers(size=True)` makes the
# engine walk every container's writable layer, and docker-py waits 60 s for the answer by
# default, so "five unreadable polls" could take five minutes (and a build that makes millions of
# files stretches each read: measured on Docker Desktop 28.4, 400,000 empty files in a layer make
# one call take 0.7 s). A read that has not answered in this long counts as unreadable, exactly
# like one that failed, so the documented windows hold in time as well as in count: the blind
# verdict arrives after at most `_DISK_BLIND_POLL_LIMIT` x (this + the poll interval), and a
# build's own wall-clock limit is overshot by at most one read. It is the engine's time to START
# answering that is bounded (the daemon computes the sizes before it sends a byte), which is the
# whole of the cost; the transport honours it (measured on this host's named pipe: a 0.3 s bound
# on that 0.7 s read raised `ReadTimeout` after 0.31 s).
SIZE_READ_TIMEOUT_SECONDS = 5.0


def list_containers_bounded(api, *, filters: dict, size: bool) -> list[dict]:
    """`api.containers(all=True, size=size, filters=filters)` with `SIZE_READ_TIMEOUT_SECONDS` as the
    time the engine gets to answer. docker-py's `containers()` takes no timeout of its own, so
    this issues the same request (`GET /containers/json`, the same parameters, the same decoding) through
    the client's low-level helpers, which do; the same pattern `SandboxRunner._archive_chunks` already
    uses for the archive request. Raises what the client raises (`requests`' `ReadTimeout` on a bound
    that expired, an `APIError`): every caller counts any exception as an unreadable poll."""
    params = {
        "limit": -1,
        "all": 1,
        "size": 1 if size else 0,
        "trunc_cmd": 0,
        "since": None,
        "before": None,
    }
    if filters:
        params["filters"] = docker_utils.convert_filters(filters)
    response = api._get(api._url("/containers/json"), params=params, timeout=SIZE_READ_TIMEOUT_SECONDS)
    return api._result(response, True)


def writable_layer_bytes_from_row(row: dict) -> int | None:
    """One `containers(size=True)` row's writable-layer size in bytes (`SizeRw`), or None
    when the row does not say. The ONE reading of that row: the build sandbox's watchdog
    (`SandboxRunner._writable_layer_bytes`) and the preview watcher
    (`preview_disk_watch.PreviewDiskWatcher`) both go through it, so what counts as
    "the engine reported a size" cannot drift between the two caps.

    An engine reports no `SizeRw` at all for an EMPTY writable layer (the field is omitted
    when zero, measured on overlay2), so a missing `SizeRw` is read as 0 only when the same
    row carries `SizeRootFs`, the proof that the engine did compute sizes; a row with neither
    is an engine that does not report them, which is None. A negative, a boolean or a
    non-integer value is not a size either: None, never a guess."""
    value = row.get("SizeRw")
    if value is None and row.get("SizeRootFs") is not None:
        return 0
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def judge_disk_reading(
    used: int | None, *, limit_bytes: int, blind_polls: int, blind_limit: int = _DISK_BLIND_POLL_LIMIT
) -> tuple[str | None, int]:
    """The verdict on one poll of a container's writable layer, shared by the build sandbox's
    watchdog and the preview watcher: `(verdict, blind_polls_now)`, the verdict being
    `"over"` (strictly more than the limit), `"blind"` (`blind_limit` unreadable polls in a
    row) or None (carry on). A readable poll resets the blind count, an unreadable one adds
    to it: a cap nobody can read is not a cap, so the caller fails closed once the engine has
    been silent for `blind_limit` polls, and a single failed call (an engine under load) is
    not a verdict."""
    if used is None:
        blind_polls += 1
        return ("blind" if blind_polls >= blind_limit else None), blind_polls
    return ("over" if used > limit_bytes else None), 0


@dataclass(frozen=True)
class _CapWords:
    """How a startup-probe gap names what it is about: the cap itself, what is left
    standing when it cannot be enforced, and what the platform then does."""

    name: str
    fallback: str
    consequence: str


_BUILD_CAP_WORDS = _CapWords(
    name="the build sandbox's disk cap",
    fallback="only the harvest's byte bound applies",
    consequence="builds will be stopped as INFRA_ERROR",
)
# A preview is never archived, so no harvest bound stands behind its cap: only its TTL ends it.
# And a preview whose size the engine does not report is torn down (fail closed), so on an
# engine that never reports one, none survives.
_PREVIEW_CAP_WORDS = _CapWords(
    name="a preview's disk cap",
    fallback="nothing else bounds a preview's disk, only its TTL ends it",
    consequence="previews will be torn down shortly after they start",
)

# The harvest's pre-flight, run INSIDE the sandbox before the archive is
# requested: a walk of /workspace that adds up the files' apparent sizes and counts
# entries, stopping the moment either passes its limit. Why it exists: the Docker
# daemon builds the workspace archive by reading every file through its holes, and on
# Docker Desktop it kept doing so after the client hung up (measured: a 20 GiB sparse
# file, the read abandoned after 64 MiB, and `container.remove` then blocked past the
# client's 60 s timeout until the daemon finished). Asking the daemon for an archive it
# will never be allowed to deliver is what costs; a walk costs milliseconds and ends
# early. ADVISORY, not the boundary: it runs where the build's own code ran, so its
# answer is only ever used to refuse sooner -- the streamed byte bound and the entry
# check in `harvest.extract_bounded` are what hold, whatever this prints (or if it
# prints nothing). `-I` keeps the interpreter off the environment and the user site.
_EXTENT_SCRIPT = (
    "import os, stat, sys\n"
    "max_bytes, max_entries, root = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]\n"
    "total = entries = 0\n"
    "stack = [root]\n"
    "while stack:\n"
    "    try:\n"
    "        it = os.scandir(stack.pop())\n"
    "    except OSError:\n"
    "        continue\n"
    "    with it:\n"
    "        for e in it:\n"
    "            entries += 1\n"
    "            try:\n"
    "                st = e.stat(follow_symlinks=False)\n"
    "            except OSError:\n"
    "                continue\n"
    "            if stat.S_ISDIR(st.st_mode):\n"
    "                stack.append(e.path)\n"
    "            elif stat.S_ISREG(st.st_mode):\n"
    "                total += st.st_size\n"
    "            if total > max_bytes:\n"
    "                print('OVER bytes'); sys.exit(0)\n"
    "            if entries > max_entries:\n"
    "                print('OVER entries'); sys.exit(0)\n"
    "print('OK')\n"
)

_ARCHIVE_CHUNK_BYTES = 2 * _MIB

# Container-internal paths for the staging/harvest airlock.
WORKSPACE_PATH = "/workspace"

# where the worker's host mounts the cgroup v2 tree, and how much of a `memory.events`
# file is ever read (it is a handful of short lines; the cap keeps a hostile one out of memory).
HOST_CGROUP_ROOT = "/sys/fs/cgroup"
_MEMORY_EVENTS_READ_LIMIT = 4096
_CONTAINER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

def oom_read_gap(engine_info: dict | None, root: str | None = None) -> str | None:
    """why the post-run OOM read (`_cgroup_oom_kill_observed`) cannot work for this engine,
    or None when its cgroup parent directory is visible to this process. Asked once at worker
    start, with `docker info`'s answer (`CgroupVersion`, `CgroupDriver`): the read needs cgroup v2
    and the engine's container cgroups under `<root>/system.slice` (systemd driver) or
    `<root>/docker` (cgroupfs), the two parents `_host_memory_events_path` builds paths from.
    It cannot see a container's own `HostConfig.CgroupParent` (no container exists yet), so a
    non-default parent is a case this does not catch."""
    info = engine_info or {}
    if str(info.get("CgroupVersion") or "") != "2":
        return f"the engine reports cgroup version {info.get('CgroupVersion')!r}, not 2"
    root = HOST_CGROUP_ROOT if root is None else root
    parent = "system.slice" if info.get("CgroupDriver") == "systemd" else "docker"
    if not os.path.isdir(f"{root}/{parent}"):
        return f"{root}/{parent} does not exist on this host (the engine's cgroups are not visible here)"
    return None


# Non-root UID:GID the executed command runs as. Does not depend on the
# pinned image defining its own non-root account.
#
# The container's PID 1 (`sleep infinity`) itself stays root — root is only
# used, via `exec_run`, to `mkdir`/`chown` the workspace directory before
# staging (Docker always creates bind targets / exec-mkdir'd dirs as root,
# so *something* must own-transfer them). The executed build command — the
# part that runs unvetted, LLM-generated code (invariant 2's concern) — is
# always invoked with `user=SANDBOX_USER`, never as root. No image-specific
# account or Dockerfile USER directive is required.
# ADR 0046 §Decision 6: those two setup execs are the ONLY root execs, and both
# happen before any untrusted code has run in the container (`_stage_workspace`, called
# from `run` straight after `_provision`; the agent command starts afterwards). Nothing
# execs as root into a container that has run untrusted code: the post-run OOM read is a
# host-side file read (`_cgroup_oom_kill_observed`).
SANDBOX_USER = "65534:65534"  # nobody:nogroup


@dataclass(frozen=True)
class SandboxRunConfig:
    """Input to one sandbox run."""

    command: list[str]
    workspace_dir: str  # local directory staged into the container's /workspace
    image: str | None = None  # defaults to settings.sandbox_image
    timeout_seconds: int | None = None  # defaults to settings.build_max_duration_minutes * 60
    memory_limit_mb: int | None = None  # defaults to settings.sandbox_memory_limit_mb
    cpu_limit_cores: float | None = None  # defaults to settings.sandbox_cpu_limit_cores
    pids_limit: int | None = None  # defaults to settings.sandbox_pids_limit
    disk_limit_mb: int | None = None  # defaults to settings.sandbox_disk_limit_mb
    env: dict[str, str] = field(default_factory=dict)
    # when set, `run()` attaches the container to this
    # pre-existing network instead of creating (and later destroying) its
    # own. Used so an agent-run sandbox shares a network with its
    # already-attached LLM proxy container (services/proxy_container.py) —
    # still `internal=True`, still per-build, still torn down by the
    # caller that created it (not by SandboxRunner, which never destroys a
    # network it didn't create itself).
    network: object | None = None  # docker.models.networks.Network, kept untyped to avoid a hard docker import in the dataclass default path
    # container runtime for this run only; unset defaults from
    # `settings.build_container_runtime`, which is itself unset by default —
    # and "unset" means no runtime argument is passed to Docker at all (see
    # `_provision`). Exists so a gVisor (`runsc`) compatibility measurement
    # can be made per run without touching global config.
    container_runtime: str | None = None
    # the build this run belongs to (ADR 0037): `forge.build_id` on the sandbox
    # container, and on the network when this runner creates it. The reconciler removes
    # either once that build is no longer RUNNING. None for a run with no build.
    build_id: str | None = None


@dataclass(frozen=True)
class SandboxRunResult:
    """Outcome of one sandbox run. `ok=False` always carries a `reason` from
    the failure taxonomy (BuildFailureReason)."""

    ok: bool
    reason: str | None = None
    exit_code: int | None = None
    logs: str = ""
    harvested_workspace_dir: str | None = None  # local dir with harvested contents
    # a platform-authored sentence for the failure that `reason` alone
    # under-describes (the limit that was crossed). Built from platform templates
    # and the configured limit only -- never from anything the sandbox wrote -- so
    # the executor may put it in `Build.failure_detail`. None where the reason is
    # self-explanatory.
    detail: str | None = None


@dataclass(frozen=True)
class _ExecOutcome:
    """What `_execute` observed. `disk_breach` is None, `"over"` (the
    writable layer passed the cap) or `"blind"` (the engine stopped telling the
    runner how big it is, so the cap could not be enforced); either kills the
    container and skips the harvest.

    `stopped` is whether every process in the container was confirmed stopped (the
    container no longer running) before anything was read out of it; False fails the run closed
    (`INFRA_ERROR`), because a harvest of a tree something is still writing to is not one the
    caps can vouch for. `workspace_refusal` is the in-sandbox pre-flight's verdict on the
    workspace's extent, asked while the container was still alive and acted on by `run()`."""

    exit_code: int | None
    timed_out: bool
    oom_killed: bool
    logs: str
    disk_breach: str | None = None
    stopped: bool = True
    workspace_refusal: HarvestLimitExceeded | None = None


class SandboxRunner:
    """Orchestrates provision -> execute -> harvest -> destroy for one run.

    A fresh `SandboxRunner` (or a fresh call to `run()`) is expected per
    build; the class holds no cross-run state. Uses the Docker SDK
    (`docker.from_env()`) against the local Docker Engine.
    """

    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self._client = client or docker.from_env()

    def run(self, config: SandboxRunConfig) -> SandboxRunResult:
        container: Container | None = None
        network = None
        owns_network = config.network is None
        harvested_dir: str | None = None
        try:
            try:
                network = config.network or self._create_run_network(config.build_id)
                container = self._provision(config, network.name)
            except Exception as exc:  # noqa: BLE001 - any provisioning failure is infra-level
                return SandboxRunResult(
                    ok=False,
                    reason=BuildFailureReason.INFRA_ERROR,
                    logs=f"provisioning failed: {exc}",
                )

            try:
                self._stage_workspace(container, config.workspace_dir)
            except Exception as exc:  # noqa: BLE001 - any staging failure is infra-level
                return SandboxRunResult(
                    ok=False,
                    reason=BuildFailureReason.INFRA_ERROR,
                    logs=f"staging failed: {exc}",
                )

            outcome = self._execute(container, config)
            exit_code, timed_out, oom_killed, logs = (
                outcome.exit_code,
                outcome.timed_out,
                outcome.oom_killed,
                outcome.logs,
            )
            disk_limit_mb = self._disk_limit_bytes(config) // _MIB

            # the disk cap was crossed (or could not be watched). The container was
            # killed, there is nothing worth harvesting -- the build fails here, with the
            # limit named, and the harvest (which would read an over-cap tree) never runs.
            if outcome.disk_breach == "over":
                return SandboxRunResult(
                    ok=False,
                    reason=BuildFailureReason.RESOURCE_LIMIT,
                    exit_code=exit_code,
                    logs=logs,
                    detail=f"the sandbox's files grew past the {disk_limit_mb} MiB disk limit",
                )
            if outcome.disk_breach == "blind":
                return SandboxRunResult(
                    ok=False,
                    reason=BuildFailureReason.INFRA_ERROR,
                    exit_code=exit_code,
                    logs=logs,
                    detail=(
                        "the container engine stopped reporting the sandbox's disk use, so the "
                        f"{disk_limit_mb} MiB disk limit could not be enforced and the build was stopped"
                    ),
                )

            # nothing is read out of a container something may still be running in.
            # `_execute` stopped every process before its final disk check; if it could not
            # confirm that, the tree is not one the caps can vouch for, and the build ends here
            # (the container is force-removed in `finally`).
            if not outcome.stopped:
                return SandboxRunResult(
                    ok=False,
                    reason=BuildFailureReason.INFRA_ERROR,
                    exit_code=exit_code,
                    logs=logs,
                    detail=(
                        "the sandbox's processes could not be confirmed stopped before its files "
                        "were read, so the build was stopped"
                    ),
                )

            try:
                harvested_dir = self._harvest_workspace(
                    container,
                    max_bytes=self._disk_limit_bytes(config),
                    refusal=outcome.workspace_refusal,
                )
            except HarvestLimitExceeded as exc:
                # the workspace archive crossed the harvest's own bound (bytes or
                # entries) -- a sparse file, a million tiny files. A resource limit, with
                # the platform's own sentence as the detail; nothing was kept.
                logger.warning("sandbox runner: the harvest was stopped: %s", exc)
                return SandboxRunResult(
                    ok=False,
                    reason=BuildFailureReason.RESOURCE_LIMIT,
                    exit_code=exit_code,
                    logs=logs,
                    detail=str(exc),
                )
            except Exception as exc:  # noqa: BLE001 - any harvest failure is HARVEST_ERROR
                return SandboxRunResult(
                    ok=False,
                    reason=BuildFailureReason.HARVEST_ERROR,
                    exit_code=exit_code,
                    logs=logs + f"\nharvest failed: {exc}",
                )

            if timed_out:
                return SandboxRunResult(
                    ok=False,
                    reason=BuildFailureReason.TIMEOUT,
                    exit_code=exit_code,
                    logs=logs,
                    harvested_workspace_dir=harvested_dir,
                )
            if oom_killed:
                return SandboxRunResult(
                    ok=False,
                    reason=BuildFailureReason.RESOURCE_LIMIT,
                    exit_code=exit_code,
                    logs=logs,
                    harvested_workspace_dir=harvested_dir,
                )
            if exit_code != 0:
                return SandboxRunResult(
                    ok=False,
                    reason=BuildFailureReason.AGENT_ERROR,
                    exit_code=exit_code,
                    logs=logs,
                    harvested_workspace_dir=harvested_dir,
                )

            return SandboxRunResult(
                ok=True,
                exit_code=exit_code,
                logs=logs,
                harvested_workspace_dir=harvested_dir,
            )
        finally:
            # Invariant 1: destroyed unconditionally, regardless of outcome
            # or exception above.
            if container is not None:
                self._destroy(container)
            # Only destroy a network this call created itself — a network
            # passed in via config.network is owned by the caller (
            # it's shared with a proxy container that outlives this one
            # sandbox run, e.g. across a harvest retry).
            if network is not None and owns_network:
                self._destroy_network(network)

    # -- provision ---------------------------------------------------

    def _create_run_network(self, build_id: str | None = None):
        """Create a fresh, isolated, default-deny (invariant 3) Docker
        network scoped to this single run only.

        A per-run network (not one long-lived shared network) is required
        so that "no inter-sandbox traffic" actually holds: two containers
        on the *same* bridge network can reach each other directly even
        with `internal=True` (that flag only blocks the route out of the
        network, not traffic between peers inside it) — verified against
        the real Docker Engine. `internal=True` still blocks all external
        egress, satisfying invariant 3's default-deny requirement.
        """
        unique_name = f"{settings.sandbox_network_name}-{uuid.uuid4().hex[:12]}"
        return self._client.networks.create(
            unique_name,
            driver="bridge",
            internal=True,
            # the instance, the `build-network` role and the build, so the
            # reconciler can remove it when that build is no longer RUNNING.
            labels=platform_labels(Role.BUILD_NETWORK, build_id=build_id),
        )

    def _destroy_network(self, network) -> None:
        """Unconditional teardown, best-effort (mirrors `_destroy`). Any
        exception, logged -- `requests`' `ReadTimeout`/`ConnectionError` are not
        `DockerException`s, and one escaping here masked the run's own result."""
        try:
            network.remove()
        except Exception as exc:  # noqa: BLE001 - teardown never raises
            logger.warning(
                "sandbox runner: network %s could not be removed (%s)",
                getattr(network, "name", "?"),
                type(exc).__name__,
            )

    def _provision(self, config: SandboxRunConfig, network_name: str) -> Container:
        image = config.image or settings.sandbox_image
        memory_mb = config.memory_limit_mb or settings.sandbox_memory_limit_mb
        cpu_cores = config.cpu_limit_cores or settings.sandbox_cpu_limit_cores
        pids_limit = config.pids_limit or settings.sandbox_pids_limit
        container_runtime = config.container_runtime or settings.build_container_runtime

        # Invariant 2: no Docker socket, no other host path, not privileged.
        # Invariant 3: attach only to the isolated internal network, no
        # published ports (no ingress during build).
        # Invariant 4: memory/cpu/pids caps (the DISK cap is not a create
        # argument: `storage_opt={'size': ...}` was measured being accepted and
        # ignored on Docker Desktop, and refused outright on native Linux overlay2
        # over ext4 (a create that fails), so it can neither be relied on nor sent
        # blind. `_execute` enforces the cap by watching the writable layer);
        # no-new-privileges, all
        # capabilities dropped except CHOWN (needed only for the root setup
        # step in `_stage_workspace` that hands the workspace to the
        # non-root user; the executed build command never gets it — it
        # always runs via `exec_run(user=SANDBOX_USER)` in `_execute`, and a
        # non-root exec cannot exercise a capability it doesn't hold, see
        # SANDBOX_USER docstring).
        create_kwargs = dict(
            image=image,
            command=["sleep", "infinity"],
            detach=True,
            network=network_name,
            mem_limit=f"{memory_mb}m",
            nano_cpus=int(cpu_cores * 1_000_000_000),
            pids_limit=pids_limit,
            security_opt=["no-new-privileges"],
            cap_drop=["ALL"],
            cap_add=["CHOWN"],
            privileged=False,
            volumes={},  # no host paths mounted; workspace transfers via tar airlock
            environment=config.env,
            ulimits=[Ulimit(name="nproc", soft=pids_limit, hard=pids_limit)],
            # the instance, the `sandbox` role and the build (ADR 0037): a sandbox is
            # disposable, and one whose build is no longer RUNNING is removed by the
            # reconciler, which is the only thing that catches a worker that died mid-run.
            labels=platform_labels(Role.SANDBOX, build_id=config.build_id),
        )
        # only send `runtime` when one was actually configured. Passing
        # `runtime=None` (or the literal "runc") is NOT the same as omitting
        # the argument on a host whose `default-runtime` is something else, so
        # the unconfigured path must leave the key out entirely — that is what
        # keeps today's behaviour identical everywhere.
        if container_runtime:
            create_kwargs["runtime"] = container_runtime

        container = self._client.containers.create(**create_kwargs)
        container.start()
        return container

    # -- stage / harvest (tar airlock; no shared host volume) --------

    def _stage_workspace(self, container: Container, workspace_dir: str) -> None:
        # Two root steps around the extract, both needed:
        #
        # 1. `mkdir -p` before — `put_archive` needs the target to exist.
        # 2. `chown -R` AFTER — `put_archive` extracts the tar as root and
        #    keeps each entry's header uid/gid *and* mode (measured: forced
        #    1000:1000 / 0755 entries arrive as 1000:1000 / 755). Those come
        #    from the host's stat: Docker Desktop on Windows yields 0:0 with
        #    mode 0777, which the sandbox user can write; a Linux host yields
        #    its own uid (e.g. a CI runner's 1001) and 0755, which it cannot.
        #    Chowning only *before* the extract therefore left `.forge/` and
        #    every other staged subdirectory unwritable by SANDBOX_USER on
        #    Linux (`agent_runner.py` got PermissionError writing
        #    `result.json`). The ownership transfer must follow the extract.
        #
        # these are the only root execs in a sandbox, and root is acceptable here ONLY
        # because no untrusted code has run in this container yet: `run` stages straight after
        # `_provision` and the agent command starts after the second step. Do not add a root exec
        # anywhere after `_execute` starts.
        #
        # Uses only the CHOWN capability already granted in `_provision`;
        # the executed command still runs as SANDBOX_USER without it.
        exit_code, output = container.exec_run(["sh", "-c", f"mkdir -p {WORKSPACE_PATH}"])
        if exit_code != 0:
            raise DockerException(
                f"workspace setup in {WORKSPACE_PATH} failed (exit {exit_code}): {output!r}"
            )

        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            for entry in os.listdir(workspace_dir):
                tar.add(os.path.join(workspace_dir, entry), arcname=entry)
        tar_stream.seek(0)
        container.put_archive(WORKSPACE_PATH, tar_stream.read())

        exit_code, output = container.exec_run(
            ["sh", "-c", f"chown -R {SANDBOX_USER} {WORKSPACE_PATH}"]
        )
        if exit_code != 0:
            raise DockerException(
                f"workspace ownership transfer in {WORKSPACE_PATH} failed (exit {exit_code}): {output!r}"
            )

    def _harvest_workspace(
        self, container: Container, *, max_bytes: int, refusal: HarvestLimitExceeded | None = None
    ) -> str:
        """**By now the container is stopped** (`_stop_processes`, called by `_execute` before
        its final disk check), so this reads only what the engine can still give for a stopped
        container, the archive; the pre-flight, which runs a process INSIDE the sandbox, was asked
        earlier, while it was alive, and its verdict arrives here as `refusal`. `get_archive` on a
        stopped container was measured to work on this host's engine.

        `get_archive(WORKSPACE_PATH)` returns a tar with the requested
        directory as a single top-level entry (e.g. `workspace/...`), not
        its contents at the tar root — extract, then hand back a directory
        whose top level IS the workspace (mirroring what was staged in
        `_stage_workspace`).

        **the archive is streamed to a host file and bounded; it is never
        held in memory.** The stream is the files' APPARENT bytes: a sparse file the
        sandbox made with `truncate -s 200G` costs the sandbox nothing and arrives
        as 200 GiB. `write_stream_bounded` stops at `max_bytes` (the disk cap) and
        `extract_bounded` refuses an archive with more than `HARVEST_MAX_ENTRIES`
        entries or whose members' logical sizes add up past `max_bytes`, both before
        extraction writes anything. Crossing either raises `HarvestLimitExceeded`
        (the caller reports `RESOURCE_LIMIT`), and the scratch directory -- archive
        included -- is removed on every path. This protects the timeout path too:
        a build that timed out is still harvested.

        The returned directory is the ONLY temporary tree this leaves behind
        (ADR 0032): its owner removes it and nothing else remains. The
        extraction happens in a scratch directory, the workspace's entries are
        moved (renamed, not copied) into a second fresh directory, and the
        scratch directory is removed. Returning the nested `workspace/` path
        instead -- as this used to do -- left its `forge-harvest-*` parent
        behind for every build ever run, because no caller knew the parent
        existed. This is the one place the tar's top-level nesting is resolved."""
        scratch = tempfile.mkdtemp(prefix="forge-harvest-scratch-")
        result_dir: str | None = None
        try:
            archive_path = os.path.join(scratch, "workspace.tar")
            extract_dir = os.path.join(scratch, "extract")
            if refusal is not None:
                raise refusal
            write_stream_bounded(
                self._archive_chunks(container), archive_path, max_bytes=max_bytes
            )
            os.mkdir(extract_dir)
            extract_bounded(
                archive_path, extract_dir, max_bytes=max_bytes, max_entries=HARVEST_MAX_ENTRIES
            )
            os.remove(archive_path)  # the extracted tree is what is moved; free the space now

            nested = os.path.join(extract_dir, os.path.basename(WORKSPACE_PATH))
            result_dir = tempfile.mkdtemp(prefix="forge-harvest-")
            source = nested if os.path.isdir(nested) else extract_dir
            for name in os.listdir(source):
                shutil.move(os.path.join(source, name), os.path.join(result_dir, name))
        except Exception:
            remove_tree(result_dir, what="harvest result")
            raise
        finally:
            remove_tree(scratch, what="harvest scratch")
        return result_dir

    def _archive_chunks(self, container: Container):
        """The workspace archive as a stream of chunks that CLOSES its HTTP response
        when it is closed or exhausted. `container.get_archive` hands back a
        generator over the response and keeps no way to close it, so an aborted read
        left the connection to be released by the garbage collector; here
        `write_stream_bounded` closes the generator, and its `finally` closes the
        response. This is the one place that uses docker-py's low-level request
        helpers (`_url`, `_get`, `_raise_for_status`), exactly as
        `APIClient.get_archive` does, for that reason. The socket timeout is left at
        the client's default (the stream is bounded, and a daemon that stops
        answering should end the harvest, not hang it)."""
        api = self._client.api
        response = api._get(
            api._url("/containers/{0}/archive", container.id),
            params={"path": WORKSPACE_PATH},
            stream=True,
            headers={"Accept-Encoding": "identity"},
        )
        try:
            api._raise_for_status(response)
        except BaseException:
            response.close()
            raise

        def chunks():
            try:
                yield from response.iter_content(_ARCHIVE_CHUNK_BYTES)
            finally:
                response.close()

        return chunks()

    def _workspace_extent_refusal(
        self, container: Container, *, max_bytes: int
    ) -> HarvestLimitExceeded | None:
        """The advisory pre-flight (`_EXTENT_SCRIPT`): the `HarvestLimitExceeded` to raise
        when the walk inside the sandbox says the workspace is over a bound, so the
        daemon is never asked for an archive it cannot be allowed to deliver. Anything
        else -- an exec that fails, an image with no `python`, output that is not
        `OK`/`OVER ...` -- means "no answer" (None) and the archive path, which is bounded
        on its own, carries on.

        it needs a LIVE container (it execs into it), so `_execute` asks it before
        stopping the sandbox, and a writer still running in that moment can make the archive
        read see more than this walk did. That is why it is advisory: the archive read is
        what the bounds hold on."""
        try:
            exit_code, output = container.exec_run(
                [
                    "python",
                    "-I",
                    "-c",
                    _EXTENT_SCRIPT,
                    str(max_bytes),
                    str(HARVEST_MAX_ENTRIES),
                    WORKSPACE_PATH,
                ],
                user=SANDBOX_USER,
            )
        except Exception:  # noqa: BLE001 - advisory: no answer is not an error
            return None
        if exit_code != 0 or not isinstance(output, (bytes, bytearray)):
            return None
        answer = bytes(output).decode("utf-8", errors="replace").strip()
        if answer == "OVER bytes":
            return HarvestLimitExceeded("bytes", max_bytes)
        if answer == "OVER entries":
            return HarvestLimitExceeded("entries", HARVEST_MAX_ENTRIES)
        return None

    # -- execute -------------------------------------------------------

    def _disk_limit_bytes(self, config: SandboxRunConfig) -> int:
        return (config.disk_limit_mb or settings.sandbox_disk_limit_mb) * _MIB

    def _writable_layer_bytes(self, container: Container) -> int | None:
        """The container's writable-layer size in bytes (`SizeRw`: what the sandbox
        has created or changed since it started), or None when the engine did not
        say. Never raises.

        What `SizeRw` counts depends on the storage driver, measured on both host
        shapes (Docker Desktop 28.4 with the containerd snapshotter, and Docker 27.5
        on native Linux overlay2 over ext4): a 100 MiB write reads back as exactly
        100 MiB on both, one call takes 1-10 ms, and a sparse file differs -- the
        snapshotter counts what it OCCUPIES (a 20 GiB sparse file added nothing),
        overlay2 counts its APPARENT size (the same file added 20 GiB). So on Linux
        the watchdog sees a sparse bomb itself, and on Docker Desktop only the
        harvest bound stops it; either way the build ends RESOURCE_LIMIT.

        How one row is read (an EMPTY layer's omitted `SizeRw`, the engine that
        reports nothing) is `writable_layer_bytes_from_row`, which the preview
        watcher reads its rows with as well."""
        try:
            rows = list_containers_bounded(self._client.api, filters={"id": [container.id]}, size=True)
        except Exception:  # noqa: BLE001 - "could not read" (or "did not answer in time") is the answer, and is counted by the caller
            return None
        for row in rows or []:
            if row.get("Id") != container.id:
                continue
            return writable_layer_bytes_from_row(row)
        return None

    def _execute(self, container: Container, config: SandboxRunConfig) -> _ExecOutcome:
        """Runs the configured command with a wall-clock timeout and a disk
        watchdog. Returns what happened (`_ExecOutcome`), logs size-capped.

        The exec's log stream is consumed on a background thread because a
        silent, long-running command (e.g. `sleep 30`, no stdout output)
        blocks a plain `for chunk in stream` loop on I/O until the process
        exits — it never gets a chance to re-check the wall clock between
        chunks. Joining the thread with a timeout is a real, reliable
        watchdog regardless of whether the command produces output.

        **The disk cap.** The same join loop wakes every
        `sandbox_disk_poll_interval_seconds` and reads the container's
        writable-layer size; over the cap the container is killed and the run
        ends `RESOURCE_LIMIT` (`disk_breach="over"`). One more read happens after
        the command exits, so a build that wrote its excess and quit inside one
        interval is caught before the harvest. The engine failing to answer five
        polls in a row, or the post-exit read never coming, kills the run too
        (`"blind"`, reported `INFRA_ERROR`): a cap nobody can read is not a cap, and
        the platform does not let a build run on in that state unannounced. The
        price is the burst a hostile build can write between two polls (the
        interval times its write rate); the harvest's own byte bound still applies
        to whatever it left. Why not `storage_opt`, measured on both host shapes: on
        Docker Desktop the engine ACCEPTS `storage_opt={'size': '100M'}` (it is
        recorded in `HostConfig.StorageOpt`) and then lets 300 MiB through, silently
        unenforced; on native Linux (Docker 27.5.1, overlay2 over ext4) it REFUSES the
        create outright (`--storage-opt is supported only for overlay over xfs with
        'pquota' mount option`). Neither can be the cap, and the second would break
        every build if it were sent.

        **Nothing runs unwatched after the command.** When the command exits, every
        process in the container is stopped (`_stop_processes`) BEFORE the final disk check and
        before anything is read out for the harvest; the reads that need the live container (the
        OOM signals, the pre-flight walk) are made first. A background process the command left
        running (`nohup ... &`, then `exit 0`) used to keep writing to the writable layer, with no
        watchdog, for the whole harvest. **Each size read is bounded in time** too
        (`SIZE_READ_TIMEOUT_SECONDS`; an expired read is an unreadable poll), so the wall-clock
        check between reads is overshot by at most one read."""
        timeout = config.timeout_seconds or (settings.build_max_duration_minutes * 60)
        max_bytes = settings.sandbox_log_max_bytes
        disk_limit = self._disk_limit_bytes(config)
        poll_interval = max(settings.sandbox_disk_poll_interval_seconds, 0.05)

        exec_id = self._client.api.exec_create(
            container.id, config.command, workdir=WORKSPACE_PATH, user=SANDBOX_USER
        )["Id"]
        exec_stream = self._client.api.exec_start(exec_id, stream=True, demux=False)

        log_chunks: list[bytes] = []
        log_bytes = 0

        def _consume() -> None:
            nonlocal log_bytes
            for chunk in exec_stream:
                if log_bytes < max_bytes:
                    log_chunks.append(chunk)
                    log_bytes += len(chunk)

        reader = threading.Thread(target=_consume, daemon=True)
        reader.start()

        deadline = time.monotonic() + timeout
        disk_breach: str | None = None
        blind_polls = 0
        while reader.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            reader.join(min(poll_interval, remaining))
            if not reader.is_alive():
                break
            used = self._writable_layer_bytes(container)
            disk_breach, blind_polls = judge_disk_reading(
                used, limit_bytes=disk_limit, blind_polls=blind_polls
            )
            if disk_breach is not None:
                break
        timed_out = disk_breach is None and reader.is_alive()

        oom_killed = False
        stopped = True
        workspace_refusal: HarvestLimitExceeded | None = None
        if timed_out or disk_breach is not None:
            stopped = self._stop_processes(container)
            reader.join(5)  # let the stream unblock now the container is dead
            exit_code = None
        else:
            exit_inspect = self._client.api.exec_inspect(exec_id)
            exit_code = exit_inspect.get("ExitCode")
            # the command is done, but whatever it left running (`nohup ... &`) is not, and
            # nothing watches it from here to the harvest. So: first everything that needs the
            # LIVE container (the OOM signals, the pre-flight walk), then stop every process in
            # it, then judge the disk on what is left. Read in that order a background writer
            # cannot add a byte between the check and the harvest, because there is no process
            # left to add it.
            oom_killed = self._oom_observed(container)
            workspace_refusal = self._workspace_extent_refusal(container, max_bytes=disk_limit)
            stopped = self._stop_processes(container)
            if stopped:
                disk_breach = self._final_disk_check(container, disk_limit)
                if disk_breach is not None:
                    exit_code = None

        logs = b"".join(log_chunks)[:max_bytes].decode("utf-8", errors="replace")
        if log_bytes > max_bytes:
            logs += "\n...[log output truncated]"

        return _ExecOutcome(
            exit_code=exit_code,
            timed_out=timed_out,
            oom_killed=oom_killed,
            logs=logs,
            disk_breach=disk_breach,
            stopped=stopped,
            workspace_refusal=workspace_refusal,
        )

    def _oom_observed(self, container: Container) -> bool:
        """Whether the run was OOM-killed: `State.OOMKilled` first, then the cgroup's own counter
        (`State.OOMKilled` alone does not see an exec'd child being killed, see
        `_cgroup_oom_kill_observed`). Needs the container ALIVE, which is why `_execute` asks it
        before `_stop_processes`: the cgroup read is an exec, and a stopped container
        refuses it. Not asked for a run that timed out or was killed for its disk: `run()` never
        looks at it there."""
        try:
            container.reload()
            if bool(container.attrs.get("State", {}).get("OOMKilled")):
                return True
        except (APIError, DockerException):
            pass
        return self._cgroup_oom_kill_observed(container)

    def _stop_processes(self, container: Container) -> bool:
        """Stops EVERY process in the container and returns True only once it is confirmed no
        longer running. The container's PID 1 is `sleep infinity`; killing it ends the
        PID namespace, and the kernel kills every other process in it with it, whatever the
        build command started in the background. Measured on this host (Docker 28.4,
        containerd snapshotter): `kill` returns after the container is `exited` (0.14 s with a
        writer running), `SizeRw` of the stopped container is readable and does not move,
        `get_archive` works, `exec` is refused (409) and so is a second `kill` (409: not
        running).

        The kill's own exception is not the answer, the container's state is: a kill that
        raised because the container is already stopped is a success, one that raised for any
        other reason is read off `State.Running`; a state that cannot be read, or that does not
        say `Running` is False, is "not confirmed", and the run fails closed (the container is
        force-removed in `run`'s `finally` regardless)."""
        try:
            container.kill()
        except Exception:  # noqa: BLE001 - the state below decides, not the exception
            pass
        try:
            container.reload()
            return container.attrs.get("State", {}).get("Running") is False
        except Exception:  # noqa: BLE001 - cannot read it: not confirmed
            return False

    def _final_disk_check(self, container: Container, disk_limit: int) -> str | None:
        """The read after the command exited: `"over"`, `"blind"` or None.
        A few quick retries before `"blind"`, since one failed call here would
        otherwise fail a build that already finished. **It judges the FROZEN
        state**: `_stop_processes` has already run, so the size read here is the size the
        harvest then archives, and nothing can grow it between the two. Worst case in time:
        three reads of at most `SIZE_READ_TIMEOUT_SECONDS` each, and two 0.2 s waits."""
        for attempt in range(3):
            used = self._writable_layer_bytes(container)
            if used is not None:
                return "over" if used > disk_limit else None
            if attempt < 2:
                time.sleep(0.2)
        return "blind"

    def probe_disk_cap(self, *, probe_mb: int = 8, preview: bool = False) -> str | None:
        """does the cap hold on THIS host? Returns None when it does, else
        one sentence naming the gap (the worker logs it at startup, so an operator
        learns the platform cannot enforce the disk cap before a build says so).

        The cap rests on the engine reporting the writable layer's size, so the
        probe measures exactly that: a throwaway container from the sandbox image
        (the configured runtime included -- a stronger runtime may keep its
        filesystem where the engine cannot see it), a `probe_mb` write, and the
        size the engine then reports. Never raises; a probe that could not run is a
        gap too (`could not be verified`), not a pass.

        **`preview=True` is the same probe for the other thing the cap
        covers.** A preview runs on its own runtime (ADR 0046 §Decision 2:
        `Settings.effective_preview_runtime`, which defaults to the build's), so the probe container
        carries exactly the runtime a preview gets, and a gap is worded for
        the preview (where there is no harvest bound to fall back on, only its TTL)."""
        subject = _PREVIEW_CAP_WORDS if preview else _BUILD_CAP_WORDS
        container = None
        try:
            kwargs: dict = {}
            # a preview probe runs on the runtime previews run on (`effective_preview_runtime`),
            # a build probe on the build's; neither unset sends no `runtime` key at all.
            probe_runtime = settings.effective_preview_runtime if preview else settings.build_container_runtime
            if probe_runtime:
                kwargs["runtime"] = probe_runtime
            container = self._client.containers.create(
                image=settings.sandbox_image,
                command=["sleep", "60"],
                detach=True,
                # labelled like any sandbox, with no build: if the worker dies during
                # the probe, the reconciler removes the leftover at the next start.
                labels=platform_labels(Role.SANDBOX),
                **kwargs,
            )
            container.start()
            exit_code, _ = container.exec_run(
                ["sh", "-c", f"dd if=/dev/zero of=/tmp/forge-disk-probe bs=1M count={probe_mb}"]
            )
            if exit_code != 0:
                return f"the disk cap could not be verified: the probe write failed (exit {exit_code})"
            used = self._writable_layer_bytes(container)
        except Exception as exc:  # noqa: BLE001 - a probe that cannot run is reported, not raised
            return f"the disk cap could not be verified: the probe failed ({type(exc).__name__})"
        finally:
            if container is not None:
                try:
                    container.remove(v=True, force=True)
                except Exception:  # noqa: BLE001
                    logger.warning("sandbox runner: the disk-cap probe container could not be removed")
        if used is None:
            return (
                "the container engine does not report a container's writable-layer size, so "
                f"{subject.name} is NOT enforced on this host ({subject.fallback}) and "
                f"{subject.consequence} until it does"
            )
        if used < probe_mb * _MIB:
            return (
                f"the container engine reported {used} bytes of writable-layer use after a "
                f"{probe_mb} MiB write, so {subject.name} is NOT reliably enforced "
                f"on this host ({subject.fallback})"
            )
        return None

    def _cgroup_oom_kill_observed(self, container: Container) -> bool:
        """Second, more reliable OOM signal alongside `State.OOMKilled`,
        added after `State.OOMKilled` was measured missing real OOM kills
        on this engine — both for a structural reason and a timing reason,
        neither host-specific:

        1. **Structural**: the command above always runs via `exec_create`,
           never as the container's own CMD — the container's actual PID 1
           is `sleep infinity` (see `SANDBOX_USER` docstring). Docker's
           `State.OOMKilled` reflects an OOM kill of the container's OWN
           PID 1 and is set when the *container* exits; when the kernel
           OOM-kills an exec'd child instead, PID 1 never dies, the
           container never exits, and `State.OOMKilled` structurally can
           never observe it. Measured directly against this engine (Docker
           28.4.0, cgroup v2/cgroupfs): an exec'd process hitting a 64MB
           `mem_limit` gets SIGKILL'd by the kernel (exec exit code 137)
           while `State.OOMKilled` stayed `False` and `State.Status` stayed
           `"running"`, every time.
        2. **Timing**: even in the case `State.OOMKilled` CAN observe (the
           container's own PID 1 killed), the daemon sets it asynchronously
           off its own cgroup-event watcher. Measured: an immediate
           `container.reload()` right after the kill (this method's exact
           sequence) read `True` 3/5 times and `False` 2/5 times across five
           back-to-back runs with no other change — a genuine race, not
           flakiness in the test. Polling `container.reload()` for up to 2s
           always converged to `True` eventually, confirming the daemon
           gets there — just not deterministically within one immediate
           check.

        The container's cgroup is shared between PID 1 and every exec'd
        process (Docker gives an exec no cgroup of its own), so the
        container's own cgroup v2 `memory.events` `oom_kill` counter
        increments for either kind of death, and reflects the kernel's own
        OOM accounting directly rather than dockerd's async notification of
        it. Measured deterministic 5/5 with no retry loop, each read faster
        than a single `container.reload()`.

        Deliberately NOT keyed on exit code: a plain SIGKILL (e.g. this
        module's own timeout-kill, or an ordinary `docker kill`) is also
        code 137 and is NOT an OOM — measured directly: `kill -9` on an
        exec'd process left `oom_kill` at 0. The kernel's cgroup accounting,
        not the exit code, is what actually distinguishes the two; this
        method never looks at exit code at all.

        **ADR 0046 §Decision 6: the file is read from the HOST, never by exec into the
        container.** The old read was a root `cat` exec in a container that had just run untrusted
        code. Now the worker (which runs on the Docker host, ADR 0002) opens the container's own
        cgroup directory itself: `<root>/<parent>/docker-<id>.scope/memory.events` under the
        systemd cgroup driver, `<root>/<parent>/<id>/memory.events` under cgroupfs, with the
        parent taken from the container's inspect (`HostConfig.CgroupParent`, else `system.slice`
        resp. `docker`). The container must be ALIVE (its cgroup is removed with it), which is why
        `_execute` asks before `_stop_processes`. Docker's own API has no such counter
        (measured on 29.x: `stats` carries `memory.stat` only, and `get_archive` of
        `/sys/fs/cgroup/memory.events` is a 404), so where the worker cannot see the engine's
        cgroup tree (Docker Desktop: the engine is in a VM; cgroup v1; a worker not on the engine
        host) there is no host path: that is "no additional signal" and `State.OOMKilled` alone
        (checked first) is the fallback, the same as the v1 fallback always was. Any failure to
        read (missing, permission, malformed) is that same "no additional signal"; this method
        never raises. Under gVisor the sandbox's cgroup counters are the host's view of the
        sandbox, not of the processes in it: whether this still sees an OOM there is still to be measured.
        """
        events_path = self._host_memory_events_path(container)
        if events_path is None:
            return False
        try:
            with open(events_path, "rb") as handle:
                output = handle.read(_MEMORY_EVENTS_READ_LIMIT)
        except OSError:
            return False
        for line in output.decode("utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "oom_kill":
                try:
                    return int(parts[1]) > 0
                except ValueError:
                    return False
        return False

    @staticmethod
    def _host_memory_events_path(container: Container) -> str | None:
        """The host path of this container's cgroup v2 `memory.events`, or None when the worker's
        host has no such file for it. Candidates only (the two Docker cgroup drivers); the first
        that exists wins. No exec, no engine call: `container.attrs` is whatever `reload()` last
        read. A container id that is not a plain name (no separator, no `..`) is refused outright, so
        the id can never steer the path elsewhere."""
        container_id = getattr(container, "id", None)
        if not isinstance(container_id, str) or not _CONTAINER_ID.fullmatch(container_id) or ".." in container_id:
            return None
        parent = ""
        attrs = getattr(container, "attrs", None)
        if isinstance(attrs, dict):
            host_config = attrs.get("HostConfig")
            if isinstance(host_config, dict):
                value = host_config.get("CgroupParent")
                if isinstance(value, str):
                    parent = value.strip("/")
        if parent and (".." in parent.split("/") or "\x00" in parent):
            return None
        root = HOST_CGROUP_ROOT
        candidates = [
            f"{root}/{parent or 'system.slice'}/docker-{container_id}.scope/memory.events",
            f"{root}/{parent or 'docker'}/{container_id}/memory.events",
        ]
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate
        return None

    # -- destroy ---------------------------------------------------------

    def _destroy(self, container: Container) -> None:
        """Unconditional teardown (invariant 1). Best-effort: failures here
        are logged-away, never raised, so they can't mask the run's result."""
        try:
            container.remove(v=True, force=True)
        except Exception as exc:  # noqa: BLE001 - teardown never raises (not only Docker's own errors)
            logger.warning("sandbox runner: a sandbox container could not be removed (%s)", type(exc).__name__)
