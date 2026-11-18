"""Unit tests for the pure-Python naming/guard half of
`app/services/preview_data.py` -- no Docker, no Postgres. The
provisioning/drop tests that need a real engine live in
`test_preview_data_integration.py`.
"""

from __future__ import annotations

import pytest
from docker.errors import NotFound

from app.config import settings
from app.services import preview_data
from app.services.preview_data import PreviewDataProvisionError
from tests.fake_admin_dsn import FAKE_ADMIN_DSN
from tests.instance_scope import TEST_INSTANCE_ID


def test_preview_identifier_is_prefixed_and_under_the_postgres_identifier_limit():
    build_id = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    name = preview_data._preview_identifier(build_id)
    assert name == "pv_3fa85f64_5717_4562_b3fc_2c963f66afa6"
    assert len(name) < 63  # Postgres's identifier byte limit


def test_preview_redis_container_name_is_distinct_from_a_services_own():
    name = preview_data.preview_redis_container_name("some-build-id")
    assert not name.startswith("forge-service-redis")
    assert name == "forge-preview-redis-some-build-id"


def test_drop_refuses_a_non_preview_identifier_before_touching_the_admin_dsn(monkeypatch):
    """(b3): the guard raises BEFORE `_admin_url()` is ever reached --
    proven by unsetting the admin DSN entirely, so a connection attempt
    would itself fail with a DIFFERENT message ("...is not configured...",
    from `service_data._admin_url`)."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)
    with pytest.raises(PreviewDataProvisionError, match="refusing to drop"):
        preview_data._drop_preview_postgres("svc_some_service")


def test_drop_refuses_a_non_preview_identifier_even_with_a_dsn_configured(monkeypatch):
    """Same guard, with a (garbage, unreachable) admin DSN configured --
    proves the guard is unconditional, not merely a side effect of the
    unconfigured-DSN error happening to look similar."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    with pytest.raises(PreviewDataProvisionError, match="refusing to drop"):
        preview_data._drop_preview_postgres("svc_some_service")


def test_drop_preview_postgres_is_a_no_op_when_nothing_is_configured(monkeypatch):
    """A `pv_`-prefixed name passes the guard; with no admin DSN at all,
    nothing could ever have been provisioned for it either, so this is a
    silent no-op (mirrors b2's "never had a preview" promise, extended to
    "never could have")."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)
    preview_data._drop_preview_postgres("pv_some_build")  # must not raise


# -- the orphan sweep's public seam -----------------------------


def test_drop_preview_postgres_by_identifier_refuses_a_non_preview_name(monkeypatch):
    """(a4): the public wrapper `worker.sweep_orphaned_preview_stores`
    actually calls carries the SAME guard as `_drop_preview_postgres`
    itself, proven the same way (b3) -- no admin DSN configured, so a
    connection attempt would fail with a different message than the
    guard's own."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)
    with pytest.raises(PreviewDataProvisionError, match="refusing to drop"):
        preview_data.drop_preview_postgres_by_identifier("svc_some_service")


def test_drop_preview_postgres_by_identifier_is_a_no_op_when_nothing_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)
    preview_data.drop_preview_postgres_by_identifier("pv_some_build")  # must not raise


def test_drop_preview_redis_by_build_id_is_a_no_op_with_no_docker_client():
    """Mirrors `_drop_preview_redis`'s own `None`-client no-op (never a
    raise) -- the public wrapper adds nothing new here, just exposes it."""
    preview_data.drop_preview_redis_by_build_id("some-build-id", docker_client=None)  # must not raise


def test_list_preview_databases_and_roles_raise_when_unconfigured(monkeypatch):
    """No admin DSN configured -- both enumeration functions fail loudly
    (never return an empty list as if there were genuinely none), the same
    `service_data_backup.list_service_databases` shape. The exception type
    is `service_data`'s own (`_admin_url()` raises before either
    function's try/except is entered) -- callers (`worker.
    sweep_orphaned_preview_stores`) gate on `service_postgres_configured`
    first and never reach this unconfigured in production."""
    from app.services.service_data import ServiceDataProvisionError

    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)
    with pytest.raises(ServiceDataProvisionError):
        preview_data.list_preview_databases()
    with pytest.raises(ServiceDataProvisionError):
        preview_data.list_preview_roles()


# -- the sweep's Docker half acts on this installation's containers only ----------------------


class _FakeContainer:
    def __init__(self, name, labels):
        self.name = name
        self.labels = labels
        self.removed = False

    def remove(self, **kwargs):
        self.removed = True


class _FakeContainers:
    """Answers every `list` with ALL its containers, ignoring the filter: an engine whose filter
    semantics cannot be relied on. Records the filter it was asked for."""

    def __init__(self, containers):
        self._containers = containers
        self.filters_seen = []

    def list(self, all=False, filters=None):
        self.filters_seen.append(filters)
        return list(self._containers)

    def get(self, name):
        for container in self._containers:
            if container.name == name:
                return container
        raise NotFound("no such container")


