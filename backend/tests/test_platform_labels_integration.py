"""ADR 0037 §Decision 1-2: every role's REAL object carries the platform's labels --
against a real Docker Engine. One test per role; each creates the object through the
platform's own code (not a hand-built label dict), then reads its labels back from the engine.

`test_platform_labels.py` proves by reading `app/` that no create site can lack the helper;
this proves what Docker actually stored for each, because a label dict that is built correctly
and then dropped by the call (a typo'd keyword, a `labels=` the SDK ignores) would pass the
source scan and leave the object invisible to the reconciler.

The expected values are literals. The instance is the suite's own (`tests/instance_scope.py`),
and everything these tests create is removed by run id (`forge.test_run`, `docker_cleanup.py`),
never by a platform label.

Skips cleanly (not fails) when Docker is unavailable; the proxy test also needs
`forge-llm-proxy:latest`.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("docker")

import docker  # noqa: E402
from docker.errors import DockerException, ImageNotFound  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import preview_data, service_data  # noqa: E402
from app.services.deploy_manager import DeployManager  # noqa: E402
from app.services.preview_manager import PreviewManager  # noqa: E402
from app.services.proxy_container import ProxyContainer  # noqa: E402
from app.services.sandbox_build_executor import SandboxBuildExecutor  # noqa: E402
from app.services.sandbox_runner import SandboxRunConfig, SandboxRunner  # noqa: E402
from tests.docker_cleanup import PlatformObjectTracker, remove_handles  # noqa: E402
from tests.instance_scope import TEST_INSTANCE_ID  # noqa: E402

SLEEP_IMAGE = "forge-labels-test-sleep:latest"
PROXY_IMAGE = "forge-llm-proxy:latest"


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


@pytest.fixture(scope="module")
def client():
    return docker.from_env()


@pytest.fixture(scope="module")
def sleep_image(client):
    """A long-running image (a service container that stays up)."""
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "Dockerfile").write_text('FROM python:3.11-slim\nCMD ["sleep", "infinity"]\n')
        client.images.build(path=tmp, tag=SLEEP_IMAGE, rm=True)
    yield SLEEP_IMAGE
    try:
        client.images.get(SLEEP_IMAGE).remove(force=True)
    except (ImageNotFound, DockerException):
        pass


@pytest.fixture()
def held():
    """Objects a test made and holds a handle to: removed by handle at the end, whether or not
    they got a label (a test that exists to catch a missing label must not leave the object it
    caught running on the host)."""
    return {"containers": [], "networks": []}


@pytest.fixture(autouse=True)
def _tracked(client, monkeypatch, held):
    """Everything created through the Docker SDK under the suite's instance id is tagged with
    this test's run id while the test runs and removed by it afterwards; whatever a test registered
    in `held` is removed by handle."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)  # no data services: not under test here
    monkeypatch.setattr(settings, "traefik_enabled", False)
    tracker = PlatformObjectTracker()
    with tracker:
        yield tracker
    tracker.remove_created(client)
    remove_handles(client, held["containers"], held["networks"])


def _platform_part(labels: dict) -> dict:
    """The `forge.*` labels except the test harness's own tag."""
    return {k: v for k, v in (labels or {}).items() if k.startswith("forge.") and k != "forge.test_run"}


def _container_labels(container) -> dict:
    container.reload()
    return container.attrs["Config"]["Labels"]


def _network_labels(network) -> dict:
    network.reload()
    return network.attrs["Labels"]


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def test_sandbox_and_its_build_network_carry_the_instance_the_role_and_the_build(client, tmp_path, held):
    build_id = f"b-sandbox-{_uid()}"
    runner = SandboxRunner(client=client)
    network = runner._create_run_network(build_id)
    held["networks"].append(network)
    container = runner._provision(
        SandboxRunConfig(command=["true"], workspace_dir=str(tmp_path), build_id=build_id), network.name
    )
    held["containers"].append(container)

    assert _platform_part(_container_labels(container)) == {
        "forge.instance": TEST_INSTANCE_ID,
        "forge.role": "sandbox",
        "forge.build_id": build_id,
    }
    assert _platform_part(_network_labels(network)) == {
        "forge.instance": TEST_INSTANCE_ID,
        "forge.role": "build-network",
        "forge.build_id": build_id,
    }


def test_a_sandbox_run_with_no_build_is_still_labelled_with_the_instance_and_role(client, tmp_path, held):
    runner = SandboxRunner(client=client)
    network = runner._create_run_network()
    held["networks"].append(network)
    container = runner._provision(SandboxRunConfig(command=["true"], workspace_dir=str(tmp_path)), network.name)
    held["containers"].append(container)

    assert _platform_part(_container_labels(container)) == {"forge.instance": TEST_INSTANCE_ID, "forge.role": "sandbox"}


class _RecordingClient:
    """Wraps the real client so the labels the ENGINE stored on the one container `create` made
    can be read back (`client.containers` builds a fresh collection on every access, so patching
    it in place does not stick)."""

    def __init__(self, real):
        self.api = real.api
        self.labels: dict | None = None
        recorder = self

        class _Containers:
            def create(self, *args, **kwargs):
                container = real.containers.create(*args, **kwargs)
                recorder.labels = _container_labels(container)
                return container

        self.containers = _Containers()


