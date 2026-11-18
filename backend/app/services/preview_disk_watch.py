"""The disk cap for previews (the rule is
"same hardening as the build container", the disk limit included).

The BUILD sandbox has an enforced disk cap: a watchdog on its writable layer
(`SizeRw`, read through `sandbox_runner.writable_layer_bytes_from_row`) that kills the build
over the limit, and fails closed when the engine stops reporting sizes. A preview runs the same
unvetted image for up to its TTL (an hour by default) and had nothing. This module extends the
cap to it with the same mechanism and the same limit (`Settings.sandbox_disk_limit_mb`), and
reuses the build watchdog's pieces rather than copying them: the row reading and the verdict
(`writable_layer_bytes_from_row`, `judge_disk_reading`, the five-polls-in-a-row blind rule) are
imported from `sandbox_runner`, the startup probe is `SandboxRunner.probe_disk_cap(preview=True)`.

**One watcher for every preview, on ONE thread of the worker process -- not a thread per
preview, and not a sweep in the worker's loop.** The loop was the obvious driver and cannot be
it: the worker is a single thread, `run_iteration` is blocked for the whole of a build (up to
`build_max_duration_minutes`, 30 minutes in prod) and its sweeps run only between work items, so a
loop-driven watcher would be blind exactly while the platform is busy, which is when previews
are alive (a preview outlives its build by an hour and builds queue back to back). One daemon
thread polls ONE `containers(size=True)` call for every live preview of this installation.

**What is watched.** Containers that carry this installation's `forge.instance` AND
`forge.role=preview` AND `forge.preview=true` AND a `forge.build_id`: the engine's label filter
only narrows the list, the Python check decides (the same rule `stop_preview_for_project` follows),
so a preview of another installation sharing the daemon, a test run, or any
container that is not positively this platform's preview is never touched. A preview
with no instance label is not watched and ends with its TTL.

**What happens over the limit, and when the engine stops answering.** A preview whose layer is
strictly over the limit, or whose size the engine has not reported for five polls in a row (a
whole failed read counts as one for every live preview, named by an unsized list), is torn down
through `PreviewManager.stop_preview` -- the one teardown every other trigger uses (container,
network, Traefik and data-store detachment, the preview's own database and Redis) -- and only
counted as stopped once the container is really gone. The record is made by the caller
(`record_preview_disk_stop`): the build's preview columns are cleared and an audit entry is
written against the PROJECT (`project:<id>`, the target the project timeline already shows),
whose action says why. A record that could not be written is retried every cycle, never lost.

**The size read is bounded in time.** Each sized call has `SIZE_READ_TIMEOUT_SECONDS` to
start answering (docker-py would wait 60 s), and an expired call is a failed one, so the five
unreadable polls take at most five times (that bound + the poll interval) and not minutes. When the
sized call fails, the previews to count are named by a second, unsized list, which needs no layer
walk: a preview first seen while reads fail, or after a worker restart, is judged like the rest.

**What it cannot do (stated, not hidden).** The burst a hostile preview writes between two polls
is unbounded (interval x write rate); there is no harvest to fall back on, since a preview is
never archived; and a sparse file counts as the engine's storage driver counts it (overlay2 at
its apparent size, Docker Desktop's snapshotter at what it occupies). The worker's startup
statement says so per host (`worker._log_disk_cap_startup_banner`).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

import docker
from docker.errors import NotFound

from app.config import settings
from app.models.build import Build
from app.services import platform_labels
from app.services.audit import write_audit_log
from app.services.build_queue import clear_preview_fields
from app.services.platform_labels import (
    LABEL_BUILD_ID,
    LABEL_INSTANCE,
    LABEL_PROJECT_ID,
    LABEL_ROLE,
    Role,
)
from app.services.preview_manager import PREVIEW_CONTAINER_PREFIX, PreviewManager
from app.services.sandbox_runner import (
    judge_disk_reading,
    list_containers_bounded,
    writable_layer_bytes_from_row,
)

logger = logging.getLogger("app.preview_disk_watch")

_MIB = 1024 * 1024

# The two audit actions. What the project timeline shows for such an entry is the action
# alone, so each one NAMES its cause; the sentence is in the entry's metadata as well.
ACTION_STOPPED_OVER = "preview.stopped_disk_limit"
ACTION_STOPPED_BLIND = "preview.stopped_disk_unreadable"

VERDICT_OVER = "over"
VERDICT_BLIND = "blind"


@dataclass(frozen=True)
class PreviewDiskStop:
    """One preview the watcher tore down and confirmed gone, waiting to be recorded."""

    build_id: str
    project_id: str | None
    verdict: str  # VERDICT_OVER or VERDICT_BLIND
    used_bytes: int | None  # None for a blind stop: nothing was read
    limit_bytes: int


def stop_detail(stop: PreviewDiskStop) -> str:
    """The platform-authored sentence for a stop (failure detail rules:
    platform templates filled with platform-known values, never anything the preview wrote)."""
    if stop.verdict == VERDICT_OVER:
        return (
            f"the preview was stopped because its container wrote more than the "
            f"{stop.limit_bytes // _MIB} MiB disk limit"
        )
    return (
        "the preview was stopped because the container engine stopped reporting its disk use, "
        "and a limit that cannot be read cannot be enforced"
    )


class PreviewDiskWatcher:
    """Polls every live preview of this installation and tears down the ones over the cap.

    Holds the only state the verdict needs across polls: which containers were seen on the
    last read that listed them (the fallback when the unsized list fails too) and how many polls in a row
    each has gone unreadable. All of it in memory: a restarted worker starts counting again,
    which errs in the safe direction (a hostile preview gets five more polls, not a pass)."""

    def __init__(
        self,
        *,
        client: docker.DockerClient | None = None,
        preview_manager: PreviewManager | None = None,
    ) -> None:
        self._client = client or docker.from_env()
        self._manager = preview_manager or PreviewManager(client=self._client)
        # container id -> (build id, project id), from the last read that worked
        self._seen: dict[str, tuple[str, str | None]] = {}
        self._blind: dict[str, int] = {}
        self._read_failing = False

    # -- one poll --------------------------------------------------------

    def poll_once(self) -> list[PreviewDiskStop]:
        """One look at every live preview. Returns the previews it tore down this time (each
        confirmed gone). Never raises: a failure is logged and the next poll tries again."""
        try:
            return self._poll()
        except Exception:  # noqa: BLE001 - the watcher must outlive anything one poll hits
            logger.exception("preview disk watch: a poll failed; the next one runs as usual")
            return []

    def _poll(self) -> list[PreviewDiskStop]:
        limit_bytes = settings.sandbox_disk_limit_mb * _MIB
        readings = self._read()
        stops: list[PreviewDiskStop] = []
        for container_id, build_id, project_id, used in readings:
            verdict, blind_polls = judge_disk_reading(
                used, limit_bytes=limit_bytes, blind_polls=self._blind.get(container_id, 0)
            )
            if blind_polls:
                self._blind[container_id] = blind_polls
            else:
                self._blind.pop(container_id, None)
            if verdict is None:
                continue
            if self._tear_down(container_id, build_id):
                logger.warning(
                    "preview disk watch: stopped the preview of build %s (%s; limit %d bytes, read %s)",
                    build_id,
                    verdict,
                    limit_bytes,
                    "nothing" if used is None else f"{used} bytes",
                )
                stops.append(
                    PreviewDiskStop(
                        build_id=build_id,
                        project_id=project_id,
                        verdict=verdict,
                        used_bytes=used,
                        limit_bytes=limit_bytes,
                    )
                )
                self._seen.pop(container_id, None)
                self._blind.pop(container_id, None)
        return stops

    def _read(self) -> list[tuple[str, str, str | None, int | None]]:
        """`(container id, build id, project id, bytes or None)` for every preview to judge.

        One engine call for all of them, bounded in time (`SIZE_READ_TIMEOUT_SECONDS`: a read that
        has not answered by then is a failed read). When the call fails, EVERY live preview of this
        installation is judged as unreadable (`None`): the cap is not being read for any of them.
        Which previews those are comes from a second, UNSIZED list (`_unreadable_previews`), so a
        preview that appeared while reads were already failing, or after the worker restarted, is
        counted like the ones the last good read showed."""
        instance = platform_labels.instance_id()
        label_filter = {"label": [f"{LABEL_INSTANCE}={instance}", f"{LABEL_ROLE}={Role.PREVIEW}"]}
        try:
            rows = list_containers_bounded(self._client.api, filters=label_filter, size=True)
        except Exception as exc:  # noqa: BLE001 - "could not read" is the answer, counted per preview
            if not self._read_failing:
                logger.warning(
                    "preview disk watch: the engine's sized container list failed (%s); every preview "
                    "is counted as unreadable until it answers",
                    type(exc).__name__,
                )
            self._read_failing = True
            return self._unreadable_previews(instance, label_filter)
        if self._read_failing:
            logger.info("preview disk watch: the engine's container list answers again")
        self._read_failing = False

        readings: list[tuple[str, str, str | None, int | None]] = []
        seen: dict[str, tuple[str, str | None]] = {}
        for row in rows or []:
            owned = _owned_preview(row, instance)
            if owned is None:
                continue
            container_id, build_id, project_id = owned
            seen[container_id] = (build_id, project_id)
            readings.append((container_id, build_id, project_id, writable_layer_bytes_from_row(row)))
        self._seen = seen
        self._blind = {cid: n for cid, n in self._blind.items() if cid in seen}
        return readings

    def _unreadable_previews(
        self, instance: str, label_filter: dict
    ) -> list[tuple[str, str, str | None, int | None]]:
        """Every live preview of this installation, each with no size, for a poll whose sized read
        failed. The list comes from an UNSIZED call: the engine does not walk any layer for
        it, so it can answer when the sized one cannot (that is what is being asked of it: which
        previews exist), and it is bounded in time like the sized one. Judging only the previews an
        earlier good read had shown left a preview first seen while reads were failing, and every
        preview after a worker restart, running uncapped until its TTL.

        When even that list fails, the previews the last good read showed are all there is to go on
        (nothing, on a worker that has not had a good read: there is no way to name a container to
        stop while the engine will not list them, and the next poll asks again)."""
        try:
            rows = list_containers_bounded(self._client.api, filters=label_filter, size=False)
        except Exception as exc:  # noqa: BLE001 - fall back to what is already known
            logger.warning(
                "preview disk watch: the unsized container list failed too (%s); counting the "
                "previews last seen as unreadable",
                type(exc).__name__,
            )
            return [(cid, build_id, project_id, None) for cid, (build_id, project_id) in self._seen.items()]
        seen: dict[str, tuple[str, str | None]] = {}
        for row in rows or []:
            owned = _owned_preview(row, instance)
            if owned is None:
                continue
            container_id, build_id, project_id = owned
            seen[container_id] = (build_id, project_id)
        self._seen = seen
        self._blind = {cid: n for cid, n in self._blind.items() if cid in seen}
        return [(cid, build_id, project_id, None) for cid, (build_id, project_id) in seen.items()]

    def _tear_down(self, container_id: str, build_id: str) -> bool:
        """Tears the preview down through the normal path and returns True only when THIS
        container is gone afterwards. A container that already went (a TTL sweep, a newer build)
        is not this watcher's to record; one that is still there after the attempt is left to the
        next poll, which finds it over the limit (or unreadable) again."""
        name = f"{PREVIEW_CONTAINER_PREFIX}-{build_id}"
        try:
            current = self._client.containers.get(name)
        except NotFound:
            return False
        except Exception:  # noqa: BLE001 - cannot tell; the next poll asks again
            logger.exception("preview disk watch: could not look up preview %s before stopping it", build_id)
            return False
        if current.id != container_id:
            # A different container now carries the name (the same build's preview was replaced
            # since the read): it is not the one that was over the limit, so it is left alone.
            return False
        try:
            self._manager.stop_preview(build_id)
        except Exception:  # noqa: BLE001 - `stop_preview` is best-effort already; belt and braces
            logger.exception("preview disk watch: stopping preview %s raised", build_id)
        try:
            self._client.containers.get(container_id)
        except NotFound:
            return True
        except Exception:  # noqa: BLE001 - cannot confirm it is gone: not recorded yet
            logger.exception("preview disk watch: could not confirm preview %s is gone", build_id)
            return False
        logger.error(
            "preview disk watch: preview %s is over its disk cap and its container could not be "
            "removed; the next poll tries again",
            build_id,
        )
        return False


def _owned_preview(row: dict, instance: str) -> tuple[str, str, str | None] | None:
    """`(container id, build id, project id)` when the row is positively this installation's
    preview, else None. The engine's label filter narrows the list; this decides."""
    container_id = row.get("Id")
    labels = row.get("Labels") or {}
    if not isinstance(container_id, str) or not container_id or not isinstance(labels, dict):
        return None
    if labels.get(LABEL_INSTANCE) != instance or labels.get(LABEL_ROLE) != Role.PREVIEW:
        return None
    if labels.get("forge.preview") != "true":
        return None
    build_id = labels.get(LABEL_BUILD_ID)
    if not isinstance(build_id, str) or not build_id:
        return None
    project_id = labels.get(LABEL_PROJECT_ID)
    return container_id, build_id, project_id if isinstance(project_id, str) and project_id else None