class _FakeClient:
    def __init__(self, containers):
        self.containers = _FakeContainers(containers)


def _preview_redis(build_id, instance):
    labels = {"forge.data_service": "preview-redis", "forge.build_id": build_id}
    if instance is not None:
        labels["forge.instance"] = instance
    return _FakeContainer(f"forge-preview-redis-{build_id}", labels)


def test_list_preview_redis_containers_returns_only_this_installations(monkeypatch):
    own = _preview_redis("b-own", TEST_INSTANCE_ID)
    foreign = _preview_redis("b-foreign", "another-installation")
    unlabelled = _preview_redis("b-none", None)
    client = _FakeClient([foreign, own, unlabelled])

    found = preview_data.list_preview_redis_containers(docker_client=client)

    assert found == [{"name": "forge-preview-redis-b-own", "build_id": "b-own"}]
    asked = client.containers.filters_seen[0]["label"]
    assert f"forge.instance={TEST_INSTANCE_ID}" in asked
    assert "forge.data_service=preview-redis" in asked


@pytest.mark.parametrize("instance", ["another-installation", None])
def test_drop_preview_redis_by_build_id_leaves_a_container_that_is_not_this_installations(instance):
    other = _preview_redis("b-1", instance)
    own_but_wrong_build = _preview_redis("b-2", TEST_INSTANCE_ID)
    own_but_wrong_build.name = "forge-preview-redis-b-3"  # the name says b-3, its label says b-2
    client = _FakeClient([other, own_but_wrong_build])

    preview_data.drop_preview_redis_by_build_id("b-1", docker_client=client)
    preview_data.drop_preview_redis_by_build_id("b-3", docker_client=client)

    assert other.removed is False
    assert own_but_wrong_build.removed is False


def test_drop_preview_redis_by_build_id_removes_this_installations_container():
    own = _preview_redis("b-1", TEST_INSTANCE_ID)
    client = _FakeClient([own])

    preview_data.drop_preview_redis_by_build_id("b-1", docker_client=client)

    assert own.removed is True


# -- each half of the provisioning rolls ITSELF back ---------------------------------------


def test_a_postgres_half_that_fails_drops_what_it_created_before_raising(monkeypatch):
    """The four statements are separate autocommit transactions: a failure after the first leaves a
    role, or a role and an empty database. The half drops its own `pv_` identifier on any failure
    (`IF EXISTS` on both, so it is right for whichever statement failed). Here the admin connection
    cannot even be built, which is the failure that proves the drop is attempted regardless of where
    the failure is."""
    dropped: list[str] = []
    monkeypatch.setattr(preview_data, "_admin_url", lambda: (_ for _ in ()).throw(RuntimeError("no admin DSN")))
    monkeypatch.setattr(preview_data, "_drop_preview_postgres", dropped.append)

    with pytest.raises(PreviewDataProvisionError, match="provisioning preview b-1's Postgres database/role failed"):
        preview_data._provision_preview_postgres("b-1")

    assert dropped == ["pv_b_1"]


def test_a_rollback_that_itself_fails_does_not_replace_the_original_error(monkeypatch, caplog):
    def broken_drop(name):
        raise PreviewDataProvisionError("the drop failed too")

    monkeypatch.setattr(preview_data, "_admin_url", lambda: (_ for _ in ()).throw(RuntimeError("no admin DSN")))
    monkeypatch.setattr(preview_data, "_drop_preview_postgres", broken_drop)

    with pytest.raises(PreviewDataProvisionError) as raised:
        preview_data._provision_preview_postgres("b-1")

    assert "provisioning preview b-1's Postgres database/role failed" in str(raised.value)
    assert "the drop failed too" not in str(raised.value)
    assert "failed to roll back its postgres database/role" in caplog.text


def test_any_exception_out_of_the_redis_half_rolls_back_both_halves_and_propagates_unchanged(monkeypatch):
    """Only a `PreviewDataProvisionError` from the Redis half used to roll the Postgres half
    back, and a Redis container the failed create left was nobody's to remove."""
    dropped: list[str] = []
    monkeypatch.setattr(preview_data, "_provision_preview_postgres", lambda build_id: "postgresql://u:p@h/d")

    def redis_fails(build_id, *, docker_client):
        raise KeyError("something unforeseen")

    monkeypatch.setattr(preview_data, "_create_preview_redis", redis_fails)
    monkeypatch.setattr(preview_data, "_drop_preview_postgres", lambda name: dropped.append(f"postgres:{name}"))
    monkeypatch.setattr(
        preview_data, "_drop_preview_redis", lambda build_id, *, docker_client: dropped.append(f"redis:{build_id}")
    )

    with pytest.raises(KeyError, match="something unforeseen"):
        preview_data.provision_preview_data_services("b-2", docker_client=object())

    assert dropped == ["postgres:pv_b_2", "redis:b-2"]
