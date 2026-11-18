"""the one place that decides what
counts as `Project.last_active_at` activity and writes it.

Before this task the column was written at row creation
(`models/project.py`'s `_utcnow` default) and in exactly one other place
(`routers/projects.py::submit_change_request`) — every other lifecycle
event that touches a project (a build starting or finishing, a deploy, a
rollback, a stop, traffic reaching the deployed service) left it
untouched, which is precisely the gap: a
zombie-idle threshold read against an unmaintained field would
archive every deployed project on the same day.

Every function here is bookkeeping, never load-bearing for the operation
it rides along with (task (b)): a failure is logged and swallowed *by
construction* — the guarantee lives in this one module's own try/except,
not in each of the several call sites remembering to add one.

"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.models.project import Project

logger = logging.getLogger("app.project_activity")

# Bounds how often a proxied request to a deployed service (the one
# counted event that would otherwise write on every request, task (a)) may
# write to the project row. Not an idleness threshold — that is a separate,
# operator-configurable — and not something an operator would ever want to
# tune: it only trades write frequency against timestamp precision on a
# signal a later threshold reads in units of months, so a few minutes of
# slack is free. A named constant, not a setting (task (a)).
TRAFFIC_TOUCH_COARSEN_INTERVAL = timedelta(minutes=15)


def service_traffic_is_observed(cfg: Settings | None = None) -> bool:
    """Whether the platform sees a request to a deployed service, and so
    whether `last_active_at` carries the traffic signal for one.

    The only recorder is `record_traffic_activity`, called from the
    platform-origin service proxy (`routers/service_proxy.py`), and
    `main.create_app` mounts that proxy only when Traefik is NOT routing.
    Under Traefik the request goes browser -> Traefik -> container and no
    platform code sees it, so an idle `last_active_at` there is the
    absence of a *measurement*, not evidence of disuse. This is the one
    place that says so; `tests/test_main_proxy_mounting.py` pins it
    against the real mount decision so the two cannot drift apart.

    `cfg` defaults to the process-wide settings; a caller that already
    holds a `Settings` (the app factory, the console's configuration
    view) passes its own."""
    return not (settings if cfg is None else cfg).traefik_enabled


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(value: datetime) -> datetime:
    """SQLite (the dev/test DB) does not
    round-trip `DateTime(timezone=True)`: a value read back after a commit
    (session-expire-and-reload) comes back tzinfo-naive even though every
    writer of this column (`_utcnow()`, here and in `models/project.py`) is
    UTC. Only `touch_last_active_for_traffic` does Python-side arithmetic
    on a stored value — every other comparison in this codebase runs in
    SQL — so the normalization lives here rather than in `models/base.py`,
    which would be a wider, unrelated change. Postgres (prod) already
    returns an aware value; this is then a no-op."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def touch_last_active(project: Project, *, now: datetime | None = None) -> None:
    """Marks `project` active right now. Unconditional — every "someone
    acting on the project" call site (task (a): a build claimed/succeeded/
    failed, a deploy confirmed, a rollback requested, a stop) calls this
    every time, riding whatever transaction the caller is already about to
    commit. Never raises (task (b)): a bookkeeping bug here must not fail
    the build/deploy/rollback/stop it happened to ride along with."""
    try:
        project.last_active_at = now or _utcnow()
    except Exception:  # noqa: BLE001 - bookkeeping must never break the caller
        logger.exception(
            "failed to record activity for project %s", getattr(project, "id", "?")
        )


def touch_last_active_for_traffic(project: Project, *, now: datetime | None = None) -> bool:
    """Coarsened activity touch for a request path (task (a): traffic
    reaching a deployed service — the strongest evidence of use, and the
    one signal that matters for the "deployed then idle" population ADR
    0010's amendment is actually worried about). Only writes when the
    stored value is already older than `TRAFFIC_TOUCH_COARSEN_INTERVAL`, so
    a busy service pays at most one write per interval rather than one per
    request. Returns whether it wrote, so a route with nothing else to
    persist can skip an empty commit on the (common) no-op path. Never
    raises, same guarantee as `touch_last_active`."""
    try:
        moment = now or _utcnow()
        stored = _as_aware_utc(project.last_active_at)
        if moment - stored < TRAFFIC_TOUCH_COARSEN_INTERVAL:
            return False
        project.last_active_at = moment
        return True
    except Exception:  # noqa: BLE001 - bookkeeping must never break the caller
        logger.exception(
            "failed to record traffic activity for project %s", getattr(project, "id", "?")
        )
        return False


def record_traffic_activity(db: Session, project: Project, *, now: datetime | None = None) -> None:
    """Traffic-path write site's own commit, guarded the same way as the
    field write it wraps (task (b), review round 2) — and its own
    `db.rollback()` cleanup call guarded in turn (review round 3 / (b5)):
    whatever infrastructure failure breaks the commit (a dropped
    connection, an already-invalidated DBAPI connection) can break the
    rollback attempted against that same broken connection, so neither
    step is allowed to propagate on its own.

    Every other counted event (build claimed/succeeded/failed, deploy
    confirmed, rollback requested, stop, change request submitted) rides a
    commit its own transaction was already going to issue regardless of
    this task — if that commit fails, the underlying operation was already
    going to fail, and this module owes it nothing extra. `proxy_service`
    is the one write site that does *not* ride an existing transaction: the
    `db.commit()` needed to persist the touch is new, added by this task,
    and an ordinary infrastructure failure there (a dropped connection,
    pool exhaustion) must not turn an already-successfully-proxied response
    into a 500. `touch_last_active_for_traffic`'s own try/except only
    covers the read/compare/assign it does in memory — not a commit the
    caller issues afterward — so that guarantee has to be repeated here,
    the one place that owns this call site's commit and its own rollback."""
    if not touch_last_active_for_traffic(project, now=now):
        return
    try:
        db.commit()
    except Exception:  # noqa: BLE001 - bookkeeping must never break the caller
        logger.exception(
            "failed to commit traffic activity for project %s", getattr(project, "id", "?")
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 - the cleanup call is bookkeeping too
            # A commit can fail for the same reason a rollback then also
            # fails (a dropped connection, an already-invalidated DBAPI
            # connection) — review round 2's finding. Guard this call the
            # same way as the commit it is cleaning up after: log and
            # swallow, never let an already-proxied response turn into an
            # unhandled 500 because the *cleanup* of a failed write also
            # failed.
            logger.exception(
                "failed to roll back traffic activity for project %s",
                getattr(project, "id", "?"),
            )
