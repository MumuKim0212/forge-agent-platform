"""`DeployManager`'s failure paths and `remove_service`'s honesty, against
a fake Docker client that behaves the way Docker does where it matters here --
no daemon needed.

The two Docker behaviours the fake reproduces are the ones the bugs lived in:

- **A network with an attached endpoint cannot be removed** (`APIError`). This
  is what once left `forge-deploy-net-<id>` behind: a failed attach
  of the project's Redis had already attached the shared Postgres, and the old
  code removed the network without detaching it first.
- **`containers.run(detach=True)` creates the container, then starts it**, so a
  start that fails leaves a `Created` container that nothing references.

The same paths run against a real engine in `tests/test_deploy_integration.py`.
"""

from __future__ import annotations

import uuid

import pytest
from docker.errors import APIError, DockerException, NotFound

from app.config import settings
from tests.fake_admin_dsn import FAKE_ADMIN_DSN
from tests.instance_scope import TEST_INSTANCE_ID
from app.services.deploy_manager import (
    DeployDataServiceError,
    DeployManager,
    DeployStartError,
)

POSTGRES_NAME = "fake-service-postgres"
PROJECT_ID = "proj-unit"
DEPLOYMENT_ID = "dep-unit"


class FakeNetwork:
    def __init__(self, client, name, labels):
        self._client = client
        self.id = uuid.uuid4().hex
        self.name = name
        self.labels = labels or {}
        self.attached: dict[str, str] = {}  # container id -> name
        self.attrs: dict = {"Containers": {}, "Labels": self.labels}
        self.remove_error: Exception | None = None

    def connect(self, container):
        target = container if isinstance(container, FakeContainer) else self._client.containers.get(container)
        if self._client.connect_errors.get(target.name):
            raise APIError(f"cannot connect {target.name}")
        self.attached[target.id] = target.name

    def disconnect(self, container, force=False):
        cid = container.id if isinstance(container, FakeContainer) else container
        by_name = {name: i for i, name in self.attached.items()}
        key = cid if cid in self.attached else by_name.get(cid)
        if key is None:
            raise NotFound(f"{cid} is not connected to {self.name}")
        del self.attached[key]

    def reload(self):
        if self.id not in self._client.network_by_id:
            raise NotFound(f"network {self.name} not found")
        self.attrs["Containers"] = {cid: {"Name": n} for cid, n in self.attached.items()}

    def remove(self):
        if self.remove_error is not None:
            raise self.remove_error
        if self.attached:
            raise APIError(f"network {self.name} has active endpoints")
        if self.id not in self._client.network_by_id:
            raise NotFound(f"network {self.name} not found")
        del self._client.network_by_id[self.id]


class FakeContainer:
    def __init__(self, client, name, labels=None, status="running"):
        self._client = client
        self.id = uuid.uuid4().hex
        self.name = name
        self.labels = labels or {}
        self.status = status
        self.remove_error: Exception | None = None

    def remove(self, v=False, force=False):
        if self.remove_error is not None:
            raise self.remove_error
        for network in self._client.network_by_id.values():
            network.attached.pop(self.id, None)
        self._client.container_by_name.pop(self.name, None)


class FakeContainers:
    def __init__(self, client):
        self._client = client

    def get(self, name):
        if self._client.lookup_error is not None:
            raise self._client.lookup_error
        try:
            return self._client.container_by_name[name]
        except KeyError:
            raise NotFound(f"no such container: {name}") from None

    def run(self, **kwargs):
        self._client.run_kwargs.append(kwargs)
        container = FakeContainer(
            self._client, kwargs["name"], labels=kwargs.get("labels"), status="created"
        )
        self._client.container_by_name[container.name] = container
        network = self._client.networks.get(kwargs["network"])
        network.attached[container.id] = container.name
        if self._client.run_error is not None:
            # Created, then failed to start: the container stays, as in Docker.
            raise self._client.run_error
        container.status = "running"
        return container


class FakeNetworks:
    def __init__(self, client):
        self._client = client

    def create(self, name, driver=None, internal=False, labels=None):
        if self._client.create_error is not None:
            raise self._client.create_error
        network = FakeNetwork(self._client, name, labels)
        self._client.network_by_id[network.id] = network
        return network

    def get(self, name):
        for network in self._client.network_by_id.values():
            if network.name == name or network.id == name:
                return network
        raise NotFound(f"network {name} not found")

    def list(self, filters=None):
        if self._client.list_error is not None:
            raise self._client.list_error
        wanted = (filters or {}).get("label")
        if wanted is None:
            return list(self._client.network_by_id.values())
        key, _, value = wanted.partition("=")
        return [n for n in self._client.network_by_id.values() if n.labels.get(key) == value]


