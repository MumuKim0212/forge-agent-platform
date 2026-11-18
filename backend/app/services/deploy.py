"""Deployment lifecycle service.

The DB half of the deploy feature: confirm validation, the `Deployment`
state machine, the
merge step (confirmed-ref advance), rollback/stop, secret issuance, and the
DB-backed deployment queue the worker claims
from (same durability rationale as builds — deploy execution
runs in the worker process, never in the API request).

Docker mechanics live in `services/deploy_manager.py`; this module owns
every DB transition and audit entry. Every state transition writes an
audit-log entry targeting `deployment:<id>` — surfaced in the project
timeline by `routers/projects.py` (never silent).

`confirm_deploy`/`request_rollback`/
`stop_active_deployment` each mark the project active (services/
project_activity.touch_last_active) — the human confirm/rollback/stop
action itself. `execute_deployment`'s own transitions (activation, every
FAILED reason) deliberately do NOT — they are the worker's automatic
execution of a confirm/rollback already counted moments earlier, not new
evidence that a human is acting on the project; counting both would just
be the same action stamped twice under two names.


`stop_active_deployment` gained a `touch_activity` keyword for exactly the
opposite reason the call was added in the first place. A *human* stopping
a deployment (`routers/deployments.py`) is activity; the idle-project sweep
(`worker.sweep_idle_projects` / `services/project_lifecycle.py`) stopping
the same deployment as stage 2 of flag-stop-archive is the platform's own
follow-through on a candidate it already flagged, which is
among the things that must NOT move the signal — moving it there would
erase, in the same transaction, the very evidence (`last_active_at` being
old) that made the project a candidate. Defaults to `True` so every
existing human-facing call site is unchanged; the sweep is the one caller
that passes `False`. The distinction lives here, at the function's one
call to `touch_last_active`, rather than as a second copy of the stop
logic or a "touch then put the old value back" fixup at the sweep's call
site.
"""

from __future__ import annotations

import logging
import secrets as _secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import ProdServiceEnvEncryptionKeyError, settings
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentFailureReason, DeploymentStatus
from app.models.project import Project
from app.services.approval import is_deploy_eligible
from app.services.audit import write_audit_log
from app.services.deploy_manager import DeployDataServiceError, service_base_url
from app.services.git_provider import (
    PR_CLOSED,
    PR_MERGED,
    PR_OPEN,
    GitProvider,
    create_provider,
)
from app.services import service_data
from app.services.project_activity import touch_last_active
from app.services.project_status import advance
from app.services.service_data import ServiceDataProvisionError
from app.services.service_env_crypto import (
    ServiceEnvDecryptionError,
    decrypt_service_env,
    encrypt_service_env,
)
from app.services.status import BuildStatus, ProjectKind, ProjectStatus

logger = logging.getLogger("app.deploy")

# Worker-driven transitions have no human actor (same convention as
# build_queue.SYSTEM_ACTOR_ID).
SYSTEM_ACTOR_ID: str | None = None


class DeployValidationError(Exception):
    """Raised when a confirm/rollback/stop request violates the deploy rules
    (wrong build state, not
    deploy-eligible, not the latest successful build, missing image, no
    rollback target, nothing active to stop). Routers map this to 400.

    a refusal may carry a `code` (`DeployRefusalCode`); the message then
    starts with it (`CODE: ...`), so the 4xx `detail` and the project read's
    `*_blocked_reason` are one vocabulary."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(f"{code}: {message}" if code else message)
        self.code = code


class DeployConflictError(Exception):
    """Raised when another deployment of the same project is already in
    in flight (per-project serialization). Routers map
    this to 409. Carries a `code` like `DeployValidationError`."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(f"{code}: {message}" if code else message)
        self.code = code


# the machine-readable reason a 409 detail starts with, so a caller can
# tell "un-archive it first" from "one is already in flight" without matching
# prose. `ApiError.message` (frontend) is the detail string as received, so the
# code is a prefix of that string rather than a second field -- a structured
# `detail` would have shown as the bare status text ("Conflict") on every
# screen that renders the message today.
PROJECT_ARCHIVED_CODE = "PROJECT_ARCHIVED"

# the same convention for the deprovision refusal (`routers/projects.py`,
# 409): the project still has an ACTIVE or DEPLOYING deployment.
DEPLOYMENT_LIVE_CODE = "DEPLOYMENT_LIVE"


class DeployRefusalCode:
    """why a confirm or a rollback is refused -- one vocabulary for the
    prefix of the 4xx `detail` (`CODE: message`) AND for the
    `confirm_blocked_reason` / `rollback_blocked_reason` of the project read
    (`deploy_controls`), because both come out of the same refusal objects.
    The frontend pins every value as a literal (`pages/deployControls.ts`)."""

    # 409
    PROJECT_ARCHIVED = PROJECT_ARCHIVED_CODE
    DEPLOYMENT_IN_FLIGHT = "DEPLOYMENT_IN_FLIGHT"
    EVAL_PROJECT = "EVAL_PROJECT"
    # 400
    POSTGRES_UNCONFIGURED = "POSTGRES_UNCONFIGURED"
    BUILD_NOT_SUCCEEDED = "BUILD_NOT_SUCCEEDED"
    NOT_DEPLOY_ELIGIBLE = "NOT_DEPLOY_ELIGIBLE"
    NOT_LATEST_SUCCESSFUL = "NOT_LATEST_SUCCESSFUL"
    IMAGE_MISSING = "IMAGE_MISSING"
    NO_ROLLBACK_TARGET = "NO_ROLLBACK_TARGET"
    # Reported by the project read only: the project has no SUCCEEDED build, so there is no
    # build a confirm could name. Never a 4xx (a confirm always names a build).
    NO_SUCCESSFUL_BUILD = "NO_SUCCESSFUL_BUILD"
    # Reported by the project read only: the Docker client could not be built or asked, which
    # the confirm/rollback routes answer with 503 before they get this far.
    DOCKER_UNAVAILABLE = "DOCKER_UNAVAILABLE"


