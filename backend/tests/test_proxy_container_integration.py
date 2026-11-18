"""Integration tests for the LLM proxy container (ADR 0014) against a
real Docker Engine. Marked `integration`; skipped cleanly when Docker is
unavailable. Proves: a proxy container multi-homed onto a fresh
`internal=True` sandbox network is reachable from a container on that
network (proving egress allowlisting works end to end) while an arbitrary
external host from the *sandbox's* side is still blocked (ADR 0003
invariant 3, unchanged by the addition of the proxy to the allowlist).

Requires the `forge-llm-proxy:latest` image to be built locally
(`docker build -t forge-llm-proxy:latest -f docker/llm-proxy/Dockerfile .`
from `backend/`) — skips cleanly if the image is absent too, since these
tests exercise the real packaged image, not a Python-level stub.
"""

from __future__ import annotations

import tempfile
import threading
import time

import httpx
import pytest

pytest.importorskip("docker")

import docker  # noqa: E402
from docker.errors import DockerException, ImageNotFound  # noqa: E402

from app.services import sandbox_build_executor as sbe  # noqa: E402
from app.services.agent_adapter import AgentAdapterResult  # noqa: E402
from app.services.proxy_container import (  # noqa: E402
    ProxyContainer,
    ProxyImageStaleError,
    ProxyStartError,
    advertised_proxy_capabilities,
    proxy_image_gap,
)
from app.services.sandbox_runner import SandboxRunConfig, SandboxRunner  # noqa: E402
from app.services.status import BuildFailureReason  # noqa: E402

PROXY_IMAGE = "forge-llm-proxy:latest"


def _docker_available() -> bool:
    try:
        client = docker.from_env()
        client.ping()
        return True
    except DockerException:
        return False


def _proxy_image_available() -> bool:
    if not _docker_available():
        return False
    try:
        docker.from_env().images.get(PROXY_IMAGE)
        return True
    except (ImageNotFound, DockerException):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _proxy_image_available(), reason=f"{PROXY_IMAGE} not built locally"),
]


