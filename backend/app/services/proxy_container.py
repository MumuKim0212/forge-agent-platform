"""Lifecycle management for one build's LLM egress proxy container
(ADR 0014).

The proxy runs as its own Docker container so its `TokenRegistry` — and the
real API key — never share a process with anything the sandbox
touches. One container runs one dialect with that dialect's one key (ADR 0035):
the same image serves both, selected by `FORGE_PROXY_DIALECT`.
`ProxyContainer` starts one proxy container per build, connects it
to that build's per-run sandbox network (multi-homed: also on the default
bridge so it can reach the real Anthropic API), issues a proxy token via
the admin API (called through `exec_run` inside the proxy container itself
— see `_exec_admin_request`, not a host-to-container HTTP call), and tears
everything down at build end regardless of outcome.

Verified against the real Docker Engine (see
tests/test_proxy_container_integration.py): a container attached to both
the default bridge network and a fresh `internal=True` network is reachable
from containers on that internal network while retaining external egress —
the same multi-homing technique is exactly what makes an `internal=True`
sandbox network (ADR 0003 invariant 3) able to reach the proxy at all.
"""

from __future__ import annotations

import json
import logging
import secrets
import time

import docker
from docker.errors import APIError, DockerException, ImageNotFound
from docker.models.containers import Container
from docker.models.networks import Network

from app.config import settings
from app.services.platform_labels import Role, platform_labels
from app.services.proxy_dialects import DEFAULT_DIALECT, get_dialect

logger = logging.getLogger("app.proxy_container")

# this container is L2's but
# L3 can reach it, and it holds the real key, so it gets L3-grade hardening.
#
# Memory: the proxy buffers one request body (bounded, `llm_proxy.MAX_REQUEST_BODY_BYTES`,
# 4 MiB) and one upstream response at a time per in-flight call, keeps a bounded build
# trace (2 MB of content, `TRACE_*`), and runs one uvicorn process. 512 MiB is above that, and far
# below what a leak or a hostile request would otherwise take from the host. The number that
# matters is the worst-case PARSE of a hostile body (about 33x its size) beside a full queue of
# buffered bodies: ~222 MiB at 4 MiB, measured and derived beside the constant in `llm_proxy.py`
# (at the old 16 MiB one request could take 612 MiB and kill this container).
# Processes: uvicorn, its worker threads and the `exec`'d admin calls; 128 is generous.
# Constants, not settings: nothing here is worth an operator's tuning.
PROXY_MEMORY_LIMIT = "512m"
PROXY_PIDS_LIMIT = 128
# nobody:nogroup, the same user the sandbox runs the agent as. Passed at run as well as
# declared in the image (`docker/llm-proxy/Dockerfile`'s USER), so an image built before
# that line existed still does not run as root.
PROXY_USER = "65534:65534"
# The admin routes (`/_forge_admin/*`) are served on THIS port, bound to the container's
# own loopback and nothing else; the sandbox-facing listener (`settings.llm_proxy_port`)
# serves only the proxy routes. The worker reaches the admin port by `exec` inside the
# container (`_exec_admin_request`), which is on loopback by construction.
PROXY_ADMIN_PORT = 8802
# how much longer than the drain's own wait the exec'd call may take (the exec itself,
# the JSON answer). Added to the wait, so the call's limit is never the thing that ends it.
_DRAIN_EXEC_MARGIN_SECONDS = 5.0

# The image says what it can do, and the worker checks it. The image
# is built by hand from the backend tree, so a worker on newer code than its image is a real state
# (the drain route is missing from an older one, and every build's usage read then fails closed
# AFTER a full paid agent run). A LABEL on the image, not a field on `/_forge_admin/health`: a label
# is read with one `image inspect`, which creates no container, no network and handles no key, so
# the same read can run at worker start (the banner) and before every build's proxy container exists.
# The health field would say the same thing, but only from a running proxy, so the start check would
# have to start a throwaway one. The cost of a label is that it is a statement stamped at build time,
# not a measurement of the route: `docker/llm-proxy/Dockerfile` carries it, and a test pins the
# Dockerfile's value to this tuple; the real-container drain tests are what prove the claim.
# Contract strings, so literals.
PROXY_IMAGE = "forge-llm-proxy:latest"
PROXY_CAPABILITIES_LABEL = "forge.proxy.capabilities"
# What THIS worker needs of the proxy it starts. `drain`: close a build's token and wait for
# the calls still alive on it, so the usage read after it is final.
REQUIRED_PROXY_CAPABILITIES = ("drain",)


class ProxyStartError(RuntimeError):
    """Raised when the proxy container fails to start or become healthy."""


