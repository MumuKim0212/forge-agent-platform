"""the smoke check against a REAL preview container (Docker).

A test-local image serves a hand-written OpenAPI document on the preview port: one `GET` that
answers 200, one `GET` that fails with a 500, one `GET` with a path parameter, one `POST`-only
path, one `GET` that redirects (302 to `/ok`) and one `GET` that sleeps far past the deadline. The
executor's own `_run_smoke_check` (default prober: the token-checked route through the real
`PreviewManager.proxy`, i.e. `docker exec` into the preview) must store exactly the four called
routes -- the 500 as a server error, the redirect as its own 302 (not followed), the slow one as
`no_answer` because the in-script wall-clock deadline cut it off -- count the parameterised one as
skipped, and finish within a bound that the slow route alone would blow without the deadline.

Readiness: the app inside the container needs a moment after `start_preview` returns. Nothing here
waits for it: `PreviewManager.proxy` already retries a refused connection until its own timeout (the
smoke check's 10 s), which is the production path too; a refused connection inside that window is
startup lag, not an answer.

Skips cleanly (not fails) when Docker is unavailable.
"""

from __future__ import annotations

import tempfile
import time
from datetime import timezone
from pathlib import Path

import pytest

pytest.importorskip("docker")
pytest.importorskip("psycopg")

import docker  # noqa: E402
from docker.errors import DockerException, ImageNotFound  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import sandbox_build_executor as sbe  # noqa: E402
from app.services.preview_manager import PreviewManager  # noqa: E402

IMAGE = "forge-preview-smoke-test:latest"

APP = '''
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DOC = {
    "openapi": "3.1.0",
    "paths": {
        "/ok": {"get": {"responses": {"200": {"description": "ok"}}}},
        "/boom": {"get": {"responses": {"200": {"description": "ok"}}}},
        "/items/{id}": {"get": {"parameters": [{"name": "id", "in": "path", "required": True}]}},
        "/make": {"post": {"responses": {"200": {"description": "ok"}}}},
        "/moved": {"get": {"responses": {"302": {"description": "moved"}}}},
        "/slow": {"get": {"responses": {"200": {"description": "ok"}}}},
    },
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/openapi.json":
            self._send(200, json.dumps(DOC))
        elif self.path == "/ok":
            self._send(200, "{}")
        elif self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/slow":
            time.sleep(25)  # finite, and far past the 10 s deadline
            self._send(200, "{}")
        elif self.path == "/boom":
            self._send(500, "Internal Server Error")  # what an unhandled exception becomes
        else:
            self._send(404, "{}")

    def do_POST(self):
        self._send(200, "{}")

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
'''


def _docker_available() -> bool:
    try:
        docker.from_env().ping()
        return True
    except DockerException:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_available(), reason="Docker Engine unavailable"),
]


@pytest.fixture(scope="module")
def docker_client():
    return docker.from_env()


@pytest.fixture(scope="module", autouse=True)
def _real_data_services(real_service_postgres_admin_dsn):
    """`start_preview` provisions a preview's throwaway data services; point it at the module's own
    throwaway Postgres, never whatever `backend/.env` holds."""
    yield


@pytest.fixture(scope="module")
def smoke_image(docker_client):
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "app.py").write_text(APP.replace("PORT", str(settings.preview_container_port)))
        Path(tmp, "Dockerfile").write_text(
            'FROM python:3.11-slim\nWORKDIR /srv\nCOPY app.py .\nCMD ["python", "app.py"]\n'
        )
        docker_client.images.build(path=tmp, tag=IMAGE, rm=True)
    yield IMAGE
    try:
        docker_client.images.get(IMAGE).remove(force=True)
    except (ImageNotFound, DockerException):
        pass


def test_the_smoke_check_of_a_real_preview_stores_the_called_routes_cuts_off_the_slow_one_and_counts_the_skipped_one(
    docker_client, smoke_image, make_build_with_request, db_session
):
    manager = PreviewManager(client=docker_client)
    build = make_build_with_request()
    build.image_ref = smoke_image
    db_session.flush()
    try:
        issued = manager.start_preview(project_id=build.project_id, build=build)
        build.preview_url = issued.url
        build.preview_expires_at = issued.expires_at.astimezone(timezone.utc)
        build.preview_token_hash = issued.token_hash
        executor = sbe.SandboxBuildExecutor(docker_client=docker_client, adapter=object(), preview_manager=manager)

        started = time.monotonic()
        executor._run_smoke_check(build)
        elapsed = time.monotonic() - started

        # /slow sleeps 25 s; its 10 s deadline (plus the quick routes) bounds the whole check.
        assert elapsed < 22
        assert build.smoke_checks == {
            "routes": [
                {"path": "/boom", "status": 500, "outcome": "server_error"},
                {"path": "/moved", "status": 302, "outcome": "ok"},  # not followed to /ok's 200
                {"path": "/ok", "status": 200, "outcome": "ok"},
                {"path": "/slow", "status": None, "outcome": "no_answer"},
            ],
            "skipped": 1,  # /items/{id}; the POST-only /make is not a GET route and is not counted
            "truncated": False,
        }
    finally:
        manager.stop_preview(build.id)
