"""DB-backed build queue. The `builds` table itself is the queue:
the worker claims the oldest eligible QUEUED row and transitions it through
RUNNING -> SUCCEEDED|FAILED. This module implements the rules for trigger and queueing
and the build state machine.

Claim semantics (single dev worker process):
- Per-project serialization: a project with any build already QUEUED-claimed
  (RUNNING) or RUNNING is not eligible for another claim.
- Global cap: the worker never has more than `settings.build_concurrency`
  RUNNING builds at once.
- SQLite (dev): single worker + WAL + short immediate-transaction claim is
  sufficient (no SKIP LOCKED needed). Postgres (prod, later
  multi-worker) would use `SELECT ... FOR UPDATE SKIP LOCKED` instead; this
  module's claim_next_build is written so that swap is localized here.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.build import Build
from app.models.project import Project
from app.services.audit import write_audit_log
from app.services.project_activity import touch_last_active
from app.services.project_status import advance
from app.services.status import BuildFailureReason, BuildStatus, ProjectStatus

logger = logging.getLogger("app.build_queue")

# Worker-driven transitions have no human actor. AuditLog.actor_id is
# nullable for exactly this case (see models/audit.py) rather than a
# fabricated "system user" row in the users table.
SYSTEM_ACTOR_ID: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _touch_project_activity(db: Session, project_id: str) -> None:
    """"a build starting or finishing" is unambiguous evidence a
    human cares about this project — the agent is doing work someone asked
    for. Looked up by id rather than a relationship because `Build` has no
    `project` relationship (models/build.py); a missing project (should
    never happen — the FK is not nullable) is a no-op, not a raise."""
    project = db.get(Project, project_id)
    if project is not None:
        touch_last_active(project)


def _projects_with_active_build(db: Session) -> set[str]:
    """Projects that already have a build claimed (RUNNING) — per-project
    serialization means such a project's other QUEUED builds must wait."""
    rows = db.scalars(select(Build.project_id).where(Build.status == BuildStatus.RUNNING))
    return set(rows)


def queue_wait(db: Session, build: Build, *, concurrency_cap: int) -> dict[str, int | bool] | None:
    """why a QUEUED build has not started, as two platform-measured numbers.

    `ahead` is how many builds, system-wide, are QUEUED or RUNNING and were created before this
    one (the order `claim_next_build` serves, ties broken by id so the count is stable); `at_cap`
    is whether the RUNNING builds already fill `concurrency_cap`. A count and a flag only -- never
    an id, a project name or an owner, because every authenticated reader sees the timeline.
    Any other status returns None: there is nothing to wait for."""
    if build.status != BuildStatus.QUEUED:
        return None
    earlier = (Build.created_at < build.created_at) | (
        (Build.created_at == build.created_at) & (Build.id < build.id)
    )
    ahead = db.query(Build).filter(
        Build.status.in_((BuildStatus.QUEUED, BuildStatus.RUNNING)), earlier
    ).count()
    running = db.query(Build).filter(Build.status == BuildStatus.RUNNING).count()
    return {"ahead": ahead, "at_cap": running >= concurrency_cap}


def claim_next_build(db: Session, *, concurrency_cap: int) -> Build | None:
    """Claim the oldest eligible QUEUED build and transition it to RUNNING.

    Returns None if the global cap is already reached, or no QUEUED build's
    project is free of an active (RUNNING) build. Commits the claim (short
    transaction) and writes an audit-log
    entry in the same transaction.
    """
    running_count = db.query(Build).filter(Build.status == BuildStatus.RUNNING).count()
    if running_count >= concurrency_cap:
        return None

    busy_projects = _projects_with_active_build(db)

    candidates = db.scalars(
        select(Build)
        .where(Build.status == BuildStatus.QUEUED)
        .order_by(Build.created_at.asc())
    )
    for build in candidates:
        if build.project_id in busy_projects:
            continue

        now = _utcnow()
        build.status = BuildStatus.RUNNING
        build.started_at = now
        build.heartbeat_at = now
        _touch_project_activity(db, build.project_id)
        write_audit_log(
            db,
            actor_id=SYSTEM_ACTOR_ID,
            action="build.started",
            target=f"build:{build.id}",
            metadata={"project_id": build.project_id},
        )
        db.commit()
        db.refresh(build)
        return build

    return None


# the bound lives in the one
# writer, so no caller can exceed it.
AGENT_SUMMARY_MAX_CHARS = 4000
AGENT_SUMMARY_TRUNCATION_MARKER = "…[truncated]"


