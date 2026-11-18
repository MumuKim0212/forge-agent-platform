"""the one Docker client the API process's two proxy routes share.

`routers/preview_proxy.py` and `routers/service_proxy.py` used to build a `PreviewManager` /
`DeployManager` per request, and each of those called `docker.from_env()` in its constructor:
a new client (a new connection pool, and a round trip to the daemon to negotiate the API version)
for every proxied request, discarded at the end of it. A page of a generated service is a dozen
requests.

A manager holds no per-request state (its own docstring says so; everything it needs is in
container names and labels), so what is shared is only the client, built lazily the first time a
proxy request needs it. docker-py's client is a `requests` session over a connection pool, which
is safe to use from many threads at once -- and the routes are plain `def` handlers running in the
threadpool, so they do.

Not used by anything else: the worker, the executors and the confirm/rollback/stop actions keep
building their own clients (`tests/test_deploy_controls.py` pins that the actions do), and the
read that checks an image's presence has its own short-timeout client on purpose.
"""

from __future__ import annotations

import threading

import docker

_lock = threading.Lock()
_client: docker.DockerClient | None = None


def shared_docker_client() -> docker.DockerClient:
    """The process-wide client, created on first use. A failure to create it (the daemon is not
    reachable) raises as `docker.from_env()` does and is NOT remembered: the next request tries
    again, so a Docker that comes up later is picked up without a restart. The lock is held while
    it is created, so concurrent first requests make one client, not several."""
    global _client
    with _lock:
        if _client is None:
            _client = docker.from_env()
        return _client


def reset_shared_docker_client() -> None:
    """Forgets the client (a test's isolation; closes nothing, the client's pool is garbage
    collected with it)."""
    global _client
    with _lock:
        _client = None
