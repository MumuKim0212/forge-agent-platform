"""ADR 0037: the reconciler's rules, against a fake Docker engine and a real (in-memory)
database. The Docker-side proof, with real containers, networks and volumes, is
`test_reconciler_integration.py`; this file pins the DECISIONS and the failure behaviour, which a
real engine cannot reach cheaply (a removal that is refused, an engine that answers with objects
the filter should have excluded, a Docker that is not there).

Object ids in the labels are literals; the instance id is the suite's own
(`tests/instance_scope.py`), and the "other" ids are literals that can never be it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from docker.errors import APIError, DockerException, NotFound

from app.models.audit import AuditLog
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.services import service_data
from app.services.reconciler import Reconciler, _Facts, removal_reason
from app.services.service_env_crypto import encrypt_service_env
from app.services.status import BuildStatus, ProjectStatus
from tests.instance_scope import TEST_INSTANCE_ID

OTHER_INSTANCE = "forge-test-someone-else"


# -- a fake engine -------------------------------------------------------------


def _matches(labels: dict, wanted) -> bool:
    wanted = [wanted] if isinstance(wanted, str) else list(wanted or [])
    for clause in wanted:
        key, sep, value = clause.partition("=")
        if key not in labels or (sep and labels[key] != value):
            return False
    return True


class FakeContainer:
    def __init__(self, client, name, labels, status="running", networks=()):
        self._client = client
        self.id = uuid.uuid4().hex
        self.name = name
        self.labels = dict(labels)
        self.status = status
        self.remove_error: Exception | None = None
        self.start_calls = 0
        self.attrs = {
            "Config": {"Labels": self.labels},
            "NetworkSettings": {"Networks": {n: {} for n in networks}},
        }

    def reload(self):
        pass

    def start(self):
        self.start_calls += 1
        self.status = "running"

    def remove(self, v=False, force=False):
        self._client.log.append(("remove-container", self.name, v, force))
        if self.remove_error is not None:
            raise self.remove_error
        for network in self._client.networks_by_id.values():
            network.attached.pop(self.id, None)
        self._client.containers_by_name.pop(self.name, None)


class FakeNetwork:
    def __init__(self, client, name, labels):
        self._client = client
        self.id = uuid.uuid4().hex
        self.name = name
        self.labels = dict(labels)
        self.attached: dict[str, str] = {}
        self.attrs = {"Labels": self.labels, "Containers": {}}

    def reload(self):
        if self.id not in self._client.networks_by_id:
            raise NotFound("network is gone")
        self.attrs["Containers"] = {cid: {"Name": name} for cid, name in self.attached.items()}

    def connect(self, container):
        self._client.log.append(("connect", self.name, container.name))
        self.attached[container.id] = container.name
        container.attrs["NetworkSettings"]["Networks"][self.name] = {}

    def disconnect(self, container, force=False):
        cid = getattr(container, "id", container)
        names = {name: i for i, name in self.attached.items()}
        key = cid if cid in self.attached else names.get(cid)
        self._client.log.append(("disconnect", self.name, self.attached.get(key, cid)))
        if key is None:
            raise NotFound("not connected")
        del self.attached[key]

    def remove(self):
        self._client.log.append(("remove-network", self.name))
        if self.attached:
            raise APIError("network has active endpoints")
        self._client.networks_by_id.pop(self.id, None)


class FakeContainers:
    def __init__(self, client):
        self._client = client

    def list(self, all=False, filters=None, **kwargs):
        self._client.list_calls.append(("containers", dict(filters or {})))
        if self._client.list_error is not None:
            raise self._client.list_error
        found = list(self._client.containers_by_name.values())
        if self._client.honour_filters:
            found = [c for c in found if _matches(c.labels, (filters or {}).get("label"))]
        return found

    def get(self, name):
        try:
            return self._client.containers_by_name[name]
        except KeyError:
            raise NotFound(f"no such container {name}") from None


class FakeNetworks:
    def __init__(self, client):
        self._client = client

    def list(self, filters=None, **kwargs):
        self._client.list_calls.append(("networks", dict(filters or {})))
        found = list(self._client.networks_by_id.values())
        if self._client.honour_filters:
            found = [n for n in found if _matches(n.labels, (filters or {}).get("label"))]
        return found

    def get(self, name):
        for network in self._client.networks_by_id.values():
            if name in (network.name, network.id):
                return network
        raise NotFound(f"no such network {name}")


class FakeDocker:
    def __init__(self, *, honour_filters=True):
        self.containers_by_name: dict[str, FakeContainer] = {}
        self.networks_by_id: dict[str, FakeNetwork] = {}
        self.honour_filters = honour_filters
        self.list_error: Exception | None = None
        self.list_calls: list[tuple] = []
        self.log: list[tuple] = []
        self.containers = FakeContainers(self)
        self.networks = FakeNetworks(self)

    def container(self, name, labels, **kw) -> FakeContainer:
        c = FakeContainer(self, name, labels, **kw)
        self.containers_by_name[name] = c
        return c

    def network(self, name, labels) -> FakeNetwork:
        n = FakeNetwork(self, name, labels)
        self.networks_by_id[n.id] = n
        return n

    def names(self) -> set[str]:
        return set(self.containers_by_name) | {n.name for n in self.networks_by_id.values()}


def _ours(role, **ids) -> dict:
    return {"forge.instance": TEST_INSTANCE_ID, "forge.role": role, **{f"forge.{k}": v for k, v in ids.items()}}


# -- database rows -------------------------------------------------------------


@pytest.fixture()
def project(db_session, seeded_ids):
    row = Project(
        name="rec-unit",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.ACTIVE,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _build(db, project, status=BuildStatus.RUNNING, **fields) -> Build:
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


def _audit(db, action):
    return [a for a in db.query(AuditLog).filter(AuditLog.action == action).all()]


# -- the decisions (pure) --------------------------------------------------------


def _facts(running=(), live=(), deployments=None):
    return _Facts(frozenset(running), frozenset(live), dict(deployments or {}))


@pytest.mark.parametrize("role", ["sandbox", "llm-proxy", "build-network"])
def test_build_objects_stay_only_while_their_build_is_running(role):
    labels = {"forge.build_id": "b-1"}
    assert removal_reason(role, labels, _facts(running={"b-1"})) is None
    assert "b-1 is not RUNNING" in removal_reason(role, labels, _facts(running={"b-2"}))
    # Not RUNNING is the whole rule: a build that SUCCEEDED, FAILED or never existed all look alike.
    assert removal_reason(role, labels, _facts()) is not None
    # An object that names no build belongs to no running build.
    assert "no build" in removal_reason(role, {}, _facts(running={"b-1"}))


@pytest.mark.parametrize("role", ["preview", "preview-redis", "preview-network"])
def test_preview_objects_stay_only_while_their_preview_is_live(role):
    labels = {"forge.build_id": "b-1"}
    assert removal_reason(role, labels, _facts(live={"b-1"})) is None
    assert "no live preview" in removal_reason(role, labels, _facts(live={"b-2"}))
    # A RUNNING build with no live preview is still not a live preview.
    assert removal_reason(role, labels, _facts(running={"b-1"})) is not None
    assert "no build" in removal_reason(role, {}, _facts(live={"b-1"}))


@pytest.mark.parametrize("role", ["deploy", "deploy-network"])
@pytest.mark.parametrize(
    "status,removed",
    [("ACTIVE", False), ("DEPLOYING", False), ("SUPERSEDED", True), ("FAILED", True), ("STOPPED", True)],
)
def test_deploy_objects_stay_only_while_their_deployment_is_active_or_in_progress(role, status, removed):
    labels = {"forge.deployment_id": "d-1"}
    reason = removal_reason(role, labels, _facts(deployments={"d-1": status}))
    assert (reason is not None) is removed
    if removed:
        assert status in reason


def test_a_deploy_object_whose_deployment_has_no_row_or_no_id_is_removed():
    assert "does not exist" in removal_reason("deploy", {"forge.deployment_id": "d-9"}, _facts())
    assert "no deployment" in removal_reason("deploy-network", {}, _facts(deployments={"d-1": "ACTIVE"}))


def test_a_deploy_object_is_judged_by_its_deployment_never_by_its_build():
    """A deploy container carries both ids. Its build RUNNING (another attempt of the same build,
    say) must not keep a superseded deployment's container alive."""
    labels = {"forge.deployment_id": "d-1", "forge.build_id": "b-1"}
    assert removal_reason("deploy", labels, _facts(running={"b-1"}, deployments={"d-1": "SUPERSEDED"})) is not None


