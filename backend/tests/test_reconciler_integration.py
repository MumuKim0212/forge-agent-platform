"""ADR 0037: the reconciler against a REAL Docker Engine.

Every scenario seeds real containers, networks and volumes under THIS test run's instance id
(`tests/instance_scope.py`) -- through the platform's own code where that is cheap
(`SandboxRunner`, `ProxyContainer`, `DeployManager`, `service_data`), so the labels under test
are the ones production writes -- plus database rows that say what should exist, runs a real
`Reconciler` pass, and reads the engine back.

What each scenario would NOT notice if the rule were wrong is stated next to it: every removal
test has a control that must survive (a RUNNING build's objects, the ACTIVE deployment's), and
every "untouched" test has a control that must go in the same pass, so a reconciler that does
nothing, or everything, fails.

Safety: the reconciler is built with the test instance id, and a stand-in for the dev host's
live objects (`forge-deploy-2013fba0-...`, which carries platform labels but no instance) is
created with the same label shape and must survive. Everything created is removed by run id
(`forge.test_run`, `docker_cleanup.PlatformObjectTracker`), never by a platform label.

Skips cleanly (not fails) when Docker is unavailable; the proxy scenarios also need
`forge-llm-proxy:latest`.
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("docker")

import docker  # noqa: E402
from docker.errors import DockerException, ImageNotFound, NotFound  # noqa: E402

import app.worker as worker  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.build import Build  # noqa: E402
from app.models.deployment import Deployment, DeploymentStatus  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.services import service_data  # noqa: E402
from app.services.build_queue import sweep_timed_out_builds  # noqa: E402
from app.services.deploy_manager import DeployManager  # noqa: E402
from app.services.platform_labels import Role, platform_labels  # noqa: E402
from app.services.proxy_container import ProxyContainer  # noqa: E402
from app.services.reconciler import Reconciler  # noqa: E402
from app.services.sandbox_runner import SandboxRunConfig, SandboxRunner  # noqa: E402
from app.services.service_env_crypto import encrypt_service_env  # noqa: E402
from app.services.status import BuildFailureReason, BuildStatus, ProjectStatus  # noqa: E402
from tests.docker_cleanup import PlatformObjectTracker, remove_handles  # noqa: E402
from tests.instance_scope import TEST_INSTANCE_ID  # noqa: E402

SLEEP_IMAGE = "forge-reconcile-test-sleep:latest"
PROXY_IMAGE = "forge-llm-proxy:latest"
OTHER_INSTANCE = "forge-test-someone-else"


def _docker_available() -> bool:
    try:
        docker.from_env().ping()
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
    pytest.mark.skipif(not _docker_available(), reason="Docker Engine unavailable"),
]
needs_proxy_image = pytest.mark.skipif(not _proxy_image_available(), reason=f"{PROXY_IMAGE} not built locally")


@pytest.fixture(scope="module")
def client():
    return docker.from_env()


@pytest.fixture(scope="module")
def sleep_image(client):
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "Dockerfile").write_text('FROM python:3.11-slim\nCMD ["sleep", "infinity"]\n')
        client.images.build(path=tmp, tag=SLEEP_IMAGE, rm=True)
    yield SLEEP_IMAGE
    try:
        client.images.get(SLEEP_IMAGE).remove(force=True)
    except (ImageNotFound, DockerException):
        pass


# Containers and networks the scenarios hold a handle to, removed by handle at the end of every
# test: the run-id tracker cannot reach an object that got no platform label, and a scenario (or a
# mutation of the code under test) that loses a label must not leave it running on the host.
_HELD: dict[str, list] = {"containers": [], "networks": []}


@pytest.fixture(autouse=True)
def _tracked(client, monkeypatch, tmp_path_factory):
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)  # no shared Postgres in these scenarios
    monkeypatch.setattr(settings, "traefik_enabled", False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("systemp")))
    _HELD["containers"].clear()
    _HELD["networks"].clear()
    tracker = PlatformObjectTracker()
    with tracker:
        yield tracker
    tracker.remove_created(client)
    remove_handles(client, _HELD["containers"], _HELD["networks"])


@pytest.fixture()
def reconciler(client):
    return Reconciler(client=client, instance_id=TEST_INSTANCE_ID)


# -- rows ---------------------------------------------------------------------


@pytest.fixture()
def make_project(db_session, seeded_ids):
    def _make(name="rec-it"):
        row = Project(
            name=f"{name}-{uuid.uuid4().hex[:6]}",
            owner_id=seeded_ids["user_id"],
            team_id=seeded_ids["team_id"],
            template_id=seeded_ids["template_id"],
            status=ProjectStatus.ACTIVE,
        )
        db_session.add(row)
        db_session.commit()
        return row

    return _make


def _build(db, project, status, **fields) -> Build:
    row = Build(project_id=project.id, type="create", status=status, **fields)
    db.add(row)
    db.commit()
    return row


def _deployment(db, project, build, status, user_id, *, started=True) -> Deployment:
    row = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=user_id,
        status=status,
        started_at=datetime.now(timezone.utc) if started else None,
    )
    db.add(row)
    db.commit()
    return row


# -- real objects ---------------------------------------------------------------


def _exists(client, name: str) -> bool:
    try:
        client.containers.get(name)
        return True
    except NotFound:
        return False


def _network_exists(client, name: str) -> bool:
    return any(n.name == name for n in client.networks.list(names=[name]))


class BuildObjects:
    """A build's sandbox (and, optionally, LLM proxy) and network, made by the platform's own code."""

    def __init__(self, client, build_id: str, tmp_path, *, proxy: bool = False):
        self.client = client
        self.build_id = build_id
        runner = SandboxRunner(client=client)
        self.network = runner._create_run_network(build_id)
        _HELD["networks"].append(self.network)
        self.sandbox = runner._provision(
            SandboxRunConfig(command=["true"], workspace_dir=str(tmp_path), build_id=build_id), self.network.name
        )
        _HELD["containers"].append(self.sandbox)
        self.proxy = None
        if proxy:
            self.proxy = ProxyContainer(client=client)
            self.proxy.start(
                real_api_key="fake-key-for-test",
                sandbox_network=self.network,
                upstream_base_url="http://example.invalid",
                build_id=build_id,
            )
            _HELD["containers"].append(self.proxy._container)

    @property
    def names(self) -> list[str]:
        out = [self.sandbox.name]
        if self.proxy is not None:
            out.append(self.proxy._container.name)
        return out

    def container_exists(self, name: str) -> bool:
        return _exists(self.client, name)

    def all_gone(self) -> bool:
        return not any(self.container_exists(n) for n in self.names) and not _network_exists(
            self.client, self.network.name
        )

    def all_present(self) -> bool:
        return all(self.container_exists(n) for n in self.names) and _network_exists(self.client, self.network.name)


class DeploymentObjects:
    """A deployment's container and network, made by the real `DeployManager.start_service`."""

    def __init__(self, client, image, *, project_id, deployment_id, build_id):
        self.client = client
        self.deployment_id = deployment_id
        DeployManager(client=client).start_service(
            project_id=project_id, deployment_id=deployment_id, build_id=build_id, image_ref=image, env={}
        )
        self.container_name = f"forge-deploy-{deployment_id}"
        container = client.containers.get(self.container_name)
        _HELD["containers"].append(container)
        self.network_name = container.labels["forge.network"]
        _HELD["networks"].append(client.networks.get(self.network_name))

    def present(self) -> bool:
        return _exists(self.client, self.container_name) and _network_exists(self.client, self.network_name)

    def gone(self) -> bool:
        return not _exists(self.client, self.container_name) and not _network_exists(self.client, self.network_name)