class ProxyImageStaleError(ProxyStartError):
    """The proxy image was inspected and does not advertise a capability the platform needs, so no
    container was started from it. The message is platform-authored (an image name, capability
    names and the rebuild command), so it may reach a build's failure detail."""


def _rebuild_command(image: str) -> str:
    return f"docker build -t {image} -f docker/llm-proxy/Dockerfile . (run from backend/)"


def advertised_proxy_capabilities(client: docker.DockerClient, image: str) -> frozenset[str]:
    """The capabilities `image` advertises in its `forge.proxy.capabilities` label (a comma-separated
    list), read with one `image inspect`. Empty for an image with no such label, or one whose value is
    not text (fail closed: unreadable is not "can do"). Raises Docker's own errors (`ImageNotFound`,
    an unreachable engine) for the caller to word."""
    labels = client.images.get(image).labels
    value = labels.get(PROXY_CAPABILITIES_LABEL) if isinstance(labels, dict) else None
    if not isinstance(value, str):
        return frozenset()
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _missing_capabilities(advertised: frozenset[str]) -> tuple[str, ...]:
    return tuple(name for name in REQUIRED_PROXY_CAPABILITIES if name not in advertised)


def _stale_image_sentence(image: str, advertised: frozenset[str], missing: tuple[str, ...]) -> str:
    return (
        f"the LLM proxy image {image!r} does not advertise {', '.join(missing)} "
        f"(its {PROXY_CAPABILITIES_LABEL!r} label is {'missing it' if advertised else 'absent'}; an image "
        "built before the label existed has none, whatever it can do), so it cannot be trusted to record a build's "
        f"cost and every build is refused before its agent runs; rebuild it with: {_rebuild_command(image)}"
    )


def proxy_image_gap(client: docker.DockerClient, image: str = PROXY_IMAGE) -> str | None:
    """None when `image` advertises everything the worker needs of it, else one sentence naming the
    gap and the rebuild command. The worker logs it at start (`worker._log_proxy_image_startup_banner`)
    and `ProxyContainer.start` refuses on the same finding. An image that is not there, or an engine
    that cannot be asked, is also a gap (the capabilities could not be verified), worded as such."""
    try:
        advertised = advertised_proxy_capabilities(client, image)
    except ImageNotFound:
        return (
            f"the LLM proxy image {image!r} was not found on this host, so no build can start its "
            f"proxy; build it with: {_rebuild_command(image)}"
        )
    except (DockerException, OSError) as exc:  # OSError: `requests`' own errors, which docker-py does not wrap
        return (
            f"the LLM proxy image {image!r} could not be inspected ({type(exc).__name__}), so its "
            "capabilities could not be verified"
        )
    missing = _missing_capabilities(advertised)
    return _stale_image_sentence(image, advertised, missing) if missing else None