def test_service_redis_has_no_removal_rule_whatever_the_database_says():
    for facts in (_facts(), _facts(running={"b"}), _facts(deployments={"d": "FAILED"})):
        assert removal_reason("service-redis", {"forge.project_id": "p"}, facts) is None


# -- one pass over a fake engine ---------------------------------------------------


def _reconciler(fake, **kw) -> Reconciler:
    return Reconciler(client=fake, instance_id=TEST_INSTANCE_ID, **kw)


def test_a_pass_removes_what_the_database_no_longer_owns_and_keeps_the_rest(db_session, project, seeded_ids):
    running = _build(db_session, project, BuildStatus.RUNNING)
    failed = _build(db_session, project, BuildStatus.FAILED)
    fake = FakeDocker()
    for build, tag in ((running, "keep"), (failed, "drop")):
        fake.network(f"net-{tag}", _ours("build-network", build_id=build.id))
        fake.container(f"sandbox-{tag}", _ours("sandbox", build_id=build.id))
        fake.container(f"proxy-{tag}", _ours("llm-proxy", build_id=build.id))

    report = _reconciler(fake).reconcile(db_session)

    assert fake.names() == {"net-keep", "sandbox-keep", "proxy-keep"}
    assert len(report.removed) == 3 and report.failed == []
    assert report.unavailable is False


