"""Active service checks: is each ACTIVE service still up?

A deployment is health-gated once, at deploy; after that its container restarts silently under
`unless-stopped`. This module asks again, on an interval, for every `ACTIVE` deployment:

1. the container exists (found by the ADR 0037 labels -- this instance, role `deploy`, the
   deployment id -- never by name);
2. it is running;
3. its restart count has not gone up since the last check (the baseline is persisted);
4. `GET /healthz` answers 200 through `docker exec` against the container's own loopback, the same
   script the deploy gate runs (`deploy_manager._build_proxy_exec_script`). It never goes through
   Traefik, so it adds nothing to the traffic counters, and it touches no `Project`, so it is not
   activity (`record_traffic_activity` is not called).

A failed check records a `FailureKind`; `service_check_failure_threshold` consecutive failures make
the service `UNHEALTHY`, one pass makes it `HEALTHY` again. Each of those two transitions writes an
audit row. **The check only observes**: it never changes a deployment's status and never restarts,
stops or removes anything.

A check that cannot be carried out (Docker unreachable, an unexpected error) is neither a failure
nor a pass: the result is recorded as `UNKNOWN`, the streak and state stay as they were.

Bounds, so one hung service cannot stall anything (and the checks run on a thread of their own,
`ServiceCheckRunner`, so no other sweep can delay them either):

* one service's whole check (reload + exec) runs in a daemon thread and is given
  `service_check_exec_timeout_seconds`; if it is still in its exec stage then it is a
  `HEALTHZ_TIMEOUT`, if it is stuck in an earlier Docker call it is inconclusive;
* a service whose previous check thread is still hung is not asked again (no thread pile-up);
  it keeps the same outcome;
* the pass as a whole stops starting new services after `service_check_cycle_timeout_seconds`, and
  the next pass starts where this one stopped (`rotation`), so hung services at the head of the
  order cannot starve the tail.

The checker leaves a trace of its own liveness (`ServiceCheckerStatus`: when a pass last started and
finished) because a dead checker leaves every service row looking as it did.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from docker.errors import DockerException, NotFound
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.deployment import Deployment, DeploymentStatus
from app.models.service_checker_status import SINGLETON_ID, ServiceCheckerStatus
from app.models.project import Project
from app.models.service_health_check import ServiceHealthCheck
from app.services import notifications, platform_labels
from app.services.audit import write_audit_log
from app.services.deploy_manager import _build_proxy_exec_script
from app.services.platform_labels import LABEL_DEPLOYMENT_ID, LABEL_INSTANCE, LABEL_ROLE, Role

logger = logging.getLogger("app.service_health")

# No human actor, as every worker-written audit row (worker.SYSTEM_ACTOR_ID).
SYSTEM_ACTOR_ID: str | None = None


class ServiceState:
    HEALTHY = "HEALTHY"
    UNHEALTHY = "UNHEALTHY"


class CheckResult:
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class FailureKind:
    CONTAINER_MISSING = "CONTAINER_MISSING"
    NOT_RUNNING = "NOT_RUNNING"
    RESTARTED = "RESTARTED"
    HEALTHZ_FAILED = "HEALTHZ_FAILED"
    HEALTHZ_TIMEOUT = "HEALTHZ_TIMEOUT"


AUDIT_UNHEALTHY = "service_health.unhealthy"
AUDIT_RECOVERED = "service_health.recovered"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Observation:
    """What one check saw. `kind` None with `inconclusive` False is a pass."""

    kind: str | None = None
    inconclusive: bool = False
    restart_count: int | None = None


def _inspect(container, exec_timeout: float, box: dict) -> None:
    """One service's check, run in a daemon thread. Writes the outcome to `box["observation"]` and
    its progress to `box["stage"]` so the caller knows where a hang was."""
    try:
        box["stage"] = "reload"
        container.reload()
        restart_count = (container.attrs or {}).get("RestartCount")
        restart_count = restart_count if isinstance(restart_count, int) else None
        if container.status != "running":
            box["observation"] = Observation(FailureKind.NOT_RUNNING, restart_count=restart_count)
            return
        baseline = box.get("baseline")
        if restart_count is not None and baseline is not None and restart_count > baseline:
            box["observation"] = Observation(FailureKind.RESTARTED, restart_count=restart_count)
            return
        box["stage"] = "exec"
        inner = max(1.0, min(5.0, exec_timeout / 2))
        url = f"http://127.0.0.1:{settings.preview_container_port}/healthz"
        exit_code, output = container.exec_run(["python", "-c", _build_proxy_exec_script(url, "GET", inner)])
        status_line, _, _ = (output or b"").partition(b"\n")
        if exit_code == 0 and status_line.strip() == b"200":
            box["observation"] = Observation(None, restart_count=restart_count)
        else:
            box["observation"] = Observation(FailureKind.HEALTHZ_FAILED, restart_count=restart_count)
    except NotFound:
        box["observation"] = Observation(FailureKind.CONTAINER_MISSING)
    except DockerException as exc:
        if box.get("stage") == "exec":
            logger.warning("service check: the /healthz exec failed (%s)", exc)
            box["observation"] = Observation(FailureKind.HEALTHZ_FAILED)
        else:
            logger.warning("service check: Docker could not inspect a container (%s)", exc)
            box["observation"] = Observation(inconclusive=True)
    except Exception:  # noqa: BLE001 - an unexpected error is "could not check", never a verdict
        logger.exception("service check: unexpected error inspecting a container")
        box["observation"] = Observation(inconclusive=True)


def _bounded_call(fn: Callable[[], object], timeout: float) -> tuple[bool, object, BaseException | None]:
    """(finished, value, error) of `fn()` run in a daemon thread given `timeout` seconds."""
    box: dict = {}

    def _run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the caller
            box["error"] = exc

    thread = threading.Thread(target=_run, name="service-check-list", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return False, None, None
    return True, box.get("value"), box.get("error")


def _find_containers(client, instance: str, timeout: float) -> dict[str, object] | None:
    """deployment id -> container, for this instance's `deploy`-role containers only (the labels
    are checked again on each object, not left to the filter's semantics). None: Docker could not
    be asked in time."""
    finished, containers, error = _bounded_call(
        lambda: client.containers.list(
            all=True,
            filters={"label": [f"{LABEL_INSTANCE}={instance}", f"{LABEL_ROLE}={Role.DEPLOY}"]},
        ),
        timeout,
    )
    if not finished or error is not None:
        if error is not None:
            logger.warning("service check: listing containers failed (%s)", error)
        return None
    found: dict[str, object] = {}
    for container in containers or []:  # type: ignore[union-attr]
        labels = getattr(container, "labels", None) or {}
        if labels.get(LABEL_INSTANCE) != instance or labels.get(LABEL_ROLE) != Role.DEPLOY:
            continue
        deployment_id = labels.get(LABEL_DEPLOYMENT_ID)
        if deployment_id:
            found[deployment_id] = container
    return found


def _timed_out(box: dict) -> Observation:
    # Stuck in the exec: the service did not answer in time. Stuck earlier: Docker did not answer,
    # which says nothing about the service.
    if box.get("stage") == "exec":
        return Observation(FailureKind.HEALTHZ_TIMEOUT)
    return Observation(inconclusive=True)


def _observe(
    deployment_id: str, container, baseline: int | None, exec_timeout: float, in_flight: dict
) -> Observation:
    previous = in_flight.get(deployment_id)
    if previous is not None:
        thread, old_box = previous
        if thread.is_alive():  # the last check of this service is still hung: do not stack another
            return _timed_out(old_box)
        in_flight.pop(deployment_id, None)  # it finished late; its answer is stale, ask afresh
    box: dict = {"stage": "start", "baseline": baseline}
    thread = threading.Thread(
        target=_inspect, args=(container, exec_timeout, box), name="service-check", daemon=True
    )
    thread.start()
    thread.join(exec_timeout)
    if thread.is_alive():
        in_flight[deployment_id] = (thread, box)
        return _timed_out(box)
    return box.get("observation") or Observation(inconclusive=True)


def _apply(
    db: Session, row: ServiceHealthCheck, obs: Observation, now: datetime, threshold: int
) -> str | None:
    """Applies one observation. Returns the audited transition (`AUDIT_UNHEALTHY` / `AUDIT_RECOVERED`)
    or None; the caller announces it after the commit."""
    row.last_checked_at = now
    if obs.restart_count is not None:
        row.last_restart_count = obs.restart_count  # one increase fails one check, not every later one
    if obs.inconclusive:
        row.last_result = CheckResult.UNKNOWN
        return None
    if obs.kind is None:
        recovered = row.state == ServiceState.UNHEALTHY
        row.last_result = CheckResult.PASS
        row.consecutive_failures = 0
        row.failing_since = None
        row.last_passed_at = now
        if recovered:
            row.state = ServiceState.HEALTHY
            row.state_changed_at = now
            write_audit_log(
                db,
                actor_id=SYSTEM_ACTOR_ID,
                action=AUDIT_RECOVERED,
                target=f"project:{row.project_id}",
                metadata={"deployment_id": row.deployment_id},
            )
            return AUDIT_RECOVERED
        return None
    if row.consecutive_failures == 0:
        row.failing_since = now
    row.consecutive_failures += 1
    row.last_result = CheckResult.FAIL
    row.last_failure_kind = obs.kind
    row.last_failure_at = now
    if row.consecutive_failures >= threshold and row.state != ServiceState.UNHEALTHY:
        row.state = ServiceState.UNHEALTHY
        row.state_changed_at = now
        write_audit_log(
            db,
            actor_id=SYSTEM_ACTOR_ID,
            action=AUDIT_UNHEALTHY,
            target=f"project:{row.project_id}",
            metadata={
                "deployment_id": row.deployment_id,
                "failure_kind": obs.kind,
                "consecutive_failures": row.consecutive_failures,
            },
        )
        return AUDIT_UNHEALTHY
    return None


def _row_for(db: Session, deployment_id: str, project_id: str, now: datetime) -> ServiceHealthCheck:
    row = db.get(ServiceHealthCheck, deployment_id)
    if row is None:
        row = ServiceHealthCheck(
            deployment_id=deployment_id,
            project_id=project_id,
            state=ServiceState.HEALTHY,
            last_result=CheckResult.UNKNOWN,
            consecutive_failures=0,
            last_checked_at=now,
            state_changed_at=now,
        )
        db.add(row)
    return row


def run_service_checks(
    db: Session,
    *,
    client=None,
    client_factory: Callable[[], object] | None = None,
    now: datetime | None = None,
    in_flight: dict | None = None,
    rotation: dict | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """One pass over every ACTIVE deployment; returns how many were checked. **Never raises**: the
    guarantee is here at the boundary, whatever the body does. `in_flight` is the caller's dict of
    still-hung checks across passes and `rotation` its dict holding where the next pass starts
    (None: fresh ones, a test's)."""
    try:
        return _run_service_checks(
            db,
            client=client,
            client_factory=client_factory,
            now=now,
            in_flight=in_flight,
            rotation=rotation,
            clock=clock,
        )
    except Exception:  # noqa: BLE001 - the contract: a failing pass is retried next cycle
        logger.exception("service check pass failed; it is retried next cycle")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.exception("service check: rollback after the failure also failed")
        return 0


def _touch_checker(db: Session, *, started: datetime | None = None, finished: datetime | None = None) -> None:
    row = db.get(ServiceCheckerStatus, SINGLETON_ID)
    if row is None:
        row = ServiceCheckerStatus(id=SINGLETON_ID)
        db.add(row)
    if started is not None:
        row.last_pass_started_at = started
    if finished is not None:
        row.last_pass_finished_at = finished
    db.commit()


def _run_service_checks(db, *, client, client_factory, now, in_flight, rotation, clock) -> int:
    moment = now or _utcnow()
    rotation = rotation if rotation is not None else {}
    _touch_checker(db, started=moment)
    checked = _check_all(
        db, client=client, client_factory=client_factory, moment=moment, in_flight=in_flight,
        rotation=rotation, clock=clock,
    )
    _touch_checker(db, finished=now or _utcnow())
    return checked


def _announce(db: Session, project_id: str, *, healthy: bool) -> None:
    """the transition the audit row records, told to the operators. After the commit, on its
    own session; `notifications.report` never raises, and the name lookup is guarded the same way,
    so nothing here can fail the pass."""
    try:
        if not notifications.any_channel_configured():
            return
        project = db.get(Project, project_id)
        notifications.report(
            notifications.session_factory_for(db),
            family=notifications.Family.SERVICE,
            subject=f"service:{project_id}",
            name=project.name if project is not None else "(unknown project)",
            healthy=healthy,
        )
    except Exception:  # noqa: BLE001
        logger.warning("service check: announcing a transition failed", exc_info=True)


def _check_all(db, *, client, client_factory, moment, in_flight, rotation, clock) -> int:
    in_flight = in_flight if in_flight is not None else {}
    threshold = settings.service_check_failure_threshold
    exec_timeout = settings.service_check_exec_timeout_seconds

    active = [
        (deployment_id, project_id)
        for deployment_id, project_id in db.execute(
            select(Deployment.id, Deployment.project_id)
            .where(Deployment.status == DeploymentStatus.ACTIVE)
            .order_by(Deployment.id)
        )
    ]
    active_ids = [deployment_id for deployment_id, _ in active]
    db.execute(delete(ServiceHealthCheck).where(ServiceHealthCheck.deployment_id.not_in(active_ids)))
    for gone in set(in_flight) - set(active_ids):
        in_flight.pop(gone, None)
    if not active:
        db.commit()
        return 0
    # Where this pass starts: after the last service the previous pass reached, so a pass cut short
    # by its bound is followed by one that begins with the services it never got to.
    start = rotation.get("start", 0) % len(active)
    active = active[start:] + active[:start]

    instance = platform_labels.instance_id()
    containers: dict[str, object] | None
    try:
        if client is None:
            client = (client_factory or _default_client)()
        containers = _find_containers(client, instance, exec_timeout)
    except Exception as exc:  # noqa: BLE001 - Docker unreachable: nobody can be judged
        logger.warning("service check: Docker is not reachable (%s)", exc)
        containers = None

    deadline = clock() + settings.service_check_cycle_timeout_seconds
    checked = 0
    for deployment_id, project_id in active:
        if clock() >= deadline:
            logger.warning("service check: the pass ran out of time; the rest wait for the next one")
            break
        row = _row_for(db, deployment_id, project_id, moment)
        if containers is None:
            obs = Observation(inconclusive=True)
        elif deployment_id not in containers:
            obs = Observation(FailureKind.CONTAINER_MISSING)
        else:
            obs = _observe(deployment_id, containers[deployment_id], row.last_restart_count, exec_timeout, in_flight)
        transition = _apply(db, row, obs, moment, threshold)
        checked += 1
        db.commit()  # one service at a time: a later hang or failure loses nothing already decided
        if transition is not None:
            _announce(db, project_id, healthy=transition == AUDIT_RECOVERED)
    rotation["start"] = start + checked
    db.commit()
    return checked


def _default_client():
    import docker

    return docker.from_env(timeout=max(1, int(settings.service_check_exec_timeout_seconds)))


class ServiceCheckRunner:
    """Runs `run_service_checks` every `service_check_interval_seconds` on one daemon thread of its
    own, with its own session per pass. The thread is the point: the worker's loop
    runs its sweeps in series, and a slow or failing one (a Postgres DSN timing out in the backup
    sweep took about a minute) would otherwise delay every check behind it.

    `lease` (optional) is the worker's lease; a pass is skipped while it is lost, since a worker
    that lost it is stopping and a stopping worker writes nothing. `run_cycle` is the unit a test
    calls directly; the loop only repeats it."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        client_factory: Callable[[], object] | None = None,
        lease=None,
    ) -> None:
        self._session_factory = session_factory
        self._client_factory = client_factory
        self._lease = lease
        self._client = None
        self._in_flight: dict = {}
        self._rotation: dict = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.cycles = 0

    def _get_client(self):
        if self._client is None:
            self._client = (self._client_factory or _default_client)()
        return self._client

    def run_cycle(self) -> int:
        """One pass. Never raises: everything, the lease read included, is inside the `try` (a
        lease read outside it ended the whole checker on its first cycle on a real install)."""
        db = None
        try:
            # `WorkerLease.lost` is a property, not a method.
            if self._lease is not None and self._lease.lost:
                return 0
            db = self._session_factory()
            return run_service_checks(
                db, client_factory=self._get_client, in_flight=self._in_flight, rotation=self._rotation
            )
        except Exception:  # noqa: BLE001
            logger.exception("service check cycle failed")
            return 0
        finally:
            self.cycles += 1
            if db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    logger.exception("service check: closing the session raised")

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.run_cycle()
                except Exception:  # noqa: BLE001 - one failed cycle is retried at the next interval
                    logger.exception("service check cycle raised; the checker carries on")
                if self._stop.wait(settings.service_check_interval_seconds):
                    break
        except BaseException:  # noqa: BLE001 - an ended checker is never silent
            logger.exception("service check thread ended; services are NOT being checked")
            raise

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="forge-service-check", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
