"""TTL preview container lifecycle (ADR 0016).

A successful build's OCI image (`Build.image_ref`) is started as a fresh,
TTL-bound preview container — a *different* container from the build
sandbox, sharing only the image (ADR 0016). Hardening mirrors the build
sandbox (`sandbox_runner.py`): no Docker socket, default-deny egress via a
fresh per-preview `internal=True` Docker network, memory/CPU/pids caps,
non-root-friendly flags (`no-new-privileges`, dropped capabilities).

Routing (this implementation's documented choice;
the routing mechanism is left open): the preview
container is **not** given a published host port. Verified against the
real Docker Engine (see task notes) that an `internal=True` network's port
publish is silently dropped by Docker (no NAT/DNAT rule set up at all) —
the same tradeoff `proxy_container.py` already made for the LLM proxy's
admin API. So instead of a host port, the platform backend reaches the
preview container over `docker exec` (`PreviewManager.proxy`, same shape as
`ProxyContainer._exec_admin_request`): a tiny Python one-liner run *inside
the already-running preview container itself* makes the HTTP request to
`127.0.0.1:<preview_container_port>` and the raw response bytes come back
over the exec's stdout. This keeps the container on a fully default-deny
network (no published port at all, so nothing on the host network can
reach it directly either) while the platform backend — the only holder of
the Docker client — can still fetch pages through it. Routers/projects.py
(or a dedicated preview router) is expected to call `PreviewManager.proxy()`
per incoming signed-URL request.

At most one live preview per project (ADR 0016): starting a new preview
tears down any previous live preview container for the same project first.

Routing (prod shape; closes the earlier
deferral): when `settings.traefik_enabled`, the preview container
additionally carries Traefik Docker-provider labels routing
`preview--{project_id}.{apps_base_domain}` to it, guarded by a **forward-auth
middleware** pointed at `routers/preview_auth.py`'s verification endpoint —
the signed preview token (this module's existing HMAC scheme) remains the
sole credential (ADR 0016 posture unchanged: routing changes, access control
does not). The container stays on its own private `internal=True` network as
always; the route goes live by connecting the OPERATOR'S TRAEFIK CONTAINER
(`settings.traefik_container_name`) into that network right after start —
the same per-service-network mechanism as deployments (see
deploy_manager.py's module docstring for why this direction is the one that
actually works with Traefik's Docker provider, verified empirically). No
health gate applies to previews, unlike deployments — same as the existing
docker-exec proxy path, which also serves immediately. Teardown
(`stop_preview`) removes the container (dropping its router) and detaches
Traefik from the private network before destroying it.

Per ADR 0023 §Amendment 2:
a preview gets its OWN throwaway Postgres database/role + Redis container
(`services/preview_data.py`) — never the service's own data services. They
are provisioned right after the preview's network is created and BEFORE
the container starts (same reasoning as `deploy_manager.py`'s data-service
bracket: a future template's `alembic upgrade head` needs the database
reachable from the first instant), attached to the preview's network the
same way `deploy_manager._connect_data_services` attaches a deployment's,
and torn down — unconditionally, even if the preview's own container is
already gone — every time `stop_preview` runs, through all four teardown
triggers. `PreviewManager.start_preview` gains an `environment=` argument
composed ENTIRELY of preview-scoped, throwaway values
(`_preview_environment`) — `services.deploy.ensure_service_env` is never
called, imported, or read from this module. Skipped entirely (no
DATABASE_URL/REDIS_URL, matching the earlier shape) when
`settings.service_postgres_configured` is false — a preview has no
equivalent of a deployment's PROVISION_UNCONFIGURED refusal, since nothing
requires it to have a database to start at all.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.types import Ulimit

from app.config import settings
from app.services import preview_data
from app.services.container_log_config import capped_log_config
from app.services.platform_labels import LABEL_INSTANCE, Role, instance_id, platform_labels
from app.services.service_container_user import SERVICE_CONTAINER_USER
from app.services.status import PreviewStartFailure

logger = logging.getLogger("app.preview_manager")

# Container-name prefix so a crashed/restarted platform process can still
# find (and the sweep can still tear down) previews it started earlier.
PREVIEW_CONTAINER_PREFIX = "forge-preview"
PREVIEW_NETWORK_PREFIX = "forge-preview-net"


# The preview's user is `SERVICE_CONTAINER_USER`, shared with deploy containers.


class PreviewStartError(RuntimeError):
    """Raised when the preview container fails to start. `code` is the platform-authored
    `status.PreviewStartFailure` value -- the only part of the failure that is ever
    stored or shown; the message may carry Docker's text and stays in the worker log."""

    def __init__(self, message: str, code: str = PreviewStartFailure.UNEXPECTED) -> None:
        super().__init__(message)
        self.code = code


def _is_missing_image(exc: BaseException | None) -> bool:
    """True when `exc` or anything in its cause chain is Docker's ImageNotFound."""
    seen = 0
    while exc is not None and seen < 10:
        if isinstance(exc, ImageNotFound):
            return True
        exc = exc.__cause__
        seen += 1
    return False