@pytest.fixture(autouse=True)
def _keep_harvests_out_of_the_host_temp_dir(tmp_path_factory, monkeypatch):
    """the two tests that run a real `SandboxRunner` here discard its result,
    which leaves a `forge-harvest-*` directory in the host's temp dir per run.
    Pointing the temp root at a pytest-owned directory keeps them out of it."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("systemp")))


@pytest.fixture()
def docker_client():
    return docker.from_env()


@pytest.fixture()
def sandbox_network(docker_client):
    network = docker_client.networks.create(
        "forge-proxy-it-net", driver="bridge", internal=True
    )
    yield network
    try:
        network.remove()
    except DockerException:
        pass


@pytest.fixture()
def stub_upstream_url():
    """A fake Anthropic API the proxy container forwards to over the real
    Docker bridge — no real key involved."""
    return "http://httpbin.org"  # any host is fine; tests never assert response body from it


def test_proxy_reachable_from_sandbox_network(docker_client, sandbox_network):
    """Core ADR 0014 proof: the sandbox can reach the proxy over the
    isolated per-run network."""
    proxy = ProxyContainer(client=docker_client)
    try:
        proxy.start(
            real_api_key="fake-key-for-test",
            sandbox_network=sandbox_network,
            upstream_base_url="http://example.invalid",
            image=PROXY_IMAGE,
        )
        token = proxy.issue_token("build-x", token_budget=1000, cost_budget_usd=10.0)
        assert token

        proxy_url = httpx.URL(proxy.base_url_for_sandbox)
        script = (
            "import socket; socket.setdefaulttimeout(3); "
            f"socket.create_connection(('{proxy_url.host}', {proxy_url.port})); print('REACHED')"
        )
        runner = SandboxRunner(client=docker_client)
        config = SandboxRunConfig(
            command=["python", "-c", script],
            workspace_dir=_empty_workspace(),
            network=sandbox_network,
        )
        result = runner.run(config)

        assert result.ok is True
        assert "REACHED" in result.logs
    finally:
        proxy.stop()


def test_arbitrary_external_host_still_blocked_from_sandbox(docker_client, sandbox_network):
    """Adding the proxy to the allowlist must not open the network up
    generally — ADR 0003 invariant 3 still holds for anything else."""
    proxy = ProxyContainer(client=docker_client)
    try:
        proxy.start(
            real_api_key="fake-key-for-test",
            sandbox_network=sandbox_network,
            upstream_base_url="http://example.invalid",
            image=PROXY_IMAGE,
        )

        runner = SandboxRunner(client=docker_client)
        config = SandboxRunConfig(
            command=[
                "sh",
                "-c",
                "python -c \"import socket; socket.setdefaulttimeout(3); "
                "socket.create_connection(('1.1.1.1', 443))\"",
            ],
            workspace_dir=_empty_workspace(),
            network=sandbox_network,
            timeout_seconds=15,
        )
        result = runner.run(config)

        assert result.ok is False
        assert result.reason == BuildFailureReason.AGENT_ERROR  # nonzero exit: connection refused/blocked
    finally:
        proxy.stop()


def _post_from_sandbox_side(docker_client, network, url: str, token: str) -> int:
    """Makes an HTTP POST from inside a throwaway container on `network` —
    exercises the same host-routing path a real sandbox uses (the proxy's
    address is only meaningfully reachable from other containers on its
    internal network, not from the test process on the Docker host; see
    ProxyContainer._wait_until_healthy)."""
    script = (
        "import urllib.request, sys\n"
        f"req = urllib.request.Request('{url}', data=b'{{}}', headers={{'x-api-key': '{token}'}}, method='POST')\n"
        "try:\n"
        "    urllib.request.urlopen(req, timeout=5)\n"
        "    print(200)\n"
        "except urllib.error.HTTPError as e:\n"
        "    print(e.code)\n"
    )
    output = docker_client.containers.run(
        "python:3.11-slim", ["python", "-c", script], network=network.name, remove=True
    )
    return int(output.decode().strip())


def test_proxy_token_revocation_blocks_further_calls(docker_client, sandbox_network):
    proxy = ProxyContainer(client=docker_client)
    try:
        proxy.start(
            real_api_key="fake-key-for-test",
            sandbox_network=sandbox_network,
            upstream_base_url="http://example.invalid",
            image=PROXY_IMAGE,
        )
        token = proxy.issue_token("build-y", token_budget=1000, cost_budget_usd=10.0)

        proxy.revoke(token)

        status = _post_from_sandbox_side(
            docker_client, sandbox_network, f"{proxy.base_url_for_sandbox}/v1/messages", token
        )
        assert status == 401
    finally:
        proxy.stop()


def test_unknown_token_rejected_with_401_403(docker_client, sandbox_network):
    proxy = ProxyContainer(client=docker_client)
    try:
        proxy.start(
            real_api_key="fake-key-for-test",
            sandbox_network=sandbox_network,
            upstream_base_url="http://example.invalid",
            image=PROXY_IMAGE,
        )

        status = _post_from_sandbox_side(
            docker_client,
            sandbox_network,
            f"{proxy.base_url_for_sandbox}/v1/messages",
            "totally-unknown-token",
        )
        assert status in (401, 403)
    finally:
        proxy.stop()


def _post_json_from_sandbox_side(
    docker_client,
    network,
    url: str,
    token: str,
    body: str,
    extra_headers: dict | None = None,
    timeout_seconds: int = 5,
) -> tuple[int, str]:
    """Like `_post_from_sandbox_side`, with a chosen body (and, optionally, more request headers);
    returns (status, response text). `timeout_seconds` is the request's own limit (the drain test holds a call
    for four seconds)."""
    headers = {"x-api-key": token, "content-type": "application/json", **(extra_headers or {})}
    script = "\n".join(
        [
            "import urllib.request, sys, json",
            f"req = urllib.request.Request('{url}', data={body!r}.encode(), "
            f"headers={headers!r}, method='POST')",
            "try:",
            f"    r = urllib.request.urlopen(req, timeout={timeout_seconds})",
            "    print(json.dumps([r.status, r.read().decode()]))",
            "except urllib.error.HTTPError as e:",
            "    print(json.dumps([e.code, e.read().decode()]))",
        ]
    )
    import json

    output = docker_client.containers.run(
        "python:3.11-slim", ["python", "-c", script], network=network.name, remove=True
    )
    status, text = json.loads(output.decode().strip())
    return status, text


def test_tool_policy_refusal_is_enforced_by_the_real_proxy_and_read_back(docker_client, sandbox_network):
    """ADR 0029 §2, against the packaged image: a server-tool request
    from the sandbox side is refused (403, never forwarded -- the upstream here
    is unresolvable, so a forwarded request would be a 502, not a 403) and shows
    up in the same `usage()` read-back the executor performs. A stale
    `forge-llm-proxy:latest` predating the tool policy fails HERE, loudly: rebuild it."""
    proxy = ProxyContainer(client=docker_client)
    try:
        proxy.start(
            real_api_key="fake-key-for-test",
            sandbox_network=sandbox_network,
            upstream_base_url="http://example.invalid",
            image=PROXY_IMAGE,
        )
        token = proxy.issue_token("build-policy", token_budget=1000, cost_budget_usd=10.0)
        url = f"{proxy.base_url_for_sandbox}/v1/messages"

        status, text = _post_json_from_sandbox_side(
            docker_client,
            sandbox_network,
            url,
            token,
            '{"model": "m", "tools": [{"type": "web_search_20250305", "name": "web_search"}]}',
        )

        assert status == 403, (status, text, "is forge-llm-proxy:latest stale? rebuild it")
        assert "tool policy: tool type 'web_search_20250305' is not allowed" in text

        usage = proxy.usage(token)
        assert usage["policy_refusals"] == ["tool policy: tool type 'web_search_20250305' is not allowed"]
        assert usage["tokens"] == 0 and usage["cost_usd"] == 0.0
        # the packaged image reports the unmetered flag the executor reads back.
        assert usage["unmetered"] is False and usage["unmetered_reason"] is None

        # An allowed shape is NOT refused: it is forwarded and (upstream being
        # unresolvable) comes back as the proxy's own ENGINE_UNREACHABLE 502.
        status, text = _post_json_from_sandbox_side(
            docker_client, sandbox_network, url, token, '{"model": "claude-opus-4-8", "tools": [{"name": "Bash"}]}'
        )
        assert status == 502
        assert proxy.usage(token)["policy_refusals"] == [
            "tool policy: tool type 'web_search_20250305' is not allowed"
        ]
    finally:
        proxy.stop()


def test_the_model_allowlist_and_the_beta_rule_are_enforced_by_the_real_proxy(docker_client, sandbox_network):
    """Against the packaged image: a request naming a model with no price row, and one
    carrying an unmeasured `anthropic-beta`, are each refused 403 and recorded (never forwarded --
    the upstream is unresolvable, so a forwarded request would be a 502), while a dated snapshot
    of a priced model and a request carrying only measured betas are forwarded (502). A stale
    `forge-llm-proxy:latest` predating the model allowlist fails HERE, loudly: rebuild it."""
    proxy = ProxyContainer(client=docker_client)
    try:
        proxy.start(
            real_api_key="fake-key-for-test",
            sandbox_network=sandbox_network,
            upstream_base_url="http://example.invalid",
            image=PROXY_IMAGE,
        )
        token = proxy.issue_token("build-caseaf", token_budget=1000, cost_budget_usd=10.0)
        url = f"{proxy.base_url_for_sandbox}/v1/messages"
        hint = "is forge-llm-proxy:latest stale? rebuild it"

        status, text = _post_json_from_sandbox_side(
            docker_client, sandbox_network, url, token, '{"model": "claude-invented-9", "max_tokens": 1}'
        )
        assert status == 403, (status, text, hint)
        assert "tool policy: model 'claude-invented-9' has no price row, so it is not allowed" in text

        status, text = _post_json_from_sandbox_side(
            docker_client,
            sandbox_network,
            url,
            token,
            '{"model": "claude-opus-4-8", "max_tokens": 1}',
            {"anthropic-beta": "claude-code-20250219,context-1m-2025-08-07"},
        )
        assert status == 403, (status, text, hint)
        assert "tool policy: anthropic-beta value 'context-1m-2025-08-07' is not allowed" in text

        refusals = proxy.usage(token)["policy_refusals"]
        assert refusals == [
            "tool policy: model 'claude-invented-9' has no price row, so it is not allowed",
            "tool policy: anthropic-beta value 'context-1m-2025-08-07' is not allowed",
        ]

        # Not refused: a snapshot of a priced model, and the measured betas.
        status, _ = _post_json_from_sandbox_side(
            docker_client,
            sandbox_network,
            url,
            token,
            '{"model": "claude-opus-5-5-20260301", "max_tokens": 1}',
            {"anthropic-beta": "claude-code-20250219,effort-2025-11-24"},
        )
        assert status == 502
        assert len(proxy.usage(token)["policy_refusals"]) == 2
    finally:
        proxy.stop()


def test_batches_endpoint_is_refused_by_the_real_proxy(docker_client, sandbox_network):
    """ADR 0029 amendment: the gate's Batches probe -- a server tool and
    mcp_servers nested under requests[].params, which the top-level body rule
    cannot see -- is refused on the endpoint, against the packaged image. The
    upstream here is unresolvable, so a forwarded request would be a 502."""
    proxy = ProxyContainer(client=docker_client)
    try:
        proxy.start(
            real_api_key="fake-key-for-test",
            sandbox_network=sandbox_network,
            upstream_base_url="http://example.invalid",
            image=PROXY_IMAGE,
        )
        token = proxy.issue_token("build-batches", token_budget=1000, cost_budget_usd=10.0)
        body = (
            '{"requests": [{"custom_id": "x", "params": {"model": "m", "max_tokens": 8, '
            '"messages": [{"role": "user", "content": "hi"}], '
            '"tools": [{"type": "web_search_20250305", "name": "web_search"}], '
            '"mcp_servers": [{"type": "url", "url": "https://evil.example", "name": "e"}]}}]}'
        )

        status, text = _post_json_from_sandbox_side(
            docker_client, sandbox_network, f"{proxy.base_url_for_sandbox}/v1/messages/batches", token, body
        )

        assert status == 403, (status, text, "is forge-llm-proxy:latest stale (older than the endpoint allowlist)? rebuild it")
        assert "endpoint POST /v1/messages/batches is not allowed" in text
        assert proxy.usage(token)["policy_refusals"] == [
            "tool policy: endpoint POST /v1/messages/batches is not allowed"
        ]
    finally:
        proxy.stop()


def test_proxy_container_removed_after_stop(docker_client, sandbox_network):
    proxy = ProxyContainer(client=docker_client)
    before_ids = {c.id for c in docker_client.containers.list(all=True)}

    proxy.start(
        real_api_key="fake-key-for-test",
        sandbox_network=sandbox_network,
        upstream_base_url="http://example.invalid",
        image=PROXY_IMAGE,
    )
    proxy.stop()

    after_ids = {c.id for c in docker_client.containers.list(all=True)}
    assert after_ids - before_ids == set()


def test_start_raises_when_image_missing(docker_client, sandbox_network):
    proxy = ProxyContainer(client=docker_client)
    with pytest.raises(Exception):
        proxy.start(
            real_api_key="fake-key",
            sandbox_network=sandbox_network,
            image="forge-llm-proxy-does-not-exist:latest",
            startup_timeout_seconds=2.0,
        )


def _empty_workspace() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="forge-proxy-it-workspace-")


# -- the proxy container is hardened, its admin routes are not served to the sandbox, and
# its request body is bounded -- each against the packaged image ---------------------------------


def _started_proxy(docker_client, sandbox_network, **start_kwargs) -> ProxyContainer:
    proxy = ProxyContainer(client=docker_client)
    proxy.start(
        real_api_key="fake-key-for-test",
        sandbox_network=sandbox_network,
        upstream_base_url="http://example.invalid",
        image=PROXY_IMAGE,
        **start_kwargs,
    )
    return proxy


def test_the_proxy_container_is_capped_stripped_of_capabilities_and_not_root(docker_client, sandbox_network):
    """The container that holds the real key, inspected as the engine sees it. Literals, not the
    constants `proxy_container.py` passes, so a change to either side is a deliberate edit."""
    proxy = _started_proxy(docker_client, sandbox_network)
    try:
        inspected = docker_client.api.inspect_container(proxy._container.id)
        host_config = inspected["HostConfig"]

        assert host_config["Memory"] == 512 * 1024 * 1024
        assert host_config["PidsLimit"] == 128
        assert host_config["CapDrop"] == ["ALL"]
        assert not host_config.get("CapAdd")
        assert "no-new-privileges" in host_config["SecurityOpt"]
        assert host_config["Privileged"] is False
        assert not host_config.get("Binds")  # no host path, and no Docker socket
        assert not host_config.get("PortBindings")  # nothing published
        assert inspected["Config"]["User"] == "65534:65534"

        # ...and the process really is that user, not just configured to be.
        exit_code, output = proxy._container.exec_run(["python", "-c", "import os; print(os.getuid(), os.getgid())"])
        assert exit_code == 0 and output.decode().strip() == "65534 65534"
    finally:
        proxy.stop()


def test_the_admin_routes_are_not_served_to_the_sandbox_network_but_still_work_over_loopback(
    docker_client, sandbox_network
):
    """From a container on the sandbox network, with the real admin secret in hand: the proxy
    port answers 404 for an admin route (it is not served there), and the admin port is not
    listening on any address the sandbox can reach. The worker's own path -- `exec` inside the
    container, on loopback -- still issues, reads and revokes."""
    proxy = _started_proxy(docker_client, sandbox_network)
    try:
        host = httpx.URL(proxy.base_url_for_sandbox).host
        script = "\n".join(
            [
                "import socket, urllib.request, urllib.error",
                "def status(method, url, headers):",
                "    req = urllib.request.Request(url, method=method, headers=headers, data=b'{}' if method == 'POST' else None)",
                "    try:",
                "        return urllib.request.urlopen(req, timeout=5).status",
                "    except urllib.error.HTTPError as e:",
                "        return e.code",
                f"secret = {{'X-Forge-Admin-Secret': {proxy._admin_secret!r}}}",
                f"print('health-on-proxy-port', status('GET', 'http://{host}:8801/_forge_admin/health', {{}}))",
                f"print('health-with-secret', status('GET', 'http://{host}:8801/_forge_admin/health', secret))",
                f"print('issue-with-secret', status('POST', 'http://{host}:8801/_forge_admin/tokens', secret))",
                "try:",
                f"    socket.create_connection(('{host}', 8802), timeout=3)",
                "    print('admin-port CONNECTED')",
                "except OSError:",
                "    print('admin-port refused')",
            ]
        )

        output = docker_client.containers.run(
            "python:3.11-slim", ["python", "-c", script], network=sandbox_network.name, remove=True
        ).decode()

        assert "health-on-proxy-port 404" in output, output
        assert "health-with-secret 404" in output, output
        assert "issue-with-secret 404" in output, output
        assert "admin-port refused" in output, output
        token = proxy.issue_token("build-admin", token_budget=1000, cost_budget_usd=1.0)  # loopback, by exec
        assert proxy.usage(token)["tokens"] == 0
        proxy.revoke(token)
    finally:
        proxy.stop()


def _raw_request_status_line(docker_client, network, host: str, port: int, request: str) -> str:
    script = "\n".join(
        [
            "import socket",
            f"s = socket.create_connection(({host!r}, {port}), timeout=5)",
            f"s.sendall({request.encode()!r})",
            "print(s.recv(200).split(b'\\r\\n')[0].decode())",
        ]
    )
    return (
        docker_client.containers.run("python:3.11-slim", ["python", "-c", script], network=network.name, remove=True)
        .decode()
        .strip()
    )


def test_a_body_over_the_limit_is_refused_413_by_the_real_container_and_a_small_one_is_not(
    docker_client, sandbox_network
):
    """The declared-length refusal against the packaged image: 20 MB declared (the limit is 16 MiB),
    no body sent at all, and the proxy answers 413 without waiting for one. The control is the same
    request with a small body, which is forwarded and (the upstream being unresolvable) comes back as
    the proxy's own 502 -- so the 413 is the limit, not some other refusal of this request."""
    proxy = _started_proxy(docker_client, sandbox_network)
    try:
        token = proxy.issue_token("build-413", token_budget=1000, cost_budget_usd=1.0)
        host = httpx.URL(proxy.base_url_for_sandbox).host

        priced_body = '{"model": "claude-opus-4-8"}'  # a request must name a model with a price row

        def request(length: int, body: str = "") -> str:
            return (
                "POST /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
                f"x-api-key: {token}\r\nContent-Type: application/json\r\nContent-Length: {length}\r\n\r\n{body}"
            )

        over = _raw_request_status_line(docker_client, sandbox_network, host, 8801, request(20_000_000))
        small = _raw_request_status_line(
            docker_client, sandbox_network, host, 8801, request(len(priced_body), priced_body)
        )

        assert over.startswith("HTTP/1.1 413"), over
        assert small.startswith("HTTP/1.1 502"), small
    finally:
        proxy.stop()