class DeployProjectArchivedError(DeployConflictError):
    """Raised by `confirm_deploy` / `request_rollback` on an `ARCHIVED`
    project (the deploy promotion and rollback rules, and the project
    status rules). A `DeployConflictError` -- the
    routers' 409 mapping applies unchanged -- but its own type, and its
    message begins with `PROJECT_ARCHIVED_CODE` and says to un-archive first,
    so it can never be mistaken for the in-flight conflict."""

    def __init__(self, project_id: str, *, action: str) -> None:
        super().__init__(
            f"project {project_id} is archived; un-archive it before {action}",
            code=PROJECT_ARCHIVED_CODE,
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _deployment_target(deployment_id: str) -> str:
    return f"deployment:{deployment_id}"


# -- queries -------------------------------------------------------------


def get_active_deployment(db: Session, project_id: str) -> Deployment | None:
    return db.scalars(
        select(Deployment).where(
            Deployment.project_id == project_id,
            Deployment.status == DeploymentStatus.ACTIVE,
        )
    ).first()


def service_url(db: Session, project: Project) -> str | None:
    """the URL a person opens for this project's service, or None unless it has an
    ACTIVE deployment (the same row the dev service proxy requires before it answers).
    Computed from settings and the id (`deploy_manager.service_base_url`), never from the
    encrypted service env. Traefik off, the route is a path under the platform origin and
    carries a trailing slash (`/api/projects/{id}/service/`); the Traefik host does not."""
    if get_active_deployment(db, project.id) is None:
        return None
    base = service_base_url(project.id)
    return base if settings.traefik_enabled else f"{base}/"


def get_in_flight_deployment(db: Session, project_id: str) -> Deployment | None:
    return db.scalars(
        select(Deployment).where(
            Deployment.project_id == project_id,
            Deployment.status == DeploymentStatus.DEPLOYING,
        )
    ).first()


def list_live_deployments(db: Session, project_id: str) -> list[Deployment]:
    """every deployment of the project
    that is serving or about to (`ACTIVE`, `DEPLOYING`), oldest first. The
    deprovision action refuses while this is non-empty -- the rows are the
    evidence, never `Project.status`."""
    return list(
        db.scalars(
            select(Deployment)
            .where(
                Deployment.project_id == project_id,
                Deployment.status.in_([DeploymentStatus.ACTIVE, DeploymentStatus.DEPLOYING]),
            )
            .order_by(Deployment.created_at.asc(), Deployment.id.asc())
        )
    )


def _in_flight_conflict(project_id: str) -> DeployConflictError:
    return DeployConflictError(
        f"another deployment of project {project_id} is already in flight",
        code=DeployRefusalCode.DEPLOYMENT_IN_FLIGHT,
    )


def _insert_deploying(db: Session, deployment: Deployment) -> None:
    """Inserts a new `DEPLOYING` row and flushes it (assigning its id).

    the check-then-insert above each
    caller is only the friendly early refusal. The guarantee is the partial
    unique index `uq_deployments_one_deploying_per_project`: two requests that
    both pass the check both reach this INSERT, and the database refuses the
    second. That refusal is mapped to the SAME `DeployConflictError` the check
    raises (routers: 409), so a caller cannot tell which of the two caught it.

    The session is rolled back first -- a flush that raised leaves the
    transaction unusable, and nothing else is pending at this point (the
    caller's own writes come after the insert). The `IntegrityError` is
    translated only when a DEPLOYING row for the project really exists
    afterwards; any other integrity failure (a missing foreign key, say) is a
    different problem and propagates unchanged rather than being reported as a
    conflict it is not."""
    project_id = deployment.project_id
    db.add(deployment)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        if get_in_flight_deployment(db, project_id) is None:
            raise
        raise _in_flight_conflict(project_id) from exc


def _latest_successful_build(db: Session, project_id: str) -> Build | None:
    return db.scalars(
        select(Build)
        .where(Build.project_id == project_id, Build.status == BuildStatus.SUCCEEDED)
        .order_by(Build.created_at.desc())
    ).first()


# -- secret injection -----------------


def ensure_service_env(
    db: Session, project: Project, *, docker_client=None
) -> dict[str, str]:
    """Issues the project's service env at first deploy and returns it —
    stable across redeploys and rollbacks of the same project (spec: "so
    service data and sessions survive redeploys"). The values are stored
    platform-side (`Project.service_env_json`) and injected as container
    env at create; they are NEVER returned by any API, logged, audited, or
    baked into an image. Variable names are the template contract; the
    generated code consumes them by name only.

    `Project.service_env_json` holds Fernet
    ciphertext, not plaintext — this function is the one seam that
    decrypts it (`services.service_env_crypto`), and the one place that
    owns what happens when decryption fails, because that is a decision
    about the ROW's lifecycle, not about the cryptography:

    - **prod**: a value that will not decrypt raises
      `ProdServiceEnvEncryptionKeyError` and fails the operation. Never
      silently re-issues — that would rotate a live service's credentials
      (its DB password) out from under it and break it. The stored row is
      left untouched.
    - **dev**: the same failure is treated as "never issued" — a fresh
      value is generated and stored, with a warning logged, exactly as if
      `service_env_json` had been `NULL`. This is expected in dev because
      an unset `SERVICE_ENV_ENCRYPTION_KEY` there is a fresh, random,
      per-process key (`config.py`), so a value encrypted before the last
      restart is routinely unreadable after it — that is not corruption.

    Scope reminder (stated once, at the seam, so it cannot be oversold from
    further away): encrypting this column protects a database
    copy that leaves the host — a backup, a dump, an ad-hoc `SELECT`. It
    does not protect against host/control-plane compromise; ADR 0002
    already made the host the trust boundary.

    ADR 0023 §Data services: `DATABASE_URL`/`REDIS_URL` join the
    other four keys, provisioned via `services.service_data` BEFORE the
    dict is composed. A project whose stored env predates this task (four
    keys only) gains the two new ones on its NEXT deploy without touching
    the other four — the check below is "does the stored env already carry
    both", not "does a stored env exist at all", so an already-fully-issued
    env still short-circuits without any Postgres provisioning call (this is
    what makes "a second deploy reuses the database/role and never
    regenerates the password" true: Postgres provisioning is simply never
    called again once both keys are present). `docker_client` is the Redis
    half's only requirement — Postgres provisioning needs no Docker client
    at all, it talks to `settings.service_postgres_admin_dsn` directly.

    **ADR 0023 §Amendment 12— the short-circuit no longer covers
    Redis.** The stored `REDIS_URL` says a Redis was once created, not that
    it exists now (a stopped, deleted or never-started container all happen),
    so every call — confirm and rollback alike, since both go through
    `execute_deployment` — re-ensures the container through
    `service_data.ensure_project_redis` before returning. A stored URL with
    no password (issued by an earlier version) is upgraded once: the container is
    replaced by a password-protected one and the stored env re-encrypted
    with the new URL, audit-logged by key name only. Raises
    `ServiceDataProvisionError` (-> PROVISION_FAILED) when the Redis cannot
    be ensured.
    """
    env: dict[str, str] | None = None
    if project.service_env_json:
        try:
            env = decrypt_service_env(project.service_env_json)
        except ServiceEnvDecryptionError as exc:
            if settings.is_prod:
                raise ProdServiceEnvEncryptionKeyError(
                    f"project {project.id}'s stored service_env_json could not "
                    "be decrypted with SERVICE_ENV_ENCRYPTION_KEY or "
                    "SERVICE_ENV_ENCRYPTION_KEY_OLD. Refusing to silently "
                    "re-issue in prod -- that would rotate this service's "
                    "live credentials out from under it. Investigate the key "
                    "configuration; the stored value has been left untouched."
                ) from exc
            logger.warning(
                "project %s: service_env_json could not be decrypted "
                "(dev-mode key likely rotated across a process restart) -- "
                "treating it as never issued and re-issuing a fresh value.",
                project.id,
            )
            project.service_env_json = None
        else:
            if "DATABASE_URL" in env and "REDIS_URL" in env:
                # Fully issued already: stable, and Postgres is never
                # re-provisioned. But the stored env is NOT evidence that the
                # Redis container still exists (ADR 0023 §Amendment
                # 12): re-ensure it on every deployment start -- start a
                # stopped one, recreate a missing one, replace a password-less
                # one -- and take whatever URL it hands back.
                stored_redis_url = env["REDIS_URL"]
                ensured_redis_url = service_data.ensure_project_redis(
                    project.id, docker_client=docker_client, redis_url=stored_redis_url
                )
                if ensured_redis_url != stored_redis_url:
                    # The one sanctioned change to an issued env value: a
                    # password-less URL from an earlier version gains a password.
                    # Same encrypted path as issuance; key names only in
                    # the audit entry.
                    env["REDIS_URL"] = ensured_redis_url
                    project.service_env_json = encrypt_service_env(env)
                    write_audit_log(
                        db,
                        actor_id=SYSTEM_ACTOR_ID,
                        action="deployment.service_env_upgraded",
                        target=f"project:{project.id}",
                        metadata={"keys": ["REDIS_URL"]},
                    )
                return env

    if env is None:
        env = {
            "FORGE_SERVICE_ID": project.id,
            "FORGE_SERVICE_NAME": project.name,
            # spelled once, in `deploy_manager.service_base_url` (Traefik host or
            # the dev platform-proxy route), shared with the Traefik Host label.
            "FORGE_SERVICE_BASE_URL": service_base_url(project.id),
            "FORGE_APP_SECRET_KEY": _secrets.token_urlsafe(32),
        }

    # ADR 0023: both the brand-new-project path above and the
    # older four-key-env path (the `else: if ... in env` fallthrough)
    # reach here with DATABASE_URL/REDIS_URL still missing.
    data_services = service_data.provision_project_data_services(
        project.id, docker_client=docker_client
    )
    env["DATABASE_URL"] = data_services["DATABASE_URL"]
    env["REDIS_URL"] = data_services["REDIS_URL"]

    project.service_env_json = encrypt_service_env(env)
    # Issuance is worth an audit trail entry, but only the key NAMES —
    # never a value.
    write_audit_log(
        db,
        actor_id=SYSTEM_ACTOR_ID,
        action="deployment.service_env_issued",
        target=f"project:{project.id}",
        metadata={"keys": sorted(env.keys())},
    )
    return env


# -- confirm / rollback / stop (API side; does not commit) ---------------
#
# what a confirm or a rollback would be refused for is decided in ONE place each
# (`confirm_refusal`, `decide_rollback`). The actions raise what they return, and
# `deploy_controls` (the project read the detail page uses) reports the same objects' codes, so
# "what the UI offers" and "what the backend accepts" cannot drift: there is no second copy of
# either rule.

Refusal = DeployValidationError | DeployConflictError


class _ImageCheckUnavailable(Exception):
    """The `image_exists` callable itself raised (the Docker client could not be built or
    asked). Only `deploy_controls` reads it: the confirm/rollback routes answer that with 503
    before they call the service, and a read must not 500 for it."""


def confirm_refusal(
    db: Session,
    *,
    project: Project,
    build: Build | None,
    image_exists,
) -> Refusal | None:
    """the refusal a confirm of `build` would get, or `None`
    when it would be accepted. Returns the exception, never raises it, so the project read can
    report its `code` without performing anything. `confirm_deploy` raises exactly this.

    The order is the order of the spec's rules and of the 4xx a caller sees first: eval
    project, archived, the platform-wide Postgres precondition, then the build's own state
    (SUCCEEDED, deploy-eligible, the project's latest successful build, image present), and
    last the per-project serialization. `build=None` means the project has no SUCCEEDED build
    at all (only the project read asks that; a confirm always names one)."""
    # an eval project is a measurement, never a service. Refused first,
    # whoever asks and however healthy the build: nothing about it is promotable.
    if project.kind == ProjectKind.EVAL:
        return DeployConflictError(
            f"project {project.id} is an eval project; eval projects are never deployed",
            code=DeployRefusalCode.EVAL_PROJECT,
        )
    # An archived project serves nothing, and activation
    # leaves it ARCHIVED -- so confirming would put a live service on a
    # project every automatic path treats as dead (and that the
    # deprovision guard exists to rule out). Builds and change requests stay
    # allowed; only what serves traffic is refused.
    if project.status == ProjectStatus.ARCHIVED:
        return DeployProjectArchivedError(project.id, action="confirming a deploy")
    # ADR 0023 §Amendment 3: refused here, first, before any of the
    # build-specific checks below — a platform-wide precondition, checked
    # through `Settings.service_postgres_configured` (the ONE predicate this
    # and `execute_deployment` both call; see that property's docstring for
    # why the worker-side check is not belt-and-braces theatre).
    if not settings.service_postgres_configured:
        return DeployValidationError(
            "no SERVICE_POSTGRES_ADMIN_DSN is configured -- an operator must "
            "set it before any build can be promoted to L4: promotion is refused, "
            "never deployed without a database.",
            code=DeployRefusalCode.POSTGRES_UNCONFIGURED,
        )
    if build is None:
        return DeployValidationError(
            f"project {project.id} has no SUCCEEDED build to deploy",
            code=DeployRefusalCode.NO_SUCCESSFUL_BUILD,
        )
    if build.status != BuildStatus.SUCCEEDED:
        return DeployValidationError(
            f"build {build.id} is {build.status}, not SUCCEEDED",
            code=DeployRefusalCode.BUILD_NOT_SUCCEEDED,
        )
    if not is_deploy_eligible(build):
        return DeployValidationError(
            f"build {build.id} is not deploy-eligible (approval is not APPROVED/AUTO_APPROVED)",
            code=DeployRefusalCode.NOT_DEPLOY_ELIGIBLE,
        )
    latest = _latest_successful_build(db, project.id)
    if latest is None or latest.id != build.id:
        return DeployValidationError(
            f"build {build.id} is not the project's latest successful build"
            " (older eligible builds cannot be deployed forward; rollback is the only path backwards)",
            code=DeployRefusalCode.NOT_LATEST_SUCCESSFUL,
        )
    if not build.image_ref or not image_exists(build.image_ref):
        return DeployValidationError(
            f"build {build.id} has no available image to deploy",
            code=DeployRefusalCode.IMAGE_MISSING,
        )
    if get_in_flight_deployment(db, project.id) is not None:
        return _in_flight_conflict(project.id)
    return None


def confirm_deploy(
    db: Session,
    *,
    project: Project,
    build: Build,
    actor_id: str,
    image_exists,
) -> Deployment:
    """validates confirmability (`confirm_refusal`) and
    creates the `Deployment` row in DEPLOYING for the worker to claim. Does not
    commit — the router owns the transaction. `image_exists` is a
    `(image_ref) -> bool` callable (DeployManager.image_exists in
    production; injectable so validation is unit-testable without Docker).
    """
    refusal = confirm_refusal(db, project=project, build=build, image_exists=image_exists)
    if refusal is not None:
        raise refusal

    deployment = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=actor_id,
        status=DeploymentStatus.DEPLOYING,
    )
    _insert_deploying(db, deployment)  # assigns deployment.id for the audit target

    # "a deploy" — the owner/admin confirm action itself, not
    # the worker's later (automatic) execution of it. Rides the router's
    # own commit (does not commit here — see docstring).
    touch_last_active(project)

    write_audit_log(
        db,
        actor_id=actor_id,
        action="deployment.confirmed",
        target=_deployment_target(deployment.id),
        metadata={"project_id": project.id, "build_id": build.id},
    )
    return deployment


