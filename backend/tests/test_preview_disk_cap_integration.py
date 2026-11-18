"""Against a real Docker Engine: a preview whose container writes past the disk cap is torn
down by the watcher, its network goes with it, a preview next to it that stays under the cap is left
running, and the project's timeline says why.

What each test can reach (a test named for a risk must be able to reach it):

* the over-the-cap test first reads the ENGINE's own size for the container and asserts it is past
  the cap before the watcher is asked anything, so it cannot pass on an engine that reports nothing;
* the preview is a REAL `PreviewManager.start_preview` (network, labels, hardening), and the stop goes
  through the real `stop_preview`, so "torn down through the normal path" is observed, not assumed;
* the blind test makes the engine's rows carry no size by wrapping the real client's one list call,
  and everything else (the lookup, the removal, the network) is the real engine.

**Every write here is finite**: `dd ... count=N` with N MiB, larger than the test's 8 MiB cap and small
in absolute terms (24 MiB). Nothing here is an endless stream, so removing the bound under test cannot
fill the host's disk; the worst a surviving preview does is exist until the `finally` removes it.

Skips cleanly (not fails) when Docker is unavailable.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("docker")

import docker  # noqa: E402
from docker.errors import DockerException, ImageNotFound, NotFound  # noqa: E402

from app.config import settings  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.build import Build  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.services.preview_disk_watch import (  # noqa: E402
    PreviewDiskWatcher,
    PreviewDiskWatchRunner,
    record_preview_disk_stop,
)
from app.services.preview_manager import PreviewManager  # noqa: E402
from app.services.sandbox_runner import writable_layer_bytes_from_row  # noqa: E402
from app.services.status import BuildStatus, ProjectStatus  # noqa: E402
from tests.docker_cleanup import remove_handles  # noqa: E402
from tests.engine_list_shim import ContainerListViaContainers  # noqa: E402

PREVIEW_DISK_TEST_IMAGE = "forge-preview-disk-test:latest"
MIB = 1024 * 1024
CAP_MB = 8
FILL_MB = 24  # finite, and three times the cap


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


@pytest.fixture(scope="module")
def preview_image(docker_client):
    """A tiny real image whose default command serves HTTP on the preview port, the shape of a built
    service's image from a preview container's point of view (the same recipe
    `test_preview_manager_integration.py` uses, under a tag of its own)."""
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "Dockerfile").write_text(
            "FROM python:3.11-slim\n"
            "WORKDIR /srv\n"
            "RUN echo forge-preview-disk-test-page > index.html\n"
            f'CMD ["python", "-m", "http.server", "{settings.preview_container_port}"]\n'
        )
        docker_client.images.build(path=tmp, tag=PREVIEW_DISK_TEST_IMAGE, rm=True)
    yield PREVIEW_DISK_TEST_IMAGE
    try:
        docker_client.images.get(PREVIEW_DISK_TEST_IMAGE).remove(force=True)
    except (ImageNotFound, DockerException):
        pass


@pytest.fixture(autouse=True)
def _cap_and_no_ambient_data_services(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_disk_limit_mb", CAP_MB)
    monkeypatch.setattr(settings, "sandbox_disk_poll_interval_seconds", 0.1)
    # No data services: a preview starts as it did before data services were added, and the test does not depend on a
    # throwaway Postgres (the conftest session guard already starts the ambient DSN as None).
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)


@pytest.fixture()
def manager(docker_client):
    return PreviewManager(client=docker_client)


@pytest.fixture()
def previews(docker_client, manager):
    """Starts previews through the real manager and removes every one of them (and their networks) at the
    end, whatever the test did: by build id through `stop_preview`, then by handle for anything left."""
    started: list[str] = []  # build ids (plain strings: the session may be closed by teardown)

    def start(db, seeded_ids, name, image):
        project = Project(
            name=name,
            owner_id=seeded_ids["user_id"],
            team_id=seeded_ids["team_id"],
            template_id=seeded_ids["template_id"],
            status=ProjectStatus.SUBMITTED,
        )
        db.add(project)
        db.commit()
        build = Build(project_id=project.id, type="create", status=BuildStatus.SUCCEEDED, image_ref=image)
        db.add(build)
        db.commit()
        issued = manager.start_preview(project_id=project.id, build=build)
        build.preview_url = issued.url
        build.preview_expires_at = issued.expires_at
        build.preview_token_hash = issued.token_hash
        db.commit()
        started.append(build.id)
        return project, build

    yield start

    for build_id in started:
        manager.stop_preview(build_id)
    leftover_containers = [c for c in docker_client.containers.list(all=True) if any(b in c.name for b in started)]
    leftover_networks = [n for n in docker_client.networks.list() if any(b in n.name for b in started)]
    remove_handles(docker_client, leftover_containers, leftover_networks)


def _container(docker_client, build):
    return docker_client.containers.get(f"forge-preview-{build.id}")


def _gone(docker_client, build) -> bool:
    try:
        _container(docker_client, build)
    except NotFound:
        return True
    return False


def _networks_of(docker_client, build) -> list[str]:
    return [n.name for n in docker_client.networks.list() if build.id in n.name]


def _write(container, megabytes: int) -> None:
    """A FINITE write inside the preview container: exactly `megabytes` MiB of zeros."""
    exit_code, output = container.exec_run(
        ["sh", "-c", f"dd if=/dev/zero of=/tmp/forge-caseai-fill bs=1M count={megabytes}"]
    )
    assert exit_code == 0, output


def _engine_reported_bytes(docker_client, container) -> int | None:
    rows = docker_client.api.containers(all=True, size=True, filters={"id": [container.id]})
    (row,) = [r for r in rows if r["Id"] == container.id]
    return writable_layer_bytes_from_row(row)


def _timeline(client, seeded_ids, project):
    return client.get(
        f"/api/projects/{project.id}/timeline", headers={"X-Forge-User-Id": seeded_ids["user_id"]}
    ).json()


def test_a_preview_that_writes_past_the_cap_is_torn_down_its_neighbour_is_not_and_the_timeline_says_why(
    docker_client, manager, previews, preview_image, db_session, seeded_ids, client
):
    hostile_project, hostile = previews(db_session, seeded_ids, "caseai-hostile", preview_image)
    _, neighbour = previews(db_session, seeded_ids, "caseai-neighbour", preview_image)
    _write(_container(docker_client, hostile), FILL_MB)
    _write(_container(docker_client, neighbour), 2)  # under the cap: the control
    # The risk is reachable only if the engine itself says the hostile layer is over the cap.
    assert _engine_reported_bytes(docker_client, _container(docker_client, hostile)) > CAP_MB * MIB
    assert _engine_reported_bytes(docker_client, _container(docker_client, neighbour)) < CAP_MB * MIB

    stops = PreviewDiskWatcher(client=docker_client, preview_manager=manager).poll_once()

    assert [(s.build_id, s.verdict) for s in stops] == [(hostile.id, "over")]
    assert _gone(docker_client, hostile)
    assert _networks_of(docker_client, hostile) == []  # the normal teardown took the network too
    assert not _gone(docker_client, neighbour)  # still running, still on its network
    assert _container(docker_client, neighbour).status == "running"
    assert len(_networks_of(docker_client, neighbour)) == 1

    record_preview_disk_stop(db_session, stops[0])

    db_session.refresh(hostile)
    assert hostile.preview_url is None  # the timeline stops offering the preview link
    timeline = _timeline(client, seeded_ids, hostile_project)
    lines = [e["data"] for e in timeline if e["kind"] == "audit_log"]
    assert [line["action"] for line in lines] == ["preview.stopped_disk_limit"]
    (entry,) = db_session.query(AuditLog).filter(AuditLog.action == "preview.stopped_disk_limit")
    assert entry.target == f"project:{hostile_project.id}"
    assert entry.metadata_json["detail"] == (
        f"the preview was stopped because its container wrote more than the {CAP_MB} MiB disk limit"
    )


def test_a_preview_the_engine_stops_reporting_a_size_for_is_torn_down_after_five_polls(
    docker_client, manager, previews, preview_image, db_session, seeded_ids
):
    """Fail closed. The engine's list call is wrapped so its rows carry neither size field (an engine
    that does not report them); the lookup, the removal and the network are the real engine's."""
    _, preview = previews(db_session, seeded_ids, "caseai-blind", preview_image)
    real_api = docker_client.api

    def sizeless_containers(**kwargs):
        rows = real_api.containers(**kwargs)
        return [{k: v for k, v in row.items() if k not in ("SizeRw", "SizeRootFs")} for row in rows]

    class BlindApi(ContainerListViaContainers):
        containers = staticmethod(sizeless_containers)

    blind_client = SimpleNamespace(api=BlindApi(), containers=docker_client.containers)
    watcher = PreviewDiskWatcher(client=blind_client, preview_manager=manager)

    for poll in range(4):
        assert watcher.poll_once() == [], f"poll {poll + 1}"
        assert not _gone(docker_client, preview)  # four silent polls are not a verdict

    (stop,) = watcher.poll_once()

    assert (stop.build_id, stop.verdict, stop.used_bytes) == (preview.id, "blind", None)
    assert _gone(docker_client, preview)
    assert _networks_of(docker_client, preview) == []