def _private_networks(container) -> set[str]:
    """The container's networks other than Docker's default bridge (a plain container is on it)."""
    container.reload()
    return set(container.attrs["NetworkSettings"]["Networks"]) - {"bridge"}


def _labelled_container(client, image, name, labels, **kw):
    """A container the test makes by hand, tagged with its run id by the tracker only when its
    instance is the suite's; for any other instance the tag is added here so cleanup still finds
    it by run id."""
    container = client.containers.run(
        image=image, name=name, detach=True, command=["sleep", "infinity"], labels=labels, **kw
    )
    _HELD["containers"].append(container)
    return container


def _uid() -> str:
    return uuid.uuid4().hex[:10]


# -- (a) L3: a sandbox, proxy and network of a FAILED build ---------------------------------


@needs_proxy_image
def test_a_failed_builds_sandbox_proxy_and_network_are_removed_and_a_running_builds_are_not(
    client, reconciler, db_session, make_project, tmp_path
):
    project = make_project()
    failed = _build(db_session, project, BuildStatus.FAILED)
    running = _build(db_session, make_project("rec-running"), BuildStatus.RUNNING)
    doomed = BuildObjects(client, failed.id, tmp_path, proxy=True)
    kept = BuildObjects(client, running.id, tmp_path, proxy=True)
    assert doomed.all_present() and kept.all_present()  # the control is real: the objects exist before the pass

    report = reconciler.reconcile(db_session)

    assert doomed.all_gone()
    assert kept.all_present()
    assert len(report.removed) == 3 and report.failed == []
    assert db_session.query(AuditLog).filter(AuditLog.action == "reconciler.removed").count() == 3


