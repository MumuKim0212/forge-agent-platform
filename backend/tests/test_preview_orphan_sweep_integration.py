"""Integration tests for the orphaned-preview-store sweep
(`worker.sweep_orphaned_preview_stores`, mechanics in `app/services/
preview_data.py`) against a REAL Postgres and a REAL Docker Engine -- no
fakes for the SQL or the container mechanics.

Uses the shared `real_service_postgres_admin_dsn` fixture (conftest.py) for
Postgres/Docker and the ordinary in-memory `db_session` fixture for the
`Build`/`Project` rows this sweep actually reads. The two are independent
stores (conftest.py's own note on this) -- a Build row here never needs to
exist in the throwaway Postgres, and a `pv_` database/role there never
needs a matching row in `db_session`, which is exactly what decision 1's
"no build row exists for it at all" clause needs to be reachable at all.

Skips cleanly (not fails) when Docker is unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("docker")
pytest.importorskip("psycopg")

import docker  # noqa: E402
import psycopg  # noqa: E402
from docker.errors import DockerException, NotFound  # noqa: E402

from app.config import settings  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.build import Build  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.services import preview_data  # noqa: E402
from app.services.build_queue import sweep_timed_out_builds  # noqa: E402
from app.services.preview_manager import PreviewManager  # noqa: E402
from app.services.service_data import create_isolated_redis, generate_redis_password  # noqa: E402
from app.services.status import BuildStatus, ProjectStatus  # noqa: E402
from app.worker import sweep_expired_previews, sweep_orphaned_preview_stores  # noqa: E402
from tests.docker_cleanup import remove_handles  # noqa: E402
from tests.instance_scope import TEST_INSTANCE_ID  # noqa: E402


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


@pytest.fixture()
def docker_client():
    return docker.from_env()


def _make_project(db, seeded_ids, name):
    project = Project(
        name=name,
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_build(db, project_id, *, status, started_at=None, preview_expires_at=None):
    build = Build(project_id=project_id, type="create", status=status)
    db.add(build)
    db.flush()
    if started_at is not None:
        build.started_at = started_at
    if preview_expires_at is not None:
        build.preview_expires_at = preview_expires_at
    db.commit()
    db.refresh(build)
    return build


def _pg_object_exists(name: str) -> bool:
    """Reads `pg_database`/`pg_roles` directly off the throwaway admin
    DSN -- proves ABSENCE (d1), not merely that a function call returned
    without raising."""
    conn = psycopg.connect(settings.service_postgres_admin_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if cur.fetchone() is not None:
                return True
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def test_orphan_sweep_leaves_a_non_terminal_build_alone(
    real_service_postgres_admin_dsn, db_session, seeded_ids, docker_client
):
    """(b1): a RUNNING build's real, fully-provisioned stores are left
    completely alone -- tested directly against the real engine, not a
    monkeypatched stand-in."""
    project = _make_project(db_session, seeded_ids, "orphan-sweep-running")
    build = _make_build(db_session, project.id, status=BuildStatus.RUNNING)
    preview_data.provision_preview_data_services(build.id, docker_client=docker_client)
    name = preview_data._preview_identifier(build.id)
    redis_name = preview_data.preview_redis_container_name(build.id)
    try:
        reclaimed = sweep_orphaned_preview_stores(db_session, docker_client=docker_client)

        assert reclaimed == 0
        assert _pg_object_exists(name)
        docker_client.containers.get(redis_name)  # must not raise NotFound
    finally:
        preview_data.drop_preview_data_services(build.id, docker_client=docker_client)


def test_orphan_sweep_composes_with_the_timeout_sweep(
    real_service_postgres_admin_dsn, db_session, seeded_ids, docker_client
):
    """(b2), (T1): starts from a REAL stuck-RUNNING build (`started_at`
    older than the max build duration), runs `sweep_timed_out_builds` to
    converge it to FAILED/TIMEOUT, THEN runs the orphan sweep and shows
    the leaked stores reclaimed. A test that only ever constructs a FAILED
    build never visits the trap this composition proves closed."""
    stale_started_at = datetime.now(timezone.utc) - timedelta(
        minutes=settings.build_max_duration_minutes + 5
    )
    project = _make_project(db_session, seeded_ids, "orphan-sweep-timeout-composition")
    build = _make_build(
        db_session, project.id, status=BuildStatus.RUNNING, started_at=stale_started_at
    )
    preview_data.provision_preview_data_services(build.id, docker_client=docker_client)
    name = preview_data._preview_identifier(build.id)
    redis_name = preview_data.preview_redis_container_name(build.id)
    assert _pg_object_exists(name)

    timed_out = sweep_timed_out_builds(
        db_session, max_duration_minutes=settings.build_max_duration_minutes
    )
    assert [b.id for b in timed_out] == [build.id]
    db_session.refresh(build)
    assert build.status == BuildStatus.FAILED
    assert build.preview_expires_at is None  # never set for a build that never succeeded

    reclaimed = sweep_orphaned_preview_stores(db_session, docker_client=docker_client)

    assert reclaimed == 2  # one Postgres reclaim (db+role, one identifier), one Redis reclaim
    assert not _pg_object_exists(name)
    with pytest.raises(NotFound):
        docker_client.containers.get(redis_name)


def test_orphan_sweep_reclaims_after_a_teardown_failure(
    real_service_postgres_admin_dsn, db_session, seeded_ids, docker_client, monkeypatch
):
    """(c1), (T2): drives the REAL `sweep_expired_previews` with
    `drop_preview_data_services` failing -- confirms the build's preview
    columns are cleared anyway (today's swallow-then-clear behaviour,
    unchanged here per the task's own boundary), and that the leftover
    stores are then reclaimed by the orphan sweep. No preview app
    container is ever started -- `PreviewManager.stop_preview`'s own
    container lookup is a clean `NotFound`/no-op either way, so only the
    data-services drop needs to be made to fail."""
    project = _make_project(db_session, seeded_ids, "orphan-sweep-teardown-failure")
    build = _make_build(
        db_session,
        project.id,
        status=BuildStatus.SUCCEEDED,
        preview_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    preview_data.provision_preview_data_services(build.id, docker_client=docker_client)
    name = preview_data._preview_identifier(build.id)
    redis_name = preview_data.preview_redis_container_name(build.id)

    def _boom(build_id, *, docker_client=None):
        raise RuntimeError("simulated teardown failure")

    monkeypatch.setattr(preview_data, "drop_preview_data_services", _boom)

    manager = PreviewManager(client=docker_client)
    torn_down = sweep_expired_previews(db_session, preview_manager=manager)
    assert torn_down == 1

    db_session.refresh(build)
    assert build.preview_url is None
    assert build.preview_expires_at is None
    assert build.preview_token_hash is None
    # The drop failed silently (T2) -- the stores are still there.
    assert _pg_object_exists(name)
    docker_client.containers.get(redis_name)  # must not raise

    reclaimed = sweep_orphaned_preview_stores(db_session, docker_client=docker_client)

    assert reclaimed == 2
    assert not _pg_object_exists(name)
    with pytest.raises(NotFound):
        docker_client.containers.get(redis_name)


def test_orphan_sweep_reclaims_a_postgres_only_half_orphan(
    real_service_postgres_admin_dsn, db_session, seeded_ids, docker_client
):
    """(d2): a `pv_` database and role with NO Redis container -- the
    exact crash-order gap this task closes (`_provision_preview_postgres`
    runs before `_create_preview_redis`)."""
    project = _make_project(db_session, seeded_ids, "orphan-sweep-postgres-only")
    build = _make_build(db_session, project.id, status=BuildStatus.FAILED)
    preview_data._provision_preview_postgres(build.id)
    name = preview_data._preview_identifier(build.id)
    assert _pg_object_exists(name)

    try:
        reclaimed = sweep_orphaned_preview_stores(db_session, docker_client=docker_client)

        assert reclaimed == 1
        assert not _pg_object_exists(name)
    finally:
        preview_data.drop_preview_data_services(build.id, docker_client=docker_client)


def test_orphan_sweep_reclaims_a_redis_only_half_orphan(
    real_service_postgres_admin_dsn, db_session, seeded_ids, docker_client
):
    """(d2): a Redis container with NO database -- proves the two halves
    are genuinely reconciled independently (decision 3), not merely
    paired off the same build id."""
    project = _make_project(db_session, seeded_ids, "orphan-sweep-redis-only")
    build = _make_build(db_session, project.id, status=BuildStatus.FAILED)
    preview_data._create_preview_redis(build.id, docker_client=docker_client)
    redis_name = preview_data.preview_redis_container_name(build.id)
    docker_client.containers.get(redis_name)  # sanity: it exists before the sweep

    try:
        reclaimed = sweep_orphaned_preview_stores(db_session, docker_client=docker_client)

        assert reclaimed == 1
        with pytest.raises(NotFound):
            docker_client.containers.get(redis_name)
    finally:
        preview_data.drop_preview_data_services(build.id, docker_client=docker_client)


def test_orphan_sweep_reclaims_a_store_with_no_build_row_at_all(
    real_service_postgres_admin_dsn, db_session, docker_client
):
    """decision 1, clause 2: a fully-provisioned `pv_` database/role/redis
    whose build id was NEVER persisted as a `Build` row anywhere in
    `db_session` -- a bare `uuid4`, matching how `test_preview_data_
    integration.py`'s own `build_id` fixture already works (this module's
    provisioning has never required a real row to exist)."""
    import uuid

    build_id = str(uuid.uuid4())
    preview_data.provision_preview_data_services(build_id, docker_client=docker_client)
    name = preview_data._preview_identifier(build_id)
    redis_name = preview_data.preview_redis_container_name(build_id)
    assert _pg_object_exists(name)

    reclaimed = sweep_orphaned_preview_stores(db_session, docker_client=docker_client)

    assert reclaimed == 2
    assert not _pg_object_exists(name)
    with pytest.raises(NotFound):
        docker_client.containers.get(redis_name)


def test_orphan_sweep_is_idempotent(
    real_service_postgres_admin_dsn, db_session, seeded_ids, docker_client
):
    """(d3): a second sweep over the same, now-clean state does nothing
    and raises nothing."""
    project = _make_project(db_session, seeded_ids, "orphan-sweep-idempotent")
    build = _make_build(db_session, project.id, status=BuildStatus.FAILED)
    preview_data.provision_preview_data_services(build.id, docker_client=docker_client)

    first = sweep_orphaned_preview_stores(db_session, docker_client=docker_client)
    assert first == 2

    second = sweep_orphaned_preview_stores(db_session, docker_client=docker_client)  # must not raise
    assert second == 0


def test_orphan_sweep_writes_one_audit_entry_per_reclaimed_store(
    real_service_postgres_admin_dsn, db_session, seeded_ids, docker_client
):
    """(a5): audited with the system actor, one entry per store, naming
    the actual identifier/container reclaimed."""
    project = _make_project(db_session, seeded_ids, "orphan-sweep-audit")
    build = _make_build(db_session, project.id, status=BuildStatus.FAILED)
    preview_data.provision_preview_data_services(build.id, docker_client=docker_client)
    name = preview_data._preview_identifier(build.id)
    redis_name = preview_data.preview_redis_container_name(build.id)

    reclaimed = sweep_orphaned_preview_stores(db_session, docker_client=docker_client)
    assert reclaimed == 2

    entries = db_session.query(AuditLog).filter_by(action="preview_orphan.reclaimed").all()
    assert len(entries) == 2
    assert all(entry.actor_id is None for entry in entries)  # SYSTEM_ACTOR_ID
    targets = {entry.target for entry in entries}
    assert targets == {f"preview_postgres:{name}", f"preview_redis:{redis_name}"}


# -- the sweep touches only its own installation's stores ------------------------------------

OTHER_INSTANCE = "forge-test-another-installation"  # not TEST_INSTANCE_ID, not a stored uuid hex


def _make_lookalike_redis(docker_client, build_id, *, instance):
    """A container the sweep would reap if it did not look at who owns it: it carries every label
    a real preview-redis carries, for a build id that has no row. `instance=None` leaves the
    instance label off (an older container, or one made by hand). Built through the
    same hardened-Redis helper, with the labels written out here instead of `platform_labels`
    (which would stamp this run's instance on it)."""
    labels = {"forge.data_service": "preview-redis", "forge.build_id": build_id}
    if instance is not None:
        labels["forge.instance"] = instance
        labels["forge.role"] = "preview-redis"
    create_isolated_redis(
        docker_client,
        name=preview_data.preview_redis_container_name(build_id),
        labels=labels,
        password=generate_redis_password(),
        restart_policy=None,
    )
    return preview_data.preview_redis_container_name(build_id)


def _make_lookalike_postgres(build_id, *, comment):
    """A `pv_` database and role with no build row. `comment=None` leaves both uncommented (made
    by an older version, or by hand); otherwise both carry it."""
    name = preview_data._preview_identifier(build_id)
    conn = psycopg.connect(settings.service_postgres_admin_dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE ROLE "{name}" NOLOGIN')
            cur.execute(f'CREATE DATABASE "{name}"')
            if comment is not None:
                cur.execute(f"COMMENT ON ROLE \"{name}\" IS '{comment}'")
                cur.execute(f"COMMENT ON DATABASE \"{name}\" IS '{comment}'")
    finally:
        conn.close()
    return name


def _drop_lookalike_postgres(name):
    conn = psycopg.connect(settings.service_postgres_admin_dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            cur.execute(f'DROP ROLE IF EXISTS "{name}"')
    finally:
        conn.close()


def _remove_container_quietly(docker_client, name):
    try:
        container = docker_client.containers.get(name)
    except DockerException:
        return
    remove_handles(docker_client, containers=[container])


def test_orphan_sweep_removes_only_its_own_instances_stores(
    real_service_postgres_admin_dsn, db_session, docker_client
):
    """ADR 0037 rule 2: a store of this test's instance with no build row is reclaimed, and a
    look-alike under another instance id -- or under none -- survives the same sweep, in BOTH
    halves. Every one of them used to be removed: the sweep listed by the data-service
    label alone, so a test run took a live dev preview's Redis and a dev worker took a test's."""
    import uuid

    own_build = str(uuid.uuid4())
    foreign_redis_build = str(uuid.uuid4())
    unlabelled_redis_build = str(uuid.uuid4())
    foreign_pg_build = str(uuid.uuid4())
    unlabelled_pg_build = str(uuid.uuid4())

    preview_data.provision_preview_data_services(own_build, docker_client=docker_client)
    own_pg = preview_data._preview_identifier(own_build)
    own_redis = preview_data.preview_redis_container_name(own_build)
    foreign_redis = _make_lookalike_redis(docker_client, foreign_redis_build, instance=OTHER_INSTANCE)
    unlabelled_redis = _make_lookalike_redis(docker_client, unlabelled_redis_build, instance=None)
    foreign_pg = _make_lookalike_postgres(foreign_pg_build, comment=f"forge.instance={OTHER_INSTANCE}")
    unlabelled_pg = _make_lookalike_postgres(unlabelled_pg_build, comment=None)
    try:
        reclaimed = sweep_orphaned_preview_stores(db_session, docker_client=docker_client)

        assert reclaimed == 2  # the control's database+role (one identifier) and its Redis
        assert not _pg_object_exists(own_pg)
        with pytest.raises(NotFound):
            docker_client.containers.get(own_redis)
        # Every look-alike is still there, and still running (not merely "not NotFound").
        assert docker_client.containers.get(foreign_redis).status == "running"
        assert docker_client.containers.get(unlabelled_redis).status == "running"
        assert _pg_object_exists(foreign_pg)
        assert _pg_object_exists(unlabelled_pg)
    finally:
        preview_data.drop_preview_data_services(own_build, docker_client=docker_client)
        _remove_container_quietly(docker_client, foreign_redis)
        _remove_container_quietly(docker_client, unlabelled_redis)
        _drop_lookalike_postgres(foreign_pg)
        _drop_lookalike_postgres(unlabelled_pg)


def test_provisioning_marks_a_previews_database_and_role_with_this_instance(
    real_service_postgres_admin_dsn, docker_client
):
    """The listing can only select what was marked: read the marker back off both objects, as a
    literal (the format is what a second installation's sweep compares against)."""
    import uuid

    build_id = str(uuid.uuid4())
    preview_data.provision_preview_data_services(build_id, docker_client=docker_client)
    name = preview_data._preview_identifier(build_id)
    try:
        conn = psycopg.connect(settings.service_postgres_admin_dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
                    (name,),
                )
                database_comment = cur.fetchone()[0]
                cur.execute(
                    "SELECT shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname = %s",
                    (name,),
                )
                role_comment = cur.fetchone()[0]
        finally:
            conn.close()
        assert database_comment == f"forge.instance={TEST_INSTANCE_ID}"
        assert role_comment == f"forge.instance={TEST_INSTANCE_ID}"
    finally:
        preview_data.drop_preview_data_services(build_id, docker_client=docker_client)


def test_the_drop_seams_re_check_ownership_without_the_listing(
    real_service_postgres_admin_dsn, docker_client
):
    """The second check, on its own: the two public drop functions are handed the NAME of an
    object that is not this installation's (what a stale listing, or a caller that skipped it,
    would do) and leave it alone. A look-alike Redis under another instance carries the very
    name the sweep would compute for its build id; a `pv_` database of another instance is
    named directly."""
    import uuid

    redis_build = str(uuid.uuid4())
    pg_build = str(uuid.uuid4())
    foreign_redis = _make_lookalike_redis(docker_client, redis_build, instance=OTHER_INSTANCE)
    foreign_pg = _make_lookalike_postgres(pg_build, comment=f"forge.instance={OTHER_INSTANCE}")
    try:
        preview_data.drop_preview_redis_by_build_id(redis_build, docker_client=docker_client)
        preview_data.drop_preview_postgres_by_identifier(foreign_pg)

        assert docker_client.containers.get(foreign_redis).status == "running"
        assert _pg_object_exists(foreign_pg)
    finally:
        _remove_container_quietly(docker_client, foreign_redis)
        _drop_lookalike_postgres(foreign_pg)