def test_a_preview_no_good_read_ever_showed_is_torn_down_when_the_sized_read_keeps_failing(
    docker_client, manager, previews, preview_image, db_session, seeded_ids
):
    """The engine's SIZED list fails on every poll from the start (a worker that restarted into a
    daemon that is slow to walk layers, a preview that started while reads were failing): no good read has
    ever shown this preview. The unsized list, which is the real engine's own and answers, names it, and
    it is torn down after five polls instead of running to its TTL. The lookup, the removal and the network
    are the real engine's."""
    _, preview = previews(db_session, seeded_ids, "casean-never-seen", preview_image)
    real_api = docker_client.api

    def sized_list_fails(**kwargs):
        if kwargs.get("size"):
            raise DockerException("the engine's size computation is not answering")
        return real_api.containers(**kwargs)

    class FailingSizedApi(ContainerListViaContainers):
        containers = staticmethod(sized_list_fails)

    client_without_sizes = SimpleNamespace(api=FailingSizedApi(), containers=docker_client.containers)
    watcher = PreviewDiskWatcher(client=client_without_sizes, preview_manager=manager)  # a fresh one: nothing seen yet

    for poll in range(4):
        assert watcher.poll_once() == [], f"poll {poll + 1}"
        assert not _gone(docker_client, preview)

    (stop,) = watcher.poll_once()

    assert (stop.build_id, stop.verdict, stop.used_bytes) == (preview.id, "blind", None)
    assert _gone(docker_client, preview)
    assert _networks_of(docker_client, preview) == []


