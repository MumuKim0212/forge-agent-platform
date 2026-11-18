"""L4 service container lifecycle (L4 runtime rules,
deployment lifecycle; ADR 0002, 0003).

The Docker half of the deploy slice: starts the confirmed build's image as a
long-running L4 service container, health-gates it, tears it down, and (dev
mode) proxies HTTP into it. The DB/lifecycle half lives in
`services/deploy.py` — this module never touches the DB.

L4 runtime posture:
- Dedicated per-deployment `internal=True` network — a service reaches only
  its own data services (none yet), never
  the platform DB, L2 internals, other services, or L3 sandboxes.
- Mandatory memory/CPU/pids caps (ADR 0002) — reuses the sandbox limit
  settings (same host, same contention concern).
- **No Docker socket** — no volumes at all, `privileged=False`,
  `cap_drop=ALL`, `no-new-privileges`.
- Restart policy `unless-stopped` so an ACTIVE service survives daemon
  restarts; the platform's own teardown uses `remove(v=True, force=True)`, which
  wins over the restart policy and takes the container's anonymous volumes
  with it.
- Labeled (`forge.deploy`/`forge.project_id`/`forge.deployment_id`/
  `forge.build_id`/`forge.network`) so a restarted platform can rediscover
  and manage running services.

ADR 0023 §Data services, §Amendment 1: the shared Postgres
(`settings.service_postgres_container_name`) and the project's own Redis
(`services.service_data.redis_container_name`) join a deployment's network
in `start_service` (before the container starts — it needs to reach its
database from the moment `alembic upgrade head` runs) and leave it in
`remove_service`, before the network is destroyed — the exact bracket
`connect_to_traefik` and the teardown's `_empty_network` already give Traefik below. Both
old and new deployments' networks hold the shared Postgres attached
simultaneously during the health-gated switchover window; that is correct,
not a leak (ADR 0023 §Amendment 1).

Routing (dev mode): the platform-proxy mechanism — no host port is
published at all; the platform backend forwards each request via
`docker exec` of a stdlib-urllib one-liner against `127.0.0.1:<port>`
*inside* the service container (same pattern as `PreviewManager.proxy` /
`ProxyContainer._exec_admin_request`, same reason: an `internal=True`
network silently drops port publishes).

Routing (prod shape): when
`settings.traefik_enabled`, the service container additionally carries
Traefik Docker-provider labels (routing `{project_id}.{apps_base_domain}`
to it) set at create time, and stays on its own private `internal=True`
network — the route flip is `connect_to_traefik()`: connecting the
OPERATOR'S TRAEFIK CONTAINER (`settings.traefik_container_name`) into the
service's network after the health gate passes.

Why this direction and not "service joins a shared Traefik network"
(empirically verified against real Traefik v3.6 + Docker Desktop, see
tests/test_deploy_traefik_integration.py):
- Traefik's Docker provider discovers labels via the socket the moment the
  container is created (regardless of network membership) and resolves the
  backend IP from the container's networks AT THAT TIME; it does NOT react
  to later `network connect` events on the service container — so joining
  a shared network post-health never becomes routable. Connecting the
  Traefik container itself is a pure kernel-level reachability change that
  needs no provider refresh at all (the provider's stored backend IP — the
  service's own-network IP — was correct all along).
- Per-service networks preserve isolation: no shared bridge
  between services, so services cannot reach each other or the platform
  even with routing live (asserted empirically in the integration tests).
- Because the (unreachable) router exists before health, each router
  carries an explicit, strictly-decreasing priority label: while old and
  new deployments' routers coexist (same Host rule), the OLD one
  deterministically wins until its container is removed — so a pre-health
  or failed new container never receives traffic, and the flip is clean.
Health gate: `wait_healthy` always runs before `connect_to_traefik()`
(`execute_deployment`), so a container is never reachable through Traefik
before passing `/healthz`. Removal (`remove_service`) drops the router
(container gone) and disconnects Traefik from the private network before
destroying it. L2 never calls a Traefik API — Docker label + network ops
only.
"""