# -- a call that outlives its sandbox is waited for before the usage is read -------------------

# A stub provider that HOLDS every call for four seconds (a fixed time: the test is bounded by it
# whatever the proxy does) and prints a line when a request arrives, so a test can tell the call is out.
_SLOW_UPSTREAM_SCRIPT = """
import json, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get('content-length') or 0))
        print('REQUEST_STARTED', flush=True)
        time.sleep(4)
        body = json.dumps({'id': 'msg_1', 'model': 'claude-opus-4-8',
                           'usage': {'input_tokens': 10, 'output_tokens': 5}}).encode()
        self.send_response(200)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

ThreadingHTTPServer(('0.0.0.0', 8080), H).serve_forever()
"""

_MESSAGES_BODY = '{"model": "claude-opus-4-8", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}'


@pytest.fixture()
def slow_upstream(docker_client, sandbox_network):
    container = docker_client.containers.run(
        "python:3.11-slim", ["python", "-c", _SLOW_UPSTREAM_SCRIPT], detach=True, network=sandbox_network.name
    )
    try:
        container.reload()
        ip = container.attrs["NetworkSettings"]["Networks"][sandbox_network.name]["IPAddress"]
        yield container, f"http://{ip}:8080"
    finally:
        container.remove(v=True, force=True)


