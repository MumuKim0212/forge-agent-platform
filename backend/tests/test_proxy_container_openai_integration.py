"""ADR 0035 §Decision 3: the PACKAGED proxy image serves the `openai` dialect, end to end on a
real Docker Engine. Marked `integration`; skipped cleanly when Docker or the image is absent.

Requires `forge-llm-proxy:latest` built from the tree (`docker build -t forge-llm-proxy:latest -f
docker/llm-proxy/Dockerfile .` from `backend/`): one image serves both dialects, so a stale image would
not know `FORGE_PROXY_DIALECT` and would serve the anthropic dialect to these requests.

The upstream is a stub Responses API in a throwaway container on the same internal network, so the
proxy's upstream call, its credential swap and its metering all happen over a real bridge, with no
real key and no real OpenAI.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("docker")

import docker  # noqa: E402
from docker.errors import DockerException, ImageNotFound  # noqa: E402

from app.services.proxy_container import ProxyContainer  # noqa: E402

PROXY_IMAGE = "forge-llm-proxy:latest"
PYTHON_IMAGE = "python:3.11-slim"


def _proxy_image_available() -> bool:
    try:
        client = docker.from_env()
        client.ping()
        client.images.get(PROXY_IMAGE)
        client.images.get(PYTHON_IMAGE)
        return True
    except (ImageNotFound, DockerException):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _proxy_image_available(), reason=f"{PROXY_IMAGE} / {PYTHON_IMAGE} not available locally"),
]

REAL_KEY = "sk-proj-FAKE-REAL-KEY-for-the-integration-test"

# A stub Responses API: records nothing, answers 200 with usage and echoes the Authorization header it
# received (and whether an x-api-key came with it) in the response BODY (`metadata`), which the proxy
# relays, and ALSO in response headers, which it must not (only the headers the pinned SDK
# reads go back to the sandbox, so the header copy is the control that proves the allowlist).
_STUB_SCRIPT = """
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get('content-length') or 0))
        body = json.dumps({
            'id': 'resp_1', 'object': 'response', 'model': 'gpt-5-mini-2025-08-07', 'status': 'completed',
            'service_tier': 'default',
            'metadata': {'received_auth': self.headers.get('authorization', ''), 'received_x_api_key': self.headers.get('x-api-key', '')},
            'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'stub says hi'}]}],
            'usage': {'input_tokens': 1000, 'input_tokens_details': {'cached_tokens': 400, 'cache_write_tokens': 0},
                      'output_tokens': 500, 'output_tokens_details': {'reasoning_tokens': 0}, 'total_tokens': 1500},
        }).encode()
        self.send_response(200)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(body)))
        self.send_header('x-received-auth', self.headers.get('authorization', ''))
        self.send_header('x-received-x-api-key', self.headers.get('x-api-key', ''))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

HTTPServer(('0.0.0.0', 8080), H).serve_forever()
"""

# Run inside a throwaway container on the sandbox network: POSTs each case to the proxy with the proxy
# token as the Bearer credential and prints one JSON line of (status, relevant headers, body text).
_CLIENT_SCRIPT = """
import json, sys, urllib.request, urllib.error
base, token = sys.argv[1], sys.argv[2]
cases = json.loads(sys.argv[3])
out = []
for path, body in cases:
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method='POST',
                                 headers={'authorization': 'Bearer ' + token, 'content-type': 'application/json'})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        status, headers, text = r.status, r.headers, r.read().decode()
    except urllib.error.HTTPError as e:
        status, headers, text = e.code, e.headers, e.read().decode()
    meta = {}
    try:
        meta = json.loads(text).get('metadata') or {}
    except Exception:
        pass
    out.append([status, meta.get('received_auth'), meta.get('received_x_api_key'), text, headers.get('x-received-auth')])
print(json.dumps(out))
"""


@pytest.fixture()
def docker_client():
    return docker.from_env()


@pytest.fixture()
def sandbox_network(docker_client):
    network = docker_client.networks.create("forge-proxy-openai-it-net", driver="bridge", internal=True)
    yield network
    try:
        network.remove()
    except DockerException:
        pass


@pytest.fixture()
def stub_upstream(docker_client, sandbox_network):
    container = docker_client.containers.run(
        PYTHON_IMAGE, ["python", "-c", _STUB_SCRIPT], detach=True, network=sandbox_network.name
    )
    try:
        container.reload()
        ip = container.attrs["NetworkSettings"]["Networks"][sandbox_network.name]["IPAddress"]
        yield f"http://{ip}:8080"
    finally:
        container.remove(v=True, force=True)


def _request_body(**over) -> dict:
    base = {
        "model": "gpt-5-mini",
        "input": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "name": "Read", "parameters": {"type": "object", "properties": {}}}],
        "store": False,
        "service_tier": "default",
    }
    base.update(over)
    return base


def _from_the_sandbox(docker_client, network, proxy_url: str, token: str, cases: list) -> list:
    output = docker_client.containers.run(
        PYTHON_IMAGE,
        ["python", "-c", _CLIENT_SCRIPT, proxy_url, token, json.dumps(cases)],
        network=network.name,
        remove=True,
    )
    return json.loads(output.decode().strip().splitlines()[-1])


def test_the_packaged_image_serves_the_openai_dialect(docker_client, sandbox_network, stub_upstream):
    proxy = ProxyContainer(client=docker_client)
    try:
        proxy.start(
            real_api_key=REAL_KEY,
            sandbox_network=sandbox_network,
            upstream_base_url=stub_upstream,
            image=PROXY_IMAGE,
            dialect="openai",
        )
        token = proxy.issue_token("build-openai", token_budget=1_000_000, cost_budget_usd=100.0)

        results = _from_the_sandbox(
            docker_client,
            sandbox_network,
            proxy.base_url_for_sandbox,
            token,
            [
                ["/v1/responses", _request_body()],
                ["/v1/responses", _request_body(tools=[{"type": "web_search"}])],
                ["/v1/responses", _request_body(store=True)],
                ["/v1/messages", {"model": "claude-opus-4-8", "messages": []}],
            ],
        )

        forwarded, web_search, stored, messages = results
        # Forwarded: 200, and the upstream saw the REAL key as a Bearer credential, never the proxy token.
        assert forwarded[0] == 200
        assert forwarded[1] == f"Bearer {REAL_KEY}"
        assert forwarded[2] == ""
        assert token not in json.dumps(forwarded)
        # the upstream's own `x-received-auth` response header never reached the sandbox.
        assert forwarded[4] is None
        # Refused: the OpenAI-shaped envelope, before anything is forwarded.
        for refused in (web_search, stored, messages):
            assert refused[0] == 403
            envelope = json.loads(refused[3])
            assert set(envelope) == {"error"}
            assert set(envelope["error"]) == {"message", "type", "code"}
            assert envelope["error"]["message"].startswith("tool policy: ")
        assert json.loads(web_search[3])["error"]["message"] == "tool policy: tool type 'web_search' is not allowed"

        # Metered through the real container: gpt-5-mini snapshot, 600 uncached + 400 cached + 500 out.
        metered = proxy.usage(token)
        assert metered["tokens"] == 1100
        assert metered["cost_usd"] == pytest.approx(0.00116)
        assert metered["unmetered"] is False
        assert len(metered["policy_refusals"]) == 3
        recorded = proxy.trace(token)
        assert recorded["calls_total"] == 1  # only the forwarded call has a step
        assert [b["text"] for b in recorded["steps"][0]["blocks"]] == ["stub says hi"]
        assert REAL_KEY not in json.dumps([recorded, metered])  # (the stub echoes the key it was given, by design)
    finally:
        proxy.stop()