@dataclass(frozen=True)
class RollbackDecision:
    """What a rollback of the project would do right now: the SUPERSEDED deployment it would
    redeploy, or the refusal it would get. Exactly one is set."""

    target: Deployment | None
    refusal: Refusal | None


ROLLBACK_WINDOW = 3  # superseded builds a rollback may reach, per project


def rollback_window(db: Session, project_id: str, active_build_id: str | None) -> list[Deployment]:
    """The `SUPERSEDED` deployments a rollback of the project may target, newest first: one per
    build (the newest of that build's rows), at most `ROLLBACK_WINDOW` builds, and never the build
    that is ACTIVE right now (re-confirming it leaves a superseded row of the same build, which is
    not a target and must not use up a slot). The ONE definition: `decide_rollback` walks it, and
    the image keep-set (`services/image_retention.keep_build_ids`) keeps exactly these builds, so
    what a rollback may reach and what the sweep leaves are the same set by construction."""
    rows = db.scalars(
        select(Deployment)
        .where(
            Deployment.project_id == project_id,
            Deployment.status == DeploymentStatus.SUPERSEDED,
        )
        .order_by(Deployment.ended_at.desc())
    ).all()
    window: list[Deployment] = []
    seen: set[str] = set()
    for row in rows:
        if row.build_id == active_build_id or row.build_id in seen:
            continue
        seen.add(row.build_id)
        window.append(row)
        if len(window) == ROLLBACK_WINDOW:
            break
    return window


