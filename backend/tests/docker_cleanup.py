"""remove the data-service containers a test run created, and ONLY those.

`real_service_postgres_admin_dsn` (conftest.py) used to end by removing every
container on the host labelled `forge.data_service=redis` or `=preview-redis`.
Those labels are the ones the PLATFORM puts on a project's production Redis, so
on a dev host that also runs the platform for real (the demo), a single Docker
test module deleted the live service's Redis and nothing noticed until a
redeploy failed. A cleanup that selects by a label the product owns removes the
product's containers; it has to select by something only the test run owns.

How: while a tracker is active, every container this process creates through
the Docker SDK that carries a `forge.data_service` label (that is, every
Redis the code under test makes) also gets `forge.test_run=<run id>`. The
tracker removes exactly the containers carrying its own run id, and
`remove_strays` removes ones left by an earlier, killed run -- by the presence
of that same key, which nothing but a test ever sets.
"""

from __future__ import annotations

import uuid

TEST_RUN_LABEL = "forge.test_run"
DATA_SERVICE_LABEL = "forge.data_service"


class DataServiceContainerTracker:
    """Context manager: tags the data-service containers created while it is
    active. Patches `ContainerCollection.create` (which `run` calls), so the
    code under test is not changed and cannot opt out."""

    def __init__(self) -> None:
        self.run_id = uuid.uuid4().hex
        self._original = None

    def __enter__(self) -> "DataServiceContainerTracker":
        from docker.models.containers import ContainerCollection

        original = ContainerCollection.create
        run_id = self.run_id

        def tagging_create(collection, image, command=None, **kwargs):
            labels = kwargs.get("labels")
            if isinstance(labels, dict) and DATA_SERVICE_LABEL in labels:
                kwargs["labels"] = {**labels, TEST_RUN_LABEL: run_id}
            return original(collection, image, command, **kwargs)

        self._original = original
        ContainerCollection.create = tagging_create
        return self

    def __exit__(self, *exc_info) -> None:
        from docker.models.containers import ContainerCollection

        if self._original is not None:
            ContainerCollection.create = self._original
            self._original = None

    def remove_created(self, client) -> list[str]:
        """Removes the containers this run created; returns their names."""
        return _remove_labelled(client, f"{TEST_RUN_LABEL}={self.run_id}")


def remove_strays(client) -> list[str]:
    """Removes containers tagged by ANY earlier test run (a run that was
    killed before its teardown). Selects by the test-run label's presence,
    never by a label the platform itself sets."""
    return _remove_labelled(client, TEST_RUN_LABEL)


def remove_handles(client, containers=(), networks=()) -> None:
    """Removes exactly the objects a test holds a handle to: containers first (with their
    anonymous volumes), then networks (force-detaching whatever is still on them). Never raises.

    The run-id tracker below cannot remove an object the code under test created WITHOUT the
    platform's labels (a label that went missing is precisely the defect some of these tests
    exist to catch, and a mutation that drops one would otherwise leave a running container on
    the host); a handle needs no label."""
    from docker.errors import DockerException, NotFound

    for container in containers:
        try:
            container.remove(v=True, force=True)
        except (NotFound, DockerException):
            pass
    for network in networks:
        try:
            network.reload()
            for container_id in list((network.attrs.get("Containers") or {}).keys()):
                try:
                    network.disconnect(container_id, force=True)
                except (NotFound, DockerException):
                    pass
            network.remove()
        except (NotFound, DockerException):
            pass


class PlatformObjectTracker:
    """removes the containers AND networks a test created through the platform's own
    code (`DeployManager`, `PreviewManager`, `ProxyContainer`, `SandboxRunner`,
    `ensure_project_redis`, ...), and only those.

    Those objects carry the platform's labels (`forge.instance`, `forge.role`, ...). A cleanup
   that selected by them would repeat an earlier defect (it removed the demo's Redis by a
    label the product owns), so this selects by `forge.test_run=<run id>` and nothing else.
    While it is active, every container or network created through the Docker SDK whose
    `forge.instance` is THIS test run's instance id (`tests/instance_scope.py`: a value no real
    installation can hold) also gets that label. An object the code under test creates with
    any other instance id, or none, is never tagged and so never removed.

    Patches `ContainerCollection.create` (which `run` calls) and `NetworkCollection.create`.
    """

    def __init__(self) -> None:
        self.run_id = uuid.uuid4().hex
        self._originals: dict[str, object] = {}

    def __enter__(self) -> "PlatformObjectTracker":
        from docker.models.containers import ContainerCollection
        from docker.models.networks import NetworkCollection

        from tests.instance_scope import TEST_INSTANCE_ID

        run_id = self.run_id

        def tagged(kwargs: dict) -> dict:
            labels = kwargs.get("labels")
            if isinstance(labels, dict) and labels.get("forge.instance") == TEST_INSTANCE_ID:
                kwargs["labels"] = {TEST_RUN_LABEL: run_id, **labels}
            return kwargs

        original_container_create = ContainerCollection.create
        original_network_create = NetworkCollection.create

        def container_create(collection, *args, **kwargs):
            return original_container_create(collection, *args, **tagged(kwargs))

        def network_create(collection, *args, **kwargs):
            return original_network_create(collection, *args, **tagged(kwargs))

        self._originals = {
            "container": original_container_create,
            "network": original_network_create,
        }
        ContainerCollection.create = container_create
        NetworkCollection.create = network_create
        return self

    def __exit__(self, *exc_info) -> None:
        from docker.models.containers import ContainerCollection
        from docker.models.networks import NetworkCollection

        if self._originals:
            ContainerCollection.create = self._originals["container"]
            NetworkCollection.create = self._originals["network"]
            self._originals = {}

    def remove_created(self, client) -> list[str]:
        """Removes what this run created: containers first, then networks (force-detaching
        whatever the test attached to them). Returns the names removed."""
        label_filter = f"{TEST_RUN_LABEL}={self.run_id}"
        removed = _remove_labelled(client, label_filter)
        removed += _remove_labelled_networks(client, label_filter)
        return removed


def _remove_labelled_networks(client, label_filter: str) -> list[str]:
    from docker.errors import DockerException, NotFound

    removed: list[str] = []
    try:
        networks = client.networks.list(filters={"label": label_filter})
    except DockerException:
        return removed
    for network in networks:
        try:
            network.reload()
            for container_id in list((network.attrs.get("Containers") or {}).keys()):
                try:
                    network.disconnect(container_id, force=True)
                except (NotFound, DockerException):
                    pass
            network.remove()
            removed.append(network.name)
        except (NotFound, DockerException):
            pass
    return removed


def _remove_labelled(client, label_filter: str) -> list[str]:
    from docker.errors import DockerException, NotFound

    removed: list[str] = []
    try:
        containers = client.containers.list(all=True, filters={"label": label_filter})
    except DockerException:
        return removed
    for container in containers:
        try:
            container.remove(v=True, force=True)
            removed.append(container.name)
        except (NotFound, DockerException):
            pass
    return removed