def test_a_succeeded_builds_sandbox_and_one_whose_build_has_no_row_are_removed_too(
    client, reconciler, db_session, make_project, tmp_path
):
    succeeded = _build(db_session, make_project(), BuildStatus.SUCCEEDED)
    orphan_of_nothing = BuildObjects(client, f"no-such-build-{_uid()}", tmp_path)
    done = BuildObjects(client, succeeded.id, tmp_path)

    reconciler.reconcile(db_session)

    assert done.all_gone()
    assert orphan_of_nothing.all_gone()


# -- (b) L4: SUPERSEDED / FAILED deployments are removed and detached from Traefik; ACTIVE is not ----


def test_a_superseded_or_failed_deployments_container_is_removed_and_detached_from_traefik_the_active_one_is_untouched(
    client, reconciler, db_session, make_project, seeded_ids, sleep_image, monkeypatch, _tracked
):
    uid = seeded_ids["user_id"]
    # A stand-in for the operator's Traefik: a plain running container. Real `connect_to_traefik`
    # attaches it to each deployment's private network, as in production.
    traefik_name = f"forge-reconcile-traefik-{_uid()}"
    traefik = _labelled_container(client, sleep_image, traefik_name, {"forge.test_run": _tracked.run_id})
    monkeypatch.setattr(settings, "traefik_enabled", True)
    monkeypatch.setattr(settings, "traefik_container_name", traefik_name)

    retired_project, live_project = make_project("rec-retired"), make_project("rec-live")
    build = _build(db_session, retired_project, BuildStatus.SUCCEEDED)
    superseded = _deployment(db_session, retired_project, build, DeploymentStatus.SUPERSEDED, uid)
    failed = _deployment(db_session, retired_project, build, DeploymentStatus.FAILED, uid)
    active = _deployment(db_session, live_project, build, DeploymentStatus.ACTIVE, uid)
    objects = {
        label: DeploymentObjects(
            client, sleep_image, project_id=dep.project_id, deployment_id=dep.id, build_id=build.id
        )
        for label, dep in (("superseded", superseded), ("failed", failed), ("active", active))
    }
    manager = DeployManager(client=client)
    for dep in (superseded, failed, active):
        manager.connect_to_traefik(dep.id)  # the route is live for all three
    assert _private_networks(traefik) == {o.network_name for o in objects.values()}

    report = reconciler.reconcile(db_session)

    assert objects["superseded"].gone() and objects["failed"].gone()
    assert objects["active"].present()
    assert traefik.status == "running"  # detached, never removed
    assert _private_networks(traefik) == {objects["active"].network_name}
    assert len(report.removed) == 4 and report.failed == []