def decide_rollback(db: Session, *, project: Project, image_exists) -> RollbackDecision:
    """the target of a rollback -- the most recent `SUPERSEDED`
    deployment whose image still exists and whose build is not the one ACTIVE right now -- or
    why there is none. `request_rollback` acts on this; the project read reports it.

    an `ARCHIVED` project is refused, like confirm; and the target never carries the
    build that is ACTIVE right now -- re-confirming the active build leaves a SUPERSEDED row of
    that same build, and "rolling back" to it would redeploy what is already running while
    reporting a rollback."""
    if project.status == ProjectStatus.ARCHIVED:
        return RollbackDecision(
            None, DeployProjectArchivedError(project.id, action="rolling back")
        )
    if get_in_flight_deployment(db, project.id) is not None:
        return RollbackDecision(None, _in_flight_conflict(project.id))

    active = get_active_deployment(db, project.id)
    active_build_id = active.build_id if active is not None else None

    # only the rollback window is looked at -- at most `ROLLBACK_WINDOW`
    # superseded builds. A build past it has had its image swept, and is refused the same way as
    # any other missing image (the `NO_ROLLBACK_TARGET` below).
    for candidate in rollback_window(db, project.id, active_build_id):
        candidate_build = db.get(Build, candidate.build_id)
        if candidate_build and candidate_build.image_ref and image_exists(candidate_build.image_ref):
            return RollbackDecision(candidate, None)
    return RollbackDecision(
        None,
        DeployValidationError(
            f"project {project.id} has no previous (SUPERSEDED) deployment with an existing image "
            "to roll back to (the currently ACTIVE build is not a rollback target)",
            code=DeployRefusalCode.NO_ROLLBACK_TARGET,
        ),
    )


def request_rollback(
    db: Session,
    *,
    project: Project,
    actor_id: str,
    image_exists,
) -> Deployment:
    """creates a NEW Deployment with `rollback_of`
    set, targeting what `decide_rollback` names. Runtime-only — no git changes,
    `confirmed_ref` does not move (execute_deployment skips the merge step when
    `rollback_of` is set). Does not commit (on a lost race for the one DEPLOYING
    slot it does roll the session back -- see `_insert_deploying`)."""
    decision = decide_rollback(db, project=project, image_exists=image_exists)
    if decision.refusal is not None:
        raise decision.refusal
    target = decision.target
    assert target is not None  # exactly one of target / refusal is set

    deployment = Deployment(
        project_id=project.id,
        build_id=target.build_id,
        confirmed_by_id=actor_id,
        rollback_of=target.id,
        status=DeploymentStatus.DEPLOYING,
    )
    _insert_deploying(db, deployment)

    # "a rollback" — rides the router's own commit.
    touch_last_active(project)

    write_audit_log(
        db,
        actor_id=actor_id,
        action="deployment.rollback_requested",
        target=_deployment_target(deployment.id),
        metadata={
            "project_id": project.id,
            "build_id": target.build_id,
            "rollback_of": target.id,
        },
    )
    return deployment


@dataclass(frozen=True)
class DeployControls:
    """what the project's deploy controls may offer right now -- the state half only (who
    may act is the router's 403, a separate axis). Every field comes from the functions the
    actions use, never from a second copy of their rules.

    `confirmable_build_id` is the one build a confirm would accept (the project's latest
    SUCCEEDED build, when nothing refuses it), else `None` with `confirm_blocked_reason`, a
    `DeployRefusalCode`. `rollback_target_id` / `rollback_blocked_reason` likewise for a
    rollback. `active_deployment_id` is what a stop would stop. `in_flight` is whether anything
    is still moving -- a build QUEUED or RUNNING, or a deployment DEPLOYING -- so a page knows
    whether to keep reading."""

    confirmable_build_id: str | None
    confirm_blocked_reason: str | None
    rollback_target_id: str | None
    rollback_blocked_reason: str | None
    active_deployment_id: str | None
    in_flight: bool


def has_work_in_flight(db: Session, project_id: str) -> bool:
    """A build of the project is QUEUED or RUNNING (the worker will change it), or one of its
    deployments is DEPLOYING (the worker will settle it)."""
    if get_in_flight_deployment(db, project_id) is not None:
        return True
    return (
        db.scalars(
            select(Build.id)
            .where(
                Build.project_id == project_id,
                Build.status.in_([BuildStatus.QUEUED, BuildStatus.RUNNING]),
            )
            .limit(1)
        ).first()
        is not None
    )


def deploy_controls(db: Session, project: Project, *, image_exists) -> DeployControls:
    """The project read's deploy half. `image_exists` is the same
    `(image_ref) -> bool` the actions take. If asking it raises (no Docker client), the
    affected control is reported blocked with `DOCKER_UNAVAILABLE` rather than failing the
    read."""

    def checked(image_ref):
        try:
            return image_exists(image_ref)
        except Exception as exc:  # noqa: BLE001 - any failure of the seam, see _ImageCheckUnavailable
            raise _ImageCheckUnavailable() from exc

    latest = _latest_successful_build(db, project.id)
    confirmable: str | None = None
    try:
        refusal = confirm_refusal(db, project=project, build=latest, image_exists=checked)
        confirm_reason = None if refusal is None else refusal.code
        if refusal is None and latest is not None:
            confirmable = latest.id
    except _ImageCheckUnavailable:
        confirm_reason = DeployRefusalCode.DOCKER_UNAVAILABLE

    try:
        decision = decide_rollback(db, project=project, image_exists=checked)
        rollback_target = decision.target
        rollback_reason = None if decision.refusal is None else decision.refusal.code
    except _ImageCheckUnavailable:
        rollback_target, rollback_reason = None, DeployRefusalCode.DOCKER_UNAVAILABLE

    active = get_active_deployment(db, project.id)
    return DeployControls(
        confirmable_build_id=confirmable,
        confirm_blocked_reason=confirm_reason,
        rollback_target_id=rollback_target.id if rollback_target is not None else None,
        rollback_blocked_reason=rollback_reason,
        active_deployment_id=active.id if active is not None else None,
        in_flight=has_work_in_flight(db, project.id),
    )