def _call_from_the_sandbox_in_the_background(docker_client, network, proxy, token):
    """The sandbox's request, on a thread: returns (thread, result list). The call is held by the
    upstream for four seconds, so the thread ends by itself; the join below is bounded."""
    result: list = []

    def run():
        try:
            result.append(
                _post_json_from_sandbox_side(
                    docker_client,
                    network,
                    f"{proxy.base_url_for_sandbox}/v1/messages",
                    token,
                    _MESSAGES_BODY,
                    timeout_seconds=30,
                )
            )
        except Exception as exc:  # noqa: BLE001 - reported by the assertions
            result.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


def _wait_until_the_upstream_has_the_call(upstream_container) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if "REQUEST_STARTED" in upstream_container.logs().decode("utf-8", "replace"):
            return
        time.sleep(0.2)
    raise AssertionError("the call never reached the upstream")


def test_a_drain_in_the_real_container_waits_for_the_call_in_flight_and_the_usage_then_includes_it(
    docker_client, sandbox_network, slow_upstream
):
    """Against the packaged image, the two listeners and the real exec. The sandbox's call is
    held by the upstream (four seconds); the drain is asked while it is out. When it returns the call
    has finished and recorded, so the usage read straight after holds its 15 tokens (a read made when
    the drain began would have said 0), and the token, closed, admits no further request. A stale
    `forge-llm-proxy:latest` predating the drain fails HERE (no drain route): rebuild it."""
    upstream_container, upstream_url = slow_upstream
    proxy = ProxyContainer(client=docker_client)
    try:
        proxy.start(
            real_api_key="fake-key-for-test",
            sandbox_network=sandbox_network,
            upstream_base_url=upstream_url,
            image=PROXY_IMAGE,
        )
        token = proxy.issue_token("build-drain", token_budget=1_000_000, cost_budget_usd=100.0)
        thread, call = _call_from_the_sandbox_in_the_background(docker_client, sandbox_network, proxy, token)
        try:
            _wait_until_the_upstream_has_the_call(upstream_container)

            drained = proxy.drain(token, timeout_seconds=30)
            usage = proxy.usage(token)
        finally:
            thread.join(timeout=60)

        assert drained is True, "is forge-llm-proxy:latest stale (older than the drain route)? rebuild it"
        assert usage["tokens"] == 15 and usage["unmetered"] is False
        assert not thread.is_alive() and call and call[0][0] == 200, call  # the call was not cut short

        # the closed token: the 401 a revoked one gets, and the record still readable
        status, _ = _post_json_from_sandbox_side(
            docker_client, sandbox_network, f"{proxy.base_url_for_sandbox}/v1/messages", token, _MESSAGES_BODY
        )
        assert status == 401
        assert proxy.usage(token)["tokens"] == 15
        proxy.revoke(token)
        with pytest.raises(ProxyStartError):
            proxy.usage(token)
    finally:
        proxy.stop()