def test_a_deploying_deployments_objects_are_untouched(
    client, reconciler, db_session, make_project, seeded_ids, sleep_image
):
    project = make_project()
    build = _build(db_session, project, BuildStatus.SUCCEEDED)
    deploying = _deployment(db_session, project, build, DeploymentStatus.DEPLOYING, seeded_ids["user_id"])
    objects = DeploymentObjects(
        client, sleep_image, project_id=project.id, deployment_id=deploying.id, build_id=build.id
    )

    reconciler.reconcile(db_session)

    assert objects.present()  # in progress: the worker that is calling the reconciler is not running it


def test_a_network_left_by_a_deployment_whose_container_never_existed_is_removed(
    client, reconciler, db_session, make_project, seeded_ids
):
    project = make_project()
    build = _build(db_session, project, BuildStatus.SUCCEEDED)
    failed = _deployment(db_session, project, build, DeploymentStatus.FAILED, seeded_ids["user_id"])
    network = client.networks.create(
        f"forge-deploy-net-{failed.id}-{_uid()}",
        driver="bridge",
        internal=True,
        labels=platform_labels(Role.DEPLOY_NETWORK, deployment_id=failed.id, project_id=project.id),
    )

    reconciler.reconcile(db_session)

    assert not _network_exists(client, network.name)


# -- (c) the service Redis is re-ensured, never removed ------------------------------------------


def _active_service(client, db_session, make_project, seeded_ids, sleep_image):
    """A project with an ACTIVE deployment whose container is on its private network, and a real
    hardened Redis on that network, as `execute_deployment` leaves it."""
    project = make_project("rec-redis")
    build = _build(db_session, project, BuildStatus.SUCCEEDED)
    active = _deployment(db_session, project, build, DeploymentStatus.ACTIVE, seeded_ids["user_id"])
    objects = DeploymentObjects(client, sleep_image, project_id=project.id, deployment_id=active.id, build_id=build.id)
    url = service_data.ensure_project_redis(project.id, docker_client=client)
    project.service_env_json = encrypt_service_env({"REDIS_URL": url})
    db_session.commit()
    network = client.networks.get(objects.network_name)
    network.connect(client.containers.get(service_data.redis_container_name(project.id)))
    return project, url, objects


def _redis_networks(client, project_id) -> set[str]:
    container = client.containers.get(service_data.redis_container_name(project_id))
    container.reload()
    return set(container.attrs["NetworkSettings"]["Networks"])


def _pings(client, project_id, url) -> bool:
    password = service_data.redis_password_from_url(url)
    container = client.containers.get(service_data.redis_container_name(project_id))
    code, out = container.exec_run(["redis-cli", "ping"], environment={"REDISCLI_AUTH": password})
    return code == 0 and b"PONG" in out


def test_a_stopped_service_redis_of_an_active_project_is_started_and_keeps_its_network_and_password(
    client, reconciler, db_session, make_project, seeded_ids, sleep_image
):
    project, url, objects = _active_service(client, db_session, make_project, seeded_ids, sleep_image)
    redis = client.containers.get(service_data.redis_container_name(project.id))
    same_container = redis.id
    redis.stop()
    redis.reload()
    assert redis.status == "exited"

    report = reconciler.reconcile(db_session)

    redis.reload()
    assert redis.id == same_container  # started, not replaced
    assert redis.status == "running"
    assert _redis_networks(client, project.id) == {objects.network_name}
    assert _pings(client, project.id, url)  # the stored credential still works
    assert any("stopped" in line for line in report.restored)