class FakeDockerClient:
    def __init__(self):
        self.network_by_id: dict[str, FakeNetwork] = {}
        self.container_by_name: dict[str, FakeContainer] = {}
        self.connect_errors: dict[str, bool] = {}
        self.create_error: Exception | None = None
        self.run_error: Exception | None = None
        self.run_kwargs: list[dict] = []
        self.lookup_error: Exception | None = None
        self.list_error: Exception | None = None
        self.containers = FakeContainers(self)
        self.networks = FakeNetworks(self)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    monkeypatch.setattr(settings, "service_postgres_container_name", POSTGRES_NAME)
    monkeypatch.setattr(settings, "traefik_enabled", False)


@pytest.fixture()
def client():
    fake = FakeDockerClient()
    # The shared Postgres exists, and so does this project's Redis, unless a test removes it.
    fake.container_by_name[POSTGRES_NAME] = FakeContainer(fake, POSTGRES_NAME)
    from app.services.service_data import redis_container_name

    fake.redis_name = redis_container_name(PROJECT_ID)
    fake.container_by_name[fake.redis_name] = FakeContainer(fake, fake.redis_name)
    return fake


@pytest.fixture()
def manager(client):
    return DeployManager(client=client)


def _start(manager, *, deployment_id=DEPLOYMENT_ID):
    manager.start_service(
        project_id=PROJECT_ID,
        deployment_id=deployment_id,
        build_id="bld-unit",
        image_ref="img-unit",
        env={"A": "b"},
    )


def _deployment_networks(client, deployment_id=DEPLOYMENT_ID):
    return [n for n in client.network_by_id.values() if deployment_id in n.name]


def _deployment_containers(client, deployment_id=DEPLOYMENT_ID):
    return [c for c in client.container_by_name.values() if deployment_id in c.name]


# -- start_service: nothing of a failed start survives it --------------------


def test_a_successful_start_leaves_a_labelled_network_and_a_running_container(client, manager):
    _start(manager)

    (network,) = _deployment_networks(client)
    # the L4 labels the network carries, plus the platform's own.
    assert network.labels == {
        "forge.deploy": "true",
        "forge.deployment_id": DEPLOYMENT_ID,
        "forge.project_id": PROJECT_ID,
        "forge.instance": TEST_INSTANCE_ID,
        "forge.role": "deploy-network",
    }
    (container,) = _deployment_containers(client)
    assert container.status == "running"
    assert container.labels["forge.network"] == network.name
    # The shared Postgres and the project's Redis joined it before the container started.
    assert set(network.attached.values()) == {POSTGRES_NAME, client.redis_name, container.name}


def test_the_deploy_container_is_created_as_nobody_like_its_preview(client, manager):
    """ADR 0016, ADR 0046 §Decision 5: the same literal user as the preview, so an app
    that writes under the root-owned `/app` fails in the preview the approver judges."""
    _start(manager)

    (kwargs,) = client.run_kwargs
    assert kwargs["user"] == "65534:65534"
    assert kwargs["cap_drop"] == ["ALL"]


def test_previews_and_deploys_take_their_user_from_one_constant():
    from app.services import deploy_manager, preview_manager, service_container_user

    assert service_container_user.SERVICE_CONTAINER_USER == "65534:65534"
    assert deploy_manager.SERVICE_CONTAINER_USER is service_container_user.SERVICE_CONTAINER_USER
    assert preview_manager.SERVICE_CONTAINER_USER is service_container_user.SERVICE_CONTAINER_USER


def test_a_failed_attach_of_the_redis_after_the_postgres_attached_leaves_no_network(client, manager):
    """The leak observed once. Postgres attaches, Redis is missing
    (`NotFound` -> `DeployDataServiceError` -> PROVISION_FAILED), and Docker
    refuses to remove a network that still has Postgres on it."""
    del client.container_by_name[client.redis_name]

    with pytest.raises(DeployDataServiceError):
        _start(manager)

    assert _deployment_networks(client) == []
    assert _deployment_containers(client) == []
    postgres = client.container_by_name[POSTGRES_NAME]
    assert all(postgres.id not in n.attached for n in client.network_by_id.values())


def test_a_failed_attach_of_the_postgres_leaves_no_network(client, manager):
    client.connect_errors[POSTGRES_NAME] = True

    with pytest.raises(DeployDataServiceError):
        _start(manager)

    assert _deployment_networks(client) == []


def test_a_start_that_fails_after_the_container_was_created_removes_the_created_container(
    client, manager
):
    """`containers.run` created it and then failed to start it: without a
    removal by name the `Created` container stays on the host forever."""
    client.run_error = APIError("OCI runtime create failed: executable file not found")

    with pytest.raises(DeployStartError):
        _start(manager)

    assert _deployment_containers(client) == []
    assert _deployment_networks(client) == []


def test_an_unexpected_exception_during_the_start_is_cleaned_up_and_propagates_unchanged(
    client, manager
):
    """Not every failure is a DockerException: the caller maps this one to
    START_FAILED, and nothing may be left behind meanwhile."""
    client.run_error = ValueError("not a Docker error")

    with pytest.raises(ValueError, match="not a Docker error"):
        _start(manager)

    assert _deployment_containers(client) == []
    assert _deployment_networks(client) == []


