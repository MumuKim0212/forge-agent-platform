"""Unit tests (no Docker): the pieces of the Redis isolation work that a
fake client can reach exactly -- the preview-teardown selection that used to
delete a project's production Redis, the order of the create / take-off-the-
bridge / start sequence, the guard that refuses a Redis left on the bridge,
and the stored-URL password handling. Real-engine behaviour (a container
really is off the bridge, really refuses `REPLICAOF`) is in
`test_service_data_integration.py` / `test_deploy_integration.py`.
"""

from __future__ import annotations

import pytest
from docker.errors import DockerException

from app.services import service_data
from app.services.preview_manager import PreviewManager
from app.services.service_data import (
    RedisNotIsolatedError,
    create_isolated_redis,
    redis_password_from_url,
    redis_server_command,
    redis_url_with_password,
)
from tests.instance_scope import TEST_INSTANCE_ID


# -- the teardown selection ---------------------------------


class _Labelled:
    def __init__(self, name, labels):
        self.name = name
        self.labels = labels
        self.removed = False

    def remove(self, **kwargs):
        self.removed = True


class _ListingClient:
    """Returns `containers` for ANY filter, the way an engine that combined
    two label filters with OR (or a stale list) would -- so the test shows
    the decision is made per container, not trusted to the engine's filter."""

    def __init__(self, containers):
        self._containers = containers
        self.filters_seen: list[dict] = []
        outer = self

        class _Containers:
            def list(self, all=False, filters=None):
                outer.filters_seen.append(filters or {})
                return list(outer._containers)

        self.containers = _Containers()


def test_preview_teardown_for_a_project_selects_only_previews_and_never_removes_anything_else():
    pid = "proj-1"
    old_preview = _Labelled(
        "forge-preview-b-old",
        {
            "forge.preview": "true",
            "forge.project_id": pid,
            "forge.build_id": "b-old",
            "forge.instance": TEST_INSTANCE_ID,
        },
    )
    current_preview = _Labelled(
        "forge-preview-b-new",
        {
            "forge.preview": "true",
            "forge.project_id": pid,
            "forge.build_id": "b-new",
            "forge.instance": TEST_INSTANCE_ID,
        },
    )
    # a preview of a project with the SAME id under another installation, and one with
    # no instance label at all, are not this installation's to stop.
    other_installations_preview = _Labelled(
        "forge-preview-b-foreign",
        {
            "forge.preview": "true",
            "forge.project_id": pid,
            "forge.build_id": "b-foreign",
            "forge.instance": "some-other-installation",
        },
    )
    unlabelled_preview = _Labelled(
        "forge-preview-b-unlabelled",
        {"forge.preview": "true", "forge.project_id": pid, "forge.build_id": "b-unlabelled"},
    )
    other_projects_preview = _Labelled(
        "forge-preview-b-x",
        {"forge.preview": "true", "forge.project_id": "proj-2", "forge.build_id": "b-x"},
    )
    # The container the old code removed: the project's production Redis has
    # the project label and no build id.
    production_redis = _Labelled(
        "forge-service-redis-proj-1",
        {"forge.data_service": "redis", "forge.project_id": pid},
    )
    deploy_container = _Labelled(
        "forge-deploy-d1",
        {"forge.deploy": "true", "forge.project_id": pid, "forge.build_id": "b-dep"},
    )
    preview_redis = _Labelled(
        "forge-preview-redis-b-old",
        {"forge.data_service": "preview-redis", "forge.build_id": "b-old"},
    )
    marked_but_buildless = _Labelled(
        "forge-preview-odd", {"forge.preview": "true", "forge.project_id": pid}
    )
    everything = [
        old_preview,
        current_preview,
        other_installations_preview,
        unlabelled_preview,
        other_projects_preview,
        production_redis,
        deploy_container,
        preview_redis,
        marked_but_buildless,
    ]
    client = _ListingClient(everything)
    manager = PreviewManager(client=client)
    stopped_builds: list[str] = []
    manager.stop_preview = lambda build_id: stopped_builds.append(build_id) or True  # type: ignore[method-assign]

    count = manager.stop_preview_for_project(pid, except_build_id="b-new")

    assert stopped_builds == ["b-old"]
    assert count == 1
    # `stop_preview` is stubbed, so any `.remove` on a listed container is
    # the old direct-removal branch coming back.
    assert [c.name for c in everything if c.removed] == []
    # And the engine is asked for previews of this project, not for "everything
    # with the project label".
    labels = client.filters_seen[0]["label"]
    assert "forge.preview=true" in labels
    assert f"forge.project_id={pid}" in labels
    assert f"forge.instance={TEST_INSTANCE_ID}" in labels


# -- the stored password -------------------------------


def test_redis_password_is_read_from_the_stored_url():
    assert redis_password_from_url("redis://:s3cret_-x@forge-service-redis-p:6379/0") == "s3cret_-x"


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "",
        "redis://forge-service-redis-p:6379/0",  # issued before Redis isolation
        "redis://:@forge-service-redis-p:6379/0",  # an empty password is no password
        "not a url at all [",
    ],
)
def test_a_stored_url_with_no_usable_password_yields_none(stored):
    assert redis_password_from_url(stored) is None