class PreviewAccessError(RuntimeError):
    """Raised when a preview token is missing, invalid, or expired."""


class PreviewSigningSecretMissingError(RuntimeError):
    """The token signer was asked to work with no usable
    `preview_signing_secret`. A configuration fault, not an access decision --
    deliberately NOT a `PreviewAccessError`, which callers turn into a quiet
    403. `Settings` never hands an empty secret over (blank is read as unset);
    this is the signer's own refusal to HMAC over an empty key, which
    anyone can compute."""


def _parse_proxy_output(output: bytes) -> tuple[int, bytes, str | None]:
    """ADR 0028 layer 1: splits the exec'd script's stdout into
    `(status, body, content_type)`. Wire format: a status line, then a
    Content-Type line (blank when upstream sent none), then the raw body —
    written by `PreviewManager.proxy`'s own script above.

    Degrades to `(status, <everything after the status line>, None)` when
    there is no second line at all (decision 1's third constraint: "a
    status line with no header line after it — an older container, a
    truncated read — must degrade to the text/plain default, never to an
    exception"). `status` itself still raises `ValueError` on genuinely
    unparseable input, unchanged from before this task — only the new
    Content-Type line is allowed to be absent."""
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


# The exit code the in-script wall-clock deadline ends the exec with (the `timeout(1)` convention).
WALL_CLOCK_EXIT_CODE = 124


def _build_proxy_exec_script(
    url: str,
    method: str,
    timeout_seconds: float,
    max_body_bytes: int | None = None,
    body: bytes | None = None,
    content_type: str | None = None,
    wall_clock_seconds: float | None = None,
    follow_redirects: bool = True,
) -> str:
    """The one-liner `container.exec_run(["python", "-c", script])` runs
    *inside* the preview container against its own loopback. ADR 0028
    layer 1: line 1 of stdout is still the bare status code, byte-for-byte
    unchanged (the deploy health gate reads only that line); line 2, new,
    carries the upstream `Content-Type` (blank when upstream sent none);
    everything from line 3 on is the raw body (at most `max_body_bytes` of it
    when given, the eval judge's bounded read; `None` reads all of it, as before).
    **No other upstream header
    is ever read** — `resp.headers`/`e.headers` are touched exactly once
    each, both times for `'Content-Type'` — so there is no code path here
    that could forward `Set-Cookie` or anything else.

    `body` becomes `data=` (base64 inside the script, so no byte of it is
    ever spelled in the script text) and `content_type`, when given, is the ONE request header sent.
    Method, URL and header value go in through `repr`. With neither, the script is the one it was
    before (urllib then adds only its own `User-Agent`/`Host`; with a body and no `content_type` it
    also adds its default `Content-Type: application/x-www-form-urlencoded`). Extracted into its
    own function so `tests/test_proxy_isolation.py` can run it directly
    (via `subprocess`, against a local test server) without Docker.

    `urlopen(timeout=N)` bounds each socket operation, not the request, and
    `docker exec` has no wall-clock limit of its own, so a hostile app that drips a byte every few
    seconds (or chains redirects) could hold one call for hours. `wall_clock_seconds` puts a
    deadline INSIDE the script: a daemon `threading.Timer` that calls `os._exit(124)` (`_exit`, so
    nothing half-written to stdout is mistaken for an answer; a thread rather than `SIGALRM`
    because it needs no signal delivery into a blocked read and also runs on the Windows host
    the tests use). `follow_redirects=False` installs an
    opener without the redirect handler, so a 3xx arrives as the `HTTPError` the script already
    writes and its status is the 3xx itself. **With the defaults (`None`, `True`) the script is
    byte-for-byte the one it was**, so no existing caller changes."""
    read_arg = "" if max_body_bytes is None else str(int(max_body_bytes))
    request_args = [repr(url)]
    imports = "import urllib.request, sys"
    deadline_lines = ""
    if wall_clock_seconds is not None:
        imports += ", threading, os"
        deadline_lines = (
            f"_deadline = threading.Timer({float(wall_clock_seconds)!r}, os._exit, ({WALL_CLOCK_EXIT_CODE},))\n"
            "_deadline.daemon = True\n"
            "_deadline.start()\n"
        )
    open_call = f"urllib.request.urlopen(req, timeout={timeout_seconds})"
    opener_lines = ""
    if not follow_redirects:
        opener_lines = (
            "class _NoRedirect(urllib.request.HTTPRedirectHandler):\n"
            "    def redirect_request(self, *a, **k):\n"
            "        return None\n"
            "opener = urllib.request.build_opener(_NoRedirect)\n"
        )
        open_call = f"opener.open(req, timeout={timeout_seconds})"
    if body:
        imports += ", base64"
        request_args.append(f"data=base64.b64decode({base64.b64encode(body).decode('ascii')!r})")
    if content_type is not None:
        request_args.append(f"headers={{'Content-Type': {content_type!r}}}")
    request_args.append(f"method={method!r}")
    return (
        f"{imports}\n"
        f"{deadline_lines}"
        f"{opener_lines}"
        f"req = urllib.request.Request({', '.join(request_args)})\n"
        "try:\n"
        f"    with {open_call} as resp:\n"
        "        sys.stdout.buffer.write(str(resp.status).encode() + b'\\n')\n"
        "        sys.stdout.buffer.write((resp.headers.get('Content-Type') or '').encode() + b'\\n')\n"
        f"        sys.stdout.buffer.write(resp.read({read_arg}))\n"
        "except urllib.error.HTTPError as e:\n"
        "    sys.stdout.buffer.write(str(e.code).encode() + b'\\n')\n"
        "    sys.stdout.buffer.write((e.headers.get('Content-Type') or '').encode() + b'\\n')\n"
        f"    sys.stdout.buffer.write(e.read({read_arg}))\n"
    )