from __future__ import annotations

import base64
import logging
import time
import uuid

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.types import Ulimit

from app.config import settings
from app.services import service_data
from app.services.container_log_config import capped_log_config
from app.services.platform_labels import Role, platform_labels
from app.services.service_container_user import SERVICE_CONTAINER_USER

logger = logging.getLogger("app.deploy_manager")

DEPLOY_CONTAINER_PREFIX = "forge-deploy"


class DeployStartError(RuntimeError):
    """Raised when the L4 service container fails to start."""


class DeployProxyError(RuntimeError):
    """Raised when a request cannot be forwarded into the service container."""


class DeployRouteError(RuntimeError):
    """Raised when a healthy container cannot be attached to (or detached
    from) the Traefik-shared network — failure taxonomy
    ROUTE_FAILED. The container itself is left running; the caller decides
    whether to remove it."""


class DeployDataServiceError(RuntimeError):
    """ADR 0023 §Amendment 1: raised when the shared Postgres or the
    project's Redis cannot be attached to a deployment's network in
    `start_service` — failure taxonomy PROVISION_FAILED
    ("reaching" the service's database/role/Redis, not only creating it).
    Distinct from `DeployStartError` so `execute_deployment` can map this
    ONE failure mode of `start_service` to PROVISION_FAILED while every
    other failure there stays START_FAILED."""


def _parse_proxy_output(output: bytes) -> tuple[int, bytes, str | None]:
    """ADR 0028 layer 1: splits `_exec_http`'s stdout into
    `(status, body, content_type)`. Same wire format and the same
    degrade-never-raise rule as `preview_manager._parse_proxy_output` —
    duplicated rather than shared, matching this module's existing
    precedent of an independent copy of the `docker exec`-a-urllib-script
    pattern (see `_exec_http` below and `PreviewManager.proxy`).

    `wait_healthy` (below) never calls this — it only ever needs the first
    line (the bare status code), so it keeps its own untouched
    `output.partition(b"\\n")` and does not care that a second line now
    exists."""
    status_line, _, rest = output.partition(b"\n")
    status = int(status_line.decode().strip())

    content_type_line, sep, body = rest.partition(b"\n")
    if not sep:
        return status, rest, None
    try:
        content_type = content_type_line.decode().strip() or None
    except UnicodeDecodeError:
        content_type = None
    return status, body, content_type


def _build_proxy_exec_script(
    url: str,
    method: str,
    timeout_seconds: float,
    max_body_bytes: int | None = None,
    body: bytes | None = None,
    content_type: str | None = None,
) -> str:
    """The one-liner `container.exec_run(["python", "-c", script])` runs
    *inside* the service container against its own loopback. ADR 0028
    layer 1: line 1 of stdout is still the bare status code, byte-for-byte
    unchanged (`wait_healthy` reads only that line); line 2, new, carries
    the upstream `Content-Type` (blank when upstream sent none); everything
    from line 3 on is the raw body. **No other upstream header is ever
    read** — `resp.headers`/`e.headers` are touched exactly once each, both
    times for `'Content-Type'` — so there is no code path here that could
    forward `Set-Cookie` or anything else. Duplicated from
    `preview_manager._build_proxy_exec_script` rather than shared, matching
    this module's existing precedent (see `_parse_proxy_output` above);
    extracted into its own function so `tests/test_proxy_isolation.py` can
    run it directly (via `subprocess`, against a local test server) without
    Docker.

    `max_body_bytes`, `body` and `content_type` are exactly what
    `preview_manager._build_proxy_exec_script` takes -- `body` becomes `data=` (base64 inside the
    script), `content_type` is the ONE request header sent, `max_body_bytes` bounds the read. With
    none of them the script is the one it was before."""
    read_arg = "" if max_body_bytes is None else str(int(max_body_bytes))
    request_args = [repr(url)]
    imports = "import urllib.request, sys"
    if body:
        imports += ", base64"
        request_args.append(f"data=base64.b64decode({base64.b64encode(body).decode('ascii')!r})")
    if content_type is not None:
        request_args.append(f"headers={{'Content-Type': {content_type!r}}}")
    request_args.append(f"method={method!r}")
    return (
        f"{imports}\n"
        f"req = urllib.request.Request({', '.join(request_args)})\n"
        "try:\n"
        f"    with urllib.request.urlopen(req, timeout={timeout_seconds}) as resp:\n"
        "        sys.stdout.buffer.write(str(resp.status).encode() + b'\\n')\n"
        "        sys.stdout.buffer.write((resp.headers.get('Content-Type') or '').encode() + b'\\n')\n"
        f"        sys.stdout.buffer.write(resp.read({read_arg}))\n"
        "except urllib.error.HTTPError as e:\n"
        "    sys.stdout.buffer.write(str(e.code).encode() + b'\\n')\n"
        "    sys.stdout.buffer.write((e.headers.get('Content-Type') or '').encode() + b'\\n')\n"
        f"    sys.stdout.buffer.write(e.read({read_arg}))\n"
    )