class ProxyContainer:
    """One LLM egress proxy container, scoped to a single build.

    Usage:
        proxy = ProxyContainer(client=docker_client)
        proxy.start(real_api_key=..., sandbox_network=network, dialect="anthropic")
        token = proxy.issue_token(build_id, token_budget=..., cost_budget_usd=...)
        ... run the sandbox with ANTHROPIC_BASE_URL=proxy.base_url_for_sandbox ...
        proxy.drain(token, timeout_seconds=...)   # nothing of the token is still running
        usage = proxy.usage(token)
        proxy.revoke(token)
        proxy.stop()
    """

    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self._client = client or docker.from_env()
        self._container: Container | None = None
        self._admin_secret = secrets.token_urlsafe(24)
        self._sandbox_ip: str | None = None
        self._port = settings.llm_proxy_port

    @property
    def base_url_for_sandbox(self) -> str:
        """The base URL (without any `/v1`) the sandbox container should point
        its engine at — the proxy's IP on the shared per-run sandbox network."""
        if self._sandbox_ip is None:
            raise ProxyStartError("proxy container not started")
        return f"http://{self._sandbox_ip}:{self._port}"

    def start(
        self,
        *,
        real_api_key: str,
        sandbox_network: Network,
        upstream_base_url: str | None = None,
        image: str = PROXY_IMAGE,
        startup_timeout_seconds: float = 15.0,
        dialect: str = DEFAULT_DIALECT,
        build_id: str | None = None,
    ) -> None:
        """`dialect` is `anthropic` or `openai` (anything else raises
        `UnknownDialectError` before a container exists); `real_api_key` is that
        dialect's key, and it is passed under that dialect's variable only.

        `build_id` is the build this proxy serves: it goes on the container as
        `forge.build_id`, which is how the reconciler decides the container may not exist
        any more (its build is no longer RUNNING). This container holds the real API key,
        so a worker that dies without reaching `stop()` is exactly the case the label
        exists for.

        an image that does not advertise `REQUIRED_PROXY_CAPABILITIES` raises
        `ProxyImageStaleError` BEFORE any container exists (a proxy that cannot drain would fail
        its build's usage read only after the agent had run and spent). An image that cannot be
        inspected (absent, or the engine does not answer) is not refused here: the run below fails
        on its own, as it always did."""
        resolved = get_dialect(dialect)
        if self._port == PROXY_ADMIN_PORT:
            raise ProxyStartError(
                f"llm_proxy_port {self._port} is the proxy's admin port; the sandbox-facing "
                "listener must use another"
            )
        try:
            advertised = advertised_proxy_capabilities(self._client, image)
        except (DockerException, OSError):  # OSError: `requests`' own errors, which docker-py does not wrap
            advertised = None
        if advertised is not None:
            missing = _missing_capabilities(advertised)
            if missing:
                raise ProxyImageStaleError(_stale_image_sentence(image, advertised, missing))
        env = {
            "FORGE_PROXY_DIALECT": resolved.name,
            resolved.key_env_var: real_api_key,
            "FORGE_PROXY_ADMIN_SECRET": self._admin_secret,
            "FORGE_PROXY_PORT": str(self._port),
            "FORGE_PROXY_ADMIN_PORT": str(PROXY_ADMIN_PORT),
        }
        if upstream_base_url:
            env["FORGE_PROXY_UPSTREAM_BASE_URL"] = upstream_base_url

        try:
            self._container = self._client.containers.run(
                image=image,
                detach=True,
                environment=env,
                # L3-grade hardening (see the constants above). Nothing is
                # published, nothing is mounted, and no capability is kept: the
                # process binds two unprivileged ports and makes outbound calls.
                mem_limit=PROXY_MEMORY_LIMIT,
                pids_limit=PROXY_PIDS_LIMIT,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                user=PROXY_USER,
                labels=platform_labels(Role.LLM_PROXY, build_id=build_id),
                # Default bridge network for external Anthropic-API
                # reachability; connected to the sandbox's internal
                # network below so the sandbox can reach it too
                # (multi-homing, verified above).
            )
        except DockerException as exc:
            raise ProxyStartError(f"proxy container failed to start: {exc}") from exc

        try:
            sandbox_network.connect(self._container)
            self._container.reload()
            self._sandbox_ip = self._container.attrs["NetworkSettings"]["Networks"][
                sandbox_network.name
            ]["IPAddress"]
            self._wait_until_healthy(startup_timeout_seconds)
        except Exception:
            self.stop()
            raise

    def _wait_until_healthy(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_output = b""
        while time.monotonic() < deadline:
            exit_code, output = self._exec_admin_request("GET", "/_forge_admin/health")
            if exit_code == 0:
                return
            last_output = output
            time.sleep(0.3)
        raise ProxyStartError(f"proxy container did not become healthy: {last_output!r}")

    def _exec_admin_request(
        self, method: str, path: str, *, body: dict | None = None, timeout_seconds: float = 5.0
    ) -> tuple[int, bytes]:
        """Runs one admin-API call **from inside the proxy container
        itself** (`exec_run` against `127.0.0.1`), not from the host
        process or via the sandbox network's assigned IP.

        The proxy's sandbox-network IP is reachable from *other containers*
        on that network (verified in
        tests/test_proxy_container_integration.py — that's the whole point
        of ADR 0014's multi-homing) but is not guaranteed reachable from
        the Docker host process itself; this has been observed to hang
        under Docker Desktop's Windows/WSL2 networking specifically. Exec'ing
        into the already-running proxy container and calling `127.0.0.1`
        sidesteps host-to-container routing entirely and works identically
        across engines/platforms.

        `timeout_seconds` is the in-container call's own limit (default 5: every admin read
        answers at once). `drain` is the one call that waits on purpose, and passes its wait
        plus a margin.
        """
        assert self._container is not None
        url = f"http://127.0.0.1:{PROXY_ADMIN_PORT}{path}"
        body_literal = repr(json.dumps(body).encode()) if body is not None else "None"
        headers = {"X-Forge-Admin-Secret": self._admin_secret}
        if body is not None:
            headers["Content-Type"] = "application/json"
        script = (
            "import urllib.request, sys\n"
            f"req = urllib.request.Request({url!r}, method={method!r}, "
            f"headers={headers!r}, data={body_literal})\n"
            "try:\n"
            f"    with urllib.request.urlopen(req, timeout={float(timeout_seconds)!r}) as resp:\n"
            "        sys.stdout.buffer.write(resp.read())\n"
            "except urllib.error.HTTPError as e:\n"
            "    sys.stdout.buffer.write(e.read())\n"
            "    sys.exit(e.code)\n"
        )
        return self._container.exec_run(["python", "-c", script])

    def issue_token(self, build_id: str, *, token_budget: int, cost_budget_usd: float) -> str:
        exit_code, output = self._exec_admin_request(
            "POST",
            "/_forge_admin/tokens",
            body={"build_id": build_id, "token_budget": token_budget, "cost_budget_usd": cost_budget_usd},
        )
        if exit_code != 0:
            raise ProxyStartError(f"issue_token failed ({exit_code}): {output!r}")
        return json.loads(output)["token"]

    def drain(self, token: str, *, timeout_seconds: float) -> bool:
        """Closes `token` on the proxy (no new request is admitted,
        a queued one is refused rather than forwarded) and waits up to `timeout_seconds` for the
        requests already alive to finish and record. True: nothing of the token is left, so the
        `usage` and `trace` read next are final. False: the wait ran out with a call still alive,
        so a read now could be short.

        One exec carries the whole wait, so it must stay under the Docker client's own read
        timeout (docker-py's default is 60 s): the in-container call is given the wait plus a
        margin. The executor therefore asks in STEPS of at most 30 s and repeats until idle, up to a
        total that covers the proxy's upstream timeout (`close` is idempotent, so each step
        simply waits on). Raises `ProxyStartError` for anything but a well-formed answer, including the 404
        a proxy image built before the drain route existed gives (it has no such route); the caller treats every
        raise as "the usage could not be read back safely"."""
        exit_code, output = self._exec_admin_request(
            "POST",
            f"/_forge_admin/tokens/{token}/drain?timeout_seconds={float(timeout_seconds)!r}",
            timeout_seconds=float(timeout_seconds) + _DRAIN_EXEC_MARGIN_SECONDS,
        )
        if exit_code != 0:
            raise ProxyStartError(
                f"drain failed ({exit_code}): {output[:200]!r} (a proxy image built before the drain route existed "
                "has no drain route: rebuild forge-llm-proxy:latest)"
            )
        try:
            drained = json.loads(output)["drained"]
        except (ValueError, KeyError, TypeError):
            raise ProxyStartError(f"the drain answer is not readable: {output[:200]!r}") from None
        if not isinstance(drained, bool):
            raise ProxyStartError(f"the drain answer's 'drained' is not a boolean: {output[:200]!r}")
        return drained

    def usage(self, token: str) -> dict:
        exit_code, output = self._exec_admin_request(
            "GET", f"/_forge_admin/tokens/{token}/usage"
        )
        if exit_code != 0:
            raise ProxyStartError(f"usage lookup failed ({exit_code}): {output!r}")
        return json.loads(output)

    def trace(self, token: str) -> dict:
        """The build's bounded call record, a sibling read of `usage` and read
        separately on purpose: the two never share a failure. Large (up to a few MB), so
        it is read once, at build end."""
        exit_code, output = self._exec_admin_request(
            "GET", f"/_forge_admin/tokens/{token}/trace"
        )
        if exit_code != 0:
            raise ProxyStartError(f"trace lookup failed ({exit_code}): {output[:200]!r}")
        return json.loads(output)

    def revoke(self, token: str) -> None:
        try:
            self._exec_admin_request("DELETE", f"/_forge_admin/tokens/{token}")
        except Exception as exc:  # noqa: BLE001 - best-effort; container teardown makes the token unusable regardless
            # any exception, not only Docker's own -- a docker-py `ReadTimeout` or
            # `ConnectionError` is a `requests` error, and one escaping here skipped every
            # teardown step after it. The class name only: the call's text carries the token.
            logger.warning("proxy container: the token could not be revoked (%s)", type(exc).__name__)

    def stop(self) -> None:
        """Unconditional teardown, best-effort — mirrors SandboxRunner._destroy
        so a proxy failure never leaks a container. Never raises, whatever
        the exception (the container is cleared either way, so a second call is a
        no-op and `execute`'s teardown cannot be ended early by this one)."""
        if self._container is not None:
            try:
                self._container.remove(v=True, force=True)
            except Exception as exc:  # noqa: BLE001 - teardown never raises
                logger.warning(
                    "proxy container: container %s could not be removed (%s)",
                    str(getattr(self._container, "id", "?"))[:12],
                    type(exc).__name__,
                )
            self._container = None