def _traefik_router_name(build_id: str) -> str:
    return f"forge-preview-{build_id}"


def traefik_preview_host(project_id: str) -> str:
    """Routing (prod): `preview--{project_id}.{apps_base_domain}`
    — `Project.id` stands in for the spec's `{project_slug}` (see
    deploy_manager.py's `traefik_service_labels` for the same choice)."""
    return f"preview--{project_id}.{settings.apps_base_domain}"


def traefik_preview_url(project_id: str, token: str) -> str:
    """the prod-shape preview URL, shown once Traefik
    routes directly to the preview instead of the dev-mode platform
    proxy. Scheme follows `settings.service_url_scheme` — plaintext by
    default, same as every other URL Traefik terminates for an external
    caller (this is also the URL a plaintext install carries the signed
    preview token — the credential itself — inside)."""
    return f"{settings.service_url_scheme}://{traefik_preview_host(project_id)}/?token={token}"


def traefik_preview_labels(*, project_id: str, build_id: str, network_name: str) -> dict[str, str]:
    """Docker labels routing the preview host to this build's preview
    container, guarded by a forward-auth middleware calling
    `routers/preview_auth.py`'s verification endpoint. Traefik's ForwardAuth
    does NOT append the original request's query string to its own request
    against `address` — it sends the original path+query in the
    `X-Forwarded-Uri` request header instead (verified empirically against
    real Traefik v3.6), which is exactly where `preview_auth` reads the
    `token` query param from. The auth address itself only needs the build
    id (already fixed per-router here), so the existing
    `verify_preview_token` rules (expired/tampered/wrong-build/superseded)
    apply completely unchanged. `network_name` is the preview's OWN private
    network (the per-service-network mechanism, see module docstring).

    the entrypoint is operator configuration
    (`settings.traefik_entrypoint`, default `"web"`); the forward-auth
    `address` follows `settings.traefik_forward_auth_scheme` (default
    `http`), NOT `service_url_scheme`: it is Traefik's hop to the host API on the
    Docker bridge, and `SERVICE_URL_SCHEME=https` must not turn it into https. TLS
    labels (`…tls` + `…tls.certresolver`) are added ONLY when
    `settings.traefik_cert_resolver` is set — unset emits neither key at
    all, never `tls=false`, matching the unset-sends-no-key
    precedent."""
    router = _traefik_router_name(build_id)
    host = traefik_preview_host(project_id)
    auth_url = (
        f"{settings.traefik_forward_auth_scheme}://{settings.traefik_forward_auth_host}"
        f"/api/internal/preview-auth/{build_id}"
    )
    labels = {
        "traefik.enable": "true",
        f"traefik.http.routers.{router}.rule": f"Host(`{host}`)",
        f"traefik.http.routers.{router}.entrypoints": settings.traefik_entrypoint,
        f"traefik.http.routers.{router}.middlewares": f"{router}-fwdauth",
        f"traefik.http.middlewares.{router}-fwdauth.forwardauth.address": auth_url,
        "traefik.docker.network": network_name,
        f"traefik.http.services.{router}.loadbalancer.server.port": str(
            settings.preview_container_port
        ),
    }
    if settings.traefik_cert_resolver:
        labels[f"traefik.http.routers.{router}.tls"] = "true"
        labels[f"traefik.http.routers.{router}.tls.certresolver"] = settings.traefik_cert_resolver
    return labels


@dataclass(frozen=True)
class PreviewIssued:
    url: str
    token: str  # raw token — caller stores only its hash (see token_hash)
    token_hash: str
    expires_at: datetime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sign(build_id: str, nonce: str, expires_at_ts: int) -> str:
    """HMAC-SHA256 over `build_id:nonce:expires_at`, keyed by
    `settings.preview_signing_secret`. The signature itself, plus the
    payload, forms the opaque bearer token — nothing beyond what's needed
    to validate it is ever persisted (`Build.preview_token_hash` stores
    only a hash of the whole token, never the secret or a reversible
    value)."""
    secret = settings.preview_signing_secret
    if secret is None or not secret.strip():
        raise PreviewSigningSecretMissingError(
            "preview_signing_secret is empty: refusing to sign or verify a preview token "
            "with an empty HMAC key."
        )
    payload = f"{build_id}:{nonce}:{expires_at_ts}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def issue_preview_token(build_id: str, *, ttl_minutes: int | None = None) -> tuple[str, datetime]:
    """Mint a signed, expiring preview token bound to `build_id`. Returns
    (token, expires_at). The token embeds its own payload + signature
    (`<build_id>.<nonce>.<expires_ts>.<signature>`) so validation needs only
    the signing secret, never a DB lookup by token value — callers still
    look the build up by id from the URL path and compare against the
    build's own `preview_token_hash` (belt-and-braces: a token only works
    for the exact build row that issued it)."""
    ttl = ttl_minutes if ttl_minutes is not None else settings.preview_ttl_minutes
    expires_at = _utcnow() + timedelta(minutes=ttl)
    nonce = uuid.uuid4().hex
    expires_ts = int(expires_at.timestamp())
    signature = _sign(build_id, nonce, expires_ts)
    token = f"{nonce}.{expires_ts}.{signature}"
    return token, expires_at