def record_preview_disk_stop(db, stop: PreviewDiskStop) -> None:
    """Records a stop where the project's timeline shows it: clears the build's live-preview
    columns (so the preview link stops being offered) and writes one audit entry against
    `project:<id>`, whose action names the cause, in ONE transaction. Raises if the commit
    fails (after a rollback), so the caller can retry; a build with no row has no timeline to
    write to and is not an error."""
    build = db.get(Build, stop.build_id)
    if build is None:
        logger.warning("preview disk watch: build %s has no row; its stop has no timeline to go on", stop.build_id)
        return
    try:
        clear_preview_fields(db, stop.build_id, commit=False)
        write_audit_log(
            db,
            actor_id=None,
            action=ACTION_STOPPED_OVER if stop.verdict == VERDICT_OVER else ACTION_STOPPED_BLIND,
            target=f"project:{build.project_id}",
            metadata={
                "build_id": stop.build_id,
                "limit_bytes": stop.limit_bytes,
                "used_bytes": stop.used_bytes,
                "detail": stop_detail(stop),
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        raise


class PreviewDiskWatchRunner:
    """Drives one `PreviewDiskWatcher` on one daemon thread, and records what it stops.

    The thread is the point (see the module docstring): it keeps watching while the worker's
    own thread is inside a build. `run_cycle` is the unit a test calls directly; the loop only
    repeats it every `sandbox_disk_poll_interval_seconds`. The loop catches every `Exception`,
    so only a `BaseException` can end it, and an ended watcher is never silent: the exit is
    logged at ERROR."""

    def __init__(self, watcher: PreviewDiskWatcher, session_factory: Callable[[], object]) -> None:
        self._watcher = watcher
        self._session_factory = session_factory
        self._unrecorded: list[PreviewDiskStop] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_cycle(self) -> None:
        """One poll, then every stop not yet recorded. Never raises."""
        self._unrecorded.extend(self._watcher.poll_once())
        self._flush()

    def _flush(self) -> None:
        if not self._unrecorded:
            return
        remaining: list[PreviewDiskStop] = []
        db = None
        try:
            db = self._session_factory()
            for stop in self._unrecorded:
                try:
                    record_preview_disk_stop(db, stop)
                except Exception:  # noqa: BLE001 - kept and retried next cycle
                    logger.exception(
                        "preview disk watch: the stop of preview %s could not be recorded; retrying", stop.build_id
                    )
                    remaining.append(stop)
        except Exception:  # noqa: BLE001 - no session at all: everything waits
            logger.exception("preview disk watch: no database session to record stops with; retrying")
            remaining = list(self._unrecorded)
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    logger.exception("preview disk watch: closing the session raised")
        self._unrecorded = remaining

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="forge-preview-disk-watch", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.run_cycle()
                except Exception:  # noqa: BLE001 - one bad cycle must not end the watching
                    logger.exception("preview disk watch: a cycle failed; the next one runs as usual")
                self._stop.wait(max(settings.sandbox_disk_poll_interval_seconds, 0.05))
        finally:
            if not self._stop.is_set():
                logger.error("preview disk watch: the watcher thread ended; previews are NOT being watched")