def test_containers_go_before_the_networks_they_sit_on_and_every_container_removal_takes_its_volumes(
    db_session, project
):
    failed = _build(db_session, project, BuildStatus.FAILED)
    fake = FakeDocker()
    network = fake.network("net", _ours("build-network", build_id=failed.id))
    container = fake.container("sandbox", _ours("sandbox", build_id=failed.id))
    network.attached[container.id] = container.name

    _reconciler(fake).reconcile(db_session)

    kinds = [entry[0] for entry in fake.log]
    assert kinds.index("remove-container") < kinds.index("remove-network")
    assert ("remove-container", "sandbox", True, True) in fake.log  # v=True, force=True


def test_a_deploy_containers_traefik_attachment_is_undone_before_the_container_is_removed(
    db_session, project, seeded_ids, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "traefik_enabled", True)
    monkeypatch.setattr(settings, "traefik_container_name", "traefik-standin")
    build = _build(db_session, project, BuildStatus.SUCCEEDED)
    old = _deployment(db_session, project, build, DeploymentStatus.SUPERSEDED, seeded_ids["user_id"])
    fake = FakeDocker()
    traefik = fake.container("traefik-standin", {})
    network = fake.network("deploy-net", _ours("deploy-network", deployment_id=old.id))
    deploy = fake.container(
        "forge-deploy-old",
        {**_ours("deploy", deployment_id=old.id, build_id=build.id), "forge.network": "deploy-net"},
    )
    network.attached[deploy.id] = deploy.name
    network.attached[traefik.id] = traefik.name

    report = _reconciler(fake).reconcile(db_session)

    assert fake.names() == {"traefik-standin"}  # the operator's Traefik was detached, never removed
    steps = [(e[0], e[2] if e[0] == "disconnect" else e[1]) for e in fake.log]
    assert steps.index(("disconnect", "traefik-standin")) < steps.index(("remove-container", "forge-deploy-old"))
    assert len(report.removed) == 2


def test_the_active_and_the_in_progress_deployment_are_untouched_the_old_ones_are_removed(
    db_session, project, seeded_ids
):
    uid = seeded_ids["user_id"]
    build = _build(db_session, project, BuildStatus.SUCCEEDED)
    states = {
        "active": DeploymentStatus.ACTIVE,
        "deploying": DeploymentStatus.DEPLOYING,
        "superseded": DeploymentStatus.SUPERSEDED,
        "failed": DeploymentStatus.FAILED,
        "stopped": DeploymentStatus.STOPPED,
    }
    fake = FakeDocker()
    for label, status in states.items():
        # Direct inserts: a project may only hold ONE ACTIVE and ONE DEPLOYING by index, so the
        # others are put on their own projects.
        other = Project(
            name=f"rec-{label}",
            owner_id=seeded_ids["user_id"],
            team_id=seeded_ids["team_id"],
            template_id=seeded_ids["template_id"],
            status=ProjectStatus.ACTIVE,
        )
        db_session.add(other)
        db_session.commit()
        dep = _deployment(db_session, other, build, status, uid)
        fake.network(f"net-{label}", _ours("deploy-network", deployment_id=dep.id))
        fake.container(f"deploy-{label}", _ours("deploy", deployment_id=dep.id, build_id=build.id))

    _reconciler(fake).reconcile(db_session)

    assert fake.names() == {"net-active", "deploy-active", "net-deploying", "deploy-deploying"}