def test_redis_url_round_trips_through_the_password_reader():
    url = redis_url_with_password("forge-service-redis-p", "abc_DEF-123")
    assert url == "redis://:abc_DEF-123@forge-service-redis-p:6379/0"
    assert redis_password_from_url(url) == "abc_DEF-123"


def test_the_redis_command_line_carries_the_password_and_disables_the_four_commands():
    command = redis_server_command("pw")
    assert command[0] == "redis-server"  # the image entrypoint's branch depends on it
    assert command[command.index("--requirepass") + 1] == "pw"
    renamed = [command[i + 1] for i, arg in enumerate(command) if arg == "--rename-command"]
    assert renamed == ["REPLICAOF", "SLAVEOF", "MIGRATE", "MODULE"]
    for i, arg in enumerate(command):
        if arg == "--rename-command":
            assert command[i + 2] == ""  # renamed to the empty string = disabled


# -- create -> take off the bridge -> start ------------------


class _FakeRedis:
    """A container whose network membership is what `docker create` shows
    (a bridge endpoint), changed only by `disconnect`."""

    def __init__(self, calls, *, stays_on_bridge=False):
        self.name = "forge-service-redis-p"
        self.calls = calls
        self.stays_on_bridge = stays_on_bridge
        self.networks = {"bridge": {}}
        self.attrs: dict = {"NetworkSettings": {"Networks": dict(self.networks)}}

    def reload(self):
        self.attrs = {"NetworkSettings": {"Networks": dict(self.networks)}}

    def start(self):
        self.calls.append("start")

    def remove(self, **kwargs):
        self.calls.append(("remove", kwargs))


class _FakeDocker:
    def __init__(self, *, stays_on_bridge=False, start_fails=False):
        self.calls: list = []
        self.redis = _FakeRedis(self.calls, stays_on_bridge=stays_on_bridge)
        self.start_fails = start_fails
        outer = self

        class _Containers:
            def create(self, **kwargs):
                outer.calls.append(("create", kwargs))
                if outer.start_fails:
                    outer.redis.start = lambda: (_ for _ in ()).throw(DockerException("boom"))  # type: ignore[method-assign]
                return outer.redis

        class _Network:
            def disconnect(self, container, **kwargs):
                outer.calls.append("disconnect-bridge")
                if not outer.redis.stays_on_bridge:
                    outer.redis.networks.pop("bridge", None)

        class _Networks:
            def get(self, name):
                assert name == "bridge"
                return _Network()

        self.containers = _Containers()
        self.networks = _Networks()


def test_the_bridge_endpoint_is_dropped_before_the_container_starts():
    docker_client = _FakeDocker()

    create_isolated_redis(
        docker_client,
        name="forge-service-redis-p",
        labels={"forge.data_service": "redis"},
        password="pw",
        restart_policy={"Name": "unless-stopped"},
    )

    steps = [c if isinstance(c, str) else c[0] for c in docker_client.calls]
    assert steps == ["create", "disconnect-bridge", "start"]
    create_kwargs = docker_client.calls[0][1]
    assert create_kwargs["restart_policy"] == {"Name": "unless-stopped"}
    assert create_kwargs["command"] == redis_server_command("pw")
    assert "network" not in create_kwargs and "network_mode" not in create_kwargs


def test_a_redis_that_is_still_on_the_bridge_after_start_is_refused_and_removed():
    docker_client = _FakeDocker(stays_on_bridge=True)

    with pytest.raises(RedisNotIsolatedError):
        create_isolated_redis(
            docker_client,
            name="forge-service-redis-p",
            labels={"forge.data_service": "redis"},
            password="pw",
        )

    removals = [c for c in docker_client.calls if isinstance(c, tuple) and c[0] == "remove"]
    assert len(removals) == 1
    assert removals[0][1] == {"v": True, "force": True}  # the volumes go with it


def test_a_redis_that_fails_to_start_is_removed_not_left_half_made():
    docker_client = _FakeDocker(start_fails=True)

    with pytest.raises(DockerException):
        create_isolated_redis(
            docker_client,
            name="forge-service-redis-p",
            labels={"forge.data_service": "redis"},
            password="pw",
        )

    assert any(isinstance(c, tuple) and c[0] == "remove" for c in docker_client.calls)


def test_a_preview_redis_is_not_given_a_restart_policy():
    """The service Redis restarts with the daemon; a preview's does not (its
    consumer has no restart policy either). `restart_policy=None` must not
    send the key at all."""
    docker_client = _FakeDocker()

    create_isolated_redis(
        docker_client,
        name="forge-preview-redis-b",
        labels={"forge.data_service": "preview-redis"},
        password="pw",
        restart_policy=None,
    )

    assert "restart_policy" not in docker_client.calls[0][1]


def test_the_module_exposes_one_restart_policy_for_the_service_redis():
    assert service_data.SERVICE_REDIS_RESTART_POLICY == {"Name": "unless-stopped"}