def test_a_failed_network_creation_raises_and_creates_nothing(client, manager):
    client.create_error = APIError("cannot create network")

    with pytest.raises(DeployStartError, match="network creation failed"):
        _start(manager)

    assert client.network_by_id == {}


def test_a_cleanup_that_cannot_finish_is_an_error_in_the_log_and_the_cause_still_propagates(
    client, manager, caplog
):
    """`_abort_start` runs while another exception is propagating: it must not
    replace it, and it must say what it could not remove."""
    del client.container_by_name[client.redis_name]
    real_create = client.networks.create

    def stubborn_network(name, driver=None, internal=False, labels=None):
        network = real_create(name, driver=driver, internal=internal, labels=labels)
        network.remove_error = APIError("network is busy")
        return network

    client.networks.create = stubborn_network

    with caplog.at_level("ERROR", logger="app.deploy_manager"):
        with pytest.raises(DeployDataServiceError):  # the cause, not the cleanup's failure
            _start(manager)

    messages = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("could not be removed" in m for m in messages)
    assert any("left" in m and DEPLOYMENT_ID in m for m in messages)


# -- remove_service: True only when nothing is left ---------------------------


def test_remove_service_removes_the_container_and_the_network_and_says_so(client, manager):
    _start(manager)

    assert manager.remove_service(DEPLOYMENT_ID) is True

    assert _deployment_containers(client) == []
    assert _deployment_networks(client) == []
    postgres = client.container_by_name[POSTGRES_NAME]
    assert all(postgres.id not in n.attached for n in client.network_by_id.values())


def test_remove_service_is_true_and_harmless_when_there_is_nothing_to_remove(client, manager):
    assert manager.remove_service("never-existed") is True


def test_remove_service_returns_false_when_the_container_cannot_be_removed(client, manager, caplog):
    _start(manager)
    (container,) = _deployment_containers(client)
    container.remove_error = APIError("removal already in progress")

    with caplog.at_level("ERROR", logger="app.deploy_manager"):
        assert manager.remove_service(DEPLOYMENT_ID) is False

    assert _deployment_containers(client) == [container]  # still there
    # The service is still attached to its network and still running: the
    # network (and what is attached to it) is left alone.
    (network,) = _deployment_networks(client)
    assert container.id in network.attached
    assert any(r.levelname == "ERROR" and "could not be removed" in r.getMessage() for r in caplog.records)


def test_remove_service_returns_false_when_the_network_cannot_be_removed(client, manager, caplog):
    _start(manager)
    (network,) = _deployment_networks(client)
    network.remove_error = APIError("network is busy")

    with caplog.at_level("ERROR", logger="app.deploy_manager"):
        assert manager.remove_service(DEPLOYMENT_ID) is False

    assert _deployment_containers(client) == []  # the container is gone ...
    assert _deployment_networks(client) == [network]  # ... the network is not, and that is said
    assert any(r.levelname == "ERROR" and network.name in r.getMessage() for r in caplog.records)


def test_remove_service_returns_false_when_the_daemon_cannot_be_asked(client, manager):
    _start(manager)
    client.lookup_error = DockerException("daemon unreachable")

    assert manager.remove_service(DEPLOYMENT_ID) is False

    client.lookup_error = None
    assert len(_deployment_containers(client)) == 1  # nothing was claimed removed


def test_a_network_whose_container_never_existed_is_found_by_its_deployment_label(client, manager):
    """The container is gone (or was never created); the network carries the
    deployment id, so removing the service still removes it."""
    network = client.networks.create(
        "forge-deploy-net-orphan",
        driver="bridge",
        internal=True,
        labels={"forge.deploy": "true", "forge.deployment_id": "dep-orphan"},
    )
    network.attached["x"] = "some-attached-endpoint"  # something is still on it

    assert manager.remove_service("dep-orphan") is True

    assert client.network_by_id == {}


def test_a_network_from_before_the_label_is_still_found_through_its_container(client, manager):
    """Networks created before the labels were introduced carry none; the container's
    `forge.network` label is the way to them."""
    legacy = client.networks.create("forge-deploy-net-legacy", driver="bridge", internal=True)
    container = FakeContainer(
        client,
        "forge-deploy-dep-legacy",
        labels={"forge.network": legacy.name, "forge.project_id": PROJECT_ID},
    )
    client.container_by_name[container.name] = container
    legacy.attached[container.id] = container.name

    assert manager.remove_service("dep-legacy") is True

    assert client.network_by_id == {}
    assert client.container_by_name.get(container.name) is None
    assert POSTGRES_NAME in client.container_by_name  # only the deployment's own things went


def test_remove_service_detaches_whatever_else_is_attached_not_only_the_known_names(client, manager):
    """Traefik, the data services and anything a failed start attached but never
    recorded: the network is emptied by what is on it, not by a list of names."""
    _start(manager)
    (network,) = _deployment_networks(client)
    network.attached["traefik-id"] = "some-traefik"
    network.attached["stranger-id"] = "a-container-nobody-listed"

    assert manager.remove_service(DEPLOYMENT_ID) is True

    assert client.network_by_id == {}