def test_the_preview_of_a_build_with_no_expiry_is_removed_and_a_live_one_kept(db_session, project):
    live = _build(db_session, project, BuildStatus.SUCCEEDED, preview_expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    dead = _build(db_session, project, BuildStatus.SUCCEEDED)
    fake = FakeDocker()
    for build, tag in ((live, "live"), (dead, "dead")):
        fake.container(f"preview-{tag}", _ours("preview", build_id=build.id))
        fake.container(f"redis-{tag}", _ours("preview-redis", build_id=build.id))
        fake.network(f"pnet-{tag}", _ours("preview-network", build_id=build.id))

    _reconciler(fake).reconcile(db_session)

    assert fake.names() == {"preview-live", "redis-live", "pnet-live"}


def test_objects_of_another_instance_unlabelled_or_with_an_unknown_role_are_untouched(db_session, project):
    failed = _build(db_session, project, BuildStatus.FAILED)
    fake = FakeDocker(honour_filters=False)  # an engine whose filter let everything through
    fake.container("other-instance", {**_ours("sandbox", build_id=failed.id), "forge.instance": OTHER_INSTANCE})
    fake.container("no-instance", {"forge.role": "sandbox", "forge.build_id": failed.id})
    fake.container("no-labels", {})
    fake.container("unknown-role", _ours("sandbox-ish", build_id=failed.id))
    fake.container("no-role", {"forge.instance": TEST_INSTANCE_ID, "forge.build_id": failed.id})
    fake.container("demo-like", {"forge.deploy": "true", "forge.deployment_id": "d", "forge.build_id": failed.id})
    fake.network("other-net", {**_ours("build-network", build_id=failed.id), "forge.instance": OTHER_INSTANCE})
    fake.network("plain-net", {})
    ours = fake.container("ours", _ours("sandbox", build_id=failed.id))

    report = _reconciler(fake).reconcile(db_session)

    assert "ours" not in fake.names()  # the control: the one object that IS ours went
    assert fake.names() == {
        "other-instance",
        "no-instance",
        "no-labels",
        "unknown-role",
        "no-role",
        "demo-like",
        "other-net",
        "plain-net",
    }
    assert len(report.removed) == 1 and ours.name in report.removed[0]


def test_a_role_stamped_on_the_wrong_kind_of_object_is_not_acted_on(db_session, project):
    failed = _build(db_session, project, BuildStatus.FAILED)
    fake = FakeDocker()
    fake.container("container-with-a-network-role", _ours("build-network", build_id=failed.id))
    fake.network("network-with-a-container-role", _ours("sandbox", build_id=failed.id))

    _reconciler(fake).reconcile(db_session)

    assert fake.names() == {"container-with-a-network-role", "network-with-a-container-role"}


def test_the_docker_lists_are_filtered_by_this_installations_instance_label(db_session, project):
    fake = FakeDocker()

    _reconciler(fake).reconcile(db_session)

    assert ("containers", {"label": f"forge.instance={TEST_INSTANCE_ID}"}) in fake.list_calls
    assert ("networks", {"label": f"forge.instance={TEST_INSTANCE_ID}"}) in fake.list_calls


def test_service_redis_is_never_removed_even_with_no_project_deployment_or_a_failed_one(
    db_session, project, seeded_ids
):
    build = _build(db_session, project, BuildStatus.SUCCEEDED)
    _deployment(db_session, project, build, DeploymentStatus.FAILED, seeded_ids["user_id"])
    fake = FakeDocker()
    fake.container("redis-of-a-failed-project", _ours("service-redis", project_id=project.id))
    fake.container("redis-of-no-project", _ours("service-redis", project_id="no-such-project"))
    fake.container("redis-with-no-id", _ours("service-redis"))

    report = _reconciler(fake).reconcile(db_session)

    assert fake.names() == {"redis-of-a-failed-project", "redis-of-no-project", "redis-with-no-id"}
    assert report.removed == []


def test_a_container_that_cannot_be_removed_is_reported_and_leaves_its_network_and_the_rest_alone(
    db_session, project
):
    stuck = _build(db_session, project, BuildStatus.FAILED)
    fine = _build(db_session, project, BuildStatus.FAILED)
    fake = FakeDocker()
    stuck_net = fake.network("stuck-net", _ours("build-network", build_id=stuck.id))
    stuck_box = fake.container("stuck-box", _ours("sandbox", build_id=stuck.id))
    stuck_net.attached[stuck_box.id] = stuck_box.name
    stuck_box.remove_error = APIError("daemon refused")
    fake.network("fine-net", _ours("build-network", build_id=fine.id))
    fake.container("fine-box", _ours("sandbox", build_id=fine.id))

    report = _reconciler(fake).reconcile(db_session)

    assert fake.names() == {"stuck-net", "stuck-box"}  # the sibling build's objects are gone
    assert stuck_box.id in stuck_net.attached  # still attached: the live container was not stripped of its network
    assert len(report.failed) == 2 and len(report.removed) == 2
    assert any("stuck-box" in line for line in report.failed)
    assert any("stuck-net" in line for line in report.failed)
    assert _audit(db_session, "reconciler.removed") and all(
        "stuck" not in a.metadata_json["name"] for a in _audit(db_session, "reconciler.removed")
    )


def test_an_object_removed_by_someone_else_between_the_list_and_the_call_is_not_reported_as_removed(
    db_session, project
):
    failed = _build(db_session, project, BuildStatus.FAILED)
    fake = FakeDocker()
    box = fake.container("vanishing", _ours("sandbox", build_id=failed.id))
    box.remove_error = NotFound("already gone")

    report = _reconciler(fake).reconcile(db_session)

    assert report.removed == [] and report.failed == []
    assert _audit(db_session, "reconciler.removed") == []


def test_every_removal_is_audit_logged_against_its_row_with_no_actor(db_session, project, seeded_ids):
    failed = _build(db_session, project, BuildStatus.FAILED)
    build = _build(db_session, project, BuildStatus.SUCCEEDED)
    old = _deployment(db_session, project, build, DeploymentStatus.SUPERSEDED, seeded_ids["user_id"])
    fake = FakeDocker()
    fake.container("sandbox-x", _ours("sandbox", build_id=failed.id, project_id=project.id))
    fake.network("net-x", _ours("deploy-network", deployment_id=old.id))

    _reconciler(fake).reconcile(db_session)

    entries = {a.target: a for a in _audit(db_session, "reconciler.removed")}
    assert set(entries) == {f"build:{failed.id}", f"deployment:{old.id}"}
    box_entry = entries[f"build:{failed.id}"]
    assert box_entry.actor_id is None
    assert box_entry.metadata_json["role"] == "sandbox"
    assert box_entry.metadata_json["object"] == "container"
    assert box_entry.metadata_json["name"] == "sandbox-x"
    assert box_entry.metadata_json["project_id"] == project.id
    assert f"build {failed.id} is not RUNNING" == box_entry.metadata_json["reason"]


def test_a_quiet_pass_writes_nothing(db_session, project, caplog):
    running = _build(db_session, project, BuildStatus.RUNNING)
    fake = FakeDocker()
    fake.container("sandbox", _ours("sandbox", build_id=running.id))

    with caplog.at_level(logging.INFO, logger="app.reconciler"):
        report = _reconciler(fake).reconcile(db_session)

    assert not report.changed and report.failed == []
    assert _audit(db_session, "reconciler.removed") == []
    assert [r for r in caplog.records if r.levelno >= logging.INFO] == []


def test_a_pass_never_raises_when_docker_is_not_there(db_session, caplog):
    def no_docker():
        raise DockerException("daemon not running")

    with caplog.at_level(logging.WARNING, logger="app.reconciler"):
        report = Reconciler(client_factory=no_docker, instance_id=TEST_INSTANCE_ID).reconcile(db_session)

    assert report.unavailable is True
    assert any("skipped" in r.getMessage() for r in caplog.records)


def test_a_host_with_no_docker_is_warned_about_once_not_every_interval(db_session, caplog):
    attempts = []

    def no_docker():
        attempts.append(1)
        raise DockerException("daemon not running")

    reconciler = Reconciler(client_factory=no_docker, instance_id=TEST_INSTANCE_ID)
    with caplog.at_level(logging.DEBUG, logger="app.reconciler"):
        for _ in range(3):
            assert reconciler.reconcile(db_session).unavailable is True

    assert len(attempts) == 3  # it still tries every pass: Docker may come up
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_docker_coming_back_is_logged_and_the_pass_then_runs(db_session, project, caplog):
    failed = _build(db_session, project, BuildStatus.FAILED)
    fake = FakeDocker()
    fake.container("late-box", _ours("sandbox", build_id=failed.id))
    up = {"now": False}

    def factory():
        if not up["now"]:
            raise DockerException("daemon not running")
        return fake

    reconciler = Reconciler(client_factory=factory, instance_id=TEST_INSTANCE_ID)
    assert reconciler.reconcile(db_session).unavailable is True

    up["now"] = True
    with caplog.at_level(logging.INFO, logger="app.reconciler"):
        report = reconciler.reconcile(db_session)

    assert report.unavailable is False and fake.names() == set()
    assert any("reachable again" in r.getMessage() for r in caplog.records)


def test_a_pass_never_raises_when_a_list_call_fails_and_the_service_redis_step_still_runs(
    db_session, project, seeded_ids, monkeypatch
):
    fake = FakeDocker()
    fake.list_error = APIError("boom")
    ensured = []
    monkeypatch.setattr(
        service_data, "ensure_project_redis", lambda pid, **kw: ensured.append(pid) or kw["redis_url"]
    )
    build = _build(db_session, project, BuildStatus.SUCCEEDED)
    _deployment(db_session, project, build, DeploymentStatus.ACTIVE, seeded_ids["user_id"])
    project.service_env_json = encrypt_service_env({"REDIS_URL": _hardened_url(project.id)})
    db_session.commit()

    report = _reconciler(fake).reconcile(db_session)

    assert any("_reap raised" in line for line in report.failed)
    assert ensured == [project.id]  # the second step ran although the first one raised


def test_the_time_out_destroy_removes_only_that_builds_build_objects(db_session, project):
    timed_out = _build(db_session, project, BuildStatus.RUNNING)
    other = _build(db_session, project, BuildStatus.RUNNING)
    fake = FakeDocker()
    fake.container("sandbox-t", _ours("sandbox", build_id=timed_out.id))
    fake.container("proxy-t", _ours("llm-proxy", build_id=timed_out.id))
    fake.network("net-t", _ours("build-network", build_id=timed_out.id))
    fake.container("preview-t", _ours("preview", build_id=timed_out.id))  # not L3 build objects
    fake.container("sandbox-o", _ours("sandbox", build_id=other.id))
    fake.container("sandbox-foreign", {**_ours("sandbox", build_id=timed_out.id), "forge.instance": OTHER_INSTANCE})
    fake.container("sandbox-unlabelled", {"forge.build_id": timed_out.id})

    report = _reconciler(fake).destroy_build_objects(db_session, timed_out.id)

    assert fake.names() == {"preview-t", "sandbox-o", "sandbox-foreign", "sandbox-unlabelled"}
    assert len(report.removed) == 3
    assert _audit(db_session, "reconciler.removed")[0].metadata_json["reason"] == "its build timed out"


def test_the_time_out_destroy_never_raises(db_session, project):
    fake = FakeDocker()
    fake.list_error = APIError("boom")

    report = _reconciler(fake).destroy_build_objects(db_session, "any-build")

    assert report.removed == [] and report.failed


# -- the service Redis rule ----------------------------------------------------------------


def _hardened_url(project_id: str, password: str = "s3cret-pw") -> str:
    return service_data.redis_url_with_password(service_data.redis_container_name(project_id), password)


@pytest.fixture()
def active_project(db_session, project, seeded_ids):
    build = _build(db_session, project, BuildStatus.SUCCEEDED)
    dep = _deployment(db_session, project, build, DeploymentStatus.ACTIVE, seeded_ids["user_id"])
    project.service_env_json = encrypt_service_env({"DATABASE_URL": "postgresql://x", "REDIS_URL": _hardened_url(project.id)})
    db_session.commit()
    return project, dep


@pytest.fixture()
def ensure_spy(monkeypatch):
    calls: list[tuple] = []

    def fake_ensure(project_id, *, docker_client, redis_url=None):
        calls.append((project_id, redis_url))
        return redis_url

    monkeypatch.setattr(service_data, "ensure_project_redis", fake_ensure)
    return calls


def _deploy_side(fake, dep, *, network_name="active-net"):
    network = fake.network(network_name, _ours("deploy-network", deployment_id=dep.id))
    deploy = fake.container(
        f"forge-deploy-{dep.id}",
        {**_ours("deploy", deployment_id=dep.id), "forge.network": network_name},
        networks=[network_name],
    )
    network.attached[deploy.id] = deploy.name
    return network


def test_a_missing_redis_is_ensured_with_the_stored_url_attached_to_the_active_network_and_audited(
    db_session, active_project, ensure_spy, monkeypatch
):
    project, dep = active_project
    fake = FakeDocker()
    _deploy_side(fake, dep)

    def appears(project_id, *, docker_client, redis_url=None):
        # What `ensure_project_redis` does for a missing container, as the fake sees it: one
        # appears with no network at all (`create_isolated_redis`).
        ensure_spy.append((project_id, redis_url))
        docker_client.container(
            service_data.redis_container_name(project_id),
            _ours("service-redis", project_id=project_id),
            networks=[],
        )
        return redis_url

    monkeypatch.setattr(service_data, "ensure_project_redis", appears)

    report = _reconciler(fake).reconcile(db_session)

    assert ensure_spy == [(project.id, _hardened_url(project.id))]  # the STORED url: the credential the service holds
    assert ("connect", "active-net", service_data.redis_container_name(project.id)) in fake.log
    assert any("absent" in line for line in report.restored)
    (entry,) = _audit(db_session, "reconciler.restored")
    assert entry.target == f"project:{project.id}"
    assert entry.metadata_json == {
        "role": "service-redis",
        "state_before": "absent",
        "deployment_id": dep.id,
    }
    assert "s3cret-pw" not in str(entry.metadata_json)  # a credential is never audited


def test_a_stopped_redis_is_ensured(db_session, active_project, ensure_spy):
    project, dep = active_project
    fake = FakeDocker()
    _deploy_side(fake, dep)
    fake.container(
        service_data.redis_container_name(project.id),
        _ours("service-redis", project_id=project.id),
        status="exited",
        networks=["active-net"],
    )

    report = _reconciler(fake).reconcile(db_session)

    assert ensure_spy == [(project.id, _hardened_url(project.id))]
    assert any("stopped" in line for line in report.restored)


def test_a_running_redis_that_is_not_on_the_active_network_is_attached(db_session, active_project, ensure_spy):
    project, dep = active_project
    fake = FakeDocker()
    _deploy_side(fake, dep)
    fake.container(
        service_data.redis_container_name(project.id),
        _ours("service-redis", project_id=project.id),
        networks=["some-old-net"],
    )

    report = _reconciler(fake).reconcile(db_session)

    assert ("connect", "active-net", service_data.redis_container_name(project.id)) in fake.log
    assert any("detached" in line for line in report.restored)


def test_a_healthy_redis_is_left_alone_without_calling_ensure_or_writing_an_audit_entry(
    db_session, active_project, ensure_spy
):
    project, dep = active_project
    fake = FakeDocker()
    _deploy_side(fake, dep)
    fake.container(
        service_data.redis_container_name(project.id),
        _ours("service-redis", project_id=project.id),
        networks=["active-net"],
    )

    report = _reconciler(fake).reconcile(db_session)

    assert ensure_spy == []
    assert report.restored == [] and _audit(db_session, "reconciler.restored") == []
    assert not any(entry[0] == "connect" for entry in fake.log)


def test_a_password_less_stored_url_from_before_the_hardened_redis_is_left_to_the_next_deployment(
    db_session, project, seeded_ids, ensure_spy, caplog
):
    build = _build(db_session, project, BuildStatus.SUCCEEDED)
    dep = _deployment(db_session, project, build, DeploymentStatus.ACTIVE, seeded_ids["user_id"])
    legacy_url = f"redis://{service_data.redis_container_name(project.id)}:6379/0"
    project.service_env_json = encrypt_service_env({"REDIS_URL": legacy_url})
    db_session.commit()
    fake = FakeDocker()
    _deploy_side(fake, dep)  # and its Redis is missing: still not replaced from here
    reconciler = _reconciler(fake)

    with caplog.at_level(logging.DEBUG, logger="app.reconciler"):
        first = reconciler.reconcile(db_session)
        reconciler.reconcile(db_session)
        reconciler.reconcile(db_session)

    assert ensure_spy == []
    assert first.restored == []
    warned = [r for r in caplog.records if "password-less" in r.getMessage()]
    assert [r.levelno for r in warned] == [logging.WARNING, logging.DEBUG, logging.DEBUG]  # said once, not every interval


def test_a_project_with_no_readable_stored_env_is_skipped_not_reissued(db_session, project, seeded_ids, ensure_spy):
    build = _build(db_session, project, BuildStatus.SUCCEEDED)
    _deployment(db_session, project, build, DeploymentStatus.ACTIVE, seeded_ids["user_id"])
    fake = FakeDocker()

    _reconciler(fake).reconcile(db_session)  # no service_env_json at all
    project.service_env_json = "not-a-fernet-token"
    db_session.commit()
    _reconciler(fake).reconcile(db_session)  # one that does not decrypt

    assert ensure_spy == []


def test_only_projects_with_an_active_deployment_have_their_redis_ensured(db_session, project, seeded_ids, ensure_spy):
    build = _build(db_session, project, BuildStatus.SUCCEEDED)
    for status in (
        DeploymentStatus.FAILED,
        DeploymentStatus.SUPERSEDED,
        DeploymentStatus.STOPPED,
        DeploymentStatus.DEPLOYING,
    ):
        _deployment(db_session, project, build, status, seeded_ids["user_id"])
    project.service_env_json = encrypt_service_env({"REDIS_URL": _hardened_url(project.id)})
    db_session.commit()

    _reconciler(FakeDocker()).reconcile(db_session)

    assert ensure_spy == []  # a Redis is re-ensured for a running service, not for a project's history


def test_one_projects_failure_does_not_stop_the_next(db_session, project, seeded_ids, monkeypatch):
    uid = seeded_ids["user_id"]
    projects = []
    for n in range(2):
        p = Project(
            name=f"rec-multi-{n}",
            owner_id=uid,
            team_id=seeded_ids["team_id"],
            template_id=seeded_ids["template_id"],
            status=ProjectStatus.ACTIVE,
        )
        db_session.add(p)
        db_session.commit()
        b = _build(db_session, p, BuildStatus.SUCCEEDED)
        _deployment(db_session, p, b, DeploymentStatus.ACTIVE, uid)
        p.service_env_json = encrypt_service_env({"REDIS_URL": _hardened_url(p.id)})
        db_session.commit()
        projects.append(p.id)
    seen = []

    def flaky(project_id, *, docker_client, redis_url=None):
        seen.append(project_id)
        if len(seen) == 1:
            raise RuntimeError("one project's Redis is broken")
        return redis_url

    monkeypatch.setattr(service_data, "ensure_project_redis", flaky)

    report = _reconciler(FakeDocker()).reconcile(db_session)

    assert sorted(seen) == sorted(projects)
    assert len(report.failed) == 1 and len(report.restored) == 1


def test_a_returned_url_that_differs_from_the_stored_one_never_rewrites_the_stored_env(
    db_session, active_project, monkeypatch, caplog
):
    project, dep = active_project
    before = project.service_env_json
    monkeypatch.setattr(
        service_data, "ensure_project_redis", lambda pid, **kw: _hardened_url(pid, password="a-different-one")
    )
    fake = FakeDocker()
    _deploy_side(fake, dep)  # Redis absent -> ensured

    with caplog.at_level(logging.ERROR, logger="app.reconciler"):
        _reconciler(fake).reconcile(db_session)

    db_session.refresh(project)
    assert project.service_env_json == before
    assert any("differs from the stored one" in r.getMessage() for r in caplog.records)


def test_an_active_deployment_with_no_container_still_gets_its_redis_but_nothing_to_attach_to(
    db_session, active_project, ensure_spy
):
    project, dep = active_project
    fake = FakeDocker()  # no deploy container, no network

    report = _reconciler(fake).reconcile(db_session)

    assert ensure_spy == [(project.id, _hardened_url(project.id))]
    assert not any(entry[0] == "connect" for entry in fake.log)
    assert len(report.restored) == 1