def bound_agent_summary(text: str | None) -> str | None:
    """The agent's text as `mark_succeeded` stores it: as received (no stripping, no
    escaping) unless it exceeds `AGENT_SUMMARY_MAX_CHARS`, in which case it is cut so
    that the result, visible truncation marker included, is exactly that long. `None`
    and the empty string store NULL (there is nothing to show)."""
    if not text:
        return None
    if len(text) > AGENT_SUMMARY_MAX_CHARS:
        keep = AGENT_SUMMARY_MAX_CHARS - len(AGENT_SUMMARY_TRUNCATION_MARKER)
        return text[:keep] + AGENT_SUMMARY_TRUNCATION_MARKER
    return text


def _advance_on_preview(db: Session, build: Build) -> None:
    """a build that
    SUCCEEDED with a live preview moves its project to PREVIEW_READY. The
    sandbox executor has issued (or failed to issue) the preview by the time
    the worker calls `mark_succeeded`, so `preview_url` is the fact. A build
    with no preview (its start failed, or the executor issues none) changes
    nothing, and so does a project that is already further along -- `advance`
    is forward-only."""
    if not build.preview_url:
        return
    project = db.get(Project, build.project_id)
    if project is not None:
        advance(db, project, ProjectStatus.PREVIEW_READY, cause=f"build:{build.id}")


def mark_succeeded(db: Session, build: Build, *, agent_summary: str | None = None) -> Build:
    """The only writer of `BuildStatus.SUCCEEDED` -- and of `Build.agent_summary`, which
    is the agent's own text: L3-authored, bounded here, never read by anything
    but its display."""
    build.status = BuildStatus.SUCCEEDED
    build.ended_at = _utcnow()
    build.agent_summary = bound_agent_summary(agent_summary)
    # A build finishing updates last_active_at.
    _touch_project_activity(db, build.project_id)
    _advance_on_preview(db, build)
    write_audit_log(
        db,
        actor_id=SYSTEM_ACTOR_ID,
        action="build.succeeded",
        target=f"build:{build.id}",
        metadata={"project_id": build.project_id},
    )
    db.commit()
    db.refresh(build)
    return build


# the bound
# lives in the one writer, so no caller can exceed it.
FAILURE_DETAIL_MAX_CHARS = 1000
FAILURE_DETAIL_TRUNCATION_MARKER = "…[truncated]"


def bound_failure_detail(reason: str, detail: str) -> str:
    """The text `mark_failed` stores: never empty (a blank detail becomes a
    template naming the code -- no FAILED row is silent), and at most
    `FAILURE_DETAIL_MAX_CHARS` characters including the visible truncation
    marker."""
    text = (detail or "").strip()
    if not text:
        return f"the build failed ({reason}); no further detail was recorded"
    if len(text) > FAILURE_DETAIL_MAX_CHARS:
        keep = FAILURE_DETAIL_MAX_CHARS - len(FAILURE_DETAIL_TRUNCATION_MARKER)
        text = text[:keep] + FAILURE_DETAIL_TRUNCATION_MARKER
    return text


def mark_failed(db: Session, build: Build, *, reason: str, detail: str) -> Build:
    """The only writer of `BuildStatus.FAILED`. `detail` is required so
    no caller can forget it; it must be platform-authored -- a platform template
    filled with bounded platform values -- never L3 text or a library's
    free-form exception text. Stored on the row and in the audit metadata."""
    stored_detail = bound_failure_detail(reason, detail)
    build.status = BuildStatus.FAILED
    build.ended_at = _utcnow()
    build.failure_reason = reason
    build.failure_detail = stored_detail
    # a failed build is still evidence someone requested a
    # change and the platform acted on this project just now.
    _touch_project_activity(db, build.project_id)
    write_audit_log(
        db,
        actor_id=SYSTEM_ACTOR_ID,
        action="build.failed",
        target=f"build:{build.id}",
        metadata={"project_id": build.project_id, "reason": reason, "detail": stored_detail},
    )
    db.commit()
    db.refresh(build)
    return build


def find_expired_preview_build_ids(db: Session, *, now: datetime | None = None) -> list[str]:
    """Build ids whose preview has passed its TTL and is still marked live
    (preview container teardown on TTL expiry).
    Read-only: the caller (worker sweep) tears down the actual container
    via `PreviewManager` and then clears these columns via
    `clear_preview_fields`."""
    cutoff = now or _utcnow()
    rows = db.scalars(
        select(Build.id).where(
            Build.preview_expires_at.is_not(None),
            Build.preview_expires_at < cutoff,
        )
    )
    return list(rows)


