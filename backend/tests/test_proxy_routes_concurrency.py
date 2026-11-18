"""the two platform-origin proxy routes (`routers/preview_proxy.py::proxy_preview`,
`routers/service_proxy.py::proxy_service`) do not stall the rest of the API, and share one Docker client.

Both handlers were `async def` and did only blocking work: sync SQLAlchemy, a `docker.from_env()`
per request, a `docker exec` of up to 5 seconds, a `time.sleep(0.3)` retry loop. An `async def`
handler runs ON the event loop, so one slow upstream held every other request the worker was
serving. They are plain `def` now: FastAPI runs them in the threadpool. Nothing in either awaits
anything, so nothing had to stay async.

The concurrency tests drive the real application over `httpx.ASGITransport` in ONE event loop
(two requests in flight at once, as a server would have them) with the manager seam replaced by a
fake whose upstream blocks on a `threading.Event`. The wait is bounded (`BLOCK_LIMIT`), so with the
fix reverted the test still ends -- red, after the limit. Docker is not involved: the daemon
is faked at the client, and the real managers and routes run against it.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from docker.errors import DockerException

import app.routers.preview_proxy as preview_proxy_router
import app.routers.service_proxy as service_proxy_router
from app.main import app
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.services import shared_docker_client as shared_module
from app.services.preview_manager import hash_token, issue_preview_token
from app.services.status import BuildStatus, ProjectStatus

BLOCK_LIMIT = 3.0  # seconds a fake upstream waits at most for the test to release it


@pytest.fixture()
def project_row(db_session, seeded_ids):
    project = Project(
        name="proxy-concurrency-test",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.commit()
    return project


@pytest.fixture()
def preview_url(db_session, project_row):
    build = Build(id="pv-concurrency-1", project_id=project_row.id, type="create", status=BuildStatus.SUCCEEDED)
    db_session.add(build)
    db_session.commit()
    token, _expires_at = issue_preview_token(build.id)
    build.preview_token_hash = hash_token(token)
    db_session.commit()
    return f"/api/projects/{project_row.id}/preview/{build.id}/page?token={token}"


@pytest.fixture()
def service_url(db_session, project_row):
    build = Build(id="dep-concurrency-1", project_id=project_row.id, type="create", status=BuildStatus.SUCCEEDED)
    db_session.add(build)
    db_session.flush()
    db_session.add(
        Deployment(
            project_id=project_row.id,
            build_id=build.id,
            confirmed_by_id=project_row.owner_id,
            status=DeploymentStatus.ACTIVE,
        )
    )
    db_session.commit()
    return f"/api/projects/{project_row.id}/service/healthz"


class _BlockingUpstream:
    """The fake manager's `proxy()`: tells the test it has been entered, then holds the calling
    thread until the test releases it (or `BLOCK_LIMIT` passes)."""

    def __init__(self, db=None):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.in_transaction_during_upstream: bool | None = None
        self._db = db

    def proxy(self, build_or_deployment_id, *, method="GET", path="/", **_forwarded):
        if self._db is not None:
            self.in_transaction_during_upstream = self._db.in_transaction()
        self.entered.set()
        self.release.wait(timeout=BLOCK_LIMIT)
        return 200, b"slow body", "text/plain"


def _seam(route: str) -> tuple[object, str]:
    """The module and attribute a test replaces to put a fake manager behind each route."""
    return (
        (preview_proxy_router, "_preview_manager") if route == "preview" else (service_proxy_router, "_proxy_manager")
    )


@pytest.mark.parametrize("route", ["preview", "service"])
def test_a_slow_upstream_does_not_stall_another_request(
    client, monkeypatch, preview_url, service_url, route
):
    """Two requests in one event loop: the proxy request, held inside its upstream, and
    `/api/health`. With the handler on the event loop the health request cannot even start until
    the upstream returns, so the proxy request is already DONE by the time health answers; in a
    threadpool health answers while the upstream is still held."""
    upstream = _BlockingUpstream()
    owner, name = _seam(route)
    monkeypatch.setattr(owner, name, lambda: upstream)
    url = preview_url if route == "preview" else service_url

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            slow = asyncio.create_task(http.get(url))
            # Waits for the handler to be inside its upstream WITHOUT holding the loop: if the
            # handler itself holds the loop, this cannot return until the handler does.
            await asyncio.to_thread(upstream.entered.wait, BLOCK_LIMIT * 2)
            health = await http.get("/api/health")
            slow_was_done = slow.done()
            upstream.release.set()
            proxied = await slow
            return health, slow_was_done, proxied

    health, slow_was_done, proxied = asyncio.run(scenario())

    assert upstream.entered.is_set()
    assert health.status_code == 200 and health.json() == {"status": "ok"}
    assert slow_was_done is False, "the proxy request finished before /api/health answered: it held the event loop"
    assert proxied.status_code == 200 and proxied.content == b"slow body"


@pytest.mark.parametrize("route", ["preview", "service"])
def test_the_database_connection_is_given_back_before_the_upstream_call(
    client, db_session, monkeypatch, preview_url, service_url, route
):
    """The first read of each handler begins a transaction, which keeps a pooled connection checked
    out until the request ends. During a slow upstream that would be a connection per slow request;
    here the session is out of its transaction while the upstream runs."""
    upstream = _BlockingUpstream(db=db_session)
    upstream.release.set()  # not testing the wait here
    owner, name = _seam(route)
    monkeypatch.setattr(owner, name, lambda: upstream)
    url = preview_url if route == "preview" else service_url

    response = client.get(url)

    assert response.status_code == 200
    assert upstream.in_transaction_during_upstream is False


# --- one Docker client ------------------------------------------------------------------------


class _FakeDaemon:
    """A Docker client as the managers use it: `containers.get(name)` returns a running container
    whose `exec_run` answers with the wire format of the proxy script (status line, content-type
    line, body). Counts the clients that were built."""

    built = 0

    def __init__(self):
        type(self).built += 1
        container = SimpleNamespace(status="running", exec_run=lambda cmd: (0, b"200\ntext/html\nhello"))
        self.containers = SimpleNamespace(get=lambda name: container)


@pytest.fixture()
def one_fake_daemon(monkeypatch):
    _FakeDaemon.built = 0
    shared_module.reset_shared_docker_client()
    monkeypatch.setattr(shared_module.docker, "from_env", lambda: _FakeDaemon())
    yield _FakeDaemon
    shared_module.reset_shared_docker_client()


def test_both_routes_share_one_docker_client_across_requests(client, one_fake_daemon, preview_url, service_url):
    for _ in range(4):
        preview = client.get(preview_url)
        service = client.get(service_url)
        assert (preview.status_code, preview.content) == (200, b"hello")
        assert (service.status_code, service.content) == (200, b"hello")

    assert one_fake_daemon.built == 1  # eight proxied requests, one `docker.from_env()`


def test_a_docker_that_was_not_reachable_is_tried_again_not_remembered(client, monkeypatch, service_url):
    shared_module.reset_shared_docker_client()
    attempts: list[int] = []

    def _from_env():
        attempts.append(1)
        if len(attempts) == 1:
            raise DockerException("Error while fetching server API version")
        return _FakeDaemon()

    monkeypatch.setattr(shared_module.docker, "from_env", _from_env)
    try:
        first = client.get(service_url)
        second = client.get(service_url)
    finally:
        shared_module.reset_shared_docker_client()

    assert first.status_code == 502  # the route's own answer to an unreachable daemon
    assert first.headers["x-content-type-options"] == "nosniff"  # with the isolation headers, as ever
    assert second.status_code == 200 and second.content == b"hello"  # Docker came up: no restart needed
    assert len(attempts) == 2


def test_concurrent_first_requests_build_one_client_not_several(monkeypatch):
    shared_module.reset_shared_docker_client()
    built: list[int] = []
    gate = threading.Event()

    def _slow_from_env():
        built.append(1)
        gate.wait(timeout=BLOCK_LIMIT)  # long enough for the others to arrive; bounded
        return object()

    monkeypatch.setattr(shared_module.docker, "from_env", _slow_from_env)
    seen: list[object] = []
    threads = [threading.Thread(target=lambda: seen.append(shared_module.shared_docker_client())) for _ in range(6)]
    try:
        for thread in threads:
            thread.start()
        gate.set()
        for thread in threads:
            thread.join(timeout=BLOCK_LIMIT * 2)
    finally:
        shared_module.reset_shared_docker_client()

    assert len(built) == 1
    assert len(seen) == 6 and all(client is seen[0] for client in seen)