def _traefik_router_name(deployment_id: str) -> str:
    # Traefik router/service names must be unique per Traefik instance and
    # are safe as plain identifiers (no dots — those are label-key
    # separators); the deployment id (a uuid4) is already DNS/label-safe.
    return f"forge-deploy-{deployment_id}"


# Base for the strictly-decreasing router priority (see module docstring:
# older deployment's router must deterministically win while old and new
# coexist). Comfortably above any unix epoch this platform will ever see,
# so priorities stay positive.
_TRAEFIK_PRIORITY_BASE = 4_000_000_000


def traefik_router_priority(now: float | None = None) -> int:
    """A per-deployment router priority that strictly DECREASES over time,
    so during the dual-router window (old + new routers share one Host
    rule) the OLDER — already-healthy, already-routed — deployment wins
    deterministically until its container is removed. Per-project
    serialization plus the multi-second health gate
    guarantees two deployments of one project never share the same
    second."""
    return int(_TRAEFIK_PRIORITY_BASE - (now if now is not None else time.time()))


def service_host(project_id: str) -> str:
    """the stable per-project hostname (`{project_id}.{apps_base_domain}`). The one
    place it is spelled: the Traefik `Host(...)` rule and `service_base_url` both read it."""
    return f"{project_id}.{settings.apps_base_domain}"


def service_base_url(project_id: str) -> str:
    """the base URL of a project's service, from settings and the id alone (it never
    reads the encrypted service env). Traefik on: `{service_url_scheme}://{service_host}`
    (plaintext by default). Traefik off: the platform-proxy route
    `/api/projects/{id}/service`, with no trailing slash -- the value
    `FORGE_SERVICE_BASE_URL` has always carried. `services/deploy.service_url` is what a
    person opens (this plus the dev form's slash, gated on an ACTIVE deployment)."""
    if settings.traefik_enabled:
        return f"{settings.service_url_scheme}://{service_host(project_id)}"
    return f"/api/projects/{project_id}/service"