def stop_active_deployment(
    db: Session,
    *,
    project: Project,
    actor_id: str,
    manager,
    touch_activity: bool = True,
) -> Deployment:
    """route removed (dev-mode routing resolves the
    ACTIVE row, so the DB transition IS the route removal), container
    stopped and removed, ACTIVE -> STOPPED. Synchronous — a stop is one
    Docker remove, not a multi-step health-gated operation, so it runs in
    the API request. Does not commit.

    `touch_activity` (see this module's own docstring):
    `True` (default) for every human-facing caller
    (`routers/deployments.py`) — a person stopping a deployment is
    activity. The idle-project sweep passes `False`: the platform stopping
    a deployment it just flagged as idle is not new evidence of use, and
    must not reset the very staleness that made the project a candidate.
    """
    active = get_active_deployment(db, project.id)
    if active is None:
        raise DeployValidationError(f"project {project.id} has no ACTIVE deployment to stop")

    # `remove_service` says whether anything is left. STOPPED is still
    # recorded either way (the route is the DB row in dev mode, and the owner
    # asked for a stop), but a container that survived is never claimed gone:
    # ERROR in the log, and the stop's own audit entry says so.
    removed = manager.remove_service(active.id)
    removal_failed = removed is False
    if removal_failed:
        logger.error(
            "deployment %s: stopped, but its container or network was NOT removed and may "
            "still be running on the host",
            active.id,
        )
    active.status = DeploymentStatus.STOPPED
    active.ended_at = _utcnow()

    if touch_activity:
        # "a stop" — rides the router's own commit.
        touch_last_active(project)

    metadata = {"project_id": project.id, "build_id": active.build_id}
    if removal_failed:
        metadata["container_removed"] = False
    write_audit_log(
        db,
        actor_id=actor_id,
        action="deployment.stopped",
        target=_deployment_target(active.id),
        metadata=metadata,
    )
    return active


# -- deployment queue (worker side; commits, mirrors build_queue) --------


def claim_next_deployment(db: Session) -> Deployment | None:
    """Claims the oldest unclaimed DEPLOYING row (started_at is NULL) and
    stamps started_at. Same short-transaction claim discipline as
    `build_queue.claim_next_build`; per-project serialization is
    already guaranteed at creation time (confirm/rollback reject while one
    is in flight), so at most one row per project can ever be here."""
    deployment = db.scalars(
        select(Deployment)
        .where(
            Deployment.status == DeploymentStatus.DEPLOYING,
            Deployment.started_at.is_(None),
        )
        .order_by(Deployment.created_at.asc())
    ).first()
    if deployment is None:
        return None

    deployment.started_at = _utcnow()
    write_audit_log(
        db,
        actor_id=SYSTEM_ACTOR_ID,
        action="deployment.started",
        target=_deployment_target(deployment.id),
        metadata={"project_id": deployment.project_id, "build_id": deployment.build_id},
    )
    db.commit()
    db.refresh(deployment)
    return deployment


# ADR 0037 §Decision 5-6: why a deployment was failed by its own worker going away,
# recorded as `cause` on the `deployment.failed` audit entry. The failure REASON is
# INFRA_ERROR in both cases (the taxonomy has no code for a restarted worker).
CAUSE_WORKER_RESTARTED = "worker restarted"
CAUSE_WORKER_STOPPED = "worker stopped"


def fail_orphaned_deployments(db: Session) -> list[Deployment]:
    """ADR 0037 §Decision 5 -- **the single-worker assumption lives here too** (see
    `build_queue.fail_orphaned_builds`). When the one worker process starts, a deployment it
    had CLAIMED (`DEPLOYING` with `started_at` set) belongs to a process that is gone: it can
    never finish, and it blocks its project (one `DEPLOYING` row per project). Each is failed
    INFRA_ERROR with the audit cause `worker restarted`; the reconciler's pass that follows
    removes its container and network (the deployment is no longer `DEPLOYING`).

    **An unclaimed `DEPLOYING` row (`started_at` NULL) is not touched:** this table is the
    deployments' queue (`claim_next_deployment`), so that row is a confirm waiting for a
    worker, made perhaps while this one was down, and failing it would throw the owner's
    request away. Same reading as `sweep_stale_deployments`, which also looks only at claimed
    rows.

    With more than one worker this must become a lease first. Call once, at worker start,
    before anything is claimed."""
    claimed = db.scalars(
        select(Deployment).where(
            Deployment.status == DeploymentStatus.DEPLOYING,
            Deployment.started_at.is_not(None),
        )
    ).all()
    return [
        fail_deployment(
            db, deployment, DeploymentFailureReason.INFRA_ERROR, cause=CAUSE_WORKER_RESTARTED
        )
        for deployment in claimed
    ]


def sweep_stale_deployments(
    db: Session, *, max_duration_minutes: int, manager=None, manager_factory=None
) -> list[Deployment]:
    """Fails any claimed DEPLOYING row older than the max duration (reuses
    `build_max_duration_minutes` — a deployment is far faster than a build,
    so the build ceiling is a generous stuck-detector). Mirrors
    `build_queue.sweep_timed_out_builds`: a crashed worker never leaves a
    deployment stuck in DEPLOYING forever, which would also block the
    project's per-deployment serialization.

    a row is stale because its worker died somewhere between creating
    the deployment's network and activating it, so a container and a network
    may be sitting on the host for it. Each stale row's container and network
    are removed before the row is failed -- "a failed deployment leaves no
    network behind, whatever its reason" -- through `manager`, or through the
    one `manager_factory()` builds the first time something is actually stale
    (so a quiet tick never constructs a Docker client). A removal that fails,
    or a manager that cannot be built (Docker unreachable), is logged and
    recorded on the FAILED entry; it never stops the row from being failed.
    With neither argument the sweep only fails rows, as before."""
    cutoff = _utcnow() - timedelta(minutes=max_duration_minutes)
    stale = db.scalars(
        select(Deployment).where(
            Deployment.status == DeploymentStatus.DEPLOYING,
            Deployment.started_at.is_not(None),
            Deployment.started_at < cutoff,
        )
    ).all()
    manager_unavailable = False
    if stale and manager is None and manager_factory is not None:
        try:
            manager = manager_factory()
        except Exception:  # noqa: BLE001 - Docker unreachable must not stop the rows being failed
            logger.exception("stale-deployment sweep: no deploy manager; cannot remove their containers")
            manager_unavailable = True
    for deployment in stale:
        clean = not manager_unavailable and (manager is None or _remove_runtime(manager, deployment.id))
        fail_deployment(
            db, deployment, DeploymentFailureReason.INFRA_ERROR, cleanup_incomplete=not clean
        )
    return stale


# -- execution (worker side; commits) -------------------------------------


class _MergeFailed(Exception):
    """Internal: merge step failed; maps to MERGE_FAILED."""