def test_a_drain_in_the_real_container_that_runs_out_says_false_in_about_its_wait_and_a_later_one_says_true(
    docker_client, sandbox_network, slow_upstream
):
    upstream_container, upstream_url = slow_upstream
    proxy = ProxyContainer(client=docker_client)
    try:
        proxy.start(
            real_api_key="fake-key-for-test",
            sandbox_network=sandbox_network,
            upstream_base_url=upstream_url,
            image=PROXY_IMAGE,
        )
        token = proxy.issue_token("build-drain-short", token_budget=1_000_000, cost_budget_usd=100.0)
        thread, call = _call_from_the_sandbox_in_the_background(docker_client, sandbox_network, proxy, token)
        try:
            _wait_until_the_upstream_has_the_call(upstream_container)

            started = time.monotonic()
            short = proxy.drain(token, timeout_seconds=1.0)
            waited = time.monotonic() - started
            later = proxy.drain(token, timeout_seconds=30)
            usage = proxy.usage(token)
        finally:
            thread.join(timeout=60)

        assert short is False
        assert 1.0 <= waited < 20  # it waited for what it was given, and returned (it did not wait for the call: `short` is False)
        assert later is True
        assert usage["tokens"] == 15
        assert call and call[0][0] == 200, call
    finally:
        proxy.stop()