def traefik_service_labels(
    *, project_id: str, deployment_id: str, network_name: str, priority: int
) -> dict[str, str]:
    """Routing (prod): Docker labels routing
    `{project_id}.{apps_base_domain}` to this deployment's container — the
    stable per-project hostname (`Project.id` stands in for the spec's
    `{project_slug}`: it is already the DNS-safe identifier used everywhere
    else, and no separate slug field exists). `network_name` is the
    deployment's OWN private network — `traefik.docker.network` tells the
    provider which network's IP to use; the container is never reachable
    through Traefik until `connect_to_traefik()` attaches the Traefik
    container to that network post-health (see module docstring). Router/
    service names are namespaced by deployment id so a superseded
    deployment's labels never collide with the new one's; `priority` comes
    from `traefik_router_priority()` (older wins during the flip window).

    the entrypoint is operator configuration
    (`settings.traefik_entrypoint`, default `"web"` — same value as before
    this became configurable), not a literal. TLS labels (`…tls` +
    `…tls.certresolver`) are added ONLY when `settings.traefik_cert_resolver`
    is set — unset emits neither key at all, never `tls=false`, matching the
    unset-sends-no-key precedent."""
    router = _traefik_router_name(deployment_id)
    host = service_host(project_id)
    labels = {
        "traefik.enable": "true",
        f"traefik.http.routers.{router}.rule": f"Host(`{host}`)",
        f"traefik.http.routers.{router}.entrypoints": settings.traefik_entrypoint,
        f"traefik.http.routers.{router}.priority": str(priority),
        "traefik.docker.network": network_name,
        f"traefik.http.services.{router}.loadbalancer.server.port": str(
            settings.preview_container_port
        ),
    }
    if settings.traefik_cert_resolver:
        labels[f"traefik.http.routers.{router}.tls"] = "true"
        labels[f"traefik.http.routers.{router}.tls.certresolver"] = settings.traefik_cert_resolver
    return labels


# the budget, in seconds, of the Docker client the PROJECT READ builds to ask whether a
# build's image still exists (`routers/deployments._image_check_manager`). docker-py connects when
# the client is built (it asks the daemon for its API version) and every call after that waits up
# to its default 60 s, so a daemon that accepts the connection and hangs would hold the read, and
# the DB session behind it, for a minute per call; the page polls that read while a build is in
# flight. A few seconds is far above a healthy daemon's answer (milliseconds) and short enough that
# a hung one is reported as `DOCKER_UNAVAILABLE` quickly. The confirm/rollback/stop actions keep the
# default client: they are one-off requests the user is waiting on, answered with 503 on failure.
IMAGE_CHECK_TIMEOUT_SECONDS = 3.0


