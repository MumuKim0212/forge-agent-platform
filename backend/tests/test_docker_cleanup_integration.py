"""the Docker test fixtures remove the data-service containers they
created and nothing else -- against a real Docker Engine.

The defect this pins: `real_service_postgres_admin_dsn` ended
by removing every container on the host labelled `forge.data_service=redis` /
`=preview-redis`, which is exactly how the platform labels a project's
production Redis. Running one Docker test module therefore deleted the Redis of
a deployed service on the same host. The bystanders below carry those exact
labels and stand for that Redis.

Skips cleanly (not fails) when Docker is unavailable.
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("docker")

import docker  # noqa: E402
from docker.errors import DockerException, NotFound  # noqa: E402

from tests.docker_cleanup import (  # noqa: E402
    DataServiceContainerTracker,
    TEST_RUN_LABEL,
    remove_strays,
)


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

IMAGE = "redis:8-alpine"


@pytest.fixture()
def client():
    return docker.from_env()


def _run(client, name, labels):
    return client.containers.run(image=IMAGE, name=name, detach=True, labels=labels)


def _exists(client, name) -> bool:
    try:
        client.containers.get(name)
        return True
    except NotFound:
        return False


def test_removal_takes_only_what_this_run_created_never_a_platform_labelled_bystander(client):
    suffix = uuid.uuid4().hex[:8]
    bystander_service = f"forge-service-redis-bystander-{suffix}"
    bystander_preview = f"forge-preview-redis-bystander-{suffix}"
    created_service = f"forge-service-redis-created-{suffix}"
    created_preview = f"forge-preview-redis-created-{suffix}"
    created_unlabelled = f"forge-created-unlabelled-{suffix}"
    names = [
        bystander_service,
        bystander_preview,
        created_service,
        created_preview,
        created_unlabelled,
    ]
    try:
        # What the platform made, before the tracker exists: must survive.
        _run(client, bystander_service, {"forge.data_service": "redis", "forge.project_id": "p"})
        _run(
            client,
            bystander_preview,
            {"forge.data_service": "preview-redis", "forge.build_id": "b"},
        )

        with DataServiceContainerTracker() as tracker:
            _run(client, created_service, {"forge.data_service": "redis", "forge.project_id": "q"})
            _run(
                client,
                created_preview,
                {"forge.data_service": "preview-redis", "forge.build_id": "c"},
            )
            _run(client, created_unlabelled, {"forge.project_id": "q"})

        removed = tracker.remove_created(client)

        assert sorted(removed) == sorted([created_service, created_preview])
        assert not _exists(client, created_service)
        assert not _exists(client, created_preview)
        # Not a data-service container, so not this tracker's to touch.
        assert _exists(client, created_unlabelled)
        # The point of the task: what the platform made is still there.
        assert _exists(client, bystander_service)
        assert _exists(client, bystander_preview)
    finally:
        for name in names:
            try:
                client.containers.get(name).remove(v=True, force=True)
            except (NotFound, DockerException):
                pass


def test_a_container_made_after_the_tracker_closed_is_not_tagged(client):
    name = f"forge-service-redis-after-{uuid.uuid4().hex[:8]}"
    try:
        with DataServiceContainerTracker():
            pass
        container = _run(client, name, {"forge.data_service": "redis", "forge.project_id": "r"})
        container.reload()
        assert TEST_RUN_LABEL not in container.labels  # the patch was undone on exit
    finally:
        try:
            client.containers.get(name).remove(v=True, force=True)
        except (NotFound, DockerException):
            pass


def test_stray_removal_takes_an_earlier_runs_container_but_not_a_platform_one(client):
    suffix = uuid.uuid4().hex[:8]
    stray = f"forge-service-redis-stray-{suffix}"
    platform = f"forge-service-redis-platform-{suffix}"
    try:
        _run(
            client,
            stray,
            {"forge.data_service": "redis", "forge.project_id": "s", TEST_RUN_LABEL: "old-run"},
        )
        _run(client, platform, {"forge.data_service": "redis", "forge.project_id": "t"})

        removed = remove_strays(client)

        assert stray in removed
        assert platform not in removed
        assert not _exists(client, stray)
        assert _exists(client, platform)
    finally:
        for name in (stray, platform):
            try:
                client.containers.get(name).remove(v=True, force=True)
            except (NotFound, DockerException):
                pass


def test_the_real_postgres_fixture_activates_the_tracker(real_service_postgres_admin_dsn, client):
    """The wiring: a module that uses `real_service_postgres_admin_dsn` gets
    every data-service container it creates tagged with a run id, which is
    what its teardown removes by. Without the tracker in the fixture the tag
    is absent and the teardown would have nothing safe to select."""
    name = f"forge-service-redis-wired-{uuid.uuid4().hex[:8]}"
    try:
        container = _run(client, name, {"forge.data_service": "redis", "forge.project_id": "w"})
        container.reload()
        assert TEST_RUN_LABEL in container.labels
        assert container.labels[TEST_RUN_LABEL]
    finally:
        try:
            client.containers.get(name).remove(v=True, force=True)
        except (NotFound, DockerException):
            pass