def test_a_drain_of_an_unknown_token_in_the_real_container_raises(docker_client, sandbox_network):
    proxy = _started_proxy(docker_client, sandbox_network)
    try:
        with pytest.raises(ProxyStartError) as raised:
            proxy.drain("no-such-token", timeout_seconds=1)
        # the exit code is the HTTP status modulo 256 (404 -> 148), so the body is what names the cause
        assert "unknown token" in str(raised.value)
    finally:
        proxy.stop()


# -- the executor's drain loop in a real container, and the image's capability label ------------


def test_the_executors_drain_loop_waits_in_steps_for_a_call_longer_than_one_step_and_the_usage_includes_it(
    docker_client, sandbox_network, slow_upstream, monkeypatch
):
    """Against the packaged image and the real exec. The upstream holds the sandbox's call for
    four seconds; the executor's steps are one second (the total thirty), so the first steps answer
    `drained: false` after their wait and a later one answers true. The usage read after it holds the
    call's 15 tokens. A single step (one second) would have raised `_DrainError` here instead."""
    monkeypatch.setattr(sbe, "PROXY_DRAIN_STEP_SECONDS", 1.0)
    monkeypatch.setattr(sbe, "PROXY_DRAIN_TOTAL_SECONDS", 30.0)
    upstream_container, upstream_url = slow_upstream
    proxy = ProxyContainer(client=docker_client)
    try:
        proxy.start(
            real_api_key="fake-key-for-test",
            sandbox_network=sandbox_network,
            upstream_base_url=upstream_url,
            image=PROXY_IMAGE,
        )
        token = proxy.issue_token("build-drain-steps", token_budget=1_000_000, cost_budget_usd=100.0)
        waits: list[float] = []
        real_drain = proxy.drain

        def spying_drain(token, *, timeout_seconds):
            waits.append(timeout_seconds)
            return real_drain(token, timeout_seconds=timeout_seconds)

        monkeypatch.setattr(proxy, "drain", spying_drain)
        thread, call = _call_from_the_sandbox_in_the_background(docker_client, sandbox_network, proxy, token)
        try:
            _wait_until_the_upstream_has_the_call(upstream_container)

            usage = sbe.SandboxBuildExecutor._read_proxy_usage(proxy, token)
        finally:
            thread.join(timeout=60)

        assert usage["tokens"] == 15 and usage["unmetered"] is False
        assert len(waits) >= 2 and all(wait <= 1.0 for wait in waits), waits  # asked again, a step at a time
        assert not thread.is_alive() and call and call[0][0] == 200, call  # the call was not cut short
    finally:
        proxy.stop()