def execute_deployment(
    db: Session,
    deployment: Deployment,
    *,
    manager,
    git_provider: GitProvider | None = None,
) -> Deployment:
    """Runs one claimed deployment to a terminal state (deployment
    lifecycle):

    0. Data services configured at all? (PROVISION_UNCONFIGURED otherwise,
       before anything else — the worker-side half of the same refusal
       `confirm_deploy` already gave at the API seam; see
       `Settings.service_postgres_configured`'s docstring for why this is
       not belt-and-braces theatre.)
    1. Merge preflight: `diff_ref`, the PR's state, a merged PR
       that names no commit — all refused (MERGE_FAILED) BEFORE anything
       touches L4, with no provider write. Skipped for rollbacks (runtime-only,
       they never merge).
    2. Image still present? (IMAGE_MISSING otherwise.)
    3. Provision (or reuse) the project's data services and start the new
       container with the service env injected (PROVISION_FAILED on a
       provisioning failure, START_FAILED on any other start failure).
    4. Health-gate: GET /healthz -> 200 within the window (HEALTH_TIMEOUT;
       new container removed, old deployment untouched and still routed).
    4b. (prod shape only) make the now-healthy container's Traefik
        route live — attach the Traefik container to the deployment's own
        network (ROUTE_FAILED on failure; new container removed, old
        deployment untouched and still routed) — no-op when Traefik routing
        is disabled.
    4c. Merge the build's PR + advance `projects.confirmed_ref` —
        only now, so a failed deployment leaves the PR open and the ref
        unchanged. MERGE_FAILED here removes the new container; the old
        deployment stays ACTIVE and routed. Skipped for rollbacks.
    5. Route flip: old ACTIVE -> SUPERSEDED + new -> ACTIVE in one
       transaction, then the old container is removed (best-effort — the
       route already points at the new one; under Traefik this also drops
       the old container's route, since removal drops all of its network
       memberships).
    6. On activation: tear down the build's preview (ADR 0016 teardown
       trigger: promotion).

    `manager` is a `DeployManager` (injectable for tests)."""
    # Read before anything can leave the session unusable: the outer handler
    # below logs and cleans up by id, and touching an expired ORM attribute on a
    # session that needs its rollback would raise from inside the handler.
    deployment_id = deployment.id
    project = db.get(Project, deployment.project_id)
    build = db.get(Build, deployment.build_id)
    if project is None or build is None:
        return fail_deployment(db, deployment, DeploymentFailureReason.INFRA_ERROR)

    try:
        # ADR 0023 §Amendment 3: the worker is a SEPARATE process
        # from the API that accepted the confirm (worker.py constructs its
        # own `Settings`), so a config change after confirm but before the
        # worker claims this row would otherwise be invisible to it here —
        # checked again, unconditionally, before touching anything else.
        if not settings.service_postgres_configured:
            return fail_deployment(db, deployment, DeploymentFailureReason.PROVISION_UNCONFIGURED)

        merge_plan: _MergePlan | None = None
        if deployment.rollback_of is None:
            try:
                merge_plan = _preflight_merge(db, project, build, git_provider)
            except _MergeFailed as exc:
                logger.warning("deployment %s: merge refused: %s", deployment.id, exc)
                return fail_deployment(db, deployment, DeploymentFailureReason.MERGE_FAILED)

        if not manager.image_exists(build.image_ref):
            return fail_deployment(db, deployment, DeploymentFailureReason.IMAGE_MISSING)

        try:
            env = ensure_service_env(
                db, project, docker_client=getattr(manager, "_client", None)
            )
        except ServiceDataProvisionError as exc:
            logger.warning(
                "deployment %s: data-service provisioning failed: %s", deployment.id, exc
            )
            return fail_deployment(db, deployment, DeploymentFailureReason.PROVISION_FAILED)
        db.commit()  # persist first-issue service env before the long-running part

        try:
            manager.start_service(
                project_id=project.id,
                deployment_id=deployment.id,
                build_id=build.id,
                image_ref=build.image_ref,
                env=env,
            )
        except DeployDataServiceError as exc:
            logger.warning(
                "deployment %s: data-service network attach failed: %s", deployment.id, exc
            )
            return fail_deployment(db, deployment, DeploymentFailureReason.PROVISION_FAILED)
        except Exception as exc:  # noqa: BLE001 - any other start failure is START_FAILED
            logger.warning("deployment %s: container start failed: %s", deployment.id, exc)
            return fail_deployment(db, deployment, DeploymentFailureReason.START_FAILED)

        if not manager.wait_healthy(deployment.id):
            clean = _remove_runtime(manager, deployment.id)
            return fail_deployment(
                db,
                deployment,
                DeploymentFailureReason.HEALTH_TIMEOUT,
                cleanup_incomplete=not clean,
            )

        # Routing (prod): only a container that already
        # passed the health gate ever gets its Traefik route made live
        # (no-op when Traefik routing is disabled). A failure here is
        # ROUTE_FAILED per the same taxonomy as the dev-mode route flip
        # below — new container removed, old deployment untouched and still
        # routed.
        try:
            manager.connect_to_traefik(deployment.id)
        except Exception as exc:  # noqa: BLE001 - any attach failure is ROUTE_FAILED
            logger.warning("deployment %s: traefik route attach failed: %s", deployment.id, exc)
            clean = _remove_runtime(manager, deployment.id)
            return fail_deployment(
                db,
                deployment,
                DeploymentFailureReason.ROUTE_FAILED,
                cleanup_incomplete=not clean,
            )

        # the merge, and so `confirmed_ref`, comes after the container is healthy and
        # routed and before the flip (an ACTIVE service is never unmerged). A failure leaves
        # `main`/`confirmed_ref` as they were, the PR open, and the previous deployment routed.
        if merge_plan is not None:
            try:
                _merge_and_advance_confirmed_ref(db, project, build, merge_plan)
            except _MergeFailed as exc:
                logger.warning("deployment %s: merge failed: %s", deployment.id, exc)
                clean = _remove_runtime(manager, deployment.id)
                return fail_deployment(
                    db,
                    deployment,
                    DeploymentFailureReason.MERGE_FAILED,
                    cleanup_incomplete=not clean,
                )

        old_active = get_active_deployment(db, project.id)
        try:
            activated = _activate(db, deployment, old_active)
        except Exception as exc:  # noqa: BLE001 - flip failure leaves the old deployment routed
            logger.warning("deployment %s: route flip failed: %s", deployment.id, exc)
            db.rollback()
            clean = _remove_runtime(manager, deployment.id)
            return fail_deployment(
                db,
                deployment,
                DeploymentFailureReason.ROUTE_FAILED,
                cleanup_incomplete=not clean,
            )
        if not activated:
            # ADR 0038 §Decision 6: the row is no longer DEPLOYING -- another process
            # resolved it while this one was starting the container (a second worker's start-up
            # recovery, the stale sweep). It is not this process's row to write any more: leave
            # it exactly as that process left it, and remove the container this one started so
            # nothing is left routed or running for a deployment the database calls finished.
            logger.warning(
                "deployment %s: no longer DEPLOYING at activation; removing the container this "
                "worker started and leaving the row alone",
                deployment_id,
            )
            _remove_runtime(manager, deployment_id)
            db.refresh(deployment)
            return deployment

        # From here the deployment is ACTIVE and committed: nothing below may
        # turn it into a failure (the outer handler would otherwise tear down a
        # service that is routed and serving). Both steps never raise.
        if old_active is not None:
            _remove_superseded_container(db, manager, old=old_active, new=deployment)
        _teardown_preview_on_promotion(db, build, manager)
        return deployment
    except Exception:  # noqa: BLE001 - any unexpected infrastructure error must still resolve the row
        logger.exception("deployment %s: unexpected failure", deployment_id)
        return _fail_in_flight_deployment(db, deployment, manager)
    except BaseException:  # noqa: BLE001 - re-raised below; the row must be resolved for ANY exit
        # ADR 0037 §Decision 6: `SystemExit` from the worker's shutdown handler (a
        # SIGTERM), or a `KeyboardInterrupt`. Neither is an `Exception`, so the handler above
        # never saw them, and the deployment stayed DEPLOYING with its container and network
        # on the host. The normal failure teardown runs, then the interrupt goes on its way:
        # a failure inside this cleanup is logged and never replaces it (the worker is
        # stopping either way, and the next start fails the row and reaps its objects).
        logger.warning("deployment %s: interrupted; failing it before the worker stops", deployment_id)
        try:
            _fail_in_flight_deployment(db, deployment, manager, cause=CAUSE_WORKER_STOPPED)
        except Exception:  # noqa: BLE001 - never replace the interrupt being handled
            logger.exception("deployment %s: failing it after an interrupt raised", deployment_id)
        raise


