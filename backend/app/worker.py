"""Build worker entrypoint: `python -m app.worker`.

A dedicated process, separate from the API, that claims QUEUED builds from
the `builds` table and drives QUEUED -> RUNNING -> SUCCEEDED|FAILED via a
pluggable executor. The API only ever
creates builds; it never executes them.

Dev mode: exactly one worker process against the SQLite file
(single-writer assumption, WAL-enabled in app/db.py). Prod: Postgres +
`SELECT ... FOR UPDATE SKIP LOCKED` claim is the scale-out path (not
implemented in this slice — see services/build_queue.py docstring).

the single-worker assumption is also what makes the start-up rule
sound -- when this process starts, every claimed build and deployment still in flight in the
database belongs to a process that is gone, and is failed before anything is claimed
(`recover_orphaned_rows`). Then the label-driven reconciler (`services/reconciler.py`) runs
once, and again every `RECONCILE_INTERVAL_SECONDS`. A SIGTERM stops the claiming, fails the
work in flight through its normal teardown and exits (`services/worker_shutdown.py`).

"there is one worker" is enforced, not assumed. `main()` takes the
installation's worker lease (`services/worker_lease.py`) before anything above runs -- a second
worker against a live one exits 1 with a message naming the holder -- keeps it alive from a
thread of its own, re-checks it before each timeout sweep, reconcile pass and claim (a worker
that finds it lost stops and exits 1, never reaping), and releases it when it stops.
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, engine
from app.models.build import Build
from app.schema_check import assert_schema_at_head
from app.services import project_lifecycle
from app.services.audit import write_audit_log
from app.services.build_executor import BuildExecutor, PlaceholderBuildExecutor
from app.models.deployment import Deployment, DeploymentFailureReason, DeploymentStatus
from app.services.build_queue import (
    WORKER_STOPPED_BUILD_DETAIL,
    claim_next_build,
    clear_preview_fields,
    fail_orphaned_builds,
    find_expired_preview_build_ids,
    mark_failed,
    mark_succeeded,
    sweep_timed_out_builds,
)
from app.services.engines import Unavailable, resolve_engine, sandbox_executor_active
from app.services.eval_runner import advance_runs
from app.services.deploy import (
    CAUSE_WORKER_STOPPED,
    claim_next_deployment,
    execute_deployment,
    fail_deployment,
    fail_orphaned_deployments,
    sweep_stale_deployments,
)
from app.services.instance_id import bind_instance_id
from app.services.image_retention import sweep_build_images
from app.services.reconciler import Reconciler
from app.services.status import BuildFailureReason, BuildStatus
from app.services.traefik_traffic import POLL_INTERVAL_SECONDS, TrafficSignalPoller, signal_is_configured
from app.services.host_disk import run_disk_check_if_due
from app.services.service_health import ServiceCheckRunner
from app.services.worker_lease import LeaseHeld, LeaseLost, WorkerLease
from app.services.worker_shutdown import SHUTDOWN, WorkerShutdown

logger = logging.getLogger("app.worker")

# worker-driven backup audit entries have no human actor, same idiom
# as build_queue.SYSTEM_ACTOR_ID / deploy.SYSTEM_ACTOR_ID -- redeclared here
# rather than imported, matching deploy.py's own precedent (see that
# module's docstring at its own declaration).
SYSTEM_ACTOR_ID: str | None = None


def run_one_build(db: Session, build: Build, executor: BuildExecutor) -> Build:
    """Execute a single already-claimed (RUNNING) build to completion and
    persist the terminal state. Any exception from the executor is treated
    as an infra-level failure — a build is never left RUNNING because the
    executor raised (never silent).

    every FAILED path persists a platform-authored `failure_detail`
. An executor exception's text is
    only logged -- the detail names its class. An `ok=False` result's
    `summary` is the detail (the executor writes it as platform text); with no
    summary the detail is a template naming the code, never empty."""
    try:
        # a stop request (SIGTERM) may interrupt the executor, and only here and in the
        # poll sleep (`services/worker_shutdown.py`). Entering raises if one is already pending,
        # so a build that was claimed after the request is failed, not started.
        with SHUTDOWN.interruptible():
            result = executor.execute(build)
    except Exception as exc:  # noqa: BLE001 - executor crash must still resolve the build
        logger.exception("executor raised while running build %s", build.id)
        return mark_failed(
            db,
            build,
            reason=BuildFailureReason.INFRA_ERROR,
            detail=f"the build executor raised {type(exc).__name__}; see the worker log",
        )
    except BaseException:  # noqa: BLE001 - re-raised below; the row must be resolved for ANY exit
        # ADR 0037 §Decision 6: `WorkerShutdown` (a SIGTERM) or a `KeyboardInterrupt`.
        # Neither is an `Exception`, so the handler above never saw them. The executor's own
        # `finally` has already torn down the sandbox, proxy and network by the time the
        # exception reaches here; what is left is the row, which would otherwise stay RUNNING.
        _fail_interrupted_build(db, build)
        raise

    if result.ok:
        return mark_succeeded(db, build, agent_summary=result.agent_summary)
    reason = result.reason or BuildFailureReason.AGENT_ERROR
    detail = (result.summary or "").strip() or (
        f"the build failed ({reason}); the executor gave no further detail"
    )
    return mark_failed(db, build, reason=reason, detail=detail)


def _fail_interrupted_build(db: Session, build: Build) -> None:
    """Fails the build an interrupt cut short, with the platform-authored "worker stopped" detail