def test_the_executors_drain_that_runs_out_in_a_real_container_raises_and_reads_no_usage(
    docker_client, sandbox_network, slow_upstream, monkeypatch
):
    """The same call, held four seconds, against a total of one second in steps of half a second: the
    drain runs out with the call alive, so `_DrainError` is raised and no figure is returned."""
    monkeypatch.setattr(sbe, "PROXY_DRAIN_STEP_SECONDS", 0.5)
    monkeypatch.setattr(sbe, "PROXY_DRAIN_TOTAL_SECONDS", 1.0)
    upstream_container, upstream_url = slow_upstream
    proxy = ProxyContainer(client=docker_client)
    try:
        proxy.start(
            real_api_key="fake-key-for-test",
            sandbox_network=sandbox_network,
            upstream_base_url=upstream_url,
            image=PROXY_IMAGE,
        )
        token = proxy.issue_token("build-drain-runout", token_budget=1_000_000, cost_budget_usd=100.0)
        thread, _call = _call_from_the_sandbox_in_the_background(docker_client, sandbox_network, proxy, token)
        try:
            _wait_until_the_upstream_has_the_call(upstream_container)

            with pytest.raises(sbe._DrainError) as raised:
                sbe.SandboxBuildExecutor._read_proxy_usage(proxy, token)
        finally:
            thread.join(timeout=60)

        assert "still in flight on the proxy after waiting 1 s" in str(raised.value)
        assert token not in str(raised.value)
    finally:
        proxy.stop()


STALE_PROXY_IMAGE = "forge-llm-proxy:pre-drain"  # the predecessor of the drain image, kept on a host that built both


def _image_available(tag: str) -> bool:
    try:
        docker.from_env().images.get(tag)
        return True
    except (ImageNotFound, DockerException):
        return False


needs_the_stale_proxy_image = pytest.mark.skipif(
    not _image_available(STALE_PROXY_IMAGE),
    reason=f"{STALE_PROXY_IMAGE} (a build from before the drain route) is not kept on this host",
)