class DeployManager:
    """Starts/health-checks/tears down L4 service containers. Holds no
    per-deployment state — everything needed to find a container again is
    in its name/labels, so a restarted process can still manage it.

    `timeout` (seconds) bounds every call of the client this builds, the connect that
    `docker.from_env()` itself makes included; `None` keeps docker-py's default. Ignored when a
    `client` is passed."""

    def __init__(self, client: docker.DockerClient | None = None, *, timeout: float | None = None) -> None:
        if client is not None:
            self._client = client
        elif timeout is not None:
            self._client = docker.from_env(timeout=timeout)
        else:
            self._client = docker.from_env()

    # -- naming ---------------------------------------------------------

    def _container_name(self, deployment_id: str) -> str:
        return f"{DEPLOY_CONTAINER_PREFIX}-{deployment_id}"

    # -- image ----------------------------------------------------------

    def image_exists(self, image_ref: str | None) -> bool:
        """Failure taxonomy
        IMAGE_MISSING: whether the build's image is still present on the
        host."""
        if not image_ref:
            return False
        try:
            self._client.images.get(image_ref)
            return True
        except (ImageNotFound, DockerException):
            return False

    # -- lifecycle ------------------------------------------------------

    def start_service(
        self, *, project_id: str, deployment_id: str, build_id: str, image_ref: str, env: dict[str, str]
    ) -> None:
        """Starts the service container from the promoted image with the
        project's service env injected (secret injection:
        container environment at create time, never baked into the image).
        Raises `DeployStartError` on any Docker failure.

        **a start that raises leaves
        nothing of this attempt behind -- no network, no `Created`
        container -- whatever the reason.** The guarantee is made here, at the
        one place that creates both, instead of at each failure the caller
        happens to know about: everything after the network exists runs inside
        one `try`, and any exception (a Docker error, an unexpected one, an
        interrupt) goes through `_abort_start` before it propagates. The
        network used to be torn down for two named failures only, and one of
        them (`DeployDataServiceError`) removed it WITHOUT first detaching the
        shared Postgres that `_connect_data_services` had already attached:
        Docker refuses to remove a network with an attached endpoint, the
        refusal was swallowed, and the network stayed (observed with
        two PROVISION_FAILED deployments, two leaked networks)."""
        network = None
        try:
            network = self._client.networks.create(
                f"{settings.deploy_network_name}-{deployment_id}-{uuid.uuid4().hex[:8]}",
                driver="bridge",
                internal=True,
                # lets `remove_service` find the network again from the
                # deployment id alone, when the container (whose
                # `forge.network` label is the other way to it) never existed.
                # plus the instance and role the reconciler finds it by.
                labels=platform_labels(
                    Role.DEPLOY_NETWORK,
                    deployment_id=deployment_id,
                    project_id=project_id,
                    extra={"forge.deploy": "true"},
                ),
            )
        except DockerException as exc:
            raise DeployStartError(f"deploy network creation failed: {exc}") from exc

        try:
            self._start_on_network(
                network,
                project_id=project_id,
                deployment_id=deployment_id,
                build_id=build_id,
                image_ref=image_ref,
                env=env,
            )
        except BaseException:  # noqa: BLE001 - re-raised below; cleanup must run for ANY exit
            self._abort_start(network, project_id=project_id, deployment_id=deployment_id)
            raise

        logger.info(
            "service container started: project=%s deployment=%s image=%s",
            project_id,
            deployment_id,
            image_ref,
        )

    def _start_on_network(
        self,
        network,
        *,
        project_id: str,
        deployment_id: str,
        build_id: str,
        image_ref: str,
        env: dict[str, str],
    ) -> None:
        """Everything `start_service` does once the network exists. Cleaning up
        a failure is NOT its job: `start_service` wraps this whole body, so a
        statement added here is covered without anyone remembering to."""
        # ADR 0023 §Amendment 1: the shared Postgres and this
        # project's OWN Redis join the deployment's network BEFORE the
        # service container starts — the template contract runs `alembic
        # upgrade head` at container start, so the database must
        # already be reachable at that instant, not merely by the time the
        # health gate passes. Contrast `connect_to_traefik`, which
        # deliberately waits for health: Traefik makes the container
        # PUBLICLY reachable, data services make it able to start at all.
        self._connect_data_services(network, project_id)

        # the platform's own labels (instance, role, the ids the reconciler ties the
        # container to its row by) beside the L4 ones this container has always carried.
        labels = platform_labels(
            Role.DEPLOY,
            project_id=project_id,
            deployment_id=deployment_id,
            build_id=build_id,
            extra={"forge.deploy": "true", "forge.network": network.name},
        )
        if settings.traefik_enabled:
            # Labels are immutable after create; the router they define is
            # unreachable (and outprioritized by the currently-ACTIVE
            # deployment's router) until connect_to_traefik() attaches the
            # Traefik container to this deployment's own network
            # post-health — see module docstring.
            labels.update(
                traefik_service_labels(
                    project_id=project_id,
                    deployment_id=deployment_id,
                    network_name=network.name,
                    priority=traefik_router_priority(),
                )
            )

        try:
            self._client.containers.run(
                image=image_ref,
                name=self._container_name(deployment_id),
                detach=True,
                network=network.name,
                environment=env,
                mem_limit=f"{settings.sandbox_memory_limit_mb}m",
                nano_cpus=int(settings.sandbox_cpu_limit_cores * 1_000_000_000),
                pids_limit=settings.sandbox_pids_limit,
                security_opt=["no-new-privileges"],
                cap_drop=["ALL"],
                privileged=False,
                # same user as the preview of this build (ADR 0016, ADR 0046 §Decision 5).
                user=SERVICE_CONTAINER_USER,
                volumes={},
                restart_policy={"Name": "unless-stopped"},
                ulimits=[
                    Ulimit(
                        name="nproc",
                        soft=settings.sandbox_pids_limit,
                        hard=settings.sandbox_pids_limit,
                    )
                ],
                labels=labels,
                log_config=capped_log_config(),
            )
        except DockerException as exc:
            raise DeployStartError(f"service container failed to start: {exc}") from exc

    def _abort_start(self, network, *, project_id: str, deployment_id: str) -> None:
        """undoes a start that did not finish, in the order Docker
        needs. The container goes first, and by NAME: `containers.run` creates
        the container and then starts it, so a failed start leaves a `Created`
        container that nothing references (the return value was never
        assigned) -- and its endpoint would block the network's removal too.
        Then the endpoints this class attached (Traefik, the data services),
        then the network. Never raises: it runs while another exception is
        propagating, and replacing that one would hide the cause. What it could
        not remove is logged at ERROR, once."""
        left: list[str] = []
        try:
            if not self._remove_container(self._container_name(deployment_id)):
                left.append("its container")
        except Exception:  # noqa: BLE001 - never replace the exception being handled
            logger.exception("deployment %s: removing its container after a failed start raised", deployment_id)
            left.append("its container")
        try:
            if not self._teardown_network(network):
                left.append(f"network {network.name}")
        except Exception:  # noqa: BLE001
            logger.exception("deployment %s: removing its network after a failed start raised", deployment_id)
            left.append("its network")
        if left:
            logger.error(
                "deployment %s: a failed start left %s behind; it is findable by the "
                "forge.deployment_id label",
                deployment_id,
                " and ".join(left),
            )

    def _connect_data_services(self, network, project_id: str) -> None:
        """ADR 0023 §Amendment 1: attaches the shared Postgres and
        this project's OWN Redis to a deployment's network — the same
        bracket `connect_to_traefik` and the teardown's `_empty_network` give
        Traefik.
        Unconditional — no settings toggle like `traefik_enabled`: a
        deployment only ever reaches this method once `execute_deployment`
        has already refused PROVISION_UNCONFIGURED, so a live admin DSN is
        guaranteed here in production. Still guarded on
        `service_postgres_configured` (skips entirely when unset) so a
        caller that bypasses that guard — a unit test driving this class
        directly, for one — keeps its original behaviour rather than
        gaining a new failure mode."""
        if not settings.service_postgres_configured:
            return
        try:
            postgres = self._client.containers.get(settings.service_postgres_container_name)
            network.connect(postgres)
            redis = self._client.containers.get(service_data.redis_container_name(project_id))
            network.connect(redis)
        except (NotFound, DockerException, APIError) as exc:
            raise DeployDataServiceError(
                f"failed to attach data services to project {project_id}'s deployment network: {exc}"
            ) from exc

    def connect_to_traefik(self, deployment_id: str) -> None:
        """Routing (prod): the route flip — connects the
        operator's Traefik container into the ALREADY-HEALTHY deployment's
        own private network, making the (label-defined, until-now-
        unreachable) route live. A pure kernel-level reachability change:
        needs no Traefik provider refresh (see module docstring for why the
        opposite direction — service joins a shared network — does not work
        with Traefik's Docker provider). Only ever called after
        `wait_healthy()` succeeds (`execute_deployment` — the health gate
        holds under Traefik: a container is never reachable through Traefik
        before passing `/healthz`). No-op when Traefik routing is disabled.
        Raises `DeployRouteError` on failure — the caller
        on ROUTE_FAILED removes the new container and leaves the old
        one routed."""
        if not settings.traefik_enabled:
            return
        try:
            container = self._client.containers.get(self._container_name(deployment_id))
            network_name = (container.labels or {}).get("forge.network")
            if not network_name:
                raise DeployRouteError(
                    f"deployment {deployment_id} container has no forge.network label"
                )
            network = self._client.networks.get(network_name)
            traefik = self._client.containers.get(settings.traefik_container_name)
            network.connect(traefik)
        except DeployRouteError:
            raise
        except (NotFound, DockerException, APIError) as exc:
            raise DeployRouteError(
                f"failed to attach Traefik to deployment {deployment_id}'s network: {exc}"
            ) from exc

    def wait_healthy(self, deployment_id: str, *, timeout_seconds: float | None = None) -> bool:
        """wait for the template
        contract's health endpoint (`GET /healthz` -> 200) within the
        configured window. Returns False on timeout or if the container
        died — the caller maps that to HEALTH_TIMEOUT and removes the
        container."""
        timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.deploy_health_check_timeout_seconds
        )
        deadline = time.monotonic() + timeout
        name = self._container_name(deployment_id)

        while time.monotonic() < deadline:
            try:
                container = self._client.containers.get(name)
                container.reload()
                if container.status != "running":
                    time.sleep(0.5)
                    continue
                exit_code, output = self._exec_http(container, "GET", "/healthz")
                if exit_code == 0:
                    status_line, _, _ = output.partition(b"\n")
                    if status_line.strip() == b"200":
                        return True
            except (NotFound, DockerException):
                pass
            time.sleep(0.5)
        return False

    def remove_service(self, deployment_id: str) -> bool:
        """Tears down the service container + its network for one
        deployment. Idempotent: safe on a deployment with no container or no
        network.

        **Returns True when nothing of the deployment is left** -- removed
        now, or never there -- and **False when something could not be
        removed** (it used to return True for "I found a container"
        whether or not the removal worked, so a caller marking a deployment
        STOPPED or SUPERSEDED could not tell). What is left is logged at ERROR
        here; the caller records the fact where it records the transition.
        Never raises for a Docker failure.

        The network is found two ways: through the container's
        `forge.network` label, and through the `forge.deployment_id` label the
        network itself carries -- so a deployment whose container
        was never created (or is already gone) still has its network removed.
        A container that could not be removed leaves its network alone: the
        service is still attached to it and still running."""
        name = self._container_name(deployment_id)
        try:
            container = self._client.containers.get(name)
        except NotFound:
            container = None
        except DockerException as exc:
            logger.error("deployment %s: cannot look up its container: %s", deployment_id, exc)
            return False

        labelled_network_name = None
        if container is not None:
            labelled_network_name = (container.labels or {}).get("forge.network")
            if not self._remove_container_object(container, deployment_id):
                return False

        try:
            networks = {
                network.id: network
                for network in self._client.networks.list(
                    filters={"label": f"forge.deployment_id={deployment_id}"}
                )
            }
            if labelled_network_name:
                try:
                    named = self._client.networks.get(labelled_network_name)
                    networks.setdefault(named.id, named)
                except NotFound:
                    pass  # already gone
        except DockerException as exc:
            logger.error("deployment %s: cannot look up its network: %s", deployment_id, exc)
            return False

        clean = True
        for network in networks.values():
            if not self._teardown_network(network):
                logger.error(
                    "deployment %s: network %s could not be removed and is still on the host",
                    deployment_id,
                    network.name,
                )
                clean = False
        return clean

    def _remove_container(self, name: str) -> bool:
        """True when no container called `name` exists afterwards."""
        try:
            container = self._client.containers.get(name)
        except NotFound:
            return True
        except DockerException as exc:
            logger.error("cannot look up container %s: %s", name, exc)
            return False
        return self._remove_container_object(container, name)

    def _remove_container_object(self, container, label: str) -> bool:
        """`remove(v=True, force=True)` (with its anonymous volumes; force
        wins over the restart policy). A container that is already gone is
        success; any other refusal is logged at ERROR and returns False."""
        try:
            container.remove(v=True, force=True)
        except NotFound:
            return True
        except (APIError, DockerException) as exc:
            logger.error("deployment %s: container %s could not be removed: %s", label, container.name, exc)
            return False
        return True

    def _teardown_network(self, network) -> bool:
        """Detach whatever is still attached, then remove the network. True
        when it is gone afterwards."""
        self._empty_network(network)
        return self._destroy_network(network)

    def _empty_network(self, network) -> None:
        """Best-effort: detaches every endpoint still on a deployment's
        private network, because Docker refuses to remove a network that has
        one. Everything that can be attached to this network is the platform's
        own doing -- the shared Postgres and the project's Redis
        (`_connect_data_services`) and Traefik (`connect_to_traefik`) -- so
        the sweep needs no list of names, no settings and no project id, and
        cannot miss an endpoint a failed start attached but never recorded
        (the old by-name detach was skipped on the very path that left
        Postgres attached)."""
        try:
            network.reload()
            attached = list((network.attrs.get("Containers") or {}).keys())
        except (NotFound, APIError, DockerException):
            return
        for container_id in attached:
            try:
                network.disconnect(container_id, force=True)
            except (NotFound, APIError, DockerException):
                pass

    def _destroy_network(self, network) -> bool:
        """True when the network no longer exists afterwards (already gone
        counts). A refusal is logged here so it is never silent (it was
        `pass`, which is how a leaked network stayed invisible)."""
        try:
            network.remove()
        except NotFound:
            return True
        except (APIError, DockerException) as exc:
            logger.error("network %s could not be removed: %s", network.name, exc)
            return False
        return True

    # -- routing (dev-mode platform proxy, see module docstring) ---------

    def proxy(
        self,
        deployment_id: str,
        *,
        method: str = "GET",
        path: str = "/",
        timeout_seconds: float = 5.0,
        max_body_bytes: int | None = None,
        query: str = "",
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[int, bytes, str | None]:
        """Forwards one HTTP request into the ACTIVE deployment's service
        container via `docker exec` (the platform-proxy mechanism — no published host
        port at all). Returns (status_code, body_bytes, content_type) —
        `content_type` is ADR 0028 layer 1's raw material, the upstream
        `Content-Type` header value or `None` when there was none; the
        caller (`routers/service_proxy.py`) turns that into the actual
        response headers via `services/proxy_headers.build_isolation_headers`.
        Raises `DeployProxyError` if the container is gone/unreachable.

        `max_body_bytes` bounds the read inside the container (same as
        `PreviewManager.proxy`); `query` (already percent-encoded, no leading `?`), `body` and
        `content_type` are what the platform route lets through; `path` is expected already
        percent-encoded by that route."""
        name = self._container_name(deployment_id)
        try:
            container = self._client.containers.get(name)
        except NotFound as exc:
            raise DeployProxyError(f"no service container for deployment {deployment_id}") from exc
        except DockerException as exc:
            raise DeployProxyError(f"could not reach service container: {exc}") from exc

        if container.status != "running":
            raise DeployProxyError(f"service container for deployment {deployment_id} is not running")

        exit_code, output = self._exec_http(
            container,
            method,
            path,
            timeout_seconds=timeout_seconds,
            max_body_bytes=max_body_bytes,
            query=query,
            body=body,
            content_type=content_type,
        )
        if exit_code != 0:
            raise DeployProxyError(f"service proxy exec failed (exit {exit_code}): {output!r}")

        try:
            status, body, content_type = _parse_proxy_output(output)
        except ValueError:
            raise DeployProxyError(f"service proxy returned an unparseable response: {output!r}")
        return status, body, content_type

    def _exec_http(
        self,
        container,
        method: str,
        path: str,
        *,
        timeout_seconds: float = 5.0,
        max_body_bytes: int | None = None,
        query: str = "",
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[int, bytes]:
        """One stdlib-urllib HTTP request run *inside* the service container
        against 127.0.0.1 (same shape as `PreviewManager.proxy` /
        `ProxyContainer._exec_admin_request`; the template contract's app
        port is `settings.preview_container_port` — the one port the
        template serves on, for previews and deploys alike). Output format:
        first line = status code, second line (new, ADR 0028 layer 1) = the
        upstream Content-Type (blank if none), rest = raw body bytes.
        `wait_healthy` above only ever reads the first line, so it needs no
        change for the new second line to be safe to add here."""
        port = settings.preview_container_port
        safe_path = path if path.startswith("/") else f"/{path}"
        url = f"http://127.0.0.1:{port}{safe_path}" + (f"?{query}" if query else "")
        script = _build_proxy_exec_script(
            url, method, timeout_seconds, max_body_bytes, body=body, content_type=content_type
        )
        return container.exec_run(["python", "-c", script])
