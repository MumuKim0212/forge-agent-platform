"""Host free-disk check.

The worker measures free space on the Docker data root (asked of `docker info`), and on the
platform's own data directories: the SQLite database's directory (only when the platform DB is
SQLite), `local_git_root`, and the service-data backup directory. The newest result per location
is persisted in `host_disk_checks` for the admin console.

* A location below EITHER threshold (percent free, absolute GB) is `BELOW_THRESHOLD`.
* A location that cannot be measured -- no such path (a Docker data root inside Docker Desktop's
  VM), no permission, `docker info` failing, or a read that does not return within
  `MEASURE_TIMEOUT_SECONDS` -- is `UNKNOWN` with null sizes. It is never recorded as `OK`.
* A directory the platform creates itself (backup dir, `local_git_root`, the SQLite directory) that
  does not exist yet is measured at its nearest existing ancestor: that is where it will land. The
  Docker data root gets no such walk: missing on this host it is UNKNOWN, with its path shown.

Never raises: a failing check is logged and retried next cycle.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import settings
from app.models.host_disk_check import HostDiskCheck
from app.services import notifications

logger = logging.getLogger("app.host_disk")

# A statvfs on a hung network mount can block for a long time; the check must not.
MEASURE_TIMEOUT_SECONDS = 10.0

GIB = 1024**3


class DiskState:
    OK = "OK"
    BELOW_THRESHOLD = "BELOW_THRESHOLD"
    UNKNOWN = "UNKNOWN"


class Label:
    DOCKER_DATA_ROOT = "docker_data_root"
    DATABASE_DIR = "database_dir"
    LOCAL_GIT_ROOT = "local_git_root"
    BACKUP_DIR = "backup_dir"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sqlite_directory(database_url: str | None) -> str | None:
    url = database_url or ""
    if not url.lower().startswith("sqlite"):
        return None
    path = url.split("///", 1)[1] if "///" in url else ""
    if not path or ":memory:" in path:
        return None
    return os.path.dirname(os.path.abspath(path))


def _docker_data_root(client) -> str | None:
    try:
        if client is None:
            import docker

            client = docker.from_env()
        root = client.info().get("DockerRootDir")
        return root if isinstance(root, str) and root else None
    except Exception as exc:  # noqa: BLE001 - recorded as unknown by the caller
        logger.warning("host disk check: could not read the Docker data root (%s)", exc)
        return None


def locations(docker_client=None) -> list[tuple[str, str | None]]:
    """(label, path) for every location this installation checks; a None path is unknown."""
    result: list[tuple[str, str | None]] = [(Label.DOCKER_DATA_ROOT, _docker_data_root(docker_client))]
    database_dir = _sqlite_directory(settings.database_url)
    if database_dir is not None:
        result.append((Label.DATABASE_DIR, database_dir))
    result.append((Label.LOCAL_GIT_ROOT, os.path.abspath(settings.local_git_root)))
    result.append((Label.BACKUP_DIR, os.path.abspath(settings.service_data_backup_dir)))
    return result


def _nearest_existing(path: str) -> str | None:
    current = path
    while not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return current


def measure(
    path: str, usage_fn=None, timeout: float | None = None, *, walk_up: bool = True
) -> tuple[int, int] | None:
    """(free_bytes, total_bytes) of the filesystem holding `path`, or None when it cannot be read
    in time. Runs in a daemon thread so a hung mount costs `timeout`, not the worker.

    `walk_up`: a missing path is measured at its nearest existing ancestor -- right for a directory
    the platform itself will create there. Wrong for the Docker data root, which is the path the
    DAEMON reports: missing on this host it means the daemon's filesystem is not readable from here,
    and an ancestor (`/`, the current drive) is an unrelated filesystem. There a missing path is None."""
    usage = usage_fn or shutil.disk_usage
    outcome: dict = {}

    def _read() -> None:
        try:
            target = _nearest_existing(path) if walk_up else (path if os.path.exists(path) else None)
            if target is None:
                return
            total, _used, free = usage(target)
            outcome["value"] = (int(free), int(total))
        except Exception as exc:  # noqa: BLE001
            logger.warning("host disk check: could not measure %s (%s)", path, exc)

    thread = threading.Thread(target=_read, name="host-disk-measure", daemon=True)
    thread.start()
    thread.join(MEASURE_TIMEOUT_SECONDS if timeout is None else timeout)
    return outcome.get("value")


def judge(free_bytes: int, total_bytes: int) -> str:
    percent = (free_bytes / total_bytes * 100.0) if total_bytes > 0 else 0.0
    if percent < settings.host_disk_min_free_percent:
        return DiskState.BELOW_THRESHOLD
    if free_bytes < settings.host_disk_min_free_gb * GIB:
        return DiskState.BELOW_THRESHOLD
    return DiskState.OK


_DISK_NAMES = {
    Label.DOCKER_DATA_ROOT: "Docker data root",
    Label.DATABASE_DIR: "Platform database directory",
    Label.LOCAL_GIT_ROOT: "Local git root",
    Label.BACKUP_DIR: "Backup directory",
}


def _announce(db: Session, rows: list[HostDiskCheck]) -> None:
    """each measured location's state is reported (below threshold = down, OK = up); the
    persisted notification state makes that a notice only on a change. An UNKNOWN location says
    nothing: not being able to measure is not a recovery and not a new alarm. Never raises."""
    try:
        if not notifications.any_channel_configured():
            return
        factory = notifications.session_factory_for(db)
        for row in rows:
            if row.state == DiskState.UNKNOWN:
                continue
            notifications.report(
                factory,
                family=notifications.Family.DISK,
                subject=row.label,
                name=_DISK_NAMES.get(row.label, row.label),
                healthy=row.state == DiskState.OK,
            )
    except Exception:  # noqa: BLE001
        logger.warning("host disk check: announcing a state failed", exc_info=True)


def run_disk_check_if_due(
    db: Session,
    *,
    docker_client=None,
    scan_state: dict | None = None,
    now: datetime | None = None,
    usage_fn=None,
    timeout: float | None = None,
) -> bool:
    """Measures and persists every location when due; returns whether it ran. `scan_state` is the
    caller's dict across ticks (`next_due_at`, advanced before measuring so a failure waits a full
    interval); None is always due (tests). Never raises."""
    moment = now or _utcnow()
    if scan_state is not None:
        due = scan_state.get("next_due_at")
        if due is not None and moment < due:
            return False
        scan_state["next_due_at"] = moment + timedelta(seconds=settings.host_disk_check_interval_seconds)
    try:
        rows: list[HostDiskCheck] = []
        for label, path in locations(docker_client):
            reading = (
                measure(path, usage_fn, timeout, walk_up=label != Label.DOCKER_DATA_ROOT)
                if path is not None
                else None
            )
            if reading is None:
                rows.append(
                    HostDiskCheck(
                        label=label, path=path, free_bytes=None, total_bytes=None,
                        free_percent=None, state=DiskState.UNKNOWN, checked_at=moment,
                    )
                )
                continue
            free, total = reading
            rows.append(
                HostDiskCheck(
                    label=label, path=path, free_bytes=free, total_bytes=total,
                    free_percent=(free / total * 100.0) if total > 0 else 0.0,
                    state=judge(free, total), checked_at=moment,
                )
            )
        db.execute(delete(HostDiskCheck).where(HostDiskCheck.label.not_in([row.label for row in rows])))
        for row in rows:
            db.merge(row)
        db.commit()
        _announce(db, rows)
        return True
    except Exception:  # noqa: BLE001 - a failing check must never take the worker loop down
        logger.exception("host disk check failed; it is retried next cycle")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False