def _fail_in_flight_deployment(
    db: Session, deployment: Deployment, manager, *, cause: str | None = None
) -> Deployment:
    """`execute_deployment`'s one failure teardown for something that went wrong while it
    held the row (an unexpected exception, an interrupt).

    the exception may be the database's own (a failed flush or commit leaves the
    session unusable until it is rolled back), in which case `fail_deployment`'s first
    statement would raise again and the row would stay DEPLOYING until the stale sweep. Roll
    back first, so the FAILED transition runs on a clean session.

    If the rollback or the re-read below raises, the database itself is gone: nothing can be
    recorded and the row's state is unknown, so nothing destructive is done either -- the
    exception propagates (the worker loop logs it and carries on) and the stale sweep fails
    the row, and removes its container and network, once the database is back."""
    db.rollback()
    db.refresh(deployment)
    # The row may already be ACTIVE (something raised after the commit that
    # activated it): never rewrite a live or terminal row to FAILED, and
    # never remove the container of a service that is routed and serving.
    if deployment.status != DeploymentStatus.DEPLOYING:
        logger.error(
            "deployment %s: unexpected failure after it reached %s; leaving it as it is",
            deployment.id,
            deployment.status,
        )
        return deployment
    clean = _remove_runtime(manager, deployment.id)
    return fail_deployment(
        db,
        deployment,
        DeploymentFailureReason.INFRA_ERROR,
        cleanup_incomplete=not clean,
        cause=cause,
    )


@dataclass(frozen=True)
class _MergePlan:
    """What `_preflight_merge` learned, handed to `_merge_and_advance_confirmed_ref`."""

    provider: GitProvider
    repo: object
    pr_number: int
    already_merged: bool
    merge_sha: str | None  # the provider's merge commit when already merged; else None


def _preflight_merge(
    db: Session, project: Project, build: Build, git_provider: GitProvider | None
) -> _MergePlan:
    """everything about the merge that can
    be refused BEFORE a container starts, so a refusal leaves L4 untouched. Reads the provider
    (create-or-return repo lookup, PR state); writes nothing to the provider or the database.

    The provider reports the PR as `open`, `merged` or `closed` (unmerged), and each is its
    own case ("Not open is not merged"):
    - `open`: the merge itself happens later, after the health gate.
    - `merged`: the merge already happened (re-deploying the same build after a stop, a retry
      after a failure that came after the merge, or a retry after the worker died between the
      provider's merge and the database commit). Nothing is merged again.
    - `closed`: closed without merging. `MERGE_FAILED`: unmerged code never goes live.
    Any other state is a failure too, never a pass. A merged PR that names no merge commit is
    refused here only when the project has no confirmed ref to keep.
    Raises `_MergeFailed`."""
    if not build.diff_ref or "#" not in build.diff_ref:
        raise _MergeFailed(f"build {build.id} has no mergeable diff_ref ({build.diff_ref!r})")

    _, _, pr_number_str = build.diff_ref.rpartition("#")
    try:
        pr_number = int(pr_number_str)
    except ValueError:
        raise _MergeFailed(f"build {build.id} diff_ref has no PR number ({build.diff_ref!r})")

    try:
        provider = git_provider or create_provider(settings)
        repo = provider.create_repo(name=project.id)  # create-or-return lookup
        pr = provider.get_pr_state(repo=repo, pr_number=pr_number)
        pr_state, pr_merge_commit = pr.state, pr.merge_commit  # a bare-string provider fails here
    except Exception as exc:  # noqa: BLE001 - provider/config errors (incl. GitProviderError) are all merge failures
        raise _MergeFailed(str(exc)) from exc

    if pr_state == PR_OPEN:
        return _MergePlan(provider, repo, pr_number, already_merged=False, merge_sha=None)
    if pr_state == PR_MERGED:
        if not pr_merge_commit and not project.confirmed_ref:
            # A merge the provider cannot name a commit for (a record written before merge
            # commits were kept), and nothing to keep: the next modify build would stage an
            # EMPTY workspace, so it fails closed instead.
            raise _MergeFailed(
                f"PR #{pr_number} for build {build.id} is merged, but the provider names no "
                "merge commit and the project has no confirmed ref to keep"
            )
        return _MergePlan(
            provider, repo, pr_number, already_merged=True, merge_sha=pr_merge_commit
        )
    if pr_state == PR_CLOSED:
        raise _MergeFailed(f"PR #{pr_number} for build {build.id} was closed without being merged")
    raise _MergeFailed(
        f"PR #{pr_number} for build {build.id} is in an unrecognised state {pr_state!r}"
    )


def _merge_and_advance_confirmed_ref(
    db: Session, project: Project, build: Build, plan: _MergePlan
) -> None:
    """merge the build's PR (when it is still open) and advance
    `projects.confirmed_ref` to the merge result, committed. Runs AFTER the new container is
    healthy and routed and BEFORE the activation flip, so `confirmed_ref` only ever names a
    tree that passed the health gate and was about to become ACTIVE.

    `plan` comes from `_preflight_merge`. When the PR was already merged,
    `confirmed_ref` is set from the provider's merge commit unless it already is that commit,
    so a retry never leaves it behind or unset. "Already contains it" is tested as equality:
    this provider interface has no ancestry question to ask, and confirm only accepts the
    project's latest successful build, so the one way a merged PR can meet a `confirmed_ref`
    that differs from its merge commit is a `confirmed_ref` that is BEHIND it. A merged PR
    that names no merge commit keeps an existing `confirmed_ref` (preflight refused the case
    with none).
    Raises `_MergeFailed` for any provider error; a database failure propagates."""
    previous_ref = project.confirmed_ref
    pr_number = plan.pr_number
    already_merged = plan.already_merged
    merge_sha = plan.merge_sha
    if not already_merged:
        try:
            merge_sha = plan.provider.merge_pr(repo=plan.repo, pr_number=pr_number)
        except Exception as exc:  # noqa: BLE001 - provider errors (incl. GitProviderError) are all merge failures
            raise _MergeFailed(str(exc)) from exc

    if already_merged:
        if not merge_sha:
            logger.warning(
                "deployment merge: PR #%s for build %s is merged but names no merge commit; "
                "confirmed_ref left as it is",
                pr_number,
                build.id,
            )
            return
        if previous_ref == merge_sha:
            logger.info(
                "deployment merge skipped: PR #%s for build %s is already merged and "
                "confirmed_ref already is its merge commit",
                pr_number,
                build.id,
            )
            return
        logger.warning(
            "deployment merge: PR #%s for build %s is already merged but confirmed_ref is not "
            "its merge commit; setting it",
            pr_number,
            build.id,
        )

    project.confirmed_ref = merge_sha
    metadata = {"build_id": build.id, "confirmed_ref": merge_sha}
    if already_merged:
        # A recovery, not a merge made now: say so, and what it replaced.
        metadata["already_merged"] = True
        metadata["previous_confirmed_ref"] = previous_ref
    write_audit_log(
        db,
        actor_id=SYSTEM_ACTOR_ID,
        action="deployment.merged",
        target=f"project:{project.id}",
        metadata=metadata,
    )
    db.commit()  # confirmed_ref advances only once the container is healthy and routed