def test_the_worker_thread_stops_a_hostile_preview_on_its_own_and_records_it(
    docker_client, manager, previews, preview_image, db_session, seeded_ids, monkeypatch
):
    """The real runner thread over the real watcher: nobody calls `poll_once` here. The test's own thread
    plays the worker's, busy, and only looks again when the container is gone."""
    project, hostile = previews(db_session, seeded_ids, "caseai-thread", preview_image)
    _write(_container(docker_client, hostile), FILL_MB)
    monkeypatch.setattr(db_session, "close", lambda: None)
    runner = PreviewDiskWatchRunner(PreviewDiskWatcher(client=docker_client, preview_manager=manager), lambda: db_session)

    runner.start()
    try:
        deadline = time.monotonic() + 60
        while not _gone(docker_client, hostile) and time.monotonic() < deadline:
            threading.Event().wait(0.2)
        assert _gone(docker_client, hostile), "the watcher thread did not tear the preview down within 60 s"
    finally:
        # The removal and the record are one cycle (the record follows the poll that removed it), and
        # `stop` joins the thread, so after it returns the record exists. The session is not read
        # before then: a Session is not thread-safe and the watcher thread is the one writing it.
        runner.stop(timeout=30)

    assert not runner.alive
    assert _networks_of(docker_client, hostile) == []
    (entry,) = db_session.query(AuditLog).filter(AuditLog.action == "preview.stopped_disk_limit")
    assert entry.target == f"project:{project.id}"