def hash_token(token: str) -> str:
    """One-way hash stored on `Build.preview_token_hash` — the raw token is
    never persisted, matching the logging rule (never log
    secrets/tokens in plaintext) applied to storage as well."""
    return hashlib.sha256(token.encode()).hexdigest()


def verify_preview_token(build_id: str, token: str, *, expected_hash: str | None) -> None:
    """Raises `PreviewAccessError` if the token is malformed, expired, its
    signature doesn't verify, or it doesn't match the build's stored hash
    (e.g. a superseded/replaced preview's old token)."""
    if not expected_hash:
        raise PreviewAccessError("no live preview for this build")

    parts = token.split(".")
    if len(parts) != 3:
        raise PreviewAccessError("malformed preview token")
    nonce, expires_ts_str, signature = parts
    try:
        expires_ts = int(expires_ts_str)
    except ValueError as exc:
        raise PreviewAccessError("malformed preview token") from exc

    expected_signature = _sign(build_id, nonce, expires_ts)
    # Bytes, not str: `compare_digest` on a str holding a non-ASCII character raises
    # TypeError, and the token is the client's -- a 500 with no isolation headers.
    if not hmac.compare_digest(signature.encode(), expected_signature.encode()):
        raise PreviewAccessError("invalid preview token signature")

    if _utcnow().timestamp() > expires_ts:
        raise PreviewAccessError("preview token expired")

    if not hmac.compare_digest(hash_token(token).encode(), expected_hash.encode()):
        raise PreviewAccessError("preview token does not match the live preview")


def _preview_base_url(project_id: str, build_id: str) -> str:
    """The preview's own idea of its base URL -- mirrors
    `services.deploy.ensure_service_env`'s shape (prod: the stable routed
    host; dev: the platform-proxy path) but points at the PREVIEW's own
    host/route, not the service's, and carries no token: the signed
    preview token gates ACCESS to that route (Traefik forward-auth, or the
    docker-exec proxy's own caller) -- the app running inside never needs
    to know it to serve a page. Computable before the container starts
    (unlike the URL `start_preview` ultimately returns to the caller,
    which embeds the token issued after the container exists)."""
    if settings.traefik_enabled:
        return f"{settings.service_url_scheme}://{traefik_preview_host(project_id)}"
    return f"/api/projects/{project_id}/preview/{build_id}"


def _preview_environment(
    *, project_id: str, build_id: str, data_services: dict[str, str]
) -> dict[str, str]:
    """Built in exactly ONE place, entirely from
    preview-scoped, throwaway values -- `ensure_service_env` is never
    called, imported, or read from this module (pinned by a scan,
    mirroring the role-literal ban: `tests/test_config.py::
    test_preview_manager_never_touches_the_service_env`).

    `FORGE_SERVICE_ID`/`FORGE_SERVICE_NAME` both use `project_id`:
    `PreviewManager` deliberately never touches the DB (module docstring),
    so it has no independent source for a project's display name, and
    this value is non-secret descriptive metadata, not a credential --
    reusing `project_id` for both costs nothing a real name would have
    bought (documented choice, see this task's `## Result`).
    `FORGE_APP_SECRET_KEY` is a fresh `secrets.token_urlsafe(32)` every
    call: it's the SERVICE's own secret and a preview runs unvetted L3
    output, so it must never repeat, let alone equal the service's own.
    `data_services` is `{}` when data services aren't configured for this
    preview (see module docstring) -- `dict.update` on an empty mapping
    adds neither key, matching the earlier shape exactly."""
    env = {
        "FORGE_SERVICE_ID": project_id,
        "FORGE_SERVICE_NAME": project_id,
        "FORGE_SERVICE_BASE_URL": _preview_base_url(project_id, build_id),
        "FORGE_APP_SECRET_KEY": secrets.token_urlsafe(32),
    }
    env.update(data_services)
    return env