def _activate(db: Session, deployment: Deployment, old_active: Deployment | None) -> bool:
    """The route flip: one transaction supersedes the old ACTIVE row and
    activates the new one (the partial unique index on deployments enforces
    at most one ACTIVE per project at the DB level too).

    **ADR 0038 §Decision 6: activation is a compare-and-set.** The new row moves to
    ACTIVE only if the DATABASE still has it DEPLOYING -- one conditional `UPDATE`, whose row
    count is the answer, never the in-memory status (the object may be stale: another process
    may have failed the row while this one was running the container's health gate). When it is
    not, the whole transaction is rolled back (the old row stays ACTIVE, no audit entry, no
    project transition) and the result is False: the caller removes the container it started
    and leaves the row alone. True means activated and committed."""
    now = _utcnow()
    if old_active is not None:
        old_active.status = DeploymentStatus.SUPERSEDED
        old_active.ended_at = now
        write_audit_log(
            db,
            actor_id=SYSTEM_ACTOR_ID,
            action="deployment.superseded",
            target=_deployment_target(old_active.id),
            metadata={"project_id": old_active.project_id, "superseded_by": deployment.id},
        )
        # Flush the SUPERSEDED update before ACTIVE is set: the partial
        # unique index (one ACTIVE per project) is enforced per statement,
        # so the old row must leave ACTIVE first — still one transaction.
        db.flush()

    won = db.execute(
        update(Deployment)
        .where(Deployment.id == deployment.id, Deployment.status == DeploymentStatus.DEPLOYING)
        .values(status=DeploymentStatus.ACTIVE),
        execution_options={"synchronize_session": False},
    ).rowcount
    if won != 1:
        db.rollback()  # undoes the supersede above: the row that was serving still is
        return False
    db.expire(deployment, ["status"])  # the UPDATE above bypassed the object
    write_audit_log(
        db,
        actor_id=SYSTEM_ACTOR_ID,
        action="deployment.activated",
        target=_deployment_target(deployment.id),
        metadata={"project_id": deployment.project_id, "build_id": deployment.build_id},
    )
    # in the same
    # transaction as the activation. Forward-only, so a rollback or
    # re-deploy onto an already-ACTIVE project is a no-op, and an ARCHIVED
    # project stays ARCHIVED.
    project = db.get(Project, deployment.project_id)
    if project is not None:
        advance(db, project, ProjectStatus.ACTIVE, cause=f"deployment:{deployment.id}")
    db.commit()
    return True


def _remove_runtime(manager, deployment_id: str) -> bool:
    """best-effort removal of one deployment's container and network on a
    failure path. True when nothing is left. Never raises -- it runs where the
    deployment is about to be failed, and that transition must still happen --
    but never silent either: a removal that did not happen is logged at ERROR
    here (the manager says what is left), and the caller records it on the
    `deployment.failed` audit entry."""
    try:
        removed = manager.remove_service(deployment_id)
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("deployment %s: removing its container/network raised", deployment_id)
        return False
    if removed is False:
        logger.error(
            "deployment %s: its container or network was not removed and may still be on the host",
            deployment_id,
        )
        return False
    return True


def _remove_superseded_container(db: Session, manager, *, old: Deployment, new: Deployment) -> None:
    """After the route flip the route already resolves to the new ACTIVE row;
    removing the old container is cleanup. It cannot fail the new deployment,
    but a removal that did not happen is not left to look like one that
    did: the old row is SUPERSEDED either way, so the failure is an ERROR in the
    log and its own audit entry on the old deployment. Never raises."""
    old_id, new_id, project_id = old.id, new.id, new.project_id
    if _remove_runtime(manager, old_id):
        return
    try:
        write_audit_log(
            db,
            actor_id=SYSTEM_ACTOR_ID,
            action="deployment.container_removal_failed",
            target=_deployment_target(old_id),
            metadata={"project_id": project_id, "superseded_by": new_id, "container_removed": False},
        )
        db.commit()
    except Exception:  # noqa: BLE001 - the ERROR above is already logged; do not fail an ACTIVE deployment
        logger.exception("deployment %s: recording the failed removal raised", old_id)
        db.rollback()


def _teardown_preview_on_promotion(db: Session, build: Build, manager) -> None:
    """ADR 0016 teardown trigger: promotion. Best-effort — a preview that
    fails to tear down never fails an already-ACTIVE deployment; the TTL
    sweep remains the backstop. Reuses the DeployManager's Docker client
    when it has one (fakes in unit tests don't — and unit-test builds have
    no preview fields, so this is never reached there)."""
    if not build.preview_token_hash and not build.preview_url:
        return
    try:
        from app.services.build_queue import clear_preview_fields
        from app.services.preview_manager import PreviewManager

        PreviewManager(client=getattr(manager, "_client", None)).stop_preview(build.id)
        clear_preview_fields(db, build.id)
    except Exception:  # noqa: BLE001 - the TTL sweep is the backstop
        logger.exception("preview teardown on promotion failed for build %s", build.id)


def fail_deployment(
    db: Session,
    deployment: Deployment,
    reason: str,
    *,
    cleanup_incomplete: bool = False,
    cause: str | None = None,
) -> Deployment:
    """Terminal FAILED transition with one reason from the deployment
    failure taxonomy. Public: the worker also uses it when the
    DeployManager itself cannot be constructed (Docker unreachable).

    `cleanup_incomplete`: the caller tried to remove the deployment's
    container and network and something was left. It is recorded on the audit
    entry (`container_removed: false`) so the row does not read as a clean
    failure; absent when the cleanup was complete.

    `cause`: a short platform-authored phrase saying why the row was failed when
    `reason` alone under-describes it (`CAUSE_WORKER_RESTARTED`, `CAUSE_WORKER_STOPPED`);
    recorded on the audit entry as `cause`, absent when there is none. A deployment has no
    detail column, so the audit entry is where it lives."""
    deployment.status = DeploymentStatus.FAILED
    deployment.failure_reason = reason
    deployment.ended_at = _utcnow()
    metadata = {
        "project_id": deployment.project_id,
        "build_id": deployment.build_id,
        "reason": reason,
    }
    if cleanup_incomplete:
        metadata["container_removed"] = False
    if cause is not None:
        metadata["cause"] = cause
    write_audit_log(
        db,
        actor_id=SYSTEM_ACTOR_ID,
        action="deployment.failed",
        target=_deployment_target(deployment.id),
        metadata=metadata,
    )
    db.commit()
    db.refresh(deployment)
    return deployment