def test_the_packaged_image_advertises_drain_in_its_label(docker_client):
    """Literals: what the image carries, as the engine reports it, against what the worker needs. A
    `forge-llm-proxy:latest` built before the capability label fails HERE (no label): rebuild it."""
    assert advertised_proxy_capabilities(docker_client, PROXY_IMAGE) == {"drain"}
    assert proxy_image_gap(docker_client, PROXY_IMAGE) is None


@needs_the_stale_proxy_image
def test_an_image_from_before_the_drain_route_is_reported_and_refused_before_any_container_exists(docker_client, sandbox_network):
    """The real stale image (the drain route is missing from it; observed exit 148 on a drain against
    it): the worker's start check names it, and `start` refuses without creating a container."""
    gap = proxy_image_gap(docker_client, STALE_PROXY_IMAGE)

    assert gap is not None
    assert f"'{STALE_PROXY_IMAGE}'" in gap and "does not advertise drain" in gap
    assert f"docker build -t {STALE_PROXY_IMAGE} -f docker/llm-proxy/Dockerfile ." in gap

    before_ids = {c.id for c in docker_client.containers.list(all=True)}
    proxy = ProxyContainer(client=docker_client)
    with pytest.raises(ProxyImageStaleError):
        proxy.start(real_api_key="fake-key-for-test", sandbox_network=sandbox_network, image=STALE_PROXY_IMAGE)
    after_ids = {c.id for c in docker_client.containers.list(all=True)}
    assert after_ids - before_ids == set()


def _startup_banner_for(docker_client, image: str, caplog) -> tuple[str, str]:
    """What the worker's real start banner says for an executor whose proxy image is `image`, on the
    real engine: (level, message) of the one line it logs."""
    from types import SimpleNamespace

    from app import worker

    executor = sbe.SandboxBuildExecutor(docker_client=docker_client, adapter=SimpleNamespace(), proxy_image=image)
    with caplog.at_level("INFO", logger="app.worker"):
        worker._log_proxy_image_startup_banner(executor)
    (record,) = caplog.records
    return record.levelname, record.getMessage()


def test_the_worker_start_banner_says_the_packaged_image_can_drain(docker_client, caplog):
    level, message = _startup_banner_for(docker_client, PROXY_IMAGE, caplog)

    assert level == "INFO"
    assert message.startswith("LLM proxy image: forge-llm-proxy:latest advertises drain")


@needs_the_stale_proxy_image
def test_the_worker_start_banner_reports_the_real_stale_image_by_name_with_the_rebuild_command(docker_client, caplog):
    """The start-time report, end to end on the real engine and the real image that predates the drain route:
    a WARNING (startup is not blocked) naming the image, the missing capability and the rebuild command."""
    level, message = _startup_banner_for(docker_client, STALE_PROXY_IMAGE, caplog)

    assert level == "WARNING"
    assert message.startswith("LLM proxy image: the LLM proxy image 'forge-llm-proxy:pre-drain' does not advertise drain")
    assert "docker build -t forge-llm-proxy:pre-drain -f docker/llm-proxy/Dockerfile ." in message


@needs_the_stale_proxy_image
def test_a_build_against_the_stale_image_is_refused_before_its_agent_runs_and_leaves_nothing(
    docker_client, make_build_with_request
):
    """End to end against the real engine and the real stale image: the executor creates the
    build's network, `start` refuses, and the result is INFRA_ERROR with the rebuild command. The
    agent adapter never runs (so nothing is spent), and neither a container nor a network of the build
    is left behind."""
    ran: list[bool] = []

    class _TripwireAdapter:
        def run(self, adapter_input):
            ran.append(True)
            return AgentAdapterResult(ok=False, reason=BuildFailureReason.AGENT_ERROR)

    executor = sbe.SandboxBuildExecutor(
        docker_client=docker_client, adapter=_TripwireAdapter(), proxy_image=STALE_PROXY_IMAGE
    )
    build = make_build_with_request()

    result = executor.execute(build)

    assert ran == []
    assert result.ok is False and result.reason == BuildFailureReason.INFRA_ERROR
    assert result.summary.startswith(
        "no agent ran: the LLM proxy image 'forge-llm-proxy:pre-drain' does not advertise drain"
    )
    assert "docker build -t forge-llm-proxy:pre-drain -f docker/llm-proxy/Dockerfile ." in result.summary
    by_build = {"label": f"forge.build_id={build.id}"}
    assert docker_client.containers.list(all=True, filters=by_build) == []
    assert docker_client.networks.list(filters=by_build) == []
    assert build.cost_tokens is None and build.cost_usd is None
