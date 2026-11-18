"""Integration tests for the operator-initiated deprovisioning
(`app/services/service_data_deprovision.py`) against a REAL
Postgres and a REAL Docker Engine -- no fakes for the SQL or the container
mechanics.

Uses the shared `real_service_postgres_admin_dsn` fixture (conftest.py): a
throwaway `postgres:18-alpine` container, one per module, published on
loopback only, exposing the non-superuser `forge_admin` credential (`CREATEDB
CREATEROLE NOSUPERUSER`) production actually holds -- so every drop here
proves (b1) it runs under the SAME privilege level, not a superuser's.
Skips cleanly (not fails) when Docker is unavailable.

**(T1), this module's central claim**: a SERVICE database is provisioned
by the REAL `service_data.provision_project_data_services` (b2), never
hand-rolled here, so it carries the SAME `GRANT CONNECT ON DATABASE ... TO
"<admin>"` and `GRANT "<role>" TO "<admin>" WITH SET TRUE` grants
production's does -- exactly what makes the role "hard to drop" a
plausible risk. `test_deprovision_survives_an_open_session_and_a_second_
issued_role` additionally holds an OPEN connection under the service role
and mints a SECOND credential via `issue_role` before deprovisioning, so
the proof is not merely against a freshly-provisioned, untouched database.

Guard/naming/backup-deletion unit tests that need neither Docker nor
Postgres live in `test_service_data_deprovision.py`.
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("docker")
pytest.importorskip("psycopg")

import docker  # noqa: E402
import psycopg  # noqa: E402
from docker.errors import DockerException, NotFound  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import service_data_deprovision  # noqa: E402
from app.services.service_data import (  # noqa: E402
    _project_identifier,
    issue_role,
    provision_project_data_services,
    redis_container_name,
)
from app.services.service_data_backup import project_backup_dir  # noqa: E402
from app.services.service_data_deprovision import deprovision_service_data  # noqa: E402
from tests.docker_volumes import anonymous_volume_names, volume_exists  # noqa: E402


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


@pytest.fixture()
def project_id():
    return str(uuid.uuid4())


def _connect(database_url: str) -> psycopg.Connection:
    # Same host/port swap as test_service_data_integration.py's own
    # `_connect`: DATABASE_URL is built for a container reaching the
    # shared instance by NAME, unreachable from this host process.
    from sqlalchemy.engine import make_url

    from app.config import settings

    admin_url = make_url(settings.service_postgres_admin_dsn)
    issued = make_url(database_url)
    reachable = issued.set(drivername="postgresql", host=admin_url.host, port=admin_url.port)
    return psycopg.connect(reachable.render_as_string(hide_password=False))


def _admin_connect() -> psycopg.Connection:
    from app.config import settings

    return psycopg.connect(settings.service_postgres_admin_dsn, autocommit=True)


def test_deprovision_drops_a_real_provisioned_database_and_role(
    real_service_postgres_admin_dsn, project_id, docker_client
):
    """(b1-b3): provisioned by the REAL `provision_project_data_services`
    (so it carries production's exact grants), dropped by
    `deprovision_service_data`, and proven ABSENT from `pg_database`/
    `pg_roles` afterward -- absence, not "the call returned"."""
    provision_project_data_services(project_id, docker_client=docker_client)
    name = _project_identifier(project_id)

    result = deprovision_service_data(project_id, docker_client=docker_client)
    assert result == {"backups_deleted": False}

    admin_conn = _admin_connect()
    try:
        with admin_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            assert cur.fetchone() is None, "database must be gone"
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
            assert cur.fetchone() is None, "role must be gone"
    finally:
        admin_conn.close()


def test_deprovision_removes_the_redis_container(
    real_service_postgres_admin_dsn, project_id, docker_client
):
    """(a1): the Redis container is destroyed too, not just Postgres."""
    provision_project_data_services(project_id, docker_client=docker_client)
    name = redis_container_name(project_id)
    docker_client.containers.get(name)  # exists before

    deprovision_service_data(project_id, docker_client=docker_client)

    with pytest.raises(NotFound):
        docker_client.containers.get(name)


def test_deprovision_removes_the_redis_containers_anonymous_volume(
    real_service_postgres_admin_dsn, project_id, docker_client, monkeypatch
):
    """the Redis image declares `VOLUME /data`, so every container gets
    an anonymous volume that only `remove(v=True)` takes with it. The volume
    names are captured BEFORE teardown and must no longer exist after it --
    absence of the volume, not of the container.

    The default `redis:8-alpine` declares no `VOLUME` (measured 2026-10-07),
    so it has no anonymous volume to leak; `redis:7-alpine` does (`VOLUME
    /data`), and `SERVICE_REDIS_IMAGE` is operator-configurable, so the test
    pins the image that makes the leak possible."""
    monkeypatch.setattr(settings, "service_redis_image", "redis:7-alpine")
    provision_project_data_services(project_id, docker_client=docker_client)
    container = docker_client.containers.get(redis_container_name(project_id))
    volumes = anonymous_volume_names(container)
    assert volumes, "the Redis container must hold an anonymous volume, or this test proves nothing"
    assert all(volume_exists(docker_client, v) for v in volumes)

    try:
        deprovision_service_data(project_id, docker_client=docker_client)

        leaked = [v for v in volumes if volume_exists(docker_client, v)]
        assert leaked == [], f"anonymous volumes outlived the container: {leaked}"
    finally:
        for v in volumes:  # a regression must not also leak a volume per run
            try:
                docker_client.volumes.get(v).remove(force=True)
            except DockerException:
                pass


def test_deprovision_survives_an_open_session_and_a_second_issued_role(
    real_service_postgres_admin_dsn, project_id, docker_client
):
    """(T1's actual proof): holds an OPEN connection AS the service role
    (simulating a live deployed service), has that connection create a
    table (simulating `alembic upgrade head`), and mints a SECOND
    credential via `issue_role` (an out-of-scope case)
    BEFORE deprovisioning -- so the drop is proven against a database that
    is not merely freshly provisioned and untouched. `DROP DATABASE ...
    WITH (FORCE)` (a4) disconnects the open session; the primary role
    drops with the SAME two statements `preview_data.py` uses, despite the
    extra `GRANT CONNECT`/`GRANT ... WITH SET TRUE` a service role carries
    that a preview role does not.

    The second, `issue_role`-minted credential is asserted to SURVIVE --
    this is a known limitation, not a bug this
    module is expected to close."""
    database_url = provision_project_data_services(project_id, docker_client=docker_client)[
        "DATABASE_URL"
    ]
    name = _project_identifier(project_id)

    svc_conn = _connect(database_url)
    with svc_conn.cursor() as cur:
        cur.execute("CREATE TABLE proof (id integer)")
        cur.execute("INSERT INTO proof (id) VALUES (1)")
    svc_conn.commit()
    # Deliberately left open -- the drop must survive an active session,
    # not merely a closed one.

    second_role = f"svc_second_{uuid.uuid4().hex[:8]}"
    issue_role(project_id, role_name=second_role)

    try:
        result = deprovision_service_data(project_id, docker_client=docker_client)
        assert result == {"backups_deleted": False}

        admin_conn = _admin_connect()
        try:
            with admin_conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                assert cur.fetchone() is None, "database must be gone despite the open session"
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
                assert cur.fetchone() is None, "primary role must be gone"
                # The known limitation, asserted rather than assumed: the
                # second, manually-issued role is NOT touched by this
                # module.
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (second_role,))
                assert cur.fetchone() is not None, "issue_role-minted roles are out of scope"
        finally:
            admin_conn.close()
    finally:
        try:
            svc_conn.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup of the still-open probe connection
            pass
        # Not this module's job to drop -- clean up the orphan directly so
        # the throwaway container's own teardown has one less loose end.
        admin_conn = _admin_connect()
        try:
            with admin_conn.cursor() as cur:
                cur.execute(f'DROP ROLE IF EXISTS "{second_role}"')
        finally:
            admin_conn.close()


def test_deprovision_is_idempotent_and_tolerates_a_never_provisioned_project(
    real_service_postgres_admin_dsn, project_id, docker_client
):
    """(a3): destroying an already-destroyed project raises nothing, and
    neither does a project that never provisioned anything at all."""
    provision_project_data_services(project_id, docker_client=docker_client)

    deprovision_service_data(project_id, docker_client=docker_client)
    deprovision_service_data(project_id, docker_client=docker_client)  # second call: nothing left

    deprovision_service_data(str(uuid.uuid4()), docker_client=docker_client)  # never provisioned


def test_deprovision_keeps_backups_by_default(
    real_service_postgres_admin_dsn, project_id, docker_client, monkeypatch, tmp_path
):
    """(a1/d4's mechanism): the default call never touches the backup
    directory, even though `delete_service_backups` could reach it -- the
    database/role/redis are still destroyed."""
    from app.config import settings

    monkeypatch.setattr(settings, "service_data_backup_dir", str(tmp_path))
    provision_project_data_services(project_id, docker_client=docker_client)
    name = _project_identifier(project_id)
    backup_dir = project_backup_dir(str(tmp_path), name)
    import os

    os.makedirs(backup_dir)
    with open(os.path.join(backup_dir, "20260101T000000Z.sql"), "w", encoding="utf-8") as f:
        f.write("-- dump")

    result = deprovision_service_data(project_id, docker_client=docker_client)

    assert result == {"backups_deleted": False}
    assert os.path.isdir(backup_dir)

    admin_conn = _admin_connect()
    try:
        with admin_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            assert cur.fetchone() is None
    finally:
        admin_conn.close()


def test_deprovision_deletes_backups_when_asked(
    real_service_postgres_admin_dsn, project_id, docker_client, monkeypatch, tmp_path
):
    from app.config import settings

    monkeypatch.setattr(settings, "service_data_backup_dir", str(tmp_path))
    provision_project_data_services(project_id, docker_client=docker_client)
    name = _project_identifier(project_id)
    backup_dir = project_backup_dir(str(tmp_path), name)
    import os

    os.makedirs(backup_dir)
    with open(os.path.join(backup_dir, "20260101T000000Z.sql"), "w", encoding="utf-8") as f:
        f.write("-- dump")

    result = deprovision_service_data(project_id, docker_client=docker_client, delete_backups=True)

    assert result == {"backups_deleted": True}
    assert not os.path.exists(backup_dir)


def test_a_refused_deprovision_leaves_the_real_database_in_place_and_the_same_call_drops_it_once_allowed(
    real_service_postgres_admin_dsn, docker_client, client, db_session, seeded_ids
):
    """Against a real Postgres and a
    real Docker: through the API, an ARCHIVED project with an ACTIVE (or
    DEPLOYING) deployment is refused with 409 and its database, role and Redis
    are all still there; once the deployment is STOPPED the SAME request drops
    them -- so the first half cannot pass merely because nothing could ever be
    dropped here."""
    from app.models.build import Build
    from app.models.deployment import Deployment, DeploymentStatus
    from app.models.project import Project
    from app.models.user import User
    from app.services.status import BuildStatus, ProjectStatus

    admin = db_session.query(User).filter_by(role="admin").first()
    project = Project(
        name="refused-deprovision",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.ARCHIVED,
    )
    db_session.add(project)
    db_session.flush()
    build = Build(project_id=project.id, type="create", status=BuildStatus.SUCCEEDED)
    db_session.add(build)
    db_session.flush()
    live = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=seeded_ids["user_id"],
        status=DeploymentStatus.ACTIVE,
    )
    db_session.add(live)
    db_session.commit()

    provision_project_data_services(project.id, docker_client=docker_client)
    name = _project_identifier(project.id)
    redis_name = redis_container_name(project.id)

    def database_and_role_exist() -> tuple[bool, bool]:
        conn = _admin_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                has_database = cur.fetchone() is not None
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
                return has_database, cur.fetchone() is not None
        finally:
            conn.close()

    def deprovision():
        return client.post(
            f"/api/projects/{project.id}/deprovision",
            headers={"X-Forge-User-Id": admin.id},
            json={"project_id": project.id},
        )

    try:
        assert database_and_role_exist() == (True, True)

        for live_status in (DeploymentStatus.ACTIVE, DeploymentStatus.DEPLOYING):
            live.status = live_status
            db_session.commit()
            resp = deprovision()
            assert resp.status_code == 409, resp.text
            assert resp.json()["detail"].startswith("DEPLOYMENT_LIVE: ")
            assert live.id in resp.json()["detail"]
            assert database_and_role_exist() == (True, True)  # nothing was dropped
            docker_client.containers.get(redis_name)  # the Redis container is still there

        live.status = DeploymentStatus.STOPPED
        db_session.commit()
        resp = deprovision()
        assert resp.status_code == 200, resp.text
        assert database_and_role_exist() == (False, False)  # now it really goes
        with pytest.raises(NotFound):
            docker_client.containers.get(redis_name)
    finally:
        deprovision_service_data(project.id, docker_client=docker_client)  # idempotent cleanup