def test_the_disk_cap_probe_container_is_labelled_so_a_leftover_can_be_reaped(client):
    recording = _RecordingClient(client)

    SandboxRunner(client=recording).probe_disk_cap(probe_mb=1)

    assert recording.labels is not None
    assert _platform_part(recording.labels) == {"forge.instance": TEST_INSTANCE_ID, "forge.role": "sandbox"}


def test_the_build_executors_network_carries_the_build_and_project(client, held):
    executor = SandboxBuildExecutor(docker_client=client)
    build = SimpleNamespace(id=f"b-exec-{_uid()}", project_id=f"p-exec-{_uid()}")

    network = executor._create_build_network(build)
    held["networks"].append(network)

    assert _platform_part(_network_labels(network)) == {
        "forge.instance": TEST_INSTANCE_ID,
        "forge.role": "build-network",
        "forge.build_id": build.id,
        "forge.project_id": build.project_id,
    }


@pytest.mark.skipif(not _proxy_image_available(), reason=f"{PROXY_IMAGE} not built locally")
def test_the_llm_proxy_container_carries_the_build(client, tmp_path, held):
    build_id = f"b-proxy-{_uid()}"
    network = SandboxRunner(client=client)._create_run_network(build_id)
    held["networks"].append(network)
    proxy = ProxyContainer(client=client)
    try:
        proxy.start(
            real_api_key="fake-key-for-test",
            sandbox_network=network,
            upstream_base_url="http://example.invalid",
            build_id=build_id,
        )
        labels = _container_labels(proxy._container)
        # a container inherits its IMAGE's labels, and the proxy image carries its capability
        # label. It is the image's, not one the platform put on the container, so it is set aside
        # here (and asserted on its own, so the inheritance is a stated fact and not a surprise).
        assert labels.get("forge.proxy.capabilities") == "drain"
        assert {k: v for k, v in _platform_part(labels).items() if k != "forge.proxy.capabilities"} == {
            "forge.instance": TEST_INSTANCE_ID,
            "forge.role": "llm-proxy",
            "forge.build_id": build_id,
        }
    finally:
        proxy.stop()


def test_a_preview_its_network_and_its_redis_carry_the_build_and_project(client, sleep_image):
    project_id, build_id = f"p-prev-{_uid()}", f"b-prev-{_uid()}"
    manager = PreviewManager(client=client)
    manager.start_preview(project_id=project_id, build=SimpleNamespace(id=build_id, image_ref=sleep_image))
    try:
        container = client.containers.get(f"forge-preview-{build_id}")
        labels = _container_labels(container)
        network = client.networks.get(labels["forge.network"])
        assert _platform_part(labels) == {
            "forge.instance": TEST_INSTANCE_ID,
            "forge.role": "preview",
            "forge.build_id": build_id,
            "forge.project_id": project_id,
            "forge.preview": "true",
            "forge.network": network.name,
        }
        assert _platform_part(_network_labels(network)) == {
            "forge.instance": TEST_INSTANCE_ID,
            "forge.role": "preview-network",
            "forge.build_id": build_id,
            "forge.project_id": project_id,
        }
    finally:
        manager.stop_preview(build_id)


def test_a_preview_redis_carries_the_build_and_keeps_the_marker_the_orphan_sweep_reads(client):
    build_id = f"b-pr-{_uid()}"

    preview_data._create_preview_redis(build_id, docker_client=client)

    container = client.containers.get(preview_data.preview_redis_container_name(build_id))
    assert _platform_part(_container_labels(container)) == {
        "forge.instance": TEST_INSTANCE_ID,
        "forge.role": "preview-redis",
        "forge.build_id": build_id,
        "forge.data_service": "preview-redis",
    }


def test_a_deployment_container_and_its_network_carry_the_ids_and_keep_the_l4_labels(client, sleep_image):
    project_id, deployment_id, build_id = f"p-dep-{_uid()}", f"d-dep-{_uid()}", f"b-dep-{_uid()}"
    manager = DeployManager(client=client)
    manager.start_service(
        project_id=project_id, deployment_id=deployment_id, build_id=build_id, image_ref=sleep_image, env={}
    )
    try:
        container = client.containers.get(f"forge-deploy-{deployment_id}")
        labels = _container_labels(container)
        network = client.networks.get(labels["forge.network"])
        assert _platform_part(labels) == {
            "forge.instance": TEST_INSTANCE_ID,
            "forge.role": "deploy",
            "forge.project_id": project_id,
            "forge.deployment_id": deployment_id,
            "forge.build_id": build_id,
            "forge.deploy": "true",
            "forge.network": network.name,
        }
        assert _platform_part(_network_labels(network)) == {
            "forge.instance": TEST_INSTANCE_ID,
            "forge.role": "deploy-network",
            "forge.project_id": project_id,
            "forge.deployment_id": deployment_id,
            "forge.deploy": "true",
        }
    finally:
        manager.remove_service(deployment_id)


def test_a_service_redis_carries_the_project_and_keeps_the_ownership_labels(client):
    project_id = f"p-redis-{_uid()}"

    service_data.ensure_project_redis(project_id, docker_client=client)

    container = client.containers.get(service_data.redis_container_name(project_id))
    assert _platform_part(_container_labels(container)) == {
        "forge.instance": TEST_INSTANCE_ID,
        "forge.role": "service-redis",
        "forge.project_id": project_id,
        "forge.data_service": "redis",
    }