def clear_preview_fields(db: Session, build_id: str, *, commit: bool = True) -> None:
    """Clears a build's live-preview columns after its container has been
    torn down (TTL expiry, replacement by a newer build, or manual stop) —
    so `BuildOut.preview_url` stops being shown once the container is
    actually gone. `commit=False` leaves the commit to the caller,
    so the preview disk watcher can clear the columns and write its timeline
    entry in ONE transaction: a failed commit then leaves neither, and its
    retry writes both, never the entry twice."""
    build = db.get(Build, build_id)
    if build is None:
        return
    build.preview_url = None
    build.preview_expires_at = None
    build.preview_token_hash = None
    if commit:
        db.commit()


# ADR 0037 §Decision 5-6: the platform-authored details of the two ways a build can be
# stopped by its own worker going away. Both are `INFRA_ERROR` (the taxonomy's "something in
# the platform's own infrastructure"; the taxonomy has no code for a restarted worker)
# and neither carries anything but platform text.
WORKER_RESTARTED_BUILD_DETAIL = (
    "the worker restarted while this build was running, so the build was stopped before it "
    "finished; start a new build"
)
WORKER_STOPPED_BUILD_DETAIL = (
    "the worker was asked to stop (a shutdown signal) while this build was running, so the "
    "build was stopped before it finished; start a new build"
)


def fail_orphaned_builds(db: Session) -> list[Build]:
    """ADR 0037 §Decision 5 -- **the single-worker assumption lives here.** There is one worker
    process and it runs one build at a time, so when it STARTS, a build still marked
    `RUNNING` belongs to a process that is gone: it can never finish, and without this it would
    sit `RUNNING` (blocking its project's queue) until the timeout sweep, up to the maximum
    build duration later. Every such build is failed, with a platform-authored detail and the
    usual `build.failed` audit entry; its sandbox, proxy and network are then removed by the
    reconciler's pass (the build is no longer `RUNNING`).

    Only `RUNNING` is touched: `QUEUED` is the queue and belongs to whichever worker claims it.

    **If the platform ever runs more than one worker, this function must become a lease first**
    (a worker would otherwise fail a build another live worker is running). Until then it is the
    simplest correct rule. Call it once, at worker start, before anything is claimed -- never
    from the loop."""
    running = db.scalars(select(Build).where(Build.status == BuildStatus.RUNNING)).all()
    failed = []
    for build in running:
        failed.append(
            mark_failed(
                db,
                build,
                reason=BuildFailureReason.INFRA_ERROR,
                detail=WORKER_RESTARTED_BUILD_DETAIL,
            )
        )
    return failed


def sweep_timed_out_builds(
    db: Session,
    *,
    max_duration_minutes: int,
    destroy_objects: Callable[[Session, str], object] | None = None,
) -> list[Build]:
    """Fail any RUNNING build whose started_at is older than the max build
    duration. Runs on every worker loop iteration so a crashed/restarted
    worker still converges a stuck build to FAILED/TIMEOUT (durability rationale:
    "a sandbox older than
    the max build duration is force-destroyed by the worker sweep").

    ADR 0037 §Decision 4: the sweep was always meant to FORCE-DESTROY the sandbox,
    and it only flipped the row, so a timed-out build's sandbox, proxy (and the real API key
    in it) and network stayed on the host. `destroy_objects(db, build_id)` is what destroys
    them -- the worker passes `Reconciler.destroy_build_objects` -- and it runs for each
    stale build BEFORE the row is failed. It cannot stop the row being failed: a destroyer
    that raises is logged and the build is failed anyway (the reconciler's next pass removes
    whatever was left, since the build is then no longer RUNNING). `None` keeps the old
    row-only behaviour for a caller with no Docker."""
    cutoff = _utcnow() - timedelta(minutes=max_duration_minutes)
    stale = db.scalars(
        select(Build).where(
            Build.status == BuildStatus.RUNNING,
            Build.started_at.is_not(None),
            Build.started_at < cutoff,
        )
    ).all()
    timed_out = []
    for build in stale:
        if destroy_objects is not None:
            try:
                destroy_objects(db, build.id)
            except Exception:  # noqa: BLE001 - the row must still be failed
                logger.exception("timeout sweep: destroying build %s's objects raised", build.id)
        timed_out.append(
            mark_failed(
                db,
                build,
                reason=BuildFailureReason.TIMEOUT,
                detail=(
                    "the build did not finish within the maximum build duration "
                    f"of {max_duration_minutes} minutes"
                ),
            )
        )
    return timed_out