def test_a_missing_service_redis_of_an_active_project_is_recreated_on_the_active_network_with_the_stored_password(
    client, reconciler, db_session, make_project, seeded_ids, sleep_image
):
    project, url, objects = _active_service(client, db_session, make_project, seeded_ids, sleep_image)
    old = client.containers.get(service_data.redis_container_name(project.id))
    old_id = old.id
    old.remove(v=True, force=True)
    stored_before = db_session.get(Project, project.id).service_env_json

    report = reconciler.reconcile(db_session)

    new = client.containers.get(service_data.redis_container_name(project.id))
    assert new.id != old_id and new.status == "running"
    assert _redis_networks(client, project.id) == {objects.network_name}  # attached, and never on the default bridge
    assert _pings(client, project.id, url)  # the credential the running service holds
    assert new.attrs["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
    assert db_session.get(Project, project.id).service_env_json == stored_before  # the stored env was not rewritten
    assert any("absent" in line for line in report.restored)
    entry = db_session.query(AuditLog).filter(AuditLog.action == "reconciler.restored").one()
    assert entry.target == f"project:{project.id}"


def test_a_healthy_service_redis_is_left_exactly_as_it_is(
    client, reconciler, db_session, make_project, seeded_ids, sleep_image
):
    project, url, objects = _active_service(client, db_session, make_project, seeded_ids, sleep_image)
    before = client.containers.get(service_data.redis_container_name(project.id))
    started_at = before.attrs["State"]["StartedAt"]

    report = reconciler.reconcile(db_session)

    after = client.containers.get(service_data.redis_container_name(project.id))
    assert (after.id, after.attrs["State"]["StartedAt"]) == (before.id, started_at)
    assert report.restored == [] and report.removed == []


def test_a_service_redis_is_never_removed_even_for_a_project_with_no_active_deployment(
    client, reconciler, db_session, make_project, seeded_ids
):
    project = make_project("rec-stopped")
    build = _build(db_session, project, BuildStatus.SUCCEEDED)
    _deployment(db_session, project, build, DeploymentStatus.STOPPED, seeded_ids["user_id"])
    service_data.ensure_project_redis(project.id, docker_client=client)
    redis = client.containers.get(service_data.redis_container_name(project.id))
    before = redis.id

    report = reconciler.reconcile(db_session)

    assert client.containers.get(service_data.redis_container_name(project.id)).id == before
    assert report.removed == []


def test_a_pre_hardening_service_redis_is_not_replaced_from_the_background(
    client, reconciler, db_session, make_project, seeded_ids, sleep_image
):
    """A password-less stored URL belongs to a Redis from before the Redis isolation work (default bridge, no
    password). Replacing it would change the credential a running service holds; that is a
    deployment's decision."""
    project = make_project("rec-legacy")
    build = _build(db_session, project, BuildStatus.SUCCEEDED)
    active = _deployment(db_session, project, build, DeploymentStatus.ACTIVE, seeded_ids["user_id"])
    DeploymentObjects(client, sleep_image, project_id=project.id, deployment_id=active.id, build_id=build.id)
    name = service_data.redis_container_name(project.id)
    legacy = client.containers.create(
        image=settings.service_redis_image,
        name=name,
        labels=platform_labels(Role.SERVICE_REDIS, project_id=project.id, extra={"forge.data_service": "redis"}),
    )
    project.service_env_json = encrypt_service_env({"REDIS_URL": f"redis://{name}:6379/0"})
    db_session.commit()

    report = reconciler.reconcile(db_session)

    legacy.reload()
    assert legacy.status == "created"  # not started, not recreated, not removed
    assert client.containers.get(name).id == legacy.id
    assert report.restored == []


# -- (d) not ours: another instance, no labels, an unknown role ---------------------------------


def test_objects_of_another_instance_unlabelled_or_of_unknown_role_are_untouched_while_ours_goes(
    client, reconciler, db_session, make_project, seeded_ids, sleep_image, tmp_path, _tracked
):
    project = make_project()
    failed = _build(db_session, project, BuildStatus.FAILED)
    dead_dep = _deployment(db_session, project, failed, DeploymentStatus.FAILED, seeded_ids["user_id"])

    def tagged(labels: dict) -> dict:
        # Made by hand with labels the tracker does not tag (it tags only the suite's own
        # instance), so the run id is added here and cleanup still finds them by it.
        return {**labels, "forge.test_run": _tracked.run_id}

    ours = BuildObjects(client, failed.id, tmp_path)
    other_instance = _labelled_container(
        client,
        sleep_image,
        f"forge-it-other-{_uid()}",
        tagged({**platform_labels(Role.SANDBOX, build_id=failed.id), "forge.instance": OTHER_INSTANCE}),
    )
    no_instance = _labelled_container(
        client,
        sleep_image,
        f"forge-it-noinst-{_uid()}",
        tagged({"forge.role": "sandbox", "forge.build_id": failed.id}),
    )
    unlabelled = _labelled_container(client, sleep_image, f"forge-it-plain-{_uid()}", tagged({}))
    unknown_role = _labelled_container(
        client,
        sleep_image,
        f"forge-it-role-{_uid()}",
        tagged({**platform_labels(Role.SANDBOX, build_id=failed.id), "forge.role": "sandbox-ish"}),
    )
    # What the dev host's live demo deployment looks like: platform labels, no instance, for a
    # deployment the database calls FAILED here -- the shape the reconciler must leave alone.
    demo_like = _labelled_container(
        client,
        sleep_image,
        f"forge-it-demo-{_uid()}",
        tagged(
            {
                "forge.deploy": "true",
                "forge.deployment_id": dead_dep.id,
                "forge.project_id": project.id,
                "forge.build_id": failed.id,
                "forge.network": "none-of-yours",
            }
        ),
    )
    other_net = client.networks.create(
        f"forge-it-onet-{_uid()}",
        labels=tagged({**platform_labels(Role.BUILD_NETWORK, build_id=failed.id), "forge.instance": OTHER_INSTANCE}),
    )
    plain_net = client.networks.create(f"forge-it-pnet-{_uid()}", labels=tagged({}))
    demo_net = client.networks.create(f"forge-it-dnet-{_uid()}", labels=tagged({}))
    demo_net.connect(demo_like)

    report = reconciler.reconcile(db_session)

    assert ours.all_gone()  # the control: the one set that IS ours, for a FAILED build, went
    for container in (other_instance, no_instance, unlabelled, unknown_role, demo_like):
        container.reload()
        assert container.status == "running", container.name  # exists, and was not touched
    for network in (other_net, plain_net, demo_net):
        assert _network_exists(client, network.name), network.name
    demo_like.reload()
    assert demo_net.name in demo_like.attrs["NetworkSettings"]["Networks"]  # still attached to its network
    assert len(report.removed) == 2  # the sandbox and its network, nothing else


# -- (e) a volume is never removed ------------------------------------------------------------------


def test_a_volume_is_never_removed_not_even_one_mounted_by_a_container_the_reconciler_removes(
    client, reconciler, db_session, make_project, sleep_image, _tracked
):
    failed = _build(db_session, make_project(), BuildStatus.FAILED)
    named = f"forge-it-vol-{_uid()}"
    loose = f"forge-it-loose-{_uid()}"
    labels = {**platform_labels(Role.SANDBOX, build_id=failed.id), "forge.test_run": _tracked.run_id}
    client.volumes.create(name=named, labels=labels)
    client.volumes.create(name=loose, labels=labels)
    box = client.containers.run(
        image=sleep_image,
        name=f"forge-it-volbox-{_uid()}",
        detach=True,
        labels=labels,
        volumes={named: {"bind": "/data", "mode": "rw"}},
    )
    try:
        report = reconciler.reconcile(db_session)

        assert not _exists(client, box.name)  # the container, labelled and for a FAILED build, was removed ...
        assert len(report.removed) == 1
        assert client.volumes.get(named).name == named  # ... and neither volume was
        assert client.volumes.get(loose).name == loose
    finally:
        # The container first: a volume a container still mounts cannot be removed, and a red test
        # (or a mutation of the reconciler) leaves the container standing.
        remove_handles(client, [box])
        for name in (named, loose):
            try:
                client.volumes.get(name).remove(force=True)
            except (NotFound, DockerException):
                pass


# -- worker start: orphaned rows and their objects ------------------------------------------------------


@needs_proxy_image
def test_worker_start_fails_a_running_build_and_a_deploying_deployment_and_reaps_their_objects(
    client, db_session, make_project, seeded_ids, sleep_image, tmp_path, monkeypatch
):
    uid = seeded_ids["user_id"]
    dead_build = _build(db_session, make_project("rec-start-b"), BuildStatus.RUNNING, started_at=datetime.now(timezone.utc))
    dead_objects = BuildObjects(client, dead_build.id, tmp_path, proxy=True)

    dep_project = make_project("rec-start-d")
    dep_build = _build(db_session, dep_project, BuildStatus.SUCCEEDED)
    claimed = _deployment(db_session, dep_project, dep_build, DeploymentStatus.DEPLOYING, uid, started=True)
    claimed_objects = DeploymentObjects(
        client, sleep_image, project_id=dep_project.id, deployment_id=claimed.id, build_id=dep_build.id
    )

    # Controls: a deployment that is ACTIVE, and one that is QUEUED (DEPLOYING, not yet claimed).
    live_project = make_project("rec-start-live")
    live_build = _build(db_session, live_project, BuildStatus.SUCCEEDED)
    active = _deployment(db_session, live_project, live_build, DeploymentStatus.ACTIVE, uid)
    active_objects = DeploymentObjects(
        client, sleep_image, project_id=live_project.id, deployment_id=active.id, build_id=live_build.id
    )
    queued_project = make_project("rec-start-q")
    queued = _deployment(
        db_session, queued_project, _build(db_session, queued_project, BuildStatus.SUCCEEDED), DeploymentStatus.DEPLOYING, uid, started=False
    )

    monkeypatch.setattr(worker, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    worker._recover_at_start(Reconciler(client=client, instance_id=TEST_INSTANCE_ID), {})

    for row in (dead_build, claimed, active, queued):
        db_session.refresh(row)
    assert dead_build.status == BuildStatus.FAILED
    assert dead_build.failure_reason == BuildFailureReason.INFRA_ERROR
    assert "worker restarted" in dead_build.failure_detail
    assert claimed.status == DeploymentStatus.FAILED
    assert dead_objects.all_gone()  # its sandbox, proxy and network, reaped by the pass that follows the row failing
    assert claimed_objects.gone()
    assert active.status == DeploymentStatus.ACTIVE and active_objects.present()
    assert queued.status == DeploymentStatus.DEPLOYING  # the queue survives a restart


# -- the timeout sweep -----------------------------------------------------------------------------------


@needs_proxy_image
def test_the_timeout_sweep_destroys_the_timed_out_builds_sandbox_proxy_and_network_itself(
    client, reconciler, db_session, make_project, tmp_path
):
    now = datetime.now(timezone.utc)
    stale = _build(db_session, make_project("rec-stale"), BuildStatus.RUNNING, started_at=now - timedelta(minutes=90))
    fresh = _build(db_session, make_project("rec-fresh"), BuildStatus.RUNNING, started_at=now)
    stale_objects = BuildObjects(client, stale.id, tmp_path, proxy=True)
    fresh_objects = BuildObjects(client, fresh.id, tmp_path, proxy=True)

    # ONLY the sweep runs: no reconcile pass, so nothing else could have removed them.
    sweep_timed_out_builds(
        db_session, max_duration_minutes=30, destroy_objects=reconciler.destroy_build_objects
    )

    db_session.refresh(stale)
    assert stale.status == BuildStatus.FAILED and stale.failure_reason == BuildFailureReason.TIMEOUT
    assert stale_objects.all_gone()
    assert fresh_objects.all_present()  # inside its time: untouched
    assert db_session.query(AuditLog).filter(AuditLog.action == "reconciler.removed").count() == 3


def test_a_timeout_sweep_with_no_destroyer_leaves_the_objects_for_the_next_pass(
    client, reconciler, db_session, make_project, tmp_path
):
    """The behaviour the sweep used to have, kept as the contrast: the row fails, the objects
    stay. The reconciler's next pass is what then reaps them (the build is no longer RUNNING)."""
    stale = _build(db_session, make_project("rec-rowonly"), BuildStatus.RUNNING, started_at=datetime.now(timezone.utc) - timedelta(minutes=90))
    objects = BuildObjects(client, stale.id, tmp_path)

    sweep_timed_out_builds(db_session, max_duration_minutes=30)
    assert objects.all_present()

    reconciler.reconcile(db_session)
    assert objects.all_gone()