. Never raises: it runs while the interrupt is propagating and must not replace it;
    if the database cannot be reached the row stays RUNNING and the next start fails it
    (`recover_orphaned_rows`)."""
    try:
        db.rollback()
        db.refresh(build)
        if build.status == BuildStatus.RUNNING:
            mark_failed(
                db, build, reason=BuildFailureReason.INFRA_ERROR, detail=WORKER_STOPPED_BUILD_DETAIL
            )
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("could not fail build %s after the worker was interrupted", build.id)


def assign_engine(db: Session, build: Build) -> bool:
    """ADR 0035 §Decision 5: decide the build's engine NOW, at claim, and record
    it (and the configured model) on the build as measured facts. Called for every claimed
    build whose executor runs an engine, never once at startup: a change of selection
    applies to the next claim, and a running build is never switched.

    Returns False when the build was failed instead (the selected engine is not
    available): no sandbox, proxy or token ever exists for it, the detail is the
    platform-authored one from `engines.resolve_engine` and names the engine, and the
    other engine is never tried (it would change the vendor and the cost silently)."""
    choice = resolve_engine(db, settings)
    if isinstance(choice, Unavailable):
        build.engine = choice.engine
        build.model = None
        logger.warning("build %s: %s", build.id, choice.detail)
        mark_failed(db, build, reason=BuildFailureReason.ENGINE_UNREACHABLE, detail=choice.detail)
        return False
    build.engine = choice.engine
    build.model = choice.model
    db.commit()
    db.refresh(build)
    return True


def sweep_expired_previews(db: Session, *, preview_manager=None) -> int:
    """"Teardown on: TTL
    expiry". Runs every worker loop iteration alongside the build-timeout
    sweep. Lazily constructs a real `PreviewManager` (Docker client) unless
    one is injected for tests. Best-effort: a Docker-level teardown failure
    for one build never blocks clearing the others, and is logged rather
    than raised (mirrors PreviewManager's own best-effort teardown)."""
    expired_ids = find_expired_preview_build_ids(db)
    if not expired_ids:
        return 0

    if preview_manager is None:
        from app.services.preview_manager import PreviewManager

        preview_manager = PreviewManager()

    torn_down = 0
    for build_id in expired_ids:
        try:
            preview_manager.stop_preview(build_id)
        except Exception:  # noqa: BLE001 - teardown must never crash the sweep
            logger.exception("preview sweep: failed to stop preview for build %s", build_id)
        clear_preview_fields(db, build_id)
        torn_down += 1
    return torn_down


def sweep_service_data_backups(
    db: Session,
    *,
    docker_client=None,
    now: datetime | None = None,
    scan_state: dict | None = None,
) -> int:
    """The service-data backup sweep (`_sweep_service_data_backups` has the full contract). **It never
    raises** -- the guarantee is made HERE, at the boundary, not statement by statement inside
: the body used to build its Docker client and create the backup directory outside any
    `try`, which contradicted its own docstring, and a statement-level fix would have left the next
    unguarded line to do the same. Anything the body does not record itself is logged, the session
    is rolled back, and the tick counts as 0 backups."""
    try:
        return _sweep_service_data_backups(db, docker_client=docker_client, now=now, scan_state=scan_state)
    except Exception:  # noqa: BLE001 - the contract: this sweep never raises
        logger.exception("service-data backup sweep: unexpected failure; the next scan looks again")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 - a session that cannot roll back is the caller's to recycle
            logger.exception("service-data backup sweep: rollback after the failure also failed")
        return 0


def _sweep_service_data_backups(
    db: Session,
    *,
    docker_client=None,
    now: datetime | None = None,
    scan_state: dict | None = None,
) -> int:
    """ADR 0023 §Backup / §Amendment 4 / §Amendment 9: scheduled
    Postgres backups of every `svc_`-prefixed database, with retention.
    Runs every worker loop iteration alongside the other sweeps -- and does
    nothing on most of them (see `scan_state`).

    A no-op (not an error, and no directory is created) when backup is
    disabled (`service_data_backup_enabled=False`) or no admin DSN is
    configured (`settings.service_postgres_configured`) -- a platform that
    has never enabled data-service provisioning must not log an error
    every poll interval.

    Lazily constructs a real Docker client (unless one is injected for
    tests) ONLY once at least one database is actually due for a dump --
    mirroring `sweep_expired_previews`'s own lazy `PreviewManager`
    construction: a platform with no provisioned service, or whose dumps
    are all still fresh, never touches Docker at all.

    **the sweep does not run on every tick, and a
    failing dump is not retried on every one.** Before, each 2-second tick opened an admin engine
    to enumerate the databases, and a dump that kept failing was attempted -- and audit-logged,
    about 43,000 rows a day per database -- on every tick, because a failure leaves no file and so
    the database stays "due". Two gates, both held in `scan_state`, the dict the CALLER owns
    across ticks (`main` passes the same one each iteration; the same shape as the idle-project
    and orphan sweeps'):

    - `next_due_at`: the whole sweep -- enumeration, the due check, the dumps and retention --
      runs once per `service_data_backup.scan_interval()` (15 minutes, shorter than the cadence on
      purpose, see `SCAN_INTERVAL`). It is advanced BEFORE anything is attempted, so a Postgres
      that cannot be reached is looked for again in one interval, not on the next tick.
    - `failures[database] = {"count", "retry_at"}`: after a failed dump a database is skipped
      until `service_data_backup.retry_delay(count)` has passed -- 15 min, 30 min, 1 h ... capped
      at one cadence. Each attempt is a new window, and each failed attempt writes ONE
      `service_data_backup.failed` row, carrying the consecutive-failure count and when the next
      attempt is allowed. A success clears the entry; a database that no longer exists is dropped
      from it.

    In-process state, so a worker restart looks (and retries) immediately: early is the only
    direction it can err in, the same reasoning as `sweep_idle_projects`'s. `scan_state=None` (the
    default, and what a test that exercises the dumping itself wants) is "always due, never
    backing off".

    Best-effort per database: one project's dump failure is logged and
    audit-logged as a failure, never raised, and never blocks another
    project's dump or the retention sweep that runs after. This function
    itself never raises -- unlike `sweep_timed_out_builds` and
    `sweep_stale_deployments`, which are unwrapped in `run_iteration` and
    would kill the worker process if they did (see that function's own
    docstring)."""
    if not settings.service_data_backup_enabled:
        return 0
    if not settings.service_postgres_configured:
        return 0

    from app.services import service_data_backup

    moment = now or datetime.now(timezone.utc)
    if scan_state is not None:
        next_due_at = scan_state.get("next_due_at")
        if next_due_at is not None and moment < next_due_at:
            return 0
        scan_state["next_due_at"] = moment + service_data_backup.scan_interval()
    failures: dict = scan_state.setdefault("failures", {}) if scan_state is not None else {}

    base_dir = settings.service_data_backup_dir

    try:
        databases = service_data_backup.list_service_databases()
    except Exception:  # noqa: BLE001 - enumeration failure is best-effort, never raised
        logger.exception("service-data backup sweep: failed to enumerate service databases")
        return 0

    for gone in set(failures) - set(databases):
        del failures[gone]  # a database that no longer exists has nothing left to back off from

    due = [
        name
        for name in databases
        if service_data_backup.is_due(
            service_data_backup.project_backup_dir(base_dir, name),
            interval_hours=settings.service_data_backup_interval_hours,
            now=moment,
        )
        and not _backing_off(failures.get(name), moment)
    ]

    def _record_failure(database_name: str) -> None:
        count = failures.get(database_name, {}).get("count", 0) + 1
        retry_at = moment + service_data_backup.retry_delay(count)
        failures[database_name] = {"count": count, "retry_at": retry_at}
        write_audit_log(
            db,
            actor_id=SYSTEM_ACTOR_ID,
            action="service_data_backup.failed",
            target=f"database:{database_name}",
            metadata={"consecutive_failures": count, "next_attempt_not_before": retry_at.isoformat()},
        )
        db.commit()

    backed_up = 0
    if due:
        # preparing the sweep (the backup directory, the Docker client) is part of every due
        # database's attempt. A failure here is recorded the way a failed dump is -- once per due
        # database, with the same backoff and the same `service_data_backup.failed` row -- and not as
        # one sweep-level row, so that (a) an operator reads one failure shape whatever broke, (b) the
        # backoff already in `failures` also quiets this case, and (c) retention below still runs.
        try:
            service_data_backup.ensure_backup_dir(base_dir)
            if docker_client is None:
                import docker

                docker_client = docker.from_env()
        except Exception:  # noqa: BLE001 - recorded per due database below, never raised
            logger.exception("service-data backup sweep: could not prepare (backup directory or Docker client)")
            for database_name in due:
                _record_failure(database_name)
            due = []

        for database_name in due:
            project_dir = service_data_backup.project_backup_dir(base_dir, database_name)
            try:
                os.makedirs(project_dir, exist_ok=True)
                dest_path = os.path.join(project_dir, service_data_backup.dump_filename(moment))
                service_data_backup.dump_database(
                    database_name, docker_client=docker_client, dest_path=dest_path
                )
            except Exception:  # noqa: BLE001 - one project's failed dump never blocks another's
                logger.exception("service-data backup sweep: dump failed for %s", database_name)
                _record_failure(database_name)
                continue
            failures.pop(database_name, None)
            write_audit_log(
                db,
                actor_id=SYSTEM_ACTOR_ID,
                action="service_data_backup.created",
                target=f"database:{database_name}",
            )
            db.commit()
            backed_up += 1

    try:
        service_data_backup.sweep_backup_retention(
            base_dir, retention_days=settings.service_data_backup_retention_days, now=moment
        )
    except Exception:  # noqa: BLE001 - retention failure never blocks the next tick
        logger.exception("service-data backup sweep: retention sweep failed")

    return backed_up


def _backing_off(failure: dict | None, moment: datetime) -> bool:
    """True while a database that failed its last dump is inside its backoff window."""
    return failure is not None and moment < failure["retry_at"]


# ADR 0023 §Amendment 2:
# a build in either of these two statuses is
# TERMINAL -- the only ones `sweep_orphaned_preview_stores` below ever
# reclaims a store for, and only when `preview_expires_at IS NULL` too
# (decision 1). QUEUED/RUNNING/NOT_IMPLEMENTED are all non-terminal and are
# never touched, whatever `pv_` stores exist for them (b1).
_TERMINAL_BUILD_STATUSES = {BuildStatus.SUCCEEDED, BuildStatus.FAILED}


def _preview_store_is_orphaned(status_pair: tuple[str, object] | None) -> bool:
    """Decision 1's rule, applied uniformly to both halves (Postgres and
    Docker) and to both ways a store can be found: `status_pair` is
    `(build.status, build.preview_expires_at)` for a build id/identifier
    that DOES map to a real row, or `None` when it does not (decision 1's
    second clause -- "no build row exists for it at all"). Never reverses
    a `pv_` name or a container's own build id back into anything; the
    caller always supplies this from a build id it already has (decision
    2)."""
    if status_pair is None:
        return True
    status, preview_expires_at = status_pair
    return status in _TERMINAL_BUILD_STATUSES and preview_expires_at is None


def sweep_orphaned_preview_stores(
    db: Session,
    *,
    now: datetime | None = None,
    docker_client=None,
    scan_state: dict | None = None,
) -> int:
    """ADR 0023
    §Amendment 2 ("a leaked preview database has the same shape as a
    leaked preview container and the same owner"): reclaims `pv_` Postgres
    databases/roles and `forge.data_service=preview-redis` containers that
    no longer belong to any preview -- the gap that was accepted deliberately
    rather than solved, closed here.

    **Scope (ADR 0037 rule 2): this installation's stores only.** Everything below is
    enumerated by this installation's instance (a Redis container by its `forge.instance` label,
    a database/role by the instance marker in its Postgres comment) and re-checked per object
    before removal inside `preview_data`; a store of another installation, of a test run, or one
    with no marker is never examined, however orphaned it looks from this database.

    **The orphan rule (decision 1), and nothing more elaborate:** a `pv_`
    store is an orphan when its build is TERMINAL (SUCCEEDED/FAILED) with
    `preview_expires_at IS NULL`, or when no build row exists for it at
    all. A build in any non-terminal status is never touched (b1) --
    `_preview_store_is_orphaned` is the one place this rule is evaluated,
    for every store, both halves.

    **(T1):** the crash that leaks a preview's stores also leaves its
    build stuck RUNNING (never converged by this sweep, which would
    otherwise destroy a preview mid-creation) -- `sweep_timed_out_builds`,
    called earlier in `run_iteration`, is what already converges a
    crashed build to FAILED/TIMEOUT and makes the orphan detectable at
    all. No second age threshold, `heartbeat_at` check, or multi-pass
    tracker is added here (decision/b3) -- composing with the existing
    timeout sweep is the whole mechanism.

    **Reconciled forward, never by reversing a name back to a build id**
    (decision 2): every build ROW's own id is the input; what it computes
    to (`preview_data._preview_identifier`) is only ever tested for
    membership in what Postgres enumeration returned, never parsed back
    out of a `pv_` name. The Postgres half (databases, roles -- each
    enumerated and reconciled independently of the other, decision 3) and
    the Docker half (redis containers, reconciled by their own `forge.
    build_id` LABEL, decision 4 -- never the container's name) are
    likewise reconciled independently of each other, because they orphan
    independently: a crash between provisioning Postgres and creating
    Redis leaves only the Postgres half; a container manually recreated
    without its database would leave only the Docker half.

    A no-op (0, no query, no Docker client, no audit entries) when there
    is no admin DSN configured (`settings.service_postgres_configured`) --
    nothing could have been provisioned without one either. `docker_client`
    is constructed lazily (mirrors `sweep_expired_previews`'s own lazy
    `PreviewManager`) -- ONLY once there is at least one Postgres-side
    orphan already found in this tick, or at least one build row this
    sweep would reclaim a store for, so an otherwise-quiet dev run with no
    orphan candidates never touches Docker at all (a2, pinned with
    `_ExplodingDockerClient`). The Postgres half itself always runs when
    configured -- enumerating `pv_` names and reconciling them needs no
    Docker client at any point. (Disclosed, not hidden: a preview-redis
    container whose build id matches no row at all AND whose Postgres pair
    was never created either -- reachable only by bypassing this module's
    own combined provisioning function -- would not be examined until some
    other real orphan opens the gate. Nothing in this codebase provisions
    the two halves other than together, so this is a theoretical seam, not
    an observed leak path.)

    **Never raises** (a2/a3): enumeration failure (Postgres or Docker) and
    each individual orphan's drop failure are all logged and the sweep
    continues; `run_iteration` wraps this in a second try/except only as a
    belt-and-braces layer (e1), not as the guarantee. Reuses `preview_
    data`'s existing `_require_preview_identifier` guard transitively,
    through `drop_preview_postgres_by_identifier` (a4) -- a non-`pv_`
    identifier is refused before any admin connection is opened, though
    nothing enumerated here could ever carry one.

    Audit-logged per reclaimed orphan (a5), one entry per Postgres
    identifier and one per Redis container, `SYSTEM_ACTOR_ID` (no
    fabricated user row) -- `<noun>.<past-tense verb>` naming, matching
    `service_data_backup.created`'s own shape.

    `scan_state`: same in-process due-ness dict shape as `sweep_idle_
    projects`'s own -- an early
    re-sweep after a worker restart is free and idempotent (a dropped
    `pv_` store simply stops being enumerated; nothing double-reclaims), so
    a durable due-ness record would be ceremony."""
    if not settings.service_postgres_configured:
        return 0

    moment = now or datetime.now(timezone.utc)
    if scan_state is not None:
        next_due_at = scan_state.get("next_due_at")
        if next_due_at is not None and moment < next_due_at:
            return 0
        scan_state["next_due_at"] = moment + timedelta(
            hours=settings.preview_orphan_sweep_interval_hours
        )

    from app.services import preview_data

    # Forward-computed from every build ROW (decision 2) -- never a `pv_`
    # name or a container's build id reversed into one. Keyed both by the
    # build's own id (what a Redis container's label carries directly) and
    # by what that id computes to as a Postgres identifier (what
    # enumeration returns) -- the SAME underlying rows, two lookup shapes.
    rows = db.execute(select(Build.id, Build.status, Build.preview_expires_at)).all()
    by_build_id = {row.id: (row.status, row.preview_expires_at) for row in rows}
    by_identifier = {
        preview_data._preview_identifier(build_id): status_pair
        for build_id, status_pair in by_build_id.items()
    }

    reclaimed = 0

    # -- Postgres half: independent of Docker, runs whenever configured --
    try:
        pg_names = set(preview_data.list_preview_databases()) | set(
            preview_data.list_preview_roles()
        )
    except Exception:  # noqa: BLE001 - enumeration failure must never crash the sweep
        logger.exception("preview orphan sweep: failed to enumerate pv_ databases/roles")
        pg_names = set()

    pg_orphan_names = sorted(
        name for name in pg_names if _preview_store_is_orphaned(by_identifier.get(name))
    )
    for name in pg_orphan_names:
        try:
            preview_data.drop_preview_postgres_by_identifier(name)
        except Exception:  # noqa: BLE001 - one orphan's failure must never block another's
            logger.exception("preview orphan sweep: failed to drop postgres store %s", name)
            continue
        write_audit_log(
            db,
            actor_id=SYSTEM_ACTOR_ID,
            action="preview_orphan.reclaimed",
            target=f"preview_postgres:{name}",
        )
        db.commit()
        reclaimed += 1

    # -- Docker half: lazily constructed, only once something is due --
    # "Due" is either half's own signal: a Postgres-side orphan already
    # found in THIS tick (covers a redis container whose build id matches
    # no row, when its Postgres pair -- provisioned together, always --
    # was found above), or a build row this sweep would reclaim a store
    # for regardless of what Postgres enumeration returned.
    candidate_build_ids = [
        build_id
        for build_id, status_pair in by_build_id.items()
        if _preview_store_is_orphaned(status_pair)
    ]
    if candidate_build_ids or pg_orphan_names:
        if docker_client is None:
            import docker

            docker_client = docker.from_env()
        try:
            containers = preview_data.list_preview_redis_containers(docker_client=docker_client)
        except Exception:  # noqa: BLE001 - enumeration failure must never crash the sweep
            logger.exception("preview orphan sweep: failed to enumerate preview-redis containers")
            containers = []
        for entry in containers:
            build_id = entry.get("build_id")
            if build_id is None:
                # Unreconcilable without the label (decision 4) -- never
                # guess ownership from the container's name.
                continue
            if not _preview_store_is_orphaned(by_build_id.get(build_id)):
                continue
            try:
                preview_data.drop_preview_redis_by_build_id(build_id, docker_client=docker_client)
            except Exception:  # noqa: BLE001 - one orphan's failure must never block another's
                logger.exception(
                    "preview orphan sweep: failed to drop redis container for build %s", build_id
                )
                continue
            write_audit_log(
                db,
                actor_id=SYSTEM_ACTOR_ID,
                action="preview_orphan.reclaimed",
                target=f"preview_redis:{entry['name']}",
            )
            db.commit()
            reclaimed += 1

    return reclaimed


def sweep_idle_projects(
    db: Session,
    *,
    now: datetime | None = None,
    deploy_manager=None,
    scan_state: dict | None = None,
    lease: WorkerLease | None = None,
) -> int:
    """
    The automatic flag -> stop -> archive sweep over
    idle projects. Runs every worker loop iteration alongside the other
    sweeps (orchestration/schedule/error-swallowing here; mechanics in
    `services/project_lifecycle.py`, mirroring the backup sweep's own split).

    A no-op (0, no query at all) when the sweep is disabled
    (`project_lifecycle_zombie_scan_enabled=False`) -- a platform that has
    never turned this on must not touch every project's row on every tick.

    while Traefik routes, a project with an ACTIVE deployment is
    among the candidates only when the traffic signal is trusted (a poll within 3 minutes that has
    watched the whole idle window) -- chosen in `project_lifecycle.idle_candidates`, not here.

    with a `lease`, it is confirmed still ours before each candidate is
    processed, i.e. before any deployment is stopped; `LeaseLost` (a `SystemExit`, never swallowed
    here) ends the sweep with nothing further stopped. No `lease` (unit tests) checks nothing.

    **Never raises** (a1): every per-candidate failure is caught, logged,
    rolled back and the sweep moves to the next candidate — `run_iteration`
    wraps this in a second try/except only as a belt-and-braces layer, not
    as the guarantee. **Commits per project** (a1): each candidate's own
    `db.commit()`/`db.rollback()` below means one project's failure can
    never roll back another's already-completed archival.

    `scan_state`: unlike the backup sweep's `is_due` (which reads
    a dump's mtime on disk, because an early re-dump is a real `pg_dump`),
    an early re-scan here is free and idempotent: an already-`ARCHIVED`
    project is excluded by `project_lifecycle.idle_candidates`'s own query,
    so nothing double-archives. A plain IN-PROCESS deadline is therefore
    correct — this exists only to stop a repeatedly-failing stop (Docker
    unreachable, a container already gone) from being retried on every
    2-second worker tick. `scan_state` is an ordinary mutable dict the
    CALLER owns across ticks (`main`'s loop passes the same dict every
    iteration) — never module-level state, so a fresh `{}` (or the default
    `None`) per test run is exactly "always due", which is what exercising
    the flag/stop/archive logic itself needs. A worker restart loses this
    dict and re-scans immediately: acceptable, because early is the only
    direction this can err in.
    """
    if not settings.project_lifecycle_zombie_scan_enabled:
        return 0

    moment = now or datetime.now(timezone.utc)

    if scan_state is not None:
        next_due_at = scan_state.get("next_due_at")
        if next_due_at is not None and moment < next_due_at:
            return 0
        scan_state["next_due_at"] = moment + timedelta(
            hours=settings.project_lifecycle_zombie_scan_interval_hours
        )

    threshold = timedelta(days=settings.project_lifecycle_idle_threshold_days)

    # See project_lifecycle.ACTIVITY_SIGNAL_EPOCH's own
    # docstring: refuse to judge a window that predates the day the signal
    # became real.
    if moment - threshold < project_lifecycle.ACTIVITY_SIGNAL_EPOCH:
        return 0

    cutoff = moment - threshold
    candidates = project_lifecycle.idle_candidates(db, cutoff=cutoff, now=moment)

    archived = 0
    for project in candidates:
        if lease is not None:
            lease.require()
        try:
            project_lifecycle.process_idle_candidate(db, project, deploy_manager=deploy_manager)
        except Exception:  # noqa: BLE001 - one project's failure must never block another's (a1/a4)
            logger.exception("idle-project sweep: failed to process project %s", project.id)
            db.rollback()
            continue
        db.commit()
        archived += 1
    return archived


def _log_backup_startup_banner() -> None:
    """(d4) Split out from `main()` so it is directly testable with
    `caplog` -- `main()` itself never returns."""
    logger.info(
        "service-data backup: enabled=%s interval_hours=%s dir=%s",
        settings.service_data_backup_enabled,
        settings.service_data_backup_interval_hours,
        settings.service_data_backup_dir,
    )


def _log_disk_cap_startup_banner(executor) -> None:
    """say at startup whether
    the build sandbox's disk cap can be enforced on THIS host, so an operator learns of
    a gap here and not from a build. Only an executor that runs a sandbox has a cap to
    verify (`disk_cap_gap`); the placeholder has none. Split out of `main()` so it is
    directly testable with `caplog`. Never raises -- startup is not blocked by a probe.

    the same statement for PREVIEWS, a second line from a probe of its own
    (`preview_disk_cap_gap`): a preview runs on the default runtime, never the build's, so the
    build's answer is not theirs, and a gap means something different for them (no harvest bound
    behind the cap, only the TTL). Each line names its subject, so what holds for the build
    sandbox and what holds for previews are never one sentence that could be true of only one."""
    probe = getattr(executor, "disk_cap_gap", None)
    if probe is None:
        return
    from app.services.sandbox_runner import SIZE_READ_TIMEOUT_SECONDS

    # every size read is bounded (an expired read counts as an unreadable poll), and once
    # the command ends every process in the sandbox is stopped before the last check and the harvest.
    _log_disk_cap_statement(
        "sandbox disk cap",
        probe,
        enforced=(
            "a watchdog on the writable layer (limit %s MiB, polled every %ss, each size read bounded "
            "at %ss, so five unreadable polls fail the build within %ss), then every process in the "
            "sandbox stopped before a last check and a harvest bounded at the same size; verified "
            "against this host's container engine"
        )
        % (
            settings.sandbox_disk_limit_mb,
            settings.sandbox_disk_poll_interval_seconds,
            SIZE_READ_TIMEOUT_SECONDS,
            5 * (SIZE_READ_TIMEOUT_SECONDS + settings.sandbox_disk_poll_interval_seconds),
        ),
    )
    preview_probe = getattr(executor, "preview_disk_cap_gap", None)
    if preview_probe is None:
        return
    _log_disk_cap_statement(
        "preview disk cap",
        preview_probe,
        enforced=(
            "one watcher thread for every live preview, so it keeps watching while a build runs "
            "(the build sandbox's limit, %s MiB, polled every %ss, each size read bounded at %ss, so "
            "five unreadable polls tear a preview down within %ss); a preview over the limit, or one "
            "the engine stops reporting a size for, is torn down; no harvest bound stands behind it, "
            "and what counts is what the engine reports (overlay2 counts a sparse file at its "
            "apparent size, Docker Desktop's snapshotter at what it occupies); verified against "
            "this host's container engine on the default runtime previews run on"
        )
        % (
            settings.sandbox_disk_limit_mb,
            settings.sandbox_disk_poll_interval_seconds,
            SIZE_READ_TIMEOUT_SECONDS,
            5 * (SIZE_READ_TIMEOUT_SECONDS + settings.sandbox_disk_poll_interval_seconds),
        ),
    )


def _log_proxy_image_startup_banner(executor) -> None:
    """Say at startup whether the LLM proxy image advertises what this
    worker needs of it (`drain`), so a worker started on newer code than its image (the image
    is built by hand) is told here, naming the rebuild command, and not by a build whose cost could
    not be recorded after a full paid agent run. The same finding refuses every build before its agent
    runs (`SandboxBuildExecutor.execute`). Only an executor that runs a proxy has an image to check
    (`proxy_image_gap`); the placeholder has none. Split out of `main()` so it is directly testable
    with `caplog`. Never raises -- startup is not blocked by a probe."""
    probe = getattr(executor, "proxy_image_gap", None)
    if probe is None:
        return
    image = getattr(executor, "proxy_image", "the LLM proxy image")
    try:
        gap = probe()
    except Exception:  # noqa: BLE001 - a probe that blew up is a gap too, not a reason to stop the worker
        logger.exception("LLM proxy image: the startup check raised")
        gap = "the LLM proxy image's capabilities could not be verified (the check raised)"
    if gap is None:
        logger.info(
            "LLM proxy image: %s advertises %s -- a build's token can be drained before its usage is read",
            image,
            ", ".join(getattr(executor, "proxy_required_capabilities", ())),
        )
    else:
        logger.warning("LLM proxy image: %s", gap)


def _log_oom_visibility_startup_banner(executor, client=None) -> None:
    """one WARNING at start when the cgroup v2 file the post-run OOM read uses is not
    visible for this engine (Docker Desktop, cgroup v1, a worker off the engine host). `State.OOMKilled`
    cannot see an OOM-killed exec'd child, so there an OOM-killed agent command is
    classified AGENT_ERROR, not RESOURCE_LIMIT; this makes that weaker answer visible. Only an
    executor that runs sandboxes (`starts_previews`) has the read; the placeholder runs nothing and
    does not contact Docker. Never raises -- startup is not blocked by a probe."""
    if not getattr(executor, "starts_previews", False):
        return
    from app.services.sandbox_runner import oom_read_gap

    try:
        if client is None:
            import docker

            client = docker.from_env()
        gap = oom_read_gap(client.info())
    except Exception as exc:  # noqa: BLE001 - an engine that cannot be asked is the same finding
        gap = f"the engine could not be asked about its cgroups ({type(exc).__name__})"
    if gap is not None:
        logger.warning(
            "OOM visibility: %s, so an agent command killed for running out of memory is reported "
            "as an agent error (AGENT_ERROR), not as a resource limit (RESOURCE_LIMIT)",
            gap,
        )


def _log_disk_cap_statement(label: str, probe, *, enforced: str) -> None:
    """One startup statement: INFO `<label>: enforced -- <enforced>` when the probe finds no gap,
    else a WARNING with the gap. A probe that raises is a gap too, not a reason to stop."""
    try:
        gap = probe()
    except Exception:  # noqa: BLE001 - a probe that blew up is a gap too, not a reason to stop the worker
        logger.exception("%s: the startup check raised", label)
        gap = "the disk cap could not be verified (the check raised)"
    if gap is None:
        logger.info("%s: enforced -- %s", label, enforced)
    else:
        logger.warning("%s: %s", label, gap)


def _start_preview_disk_watch(executor):
    """the one watcher for every preview, on its own thread (why a thread and not a
    sweep: `services/preview_disk_watch.py`'s docstring). Started only for an executor that starts
    previews (`starts_previews`), so a placeholder dev run touches no Docker. Returns the runner,
    or None (also when it could not be built, which is logged at ERROR: startup is not blocked,
    and the gap is not silent). The session factory reads `SessionLocal` at call time, through this
    module, so a test that redirects it redirects the watcher's records too."""
    if not getattr(executor, "starts_previews", False):
        return None
    from app.services.preview_disk_watch import PreviewDiskWatcher, PreviewDiskWatchRunner

    try:
        runner = PreviewDiskWatchRunner(PreviewDiskWatcher(), lambda: SessionLocal())
        runner.start()
    except Exception:  # noqa: BLE001 - loud, not fatal: the same posture as a disk probe that cannot run
        logger.exception("preview disk watch: could not start; previews are NOT being watched")
        return None
    return runner


def _start_service_checks(lease: WorkerLease | None = None) -> ServiceCheckRunner | None:
    """the active service checks, on a thread of their own. NOT a sweep in
    `run_iteration`: the loop runs its sweeps in series, and a slow or failing one (the backup
    sweep's Postgres DSN timing out held the first disk check for about a minute) must not delay
    them. The session factory reads `SessionLocal` at call time through this module, so a test that
    redirects it redirects the checker. With no ACTIVE deployment a pass touches no Docker, so a
    placeholder dev run needs no Docker Engine. Returns the runner, or None when it could not start
    (logged at ERROR; the worker still runs)."""
    try:
        runner = ServiceCheckRunner(lambda: SessionLocal(), lease=lease)
        runner.start()
    except Exception:  # noqa: BLE001 - loud, not fatal: the same posture as the preview disk watch
        logger.exception("service checks: could not start; services are NOT being checked")
        return None
    return runner


def _new_deploy_manager():
    """A real `DeployManager` (Docker client), built only when something needs
    one -- never at worker start, so a Docker-less dev run still starts."""
    from app.services.deploy_manager import DeployManager

    return DeployManager()


def run_one_deployment(db: Session, deployment: Deployment, *, deploy_manager=None, git_provider=None) -> Deployment:
    """execute one claimed deployment to a terminal
    state in the worker process (never in
    the API request). Lazily constructs a real `DeployManager` (Docker
    client) unless one is injected for tests; a Docker-less worker resolves
    the row to FAILED/INFRA_ERROR explicitly rather than leaving it stuck
    (never silent)."""
    if deploy_manager is None:
        try:
            from app.services.deploy_manager import DeployManager

            deploy_manager = DeployManager()
        except Exception:  # noqa: BLE001 - Docker unreachable must still resolve the deployment
            logger.exception("deploy manager unavailable for deployment %s", deployment.id)
            return fail_deployment(db, deployment, DeploymentFailureReason.INFRA_ERROR)

    try:
        # interruptible like a build (see `run_one_build`). `execute_deployment` resolves
        # the row itself for anything raised inside it, so this handler is for the one case it
        # never saw: entering raised because a stop was requested after the claim.
        with SHUTDOWN.interruptible():
            return execute_deployment(db, deployment, manager=deploy_manager, git_provider=git_provider)
    except BaseException:  # noqa: BLE001 - re-raised below
        try:
            db.rollback()
            db.refresh(deployment)
            if deployment.status == DeploymentStatus.DEPLOYING:
                fail_deployment(
                    db, deployment, DeploymentFailureReason.INFRA_ERROR, cause=CAUSE_WORKER_STOPPED
                )
        except Exception:  # noqa: BLE001 - must not replace the interrupt; the next start fails the row
            logger.exception("could not fail deployment %s after the worker was interrupted", deployment.id)
        raise


def run_traffic_poll_if_due(
    db: Session,
    poller: TrafficSignalPoller | None,
    state: dict | None,
    *,
    now: datetime | None = None,
    lease: WorkerLease | None = None,
) -> bool:
    """reads Traefik's request counters every `POLL_INTERVAL_SECONDS`
    (`state` is the caller's own dict across ticks, like the other jobs'; an empty one is due).
    Only when Traefik routes and `TRAEFIK_METRICS_PORT` is set, and never without the lease, which
    is checked first and raises `LeaseLost` rather than polling. Returns whether a poll was
    used. A failure is logged inside `poll` and never raised."""
    if poller is None or not signal_is_configured(settings):
        return False
    moment = now or datetime.now(timezone.utc)
    if state is not None:
        next_due_at = state.get("next_due_at")
        if next_due_at is not None and moment < next_due_at:
            return False
    if lease is not None:
        lease.require()
    if state is not None:
        state["next_due_at"] = moment + timedelta(seconds=POLL_INTERVAL_SECONDS)
    return poller.poll(db, now=moment)


def run_iteration(
    db: Session,
    executor: BuildExecutor,
    *,
    preview_manager=None,
    deploy_manager=None,
    git_provider=None,
    backup_docker_client=None,
    backup_scan_state: dict | None = None,
    idle_scan_state: dict | None = None,
    preview_orphan_docker_client=None,
    preview_orphan_scan_state: dict | None = None,
    eval_probe_factory=None,
    reconciler: Reconciler | None = None,
    reconcile_state: dict | None = None,
    lease: WorkerLease | None = None,
    traffic_poller: TrafficSignalPoller | None = None,
    traffic_poll_state: dict | None = None,
    image_sweep_state: dict | None = None,
    image_sweep_client=None,
    disk_check_state: dict | None = None,
    disk_check_client=None,
) -> Build | Deployment | None:
    """One worker loop tick: sweep timeouts + expired previews + stale
    deployments + service-data backups + idle projects + orphaned preview
    stores, advance an eval run, then run a single work item. Deployments are claimed before
    builds — a deployment takes seconds while a build takes minutes, so a
    confirm is never starved behind a long agent run. Returns the
    build/deployment that was run, if any.

    with a `reconciler`, the timeout sweep destroys a timed-out build's sandbox, proxy
    and network itself, and the reconciler's own pass runs when it is due
    (`run_reconcile_if_due`), straight after the sweeps that fail rows -- so an object whose row
    just failed is reaped on the same tick. No `reconciler` (the default) touches no Docker object
    beyond what each sweep already did; `main()` passes one. A stop requested during the sweeps
    ends the tick before anything is claimed.

    ADR 0038 §Decision 4: with a `lease`, it is re-checked before each timeout sweep,
    before the reconcile pass (inside `run_reconcile_if_due`) and before each claim, and a worker
    that has lost it raises `LeaseLost` (a `SystemExit`, so neither this function's wrappers nor
    `_run_loop_tick` swallow it) before the step runs. No `lease` (the default; unit tests) checks
    nothing; `main()` passes the one it holds."""
    if lease is not None:
        lease.require()
    sweep_timed_out_builds(
        db,
        max_duration_minutes=settings.build_max_duration_minutes,
        destroy_objects=reconciler.destroy_build_objects if reconciler is not None else None,
    )
    try:
        sweep_expired_previews(db, preview_manager=preview_manager)
    except Exception:  # noqa: BLE001 - the last unwrapped sweep here; a DB-level
        # error must not kill the worker loop, matching every other sweep below.
        logger.exception("expired-preview sweep raised out of run_iteration")
    if lease is not None:
        lease.require()
    sweep_stale_deployments(
        db,
        max_duration_minutes=settings.build_max_duration_minutes,
        manager=deploy_manager,
        manager_factory=_new_deploy_manager,
    )
    try:
        run_reconcile_if_due(db, reconciler, reconcile_state, lease=lease)
    except Exception:  # noqa: BLE001 - the reconciler never raises; this guards only the due-ness bookkeeping
        logger.exception("reconciler pass raised out of run_iteration")
    try:
        sweep_service_data_backups(db, docker_client=backup_docker_client, scan_state=backup_scan_state)
    except Exception:  # noqa: BLE001 - a raising dumper must never kill the worker loop
        logger.exception("service-data backup sweep raised out of run_iteration")
    # the traffic read runs before the idle sweep in the same iteration, so the sweep
    # judges against a poll at most one cadence old.
    try:
        run_traffic_poll_if_due(db, traffic_poller, traffic_poll_state, lease=lease)
    except Exception:  # noqa: BLE001 - a failing poll must never kill the worker loop
        logger.exception("traffic signal poll raised out of run_iteration")
    try:
        sweep_idle_projects(db, deploy_manager=deploy_manager, scan_state=idle_scan_state, lease=lease)
    except Exception:  # noqa: BLE001 - a raising sweep must never kill the worker loop
        logger.exception("idle-project sweep raised out of run_iteration")
    try:
        sweep_orphaned_preview_stores(
            db, docker_client=preview_orphan_docker_client, scan_state=preview_orphan_scan_state
        )
    except Exception:  # noqa: BLE001 - a raising sweep must never kill the worker loop
        logger.exception("preview orphan sweep raised out of run_iteration")
    # the hourly build-image sweep. Only a caller that passes its cadence dict
    # runs it (`main()` does), so a unit test's `run_iteration` never reaches Docker. The lease is
    # re-checked inside it, before each removal.
    if image_sweep_state is not None:
        try:
            sweep_build_images(db, client=image_sweep_client, lease=lease, scan_state=image_sweep_state)
        except Exception:  # noqa: BLE001 - the sweep never raises; a `LeaseLost` is a SystemExit and passes
            logger.exception("build image sweep raised out of run_iteration")
    # the host free-disk measurement. Like the image sweep, only a caller
    # that passes its cadence dict runs it (`main()` does); it never raises.
    if disk_check_state is not None:
        try:
            run_disk_check_if_due(db, docker_client=disk_check_client, scan_state=disk_check_state)
        except Exception:  # noqa: BLE001 - the check never raises; this guards the worker loop only
            logger.exception("host disk check raised out of run_iteration")
    # the worker drives an eval run, beside the build queue -- it queues the
    # next case's build or judges a finished one. Never raises (a failure retries next tick).
    try:
        advance_runs(db, preview_manager=preview_manager, probe_factory=eval_probe_factory)
    except Exception:  # noqa: BLE001 - a raising eval step must never kill the worker loop
        logger.exception("eval run step raised out of run_iteration")

    if SHUTDOWN.requested:
        return None  # stop claiming; the loop's next check ends the worker

    if lease is not None:
        lease.require()  # the sweeps above may have taken a while; never claim without it
    deployment = claim_next_deployment(db)
    if deployment is not None:
        return run_one_deployment(
            db, deployment, deploy_manager=deploy_manager, git_provider=git_provider
        )

    if lease is not None:
        lease.require()
    build = claim_next_build(db, concurrency_cap=settings.build_concurrency)
    if build is None:
        return None

    # the engine is decided per claimed build (not when the worker started), and only
    # for an executor that runs one -- the placeholder has no engine to choose.
    if getattr(executor, "uses_engine", False) and not assign_engine(db, build):
        return build

    return run_one_build(db, build, executor)


def recover_orphaned_rows(db: Session) -> tuple[int, int]:
    """ADR 0037 §Decision 5: at worker start, fail every claimed build and deployment a
    dead process left in flight. Returns `(builds, deployments)` failed. The rules, and the
    single-worker assumption they rest on, are in `build_queue.fail_orphaned_builds` and
    `deploy.fail_orphaned_deployments`; this only calls both, once, before anything is claimed.
    The sandbox, proxy and network objects of those rows are removed by the reconciler's pass
    that `main()` runs straight after (the rows are no longer RUNNING / DEPLOYING)."""
    builds = fail_orphaned_builds(db)
    deployments = fail_orphaned_deployments(db)
    if builds or deployments:
        logger.warning(
            "worker start: failed %d build(s) and %d deployment(s) left in flight by the previous worker",
            len(builds),
            len(deployments),
        )
    return len(builds), len(deployments)


def run_reconcile_if_due(
    db: Session,
    reconciler: Reconciler | None,
    state: dict | None,
    *,
    now: datetime | None = None,
    lease: WorkerLease | None = None,
):
    """Runs the reconciler's pass when `RECONCILE_INTERVAL_SECONDS` has passed since the last one
    (`state` is the caller's own dict across ticks, the same shape as the other sweeps'
    `scan_state`; the first call with an empty one runs). Returns the pass's report, or None when
    nothing ran. `reconciler=None` is a no-op.

    ADR 0038 §Decision 4: a pass removes containers, so with a `lease` it is confirmed
    still ours immediately before the pass runs, and `LeaseLost` is raised instead of reaping
    when it is not. A tick on which nothing is due checks nothing."""
    if reconciler is None:
        return None
    moment = now or datetime.now(timezone.utc)
    if state is not None:
        next_due_at = state.get("next_due_at")
        if next_due_at is not None and moment < next_due_at:
            return None
    if lease is not None:
        lease.require()
    if state is not None:
        state["next_due_at"] = moment + timedelta(seconds=settings.reconcile_interval_seconds)
    return reconciler.reconcile(db)


def _bind_instance_id() -> str:
    """Reads (or creates) this installation's id in the database and binds it for the process's
    labels (`services/instance_id.py`). Its own short session, closed at once."""
    db = SessionLocal()
    try:
        return bind_instance_id(db)
    finally:
        db.close()


def _new_reconciler() -> Reconciler:
    """The worker's reconciler. Its Docker client is built on first use, so a host with no Docker
    still starts the worker (a pass then logs that it was skipped)."""
    return Reconciler()


def _recover_at_start(reconciler: Reconciler, reconcile_state: dict, lease: WorkerLease | None = None) -> None:
    """the start-up pass, before anything is claimed -- fail the orphaned rows, then
    reconcile. A failure here is logged loudly and does not stop the worker: the timeout sweeps
    and the periodic pass are the backstops.

    ADR 0038 §Decision 2, 4: the lease is confirmed before the rows are failed, and again
    before the reconcile pass. `LeaseLost` is a `SystemExit`, so the handler below does not turn
    "not ours" into "recovery failed, carry on"."""
    db = SessionLocal()
    try:
        if lease is not None:
            lease.require()
        recover_orphaned_rows(db)
        report = run_reconcile_if_due(db, reconciler, reconcile_state, lease=lease)
        if report is not None and report.unavailable:
            logger.warning("worker start: the reconciler could not reach Docker; objects are not reconciled")
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("worker start: recovery of orphaned rows / reconciliation failed")
    finally:
        db.close()


def _select_executor() -> BuildExecutor:
    """sandbox-backed executor only when explicitly opted in via
    `USE_SANDBOX_EXECUTOR=true` *and* at least one engine's real key is
    configured (ADR 0035: the opt-in is engine-neutral) — never implied by one
    alone, so dev without a key stays on the placeholder.
    Which engine a build runs on is NOT decided here: this
    runs once at startup, `assign_engine` decides per claim."""
    if sandbox_executor_active(settings):
        from app.services.sandbox_build_executor import SandboxBuildExecutor

        logger.info("worker: using SandboxBuildExecutor (real agent builds)")
        return SandboxBuildExecutor()
    logger.info("worker: using PlaceholderBuildExecutor (no sandbox/agent)")
    return PlaceholderBuildExecutor()


def _run_loop_tick(executor: BuildExecutor, **iteration_kwargs) -> Build | Deployment | None:
    """One pass of `main()`'s loop: its own session, one `run_iteration`.

    **an exception out of one iteration is logged and the loop goes
    on.** Several things in `run_iteration` are deliberately not wrapped (the
    timed-out-build and stale-deployment sweeps, claiming, the build and
    deployment runs themselves), and a database error in any of them used to
    propagate out of `main()` and end the process -- no more builds or
    deployments until someone restarted it. Returns what `run_iteration`
    returned, or `None` after a failure, which is what makes the caller sleep
    one poll interval before trying again instead of spinning on a persistent
    error. Only `Exception` is caught: a `KeyboardInterrupt` or `SystemExit`
    still stops the worker."""
    try:
        db = SessionLocal()
        try:
            return run_iteration(db, executor, **iteration_kwargs)
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - see docstring: one bad iteration must not end the worker
        logger.exception("worker loop iteration failed; continuing after the poll interval")
        return None


class ContainerRuntimeNotRegisteredError(RuntimeError):
    """ADR 0046 §Decision 3: a container runtime the platform is configured to create
    containers on (`BUILD_CONTAINER_RUNTIME`, `PREVIEW_CONTAINER_RUNTIME`) is not registered with
    the Docker engine, or the engine could not be asked. In prod this ends the worker before it
    takes the lease; the message names the setting, the runtime and the registered ones."""


def _runtime_setting_names(runtime: str) -> str:
    names = []
    if (settings.build_container_runtime or "").strip() == runtime:
        names.append("BUILD_CONTAINER_RUNTIME")
    if (settings.preview_container_runtime or "").strip() == runtime:
        names.append("PREVIEW_CONTAINER_RUNTIME")
    return " and ".join(names) or "a container runtime setting"


def verify_container_runtimes(client=None) -> list[str]:
    """ADR 0046 §Decision 3: every configured runtime must appear in the engine's
    `docker info` runtimes. Returns the runtimes that do not (empty = fine). In prod a missing one
    -- or an engine that cannot be asked while a runtime is configured -- raises
    `ContainerRuntimeNotRegisteredError` (fail closed: a build or preview on an unregistered
    runtime would fail `INFRA_ERROR` at its first container, after the platform said it was up);
    in dev it logs a warning and carries on. With no runtime configured there is nothing to check
    and the engine is not contacted."""
    configured = settings.configured_container_runtimes
    if not configured:
        return []
    try:
        if client is None:
            import docker

            client = docker.from_env()
        registered = set((client.info() or {}).get("Runtimes") or {})
    except Exception as exc:  # noqa: BLE001 - an engine that cannot answer is a finding, not a crash
        message = (
            f"the container runtimes {configured} are configured but the Docker engine could not be "
            f"asked which runtimes it has ({type(exc).__name__})"
        )
        if settings.is_prod:
            raise ContainerRuntimeNotRegisteredError(message) from None
        logger.warning("container runtime: %s", message)
        return list(configured)
    missing = [name for name in configured if name not in registered]
    for name in missing:
        message = (
            f"container runtime {name!r} ({_runtime_setting_names(name)}) is not registered with the "
            f"Docker engine (registered: {sorted(registered)}); install and register it in the daemon's "
            "`runtimes` configuration, or unset the setting"
        )
        if settings.is_prod:
            raise ContainerRuntimeNotRegisteredError(message)
        logger.warning("container runtime: %s -- builds and previews on it will fail", message)
    return missing


def _acquire_worker_lease() -> WorkerLease:
    """ADR 0038 §Decision 2-3: takes the installation's worker lease, starts its heartbeat
    thread and returns it. A live holder ends the process here (exit 1, the message names its
    host, pid and heartbeat age); a dead one is waited out, up to the stale window.

    The lease gets a session FACTORY, read through this module at call time (so a test that
    redirects `SessionLocal` redirects the lease too), never the loop's session: see
    `services/worker_lease.py` for why the two never meet."""
    lease = WorkerLease(lambda: SessionLocal())
    try:
        lease.acquire()
    except LeaseHeld as exc:
        logger.error("worker refuses to start: %s", exc)
        raise SystemExit(1) from None
    lease.start_heartbeat()
    return lease


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # same refusal as the API server, before anything else -- the
    # worker never creates or migrates the schema either.
    assert_schema_at_head(engine)
    # this installation's id, read from (or created in) the database and bound for every
    # label the process puts on a container or network. Before the executor and the disk probe,
    # which create labelled objects.
    instance = _bind_instance_id()
    logger.info(
        "worker starting: forge_env=%s concurrency=%s max_duration_min=%s instance=%s reconcile_interval_s=%s",
        settings.forge_env,
        settings.build_concurrency,
        settings.build_max_duration_minutes,
        instance,
        settings.reconcile_interval_seconds,
    )
    # ADR 0046 §Decision 3: a configured runtime the engine does not have ends a prod
    # worker here, before the lease and before anything is created on it; dev only warns.
    try:
        verify_container_runtimes()
    except ContainerRuntimeNotRegisteredError as exc:
        logger.error("worker refuses to start: %s", exc)
        raise SystemExit(1) from None
    # before anything that assumes this is the only worker -- the executor and
    # the probes below create labelled objects, then recovery, the reconciler and the claims.
    lease = _acquire_worker_lease()
    try:
        _serve(lease)
    finally:
        lease.release()  # every way out: a stop, a lost lease, an error


def _serve(lease: WorkerLease) -> None:
    _log_backup_startup_banner()
    executor = _select_executor()
    _log_disk_cap_startup_banner(executor)
    _log_proxy_image_startup_banner(executor)
    _log_oom_visibility_startup_banner(executor)
    poll_interval = settings.worker_poll_interval_seconds

    # Lazily constructed on first actual use inside sweep_expired_previews /
    # sweep_service_data_backups (not here): a placeholder-executor dev run
    # with no Docker Engine available must still start the worker loop
    # cleanly (no preview or backup Docker call ever happens unless there is
    # actually a preview to tear down or a dump that is due).
    preview_manager = None
    backup_docker_client = None
    # the idle-project sweep's own in-process due-ness cache
    # (`sweep_idle_projects`'s `scan_state`), owned by this loop across
    # ticks -- module-level state, not a caller-owned dict, would leak
    # between worker restarts in a way that no longer means anything and
    # would make the dict impossible to reset between tests.
    idle_scan_state: dict = {}
    # the backup sweep's own state, same idea: when it next looks, and which databases are
    # backing off after a failed dump (`sweep_service_data_backups`'s `scan_state`).
    backup_scan_state: dict = {}
    # same shape as idle_scan_state above, for
    # sweep_orphaned_preview_stores's own due-ness gate (decision 6).
    preview_orphan_scan_state: dict = {}
    # Lazily constructed inside sweep_orphaned_preview_stores itself (not
    # here) -- a quiet dev run with no orphan candidates never touches
    # Docker at all, same reasoning as preview_manager/backup_docker_client
    # above.
    preview_orphan_docker_client = None
    # the reconciler, and its own due-ness dict (same idea as the scan states above).
    reconciler = _new_reconciler()
    reconcile_state: dict = {}
    # the traffic signal's poller (it remembers which Traefik process the loopback check ran
    # against) and its cadence dict. Built only when the signal is configured.
    traffic_poller = TrafficSignalPoller() if signal_is_configured(settings) else None
    traffic_poll_state: dict = {}
    image_sweep_state: dict = {}  # the build-image sweep's cadence
    disk_check_state: dict = {}  # the host disk check's cadence

    previous_handlers = SHUTDOWN.install()
    preview_disk_watch = None
    service_checks = None
    try:
        # Before anything is claimed: fail what the previous process left in flight, then reap.
        _recover_at_start(reconciler, reconcile_state, lease)
        # after the reap (so it never sees what the reconciler is about to remove) and
        # before the first claim; it then watches through every build the loop runs.
        preview_disk_watch = _start_preview_disk_watch(executor)
        # after recovery (it fails rows the checks read), before the first claim.
        service_checks = _start_service_checks(lease)

        while not SHUTDOWN.requested:
            ran = _run_loop_tick(
                executor,
                preview_manager=preview_manager,
                backup_docker_client=backup_docker_client,
                backup_scan_state=backup_scan_state,
                idle_scan_state=idle_scan_state,
                preview_orphan_docker_client=preview_orphan_docker_client,
                preview_orphan_scan_state=preview_orphan_scan_state,
                reconciler=reconciler,
                reconcile_state=reconcile_state,
                lease=lease,
                traffic_poller=traffic_poller,
                traffic_poll_state=traffic_poll_state,
                image_sweep_state=image_sweep_state,
                disk_check_state=disk_check_state,
            )

            if ran is None:
                with SHUTDOWN.interruptible():
                    time.sleep(poll_interval)
    except WorkerShutdown:
        # a requested stop. Whatever was in flight has already been failed through its
        # own teardown on the way here; the worker simply ends (exit status 0).
        logger.info("worker stopped on signal %s", SHUTDOWN.signum)
    except LeaseLost as exc:
        # another worker has the lease, so this one does no more claiming, sweeping or
        # reaping. Exit status 1; `main()`'s `finally` releases nothing (the row is not ours).
        logger.error("worker lost its lease and is stopping without further work: %s", exc)
        raise
    finally:
        if preview_disk_watch is not None:
            preview_disk_watch.stop()
        if service_checks is not None:
            service_checks.stop()
        SHUTDOWN.restore(previous_handlers)


if __name__ == "__main__":
    main()