class PreviewManager:
    """Starts/tears down TTL preview containers. One instance can be reused
    across builds — it holds no per-build state itself (all preview state
    lives on the `Build` row: `preview_url`/`preview_expires_at`/
    `preview_token_hash`, plus the container/network naming convention below
    so a restarted process can still find and sweep them)."""

    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self._client = client or docker.from_env()

    # -- naming -------------------------------------------------------

    def _container_name(self, build_id: str) -> str:
        return f"{PREVIEW_CONTAINER_PREFIX}-{build_id}"

    # -- lifecycle ------------------------------------------------------

    def start_preview(self, *, project_id: str, build) -> PreviewIssued:
        """Starts a fresh preview container from `build.image_ref`, tearing
        down any previous live preview for the same project first (ADR
        0016: at most one live preview per project). Returns the issued
        token/url/expiry; callers persist `preview_url`,
        `preview_expires_at`, and `hash_token(token)` onto the `Build` row
        in the same transaction (this module never touches the DB itself).
        """
        if not build.image_ref:
            raise PreviewStartError(
                f"build {build.id} has no image_ref to preview", PreviewStartFailure.NO_IMAGE
            )

        self.stop_preview_for_project(project_id, except_build_id=build.id)
        self.stop_preview(build.id)  # replace-in-place if this exact build already has one

        network = None
        try:
            network = self._client.networks.create(
                f"{PREVIEW_NETWORK_PREFIX}-{build.id}-{uuid.uuid4().hex[:8]}",
                driver="bridge",
                internal=True,
                # findable by the reconciler even when the container never existed.
                labels=platform_labels(
                    Role.PREVIEW_NETWORK, build_id=build.id, project_id=project_id
                ),
            )
        except DockerException as exc:
            raise PreviewStartError(
                f"preview network creation failed: {exc}", PreviewStartFailure.NETWORK_FAILED
            ) from exc

        # The same guarantee `DeployManager.start_service` has: a
        # start that raises leaves nothing of this attempt behind -- no network, no container,
        # no database, no Redis -- whatever the reason. The guarantee is made here, at the one
        # place that creates them, not at each failure the code below happens to know about:
        # everything after the network exists runs inside `_start_on_network`, and ANY exit
        # (a Docker error, an unexpected one, an interrupt) goes through `_abort_start` before
        # it propagates. The old code undid the data services and the network for four named
        # failures only, and for one of them (the second attach failing) it removed the
        # network WITHOUT detaching the shared Postgres the first attach had joined to it:
        # Docker refuses to remove a network with an endpoint, the refusal was swallowed, and
        # the network stayed. A failure after the container started (the token signer, a
        # missing secret) left the container, the data services and the network all behind.
        try:
            return self._start_on_network(network, project_id=project_id, build=build)
        except BaseException:  # noqa: BLE001 - re-raised below; cleanup must run for ANY exit
            self._abort_start(network, build.id)
            raise

    def _start_on_network(self, network, *, project_id: str, build) -> PreviewIssued:
        """Everything `start_preview` does once the network exists. Cleaning up a
        failure is NOT its job: `start_preview` wraps this whole body, so a statement
        added here is covered without anyone remembering to."""
        # ADR 0023 §Amendment 2: this preview's OWN throwaway
        # Postgres database/role + Redis container — created (and, on
        # network-attach failure, rolled back) BEFORE the container
        # starts, same reasoning as deploy_manager.py's data-service
        # bracket. Skipped entirely (matching the earlier shape) when no
        # admin DSN is configured — a preview has no PROVISION_UNCONFIGURED
        # refusal the way a deployment does; nothing requires
        # it to have a database to start at all.
        data_services: dict[str, str] = {}
        if settings.service_postgres_configured:
            try:
                data_services = preview_data.provision_preview_data_services(
                    build.id, docker_client=self._client
                )
            except preview_data.PreviewDataProvisionError as exc:
                raise PreviewStartError(
                    f"preview data services provisioning failed: {exc}",
                    PreviewStartFailure.IMAGE_MISSING
                    if _is_missing_image(exc)
                    else PreviewStartFailure.DATA_SERVICES_FAILED,
                ) from exc

            self._connect_data_services(network, build.id)

        # the platform's labels beside the preview marker and network name this
        # container has always carried.
        labels = platform_labels(
            Role.PREVIEW,
            project_id=project_id,
            build_id=build.id,
            extra={"forge.preview": "true", "forge.network": network.name},
        )
        if settings.traefik_enabled:
            # No health gate applies to previews (unlike deployments) — the
            # docker-exec proxy path already serves the container the
            # instant it starts, so routing it via Traefik at the same
            # point is not a new exposure window.
            labels.update(
                traefik_preview_labels(
                    project_id=project_id, build_id=build.id, network_name=network.name
                )
            )

        environment = _preview_environment(
            project_id=project_id, build_id=build.id, data_services=data_services
        )

        run_kwargs: dict = dict(
            image=build.image_ref,
            name=self._container_name(build.id),
            detach=True,
            network=network.name,
            environment=environment,
            mem_limit=f"{settings.sandbox_memory_limit_mb}m",
            nano_cpus=int(settings.sandbox_cpu_limit_cores * 1_000_000_000),
            pids_limit=settings.sandbox_pids_limit,
            security_opt=["no-new-privileges"],
            cap_drop=["ALL"],
            privileged=False,
            volumes={},
            ulimits=[
                Ulimit(
                    name="nproc",
                    soft=settings.sandbox_pids_limit,
                    hard=settings.sandbox_pids_limit,
                )
            ],
            labels=labels,
            log_config=capped_log_config(),
            # ADR 0046 §Decision 5: a preview runs unvetted L3 output, so not as root.
            # Measured on runc: the template app starts, serves, writes its
            # table and round-trips Redis as this uid with every capability dropped; the image
            # itself has no `USER`, so this is the container's `user=`, same as `SANDBOX_USER`.
            user=SERVICE_CONTAINER_USER,
        )
        # ADR 0046 §Decision 2: a preview is on the runtime seam, with its own setting
        # (default: the build runtime's value). Unset emits no `runtime` key at all (passing
        # None or "runc" is not the same as omitting it on a host with another default-runtime).
        # Deploy containers do not do this (ADR 0046: L4 stays on the default runtime).
        preview_runtime = settings.effective_preview_runtime
        if preview_runtime:
            run_kwargs["runtime"] = preview_runtime
        try:
            container = self._client.containers.run(**run_kwargs)
        except DockerException as exc:
            raise PreviewStartError(
                f"preview container failed to start: {exc}",
                PreviewStartFailure.IMAGE_MISSING
                if _is_missing_image(exc)
                else PreviewStartFailure.CONTAINER_START_FAILED,
            ) from exc

        if settings.traefik_enabled:
            try:
                traefik = self._client.containers.get(settings.traefik_container_name)
                network.connect(traefik)
            except (NotFound, DockerException, APIError) as exc:
                # Best-effort, matching the dev-mode proxy's own posture: a
                # preview whose Traefik route fails to go live still exists
                # and is reachable via the docker-exec proxy path, and the
                # TTL sweep tears it down regardless — never fail preview
                # issuance over the prod-shape route alone.
                logger.warning(
                    "preview %s: failed to attach traefik to preview network: %s", build.id, exc
                )

        token, expires_at = issue_preview_token(build.id)
        if settings.traefik_enabled:
            # Prod shape: the direct Traefik-routed host, guarded by
            # forward-auth — the platform-proxy path above still exists but
            # is no longer the one shown to users once Traefik fronts it.
            # scheme follows settings.service_url_scheme.
            url = traefik_preview_url(project_id, token)
        else:
            url = f"/api/projects/{project_id}/preview/{build.id}?token={token}"

        logger.info(
            "preview started: project=%s build=%s container=%s expires_at=%s",
            project_id,
            build.id,
            container.id[:12],
            expires_at.isoformat(),
        )

        return PreviewIssued(url=url, token=token, token_hash=hash_token(token), expires_at=expires_at)

    def _abort_start(self, network, build_id: str) -> None:
        """undoes a preview start that did not finish, in the order Docker
        needs, every step on its own so none can skip the next. The container first, by
        NAME (`containers.run` creates and then starts, so a failed start leaves a
        `Created` container nothing references, and its endpoint would block the
        network's removal); then the data services (the Postgres database and role and
        the preview's Redis container -- dropping the database also closes anything
        still connected to it); then every endpoint still on the network (the shared
        Postgres and Traefik are the platform's own attachments); then the network.
        Never raises: it runs while another exception is propagating, and replacing
        that one would hide the cause. What could not be removed is logged at ERROR,
        once, with the build id that finds it."""
        left: list[str] = []
        try:
            try:
                self._client.containers.get(self._container_name(build_id)).remove(v=True, force=True)
            except NotFound:
                pass
        except Exception:  # noqa: BLE001 - never replace the exception being handled
            logger.exception("preview %s: removing its container after a failed start raised", build_id)
            left.append("its container")
        try:
            preview_data.drop_preview_data_services(build_id, docker_client=self._client)
        except Exception:  # noqa: BLE001
            logger.exception("preview %s: dropping its data services after a failed start raised", build_id)
            left.append("its data services")
        try:
            self._empty_network(network)
        except Exception:  # noqa: BLE001
            logger.exception("preview %s: detaching its network's endpoints after a failed start raised", build_id)
        try:
            if not self._remove_network(network):
                left.append(f"network {getattr(network, 'name', '?')}")
        except Exception:  # noqa: BLE001
            logger.exception("preview %s: removing its network after a failed start raised", build_id)
            left.append("its network")
        if left:
            logger.error(
                "preview %s: a failed start left %s behind; the orphan sweep and `stop_preview` find it by the build id",
                build_id,
                " and ".join(left),
            )

    def _empty_network(self, network) -> None:
        """Best-effort: force-detaches every endpoint still on a preview's private
        network, because Docker refuses to remove a network that has one. Everything
        that can be attached to it is the platform's own doing (the shared Postgres,
        this preview's Redis, Traefik), so no list of names is needed and an endpoint a
        failed start attached but never recorded cannot be missed."""
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

    def _remove_network(self, network) -> bool:
        """True when the network no longer exists afterwards (already gone counts). A
        refusal is logged, so it is never silent."""
        try:
            network.remove()
        except NotFound:
            return True
        except (APIError, DockerException) as exc:
            logger.error("preview network %s could not be removed: %s", getattr(network, "name", "?"), exc)
            return False
        return True

    def stop_preview(self, build_id: str) -> bool:
        """Tears down the preview container + its network AND its data
        services (ADR 0023 §Amendment 2) for one build. Returns
        True if the container itself was found and removed. Best-effort
        and idempotent — safe to call on a build with no live preview.

        **The data-services drop runs unconditionally**, even when the
        container is already gone (decision (a)): the case that used to
        leak was exactly that one — the container gone and the database
        not. This is the single choke point every implemented teardown
        trigger (TTL expiry, a newer build, promotion) funnels through, so
        fixing it here fixes it for all three at once."""
        name = self._container_name(build_id)
        container = None
        try:
            container = self._client.containers.get(name)
        except (NotFound, DockerException):
            container = None

        network_name = None
        if container is not None:
            network_name = container.labels.get("forge.network") if container.labels else None
            try:
                container.remove(v=True, force=True)
            except (APIError, DockerException):
                pass

        if network_name:
            try:
                network = self._client.networks.get(network_name)
                self._disconnect_traefik(network)
                self._disconnect_data_services(network, build_id)
                self._destroy_network(network)
            except (NotFound, DockerException):
                pass

        try:
            preview_data.drop_preview_data_services(build_id, docker_client=self._client)
        except Exception:  # noqa: BLE001 - teardown must never crash the caller (worker sweep, promotion, replace-in-place)
            logger.exception("preview %s: failed to drop preview data services", build_id)

        return container is not None

    def _disconnect_traefik(self, network) -> None:
        """Best-effort: detach the Traefik container from a
        preview's private network before destroying it (an attached
        endpoint would make the network removal fail). Safe when Traefik
        was never attached or routing is disabled."""
        if not settings.traefik_enabled:
            return
        try:
            network.disconnect(settings.traefik_container_name)
        except (NotFound, APIError, DockerException):
            pass

    def _connect_data_services(self, network, build_id: str) -> None:
        """ADR 0023 §Amendment 1, applied to previews: attaches
        the shared Postgres and THIS PREVIEW's own Redis container to its
        network, mirroring `deploy_manager.DeployManager.
        _connect_data_services` exactly — needed before the container
        starts, not merely by health, for the same reason a deployment's
        is. Guarded on `service_postgres_configured` the same way; never
        reached in production when it's false, since `start_preview`
        already skips the whole data-services block in that case — this
        guard only protects a caller that drives this method directly."""
        if not settings.service_postgres_configured:
            return
        try:
            postgres = self._client.containers.get(settings.service_postgres_container_name)
            network.connect(postgres)
            redis = self._client.containers.get(preview_data.preview_redis_container_name(build_id))
            network.connect(redis)
        except (NotFound, DockerException, APIError) as exc:
            raise PreviewStartError(
                f"failed to attach data services to preview {build_id}'s network: {exc}",
                PreviewStartFailure.DATA_SERVICES_FAILED,
            ) from exc

    def _disconnect_data_services(self, network, build_id: str | None) -> None:
        """Best-effort, mirroring `_disconnect_traefik` /
        `deploy_manager.DeployManager._disconnect_data_services`: detaches
        the shared Postgres and this preview's Redis from its private
        network before it is destroyed (an attached endpoint would make
        network removal fail)."""
        if not settings.service_postgres_configured:
            return
        try:
            network.disconnect(settings.service_postgres_container_name)
        except (NotFound, APIError, DockerException):
            pass
        if build_id:
            try:
                network.disconnect(preview_data.preview_redis_container_name(build_id))
            except (NotFound, APIError, DockerException):
                pass

    def stop_preview_for_project(self, project_id: str, *, except_build_id: str | None = None) -> int:
        """Tears down every live preview container for a project (ADR 0016:
        at most one live preview per project — used before starting a new
        one, and available standalone for e.g. manual stop).

        **A container is torn down only if it positively IS this project's
        preview** — `forge.preview=true` + this `forge.project_id` + a
        `forge.build_id` — and anything else the project's label matches is
        left alone ("teardown removes
        only what it positively owns"). This used to remove every
        project-labelled container that had no build id, which is exactly
        the project's production Redis (`forge.data_service=redis` +
        `forge.project_id`), so the first successful modify build of a
        deployed project deleted its live Redis. The Docker-side filter only
        narrows the list; the per-container check below is what decides, so
        it does not depend on how the engine combines two label filters.

        **And only this installation's (ADR 0037 §Decision 2):** the listing asks for
        this installation's `forge.instance` too and each container is checked for it, so a
        project id that another installation also has (a copied database, a test run) cannot
        make this one remove that installation's preview."""
        stopped = 0
        instance = instance_id()
        try:
            containers = self._client.containers.list(
                all=True,
                filters={
                    "label": [
                        "forge.preview=true",
                        f"forge.project_id={project_id}",
                        f"{LABEL_INSTANCE}={instance}",
                    ]
                },
            )
        except DockerException:
            return 0

        for container in containers:
            labels = container.labels or {}
            if labels.get("forge.preview") != "true" or labels.get("forge.project_id") != project_id:
                continue
            if labels.get(LABEL_INSTANCE) != instance:
                continue
            build_id = labels.get("forge.build_id")
            if not build_id or build_id == except_build_id:
                continue
            if self.stop_preview(build_id):
                stopped += 1
        return stopped

    def sweep_expired(self, expired_build_ids: list[str]) -> int:
        """Tears down previews for builds whose `preview_expires_at` has
        already passed (the caller determines expiry from the DB — this
        module has no notion of "expired" beyond the token's own
        timestamp). Returns the count actually torn down. Called by the
        worker sweep every loop iteration (teardown rule:
        "Teardown on: TTL expiry...")."""
        count = 0
        for build_id in expired_build_ids:
            if self.stop_preview(build_id):
                count += 1
        return count

    def _destroy_network(self, network) -> None:
        try:
            network.remove()
        except (APIError, DockerException):
            pass

    # -- proxying (routing mechanism, see module docstring) --------------

    def proxy(
        self,
        build_id: str,
        *,
        method: str = "GET",
        path: str = "/",
        timeout_seconds: float = 5.0,
        max_body_bytes: int | None = None,
        query: str = "",
        body: bytes | None = None,
        content_type: str | None = None,
        wall_clock_seconds: float | None = None,
        follow_redirects: bool = True,
    ) -> tuple[int, bytes, str | None]:
        """Forwards one HTTP request into the preview container via
        `docker exec` (see module docstring for why: the container has no
        published host port at all — `internal=True` network, true
        default-deny). Returns (status_code, body_bytes, content_type) —
        `content_type` is ADR 0028 layer 1's raw material, the upstream
        `Content-Type` header value or `None` when there was none; the
        caller (`routers/preview_proxy.py`) turns that into the actual
        response headers via `services/proxy_headers.build_isolation_headers`.
        Raises `PreviewStartError` if the preview container isn't running.

        Retries briefly on a refused connection: a preview is typically hit
        right after its container starts (from the build-success timeline
        entry), and the app inside needs a moment to begin listening — a
        refused connect inside the retry window is startup lag, not an
        error.

        `query` (already percent-encoded, no leading `?`), `body` and
        `content_type` are what the platform route lets through to the container; `path` is
        expected already percent-encoded by that route. A retry replays the same script, so a
        refused connect never delivers a body twice (the connection was never made).

        `wall_clock_seconds`, when given, bounds the WHOLE call -- every exec
        carries an in-script deadline of what is left of it, and the retry loop gives up
        when it is spent; an exec that hit its deadline (exit 124) raises `PreviewStartError`
        with fixed text, no output kept. `follow_redirects=False` makes a 3xx its own answer.
        With the defaults the behaviour (and the script) is what it was."""
        name = self._container_name(build_id)
        try:
            container = self._client.containers.get(name)
        except NotFound as exc:
            raise PreviewStartError(f"no live preview container for build {build_id}") from exc
        except DockerException as exc:
            raise PreviewStartError(f"could not reach preview container: {exc}") from exc

        if container.status != "running":
            raise PreviewStartError(f"preview container for build {build_id} is not running")

        port = settings.preview_container_port
        safe_path = path if path.startswith("/") else f"/{path}"
        # deliberately NOT settings.service_url_scheme.
        # This request is executed by docker exec *inside* the preview
        # container against its own loopback — the app there always speaks
        # plain HTTP regardless of what Traefik terminates externally, so a
        # "https" scheme here would just break the dev-mode proxy path.
        url = f"http://127.0.0.1:{port}{safe_path}" + (f"?{query}" if query else "")

        def build_script(wall_clock: float | None) -> str:
            return _build_proxy_exec_script(
                url,
                method,
                timeout_seconds,
                max_body_bytes,
                body=body,
                content_type=content_type,
                wall_clock_seconds=wall_clock,
                follow_redirects=follow_redirects,
            )

        started = time.monotonic()
        script = build_script(wall_clock_seconds)
        exit_code, output = container.exec_run(["python", "-c", script])

        deadline = started + max(timeout_seconds, 1.0)
        call_deadline = None if wall_clock_seconds is None else started + wall_clock_seconds
        while exit_code != 0:
            if exit_code == WALL_CLOCK_EXIT_CODE and call_deadline is not None:
                raise PreviewStartError("preview proxy exec ran past its wall-clock deadline")
            if b"Connection refused" not in output or time.monotonic() >= deadline:
                raise PreviewStartError(
                    f"preview proxy exec failed (exit {exit_code}): {output!r}"
                )
            time.sleep(0.3)
            if call_deadline is not None:
                remaining = call_deadline - time.monotonic()
                if remaining <= 0:
                    raise PreviewStartError("preview proxy exec ran past its wall-clock deadline")
                script = build_script(remaining)
            exit_code, output = container.exec_run(["python", "-c", script])

        try:
            status, body, content_type = _parse_proxy_output(output)
        except ValueError:
            raise PreviewStartError(f"preview proxy returned an unparseable response: {output!r}")
        return status, body, content_type
