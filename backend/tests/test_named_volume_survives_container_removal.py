"""`remove(v=True)` takes a container's ANONYMOUS volumes only.

The platform makes `v=True` universal on container removal. That is safe only
because Docker never removes a NAMED volume with it -- the shared service
Postgres keeps its databases in `forge-service-postgres-data` (ADR 0023
§Amendment 11) and the platform never deletes it. This proves the claim on a
real daemon, with one scratch container holding one volume of each kind, rather
than trusting the documentation.

The container is created, not started: Docker makes the anonymous volume at
create time from the image's `VOLUME` (`postgres:18-alpine` declares one, and
the integration suite already needs that image).
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("docker")

import docker  # noqa: E402
from docker.errors import DockerException  # noqa: E402

from tests.docker_volumes import anonymous_volume_names, volume_exists  # noqa: E402


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


def test_remove_v_true_drops_the_anonymous_volume_and_keeps_the_named_one():
    client = docker.from_env()
    named = f"forge-test-named-{uuid.uuid4().hex[:8]}"
    client.volumes.create(name=named)
    container = None
    anonymous: list[str] = []
    try:
        container = client.containers.create(
            image="postgres:18-alpine",
            name=f"forge-test-scratch-{uuid.uuid4().hex[:8]}",
            volumes={named: {"bind": "/mnt/named", "mode": "rw"}},
        )
        container.reload()
        anonymous = anonymous_volume_names(container)
        assert anonymous, "the image must give the scratch container an anonymous volume"
        assert volume_exists(client, named)

        container.remove(v=True, force=True)
        container = None

        assert not any(volume_exists(client, v) for v in anonymous), "anonymous volume survived"
        assert volume_exists(client, named), "the NAMED volume must survive remove(v=True)"
    finally:
        if container is not None:
            container.remove(v=True, force=True)
        for v in anonymous:
            try:
                client.volumes.get(v).remove(force=True)
            except DockerException:
                pass
        try:
            client.volumes.get(named).remove(force=True)
        except DockerException:
            pass
