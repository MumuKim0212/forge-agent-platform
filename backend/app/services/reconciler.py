"""The label-driven reconciler (ADR 0037): compares the Docker objects that carry the
platform's labels with what the database says should exist, and removes or restores the
difference. **It acts only on objects it can positively identify as its own.**

What "its own" means, in code: an object is looked at only when its `forge.instance` label is
THIS installation's id (the Docker list is filtered by it, and the label is checked again
on every object that comes back) and its `forge.role` is one of `platform_labels.Role`. An
object with another instance id, with no instance label, with no role or with a role this
module does not know is never examined, let alone touched. No volume is ever removed, and the
shared Postgres is not touched: nothing here names either.

What it does, by role (ADR 0037 §Decision 3):

* `sandbox`, `llm-proxy`, `build-network` -- removed when the build they name is not `RUNNING`
  (or does not exist). A worker that dies mid-build skips every `finally`: the sandbox keeps
  running and the proxy keeps the real API key. The proxy's per-build token lives only in that
  container's memory and the database never held it, so there is nothing to revoke from here:
  removing the container is what revokes it.
* `deploy`, `deploy-network` -- removed when the deployment they name is neither `ACTIVE` nor
  an in-progress `DEPLOYING`. Anything attached to the network (Traefik, the shared Postgres,
  the project's Redis) is detached first, Traefik before the container goes; the attached
  containers themselves are never removed.
* `preview`, `preview-redis`, `preview-network` -- removed when the build's preview is no
  longer live (`preview_expires_at` is NULL, or the build is gone). This complements the TTL
  sweep, which clears those fields even when its own teardown failed. The preview's ephemeral
  Postgres database is not dropped here: `worker.sweep_orphaned_preview_stores` owns it.
* `service-redis` -- **never removed.** It is the project's cache and is created by
  `service_data.ensure_project_redis`; the reconciler re-ensures it, through that same
  function, for every project with an `ACTIVE` deployment (started if stopped, recreated if
  missing) and attaches it to the ACTIVE deployment's network. A project whose stored
  `REDIS_URL` is an older one (no password) is left alone: replacing that container changes
  the credential a running service holds, which is a deployment's decision, not a background
  repair's.

"In progress" and "RUNNING" are read as "the worker that is calling me is working on it".
That is true by construction: the worker is one process that runs one build or deployment at
a time, to completion, inside its loop, and reconciles only between those. At worker start
nothing is in progress (`worker.recover_orphaned_rows` has already failed what a dead process
left), and in the periodic pass nothing of this process's own is in flight. This is the
single-worker assumption; with a second worker it would have to become a lease.

It never raises and never fails a request: it is background repair. Every removal and restore
is logged, and audit-logged (no actor; the row ids are in the target and metadata).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from docker.errors import APIError, DockerException, NotFound
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.services import platform_labels, service_data
from app.services.audit import write_audit_log
from app.services.deploy_manager import DEPLOY_CONTAINER_PREFIX
from app.services.platform_labels import (
    LABEL_BUILD_ID,
    LABEL_DEPLOYMENT_ID,
    LABEL_INSTANCE,
    LABEL_PROJECT_ID,
    LABEL_ROLE,
    Role,
)
from app.services.service_env_crypto import ServiceEnvDecryptionError, decrypt_service_env
from app.services.status import BuildStatus

logger = logging.getLogger("app.reconciler")

# Worker-driven, no human actor (the same idiom as build_queue / deploy).
SYSTEM_ACTOR_ID: str | None = None

_BUILD_ROLES = (Role.SANDBOX, Role.LLM_PROXY, Role.BUILD_NETWORK)
_PREVIEW_ROLES = (Role.PREVIEW, Role.PREVIEW_REDIS, Role.PREVIEW_NETWORK)
_DEPLOY_ROLES = (Role.DEPLOY, Role.DEPLOY_NETWORK)
# Roles whose objects are networks; every other role is a container.
_NETWORK_ROLES = (Role.BUILD_NETWORK, Role.PREVIEW_NETWORK, Role.DEPLOY_NETWORK)
# Roles the reconciler removes nothing of. A role listed here is skipped before any rule
# is consulted, so no future rule can remove it by falling through.
_NEVER_REMOVED = (Role.SERVICE_REDIS,)


@dataclass
class ReconcileReport:
    """What one pass did. `removed` and `restored` are human-readable lines (also logged);
    `failed` is what it tried and could not do; `unavailable` is set when Docker could not
    be reached at all, in which case nothing was examined."""

    removed: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    unavailable: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.removed or self.restored)


@dataclass(frozen=True)
class _Facts:
    """The database state one pass decides from, read once for the ids the Docker objects
    name. An id missing from a set or dict is an id with no row."""

    running_builds: frozenset[str]
    live_previews: frozenset[str]
    deployment_status: dict[str, str]


def removal_reason(role: str, labels: dict[str, str], facts: _Facts) -> str | None:
    """Why an object of a known, removable role may not exist any more, or None when it may.
    Pure: the decision, separated from the Docker calls so each rule is testable on its own.
    An object that names no row at all belongs to no RUNNING build, no live preview and no
    ACTIVE deployment, so the same rule removes it."""
    if role in _BUILD_ROLES:
        build_id = labels.get(LABEL_BUILD_ID)
        if build_id is None:
            return "it names no build, so no RUNNING build owns it"
        if build_id not in facts.running_builds:
            return f"build {build_id} is not RUNNING"
        return None
    if role in _PREVIEW_ROLES:
        build_id = labels.get(LABEL_BUILD_ID)
        if build_id is None:
            return "it names no build, so no live preview owns it"
        if build_id not in facts.live_previews:
            return f"build {build_id} has no live preview"
        return None
    if role in _DEPLOY_ROLES:
        deployment_id = labels.get(LABEL_DEPLOYMENT_ID)
        if deployment_id is None:
            return "it names no deployment, so no ACTIVE or in-progress deployment owns it"
        status = facts.deployment_status.get(deployment_id)
        if status in (DeploymentStatus.ACTIVE, DeploymentStatus.DEPLOYING):
            return None
        if status is None:
            return f"deployment {deployment_id} does not exist"
        return f"deployment {deployment_id} is {status}"
    return None


@dataclass
class _Object:
    kind: str  # "container" | "network"
    handle: object  # the docker-py Container / Network
    name: str
    role: str
    labels: dict[str, str]


class Reconciler:
    """One per worker. `client_factory` builds the Docker client the first time something
    needs it, so a worker on a host with no Docker still starts (a pass then reports
    `unavailable` and logs once per pass); `client` injects one for tests."""

    def __init__(
        self,
        *,
        client=None,
        client_factory: Callable[[], object] | None = None,
        instance_id: str | None = None,
    ) -> None:
        self._client = client
        self._client_factory = client_factory
        self._instance_id = instance_id
        # A worker on a host with no Docker (the placeholder executor, a dev laptop) would
        # otherwise log the same warning every interval for ever: the first skipped pass says
        # so at WARNING, the ones after it at DEBUG, and Docker coming back is logged once.
        self._skip_logged = False
        # The per-project conditions below persist until a deployment changes them (a legacy
        # Redis URL stays legacy), so a pass every interval would repeat the same warning for ever:
        # each is warned about once per process, then logged at DEBUG.
        self._warned: set[str] = set()

    # -- plumbing ---------------------------------------------------------

    @property
    def instance_id(self) -> str:
        return self._instance_id or platform_labels.instance_id()

    def _docker(self):
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                import docker

                self._client = docker.from_env()
        return self._client

    def _warn_once(self, key: str, message: str, *args) -> None:
        level = logging.DEBUG if key in self._warned else logging.WARNING
        self._warned.add(key)
        logger.log(level, message, *args)

    # -- the pass ---------------------------------------------------------

    def reconcile(self, db: Session) -> ReconcileReport:
        """One full pass: reap, then re-ensure the service Redis. Never raises."""
        report = ReconcileReport()
        try:
            client = self._docker()
            instance = self.instance_id
        except Exception as exc:  # noqa: BLE001 - Docker unreachable (or no instance bound) is not a reason to stop the worker
            logger.log(
                logging.DEBUG if self._skip_logged else logging.WARNING,
                "reconciler: skipped, %s: %s",
                type(exc).__name__,
                exc,
            )
            self._skip_logged = True
            report.unavailable = True
            return report
        if self._skip_logged:
            self._skip_logged = False
            logger.info("reconciler: Docker is reachable again; reconciling")

        for step in (self._reap, self._ensure_service_redis):
            try:
                step(db, client, instance, report)
            except Exception:  # noqa: BLE001 - background repair never propagates
                logger.exception("reconciler: %s raised", step.__name__)
                report.failed.append(f"{step.__name__} raised")
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    logger.exception("reconciler: rollback after a failed step raised")

        if report.changed or report.failed:
            logger.info(
                "reconciler: removed %d, restored %d, failed %d",
                len(report.removed),
                len(report.restored),
                len(report.failed),
            )
        return report

    def destroy_build_objects(self, db: Session, build_id: str) -> ReconcileReport:
        """The timeout sweep's direct destruction (a sandbox older
        than the maximum build duration "is force-destroyed by the worker sweep"): every
        sandbox, proxy and build network of THIS instance that names `build_id`, regardless
        of what the row says (the sweep calls this for a row it is about to fail). Same
        identification rules as a pass; never raises."""
        report = ReconcileReport()
        try:
            client = self._docker()
            instance = self.instance_id
            objects = [
                obj
                for obj in self._list_objects(client, instance, extra_label=f"{LABEL_BUILD_ID}={build_id}")
                if obj.role in _BUILD_ROLES and obj.labels.get(LABEL_BUILD_ID) == build_id
            ]
            self._remove_all(
                db,
                client,
                objects,
                {obj_id(o): "its build timed out" for o in objects},
                report,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("reconciler: destroying build %s's objects raised", build_id)
            report.failed.append(f"destroying build {build_id}'s objects raised {type(exc).__name__}")
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                logger.exception("reconciler: rollback after a failed destroy raised")
        return report

    # -- identification ---------------------------------------------------

    def _list_objects(self, client, instance: str, *, extra_label: str | None = None) -> list[_Object]:
        """Every container and network of this instance with a known role. The Docker filter
        narrows the list; the instance label is then checked again on each object, because the
        reconciler's one hard rule is that it never acts on an object it has not positively
        identified, and that must not rest on a filter's semantics."""
        label_filter: list[str] | str = f"{LABEL_INSTANCE}={instance}"
        if extra_label is not None:
            label_filter = [label_filter, extra_label]
        found: list[_Object] = []
        for container in client.containers.list(all=True, filters={"label": label_filter}):
            obj = self._identify("container", container, container.labels, instance)
            if obj is not None:
                found.append(obj)
        for network in client.networks.list(filters={"label": label_filter}):
            labels = (network.attrs or {}).get("Labels") or {}
            obj = self._identify("network", network, labels, instance)
            if obj is not None:
                found.append(obj)
        return found

    @staticmethod
    def _identify(kind: str, handle, labels: dict | None, instance: str) -> _Object | None:
        labels = labels or {}
        if labels.get(LABEL_INSTANCE) != instance:
            return None
        role = labels.get(LABEL_ROLE)
        if role not in Role.ALL:
            logger.debug("reconciler: ignoring %s %s: unknown role %r", kind, getattr(handle, "name", "?"), role)
            return None
        if role == Role.BUILD_IMAGE:
            # an image role. A container created from a labelled image inherits the
            # image's labels unless it overrides them (ours all do); one that still shows this
            # role is not a container role, so it is not acted on.
            logger.debug("reconciler: ignoring %s %s: role %r is an image role", kind, getattr(handle, "name", "?"), role)
            return None
        if (role in _NETWORK_ROLES) != (kind == "network"):
            # A role stamped on the wrong kind of object is not an object this module
            # understands; it is not guessed at.
            logger.warning(
                "reconciler: ignoring %s %s: role %r belongs to a %s",
                kind,
                getattr(handle, "name", "?"),
                role,
                "network" if role in _NETWORK_ROLES else "container",
            )
            return None
        return _Object(kind=kind, handle=handle, name=getattr(handle, "name", "?"), role=role, labels=dict(labels))

    # -- rule: reap -------------------------------------------------------

    def _reap(self, db: Session, client, instance: str, report: ReconcileReport) -> None:
        objects = [o for o in self._list_objects(client, instance) if o.role not in _NEVER_REMOVED]
        if not objects:
            return
        facts = self._facts(db, objects)
        reasons: dict[str, str] = {}
        for obj in objects:
            reason = removal_reason(obj.role, obj.labels, facts)
            if reason is not None:
                reasons[obj_id(obj)] = reason
        self._remove_all(db, client, objects, reasons, report)

    @staticmethod
    def _facts(db: Session, objects: list[_Object]) -> _Facts:
        build_ids = {o.labels[LABEL_BUILD_ID] for o in objects if LABEL_BUILD_ID in o.labels}
        deployment_ids = {o.labels[LABEL_DEPLOYMENT_ID] for o in objects if LABEL_DEPLOYMENT_ID in o.labels}
        running: set[str] = set()
        live: set[str] = set()
        if build_ids:
            for build_id, status, expires in db.execute(
                select(Build.id, Build.status, Build.preview_expires_at).where(Build.id.in_(build_ids))
            ):
                if status == BuildStatus.RUNNING:
                    running.add(build_id)
                if expires is not None:
                    live.add(build_id)
        statuses: dict[str, str] = {}
        if deployment_ids:
            statuses = {
                dep_id: status
                for dep_id, status in db.execute(
                    select(Deployment.id, Deployment.status).where(Deployment.id.in_(deployment_ids))
                )
            }
        return _Facts(frozenset(running), frozenset(live), statuses)

    def _remove_all(
        self,
        db: Session,
        client,
        objects: list[_Object],
        reasons: dict[str, str],
        report: ReconcileReport,
    ) -> None:
        """Removes the objects that have a reason, containers before the networks they sit on.
        A deploy container's Traefik attachment goes first (ADR 0037: detached from Traefik
        before removal), so a route stops resolving before the container does."""
        doomed = [o for o in objects if obj_id(o) in reasons]
        # Rows (a build, a deployment, a preview's build) whose container could not be removed:
        # that container is still running and still attached to its network, and detaching it to
        # remove the network would leave a live container with no network. Its network is left
        # for the next pass (the same rule as `DeployManager.remove_service`).
        blocked: set[str] = set()
        for obj in (o for o in doomed if o.kind == "container"):
            if obj.role == Role.DEPLOY:
                self._detach_traefik(client, obj)
            outcome = self._remove_container(obj)
            if outcome == "failed":
                blocked.add(_tie_of(obj))
            self._record(db, obj, reasons[obj_id(obj)], outcome, report)
        for obj in (o for o in doomed if o.kind == "network"):
            if _tie_of(obj) in blocked:
                line = f"network {obj.name} ({obj.role}): left, its container could not be removed"
                logger.error("reconciler: %s", line)
                report.failed.append(line)
                continue
            outcome = self._remove_network(obj)
            self._record(db, obj, reasons[obj_id(obj)], outcome, report)

    @staticmethod
    def _remove_container(obj: _Object) -> str:
        container = obj.handle
        try:
            container.remove(v=True, force=True)
        except NotFound:
            return "gone"
        except (APIError, DockerException) as exc:
            logger.error("reconciler: container %s could not be removed: %s", obj.name, exc)
            return "failed"
        return "removed"

    @staticmethod
    def _remove_network(obj: _Object) -> str:
        """Detaches whatever is still on the network, then removes it. Docker refuses to
        remove a network with an endpoint; everything that can be on one of the platform's
        networks is the platform's own attachment (Traefik, the shared Postgres, a Redis), and
        only the attachment is undone, never the attached container."""
        network = obj.handle
        try:
            network.reload()
            attached = list((network.attrs.get("Containers") or {}).keys())
        except NotFound:
            return "gone"
        except (APIError, DockerException):
            attached = []
        for container_id in attached:
            try:
                network.disconnect(container_id, force=True)
            except (NotFound, APIError, DockerException):
                pass
        try:
            network.remove()
        except NotFound:
            return "gone"
        except (APIError, DockerException) as exc:
            logger.error("reconciler: network %s could not be removed: %s", obj.name, exc)
            return "failed"
        return "removed"

    @staticmethod
    def _detach_traefik(client, obj: _Object) -> None:
        """Best-effort: takes the operator's Traefik container off the deployment's own
        network (named by the container's `forge.network` label) before the container is
        removed. A no-op when Traefik routing is off."""
        if not settings.traefik_enabled:
            return
        network_name = obj.labels.get("forge.network")
        if not network_name:
            return
        try:
            client.networks.get(network_name).disconnect(settings.traefik_container_name, force=True)
        except (NotFound, APIError, DockerException):
            pass

    @staticmethod
    def _record(db: Session, obj: _Object, reason: str, outcome: str, report: ReconcileReport) -> None:
        line = f"{obj.kind} {obj.name} ({obj.role}): {reason}"
        if outcome == "failed":
            report.failed.append(line)
            return
        if outcome == "gone":
            return  # something else removed it between the list and the call
        report.removed.append(line)
        logger.warning("reconciler: removed %s", line)
        target = _target_of(obj)
        metadata = {"role": obj.role, "object": obj.kind, "name": obj.name, "reason": reason}
        project_id = obj.labels.get(LABEL_PROJECT_ID)
        if project_id:
            metadata["project_id"] = project_id
        try:
            write_audit_log(
                db, actor_id=SYSTEM_ACTOR_ID, action="reconciler.removed", target=target, metadata=metadata
            )
            db.commit()
        except Exception:  # noqa: BLE001 - the removal happened; a lost audit row must not stop the rest
            logger.exception("reconciler: the audit entry for %s could not be written", obj.name)
            db.rollback()

    # -- rule: service Redis ---------------------------------------------

    def _ensure_service_redis(self, db: Session, client, instance: str, report: ReconcileReport) -> None:
        rows = db.execute(
            select(Project, Deployment)
            .join(Deployment, Deployment.project_id == Project.id)
            .where(Deployment.status == DeploymentStatus.ACTIVE)
        ).all()
        for project, deployment in rows:
            project_id, deployment_id = project.id, deployment.id
            try:
                self._ensure_one_redis(db, client, project, deployment_id, report)
            except Exception:  # noqa: BLE001 - one project's failure never blocks another's
                logger.exception("reconciler: re-ensuring project %s's Redis raised", project_id)
                report.failed.append(f"project {project_id}: re-ensuring its Redis raised")
                db.rollback()

    def _ensure_one_redis(
        self, db: Session, client, project: Project, deployment_id: str, report: ReconcileReport
    ) -> None:
        project_id = project.id
        stored_url = self._stored_redis_url(project)
        if stored_url is None:
            self._warn_once(
                f"no-url:{project_id}",
                "reconciler: project %s has an ACTIVE deployment but no usable stored REDIS_URL; "
                "its Redis is not re-ensured here",
                project_id,
            )
            return
        if service_data.redis_password_from_url(stored_url) is None:
            self._warn_once(
                f"legacy:{project_id}",
                "reconciler: project %s still holds a password-less REDIS_URL from before the "
                "hardened Redis; replacing the container would change the credential its running "
                "service holds, so it is left to the project's next deployment",
                project_id,
            )
            return

        network_name = self._active_network_name(client, deployment_id)
        state = self._redis_state(client, project_id, network_name)
        if state is None:
            return

        ensured = service_data.ensure_project_redis(project_id, docker_client=client, redis_url=stored_url)
        if ensured != stored_url:
            # With a stored password this cannot differ; if it somehow does, the stored env is
            # NOT rewritten from a background pass (a running service holds the old value).
            logger.error("reconciler: project %s's re-ensured REDIS_URL differs from the stored one", project_id)
        if network_name is not None:
            self._attach(client, project_id, network_name)

        line = f"project {project_id}: service Redis was {state}, now running"
        report.restored.append(line)
        logger.warning("reconciler: restored %s", line)
        write_audit_log(
            db,
            actor_id=SYSTEM_ACTOR_ID,
            action="reconciler.restored",
            target=f"project:{project_id}",
            metadata={"role": Role.SERVICE_REDIS, "state_before": state, "deployment_id": deployment_id},
        )
        db.commit()

    @staticmethod
    def _stored_redis_url(project: Project) -> str | None:
        if not project.service_env_json:
            return None
        try:
            env = decrypt_service_env(project.service_env_json)
        except ServiceEnvDecryptionError:
            return None
        return env.get("REDIS_URL") or None

    def _active_network_name(self, client, deployment_id: str) -> str | None:
        """The ACTIVE deployment's private network, read from its container's own
        `forge.network` label (which every deploy container has carried, so the
        older demo deployment is found too). None when its container is gone."""
        try:
            container = client.containers.get(f"{DEPLOY_CONTAINER_PREFIX}-{deployment_id}")
        except NotFound:
            self._warn_once(
                f"no-container:{deployment_id}",
                "reconciler: ACTIVE deployment %s has no container on this host",
                deployment_id,
            )
            return None
        except DockerException as exc:
            logger.warning("reconciler: could not look up deployment %s's container: %s", deployment_id, exc)
            return None
        return (container.labels or {}).get("forge.network") or None

    @staticmethod
    def _redis_state(client, project_id: str, network_name: str | None) -> str | None:
        """What is wrong with the project's Redis, or None when nothing is: `absent`,
        `stopped`, or `detached` (running, but not on the ACTIVE deployment's network)."""
        try:
            container = client.containers.get(service_data.redis_container_name(project_id))
        except NotFound:
            return "absent"
        container.reload()
        if container.status != "running":
            return "stopped"
        if network_name is not None:
            networks = (container.attrs.get("NetworkSettings") or {}).get("Networks") or {}
            if network_name not in networks:
                return "detached"
        return None

    @staticmethod
    def _attach(client, project_id: str, network_name: str) -> None:
        """Puts the project's Redis on the ACTIVE deployment's network. A container the
        platform just created has no network at all (`create_isolated_redis`), and a service
        cannot reach a Redis it is not on a network with. Already attached is not an error."""
        try:
            network = client.networks.get(network_name)
            redis = client.containers.get(service_data.redis_container_name(project_id))
            redis.reload()
            if network_name in ((redis.attrs.get("NetworkSettings") or {}).get("Networks") or {}):
                return
            network.connect(redis)
        except (NotFound, APIError, DockerException) as exc:
            logger.error(
                "reconciler: project %s's Redis could not be attached to network %s: %s",
                project_id,
                network_name,
                exc,
            )


def obj_id(obj: _Object) -> str:
    """A key unique to one Docker object (containers and networks have separate id spaces,
    so the kind is part of it)."""
    return f"{obj.kind}:{getattr(obj.handle, 'id', obj.name)}"


def _tie_of(obj: _Object) -> str:
    """The row id a container and its network share (a build or a deployment), or the object's
    own id when it names none."""
    if obj.role in _DEPLOY_ROLES:
        return obj.labels.get(LABEL_DEPLOYMENT_ID) or obj_id(obj)
    return obj.labels.get(LABEL_BUILD_ID) or obj_id(obj)


def _target_of(obj: _Object) -> str:
    """The audit target: the row the object belonged to when it names one."""
    if obj.role in _DEPLOY_ROLES and LABEL_DEPLOYMENT_ID in obj.labels:
        return f"deployment:{obj.labels[LABEL_DEPLOYMENT_ID]}"
    if LABEL_BUILD_ID in obj.labels:
        return f"build:{obj.labels[LABEL_BUILD_ID]}"
    return f"docker_{obj.kind}:{obj.name}"
