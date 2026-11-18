"""a requester can find their service. `GET /projects/{id}` carries `service_url`, the URL a
person opens, computed by `services/deploy.service_url` from settings and the project id.

The expected values are literals: a rename of the helper or a change of the
URL shape must go red here, not move both sides together. The helper, `FORGE_SERVICE_BASE_URL` and
the Traefik Host label are one spelling (`deploy_manager.service_host` / `service_base_url`);
`test_the_three_spellings_agree` pins that for both settings.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.models.user import User
from app.services.deploy import ensure_service_env, service_url
from app.services.deploy_manager import traefik_service_labels
from app.services.service_env_crypto import encrypt_service_env
from app.services.status import BuildStatus, ProjectStatus
from tests.fake_admin_dsn import FAKE_ADMIN_DSN

APPS_DOMAIN = "apps.example.test"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "apps_base_domain", APPS_DOMAIN)
    monkeypatch.setattr(settings, "service_url_scheme", "http")
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    monkeypatch.setattr(
        "app.services.deploy.service_data.provision_project_data_services",
        lambda project_id, *, docker_client=None: {
            "DATABASE_URL": f"postgresql://svc_{project_id}:pw@db:5432/svc_{project_id}",
            "REDIS_URL": f"redis://redis-{project_id}:6379/0",
        },
    )


def _project(db, seeded_ids, *, status=ProjectStatus.ACTIVE, name="svc-url-project"):
    project = Project(
        name=name,
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=status,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _deployment(db, project, status):
    build = Build(project_id=project.id, type="create", status=BuildStatus.SUCCEEDED)
    db.add(build)
    db.flush()
    deployment = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=project.owner_id,
        status=status,
    )
    db.add(deployment)
    db.commit()
    return deployment


def _read(client, project, user_id):
    resp = client.get(f"/api/projects/{project.id}", headers={"X-Forge-User-Id": user_id})
    assert resp.status_code == 200
    return resp


# -- ACTIVE deployment, both Traefik modes ----------------------------------


def test_traefik_on_gives_the_absolute_service_host(client, db_session, seeded_ids, monkeypatch):
    monkeypatch.setattr(settings, "traefik_enabled", True)
    project = _project(db_session, seeded_ids)
    _deployment(db_session, project, DeploymentStatus.ACTIVE)

    body = _read(client, project, seeded_ids["user_id"]).json()

    assert body["service_url"] == f"http://{project.id}.apps.example.test"


def test_traefik_on_follows_the_configured_scheme(client, db_session, seeded_ids, monkeypatch):
    monkeypatch.setattr(settings, "traefik_enabled", True)
    monkeypatch.setattr(settings, "service_url_scheme", "https")
    project = _project(db_session, seeded_ids)
    _deployment(db_session, project, DeploymentStatus.ACTIVE)

    body = _read(client, project, seeded_ids["user_id"]).json()

    assert body["service_url"] == f"https://{project.id}.apps.example.test"


def test_traefik_off_gives_the_relative_proxy_route_with_a_trailing_slash(
    client, db_session, seeded_ids, monkeypatch
):
    monkeypatch.setattr(settings, "traefik_enabled", False)
    project = _project(db_session, seeded_ids)
    _deployment(db_session, project, DeploymentStatus.ACTIVE)

    body = _read(client, project, seeded_ids["user_id"]).json()

    assert body["service_url"] == f"/api/projects/{project.id}/service/"


# -- no ACTIVE deployment -> null --------------------------------------------


@pytest.mark.parametrize("traefik", [True, False])
def test_no_deployment_gives_null(client, db_session, seeded_ids, monkeypatch, traefik):
    monkeypatch.setattr(settings, "traefik_enabled", traefik)
    project = _project(db_session, seeded_ids)

    body = _read(client, project, seeded_ids["user_id"]).json()

    assert "service_url" in body
    assert body["service_url"] is None


@pytest.mark.parametrize(
    "status",
    [
        DeploymentStatus.DEPLOYING,
        DeploymentStatus.SUPERSEDED,
        DeploymentStatus.FAILED,
        DeploymentStatus.STOPPED,
    ],
)
@pytest.mark.parametrize("traefik", [True, False])
def test_only_a_non_active_deployment_gives_null(
    client, db_session, seeded_ids, monkeypatch, status, traefik
):
    monkeypatch.setattr(settings, "traefik_enabled", traefik)
    project = _project(db_session, seeded_ids)
    _deployment(db_session, project, status)

    body = _read(client, project, seeded_ids["user_id"]).json()

    assert body["service_url"] is None


def test_an_archived_project_gives_null(client, db_session, seeded_ids, monkeypatch):
    """What an archive leaves behind: the service stopped (`STOPPED`), the project `ARCHIVED`."""
    monkeypatch.setattr(settings, "traefik_enabled", True)
    project = _project(db_session, seeded_ids, status=ProjectStatus.ARCHIVED)
    _deployment(db_session, project, DeploymentStatus.STOPPED)

    body = _read(client, project, seeded_ids["user_id"]).json()

    assert body["status"] == "ARCHIVED"
    assert body["service_url"] is None


def test_a_superseded_deployment_does_not_hide_the_active_one(
    client, db_session, seeded_ids, monkeypatch
):
    monkeypatch.setattr(settings, "traefik_enabled", False)
    project = _project(db_session, seeded_ids)
    _deployment(db_session, project, DeploymentStatus.SUPERSEDED)
    _deployment(db_session, project, DeploymentStatus.ACTIVE)

    body = _read(client, project, seeded_ids["user_id"]).json()

    assert body["service_url"] == f"/api/projects/{project.id}/service/"


# -- who may read it ----------------------------------------------------------


def test_a_requester_who_does_not_own_the_project_can_read_it(
    client, db_session, seeded_ids, monkeypatch
):
    """Project reads are any authenticated user and the service route carries no identity, so the
    URL is not a secret."""
    monkeypatch.setattr(settings, "traefik_enabled", False)
    project = _project(db_session, seeded_ids)
    _deployment(db_session, project, DeploymentStatus.ACTIVE)
    stranger = User(
        team_id=seeded_ids["team_id"],
        name="Stranger",
        email="stranger.requester@example.com",
        role="requester",
    )
    db_session.add(stranger)
    db_session.commit()
    assert stranger.id != project.owner_id

    body = _read(client, project, stranger.id).json()

    assert body["service_url"] == f"/api/projects/{project.id}/service/"


def test_the_read_requires_identity(client, db_session, seeded_ids):
    project = _project(db_session, seeded_ids)
    _deployment(db_session, project, DeploymentStatus.ACTIVE)

    assert client.get(f"/api/projects/{project.id}").status_code == 401


# -- never from the service env ------------------------------------------------


def test_the_response_carries_nothing_from_the_service_env(
    client, db_session, seeded_ids, monkeypatch
):
    monkeypatch.setattr(settings, "traefik_enabled", True)
    project = _project(db_session, seeded_ids)
    _deployment(db_session, project, DeploymentStatus.ACTIVE)
    env = {
        "FORGE_SERVICE_BASE_URL": "http://elsewhere.invalid",
        "FORGE_APP_SECRET_KEY": "s3cr3t-value-that-must-not-leak",
        "DATABASE_URL": "postgresql://svc:db-password-must-not-leak@db/x",
        "REDIS_URL": "redis://:redis-password-must-not-leak@r:6379/0",
    }
    project.service_env_json = encrypt_service_env(env)
    db_session.commit()

    resp = _read(client, project, seeded_ids["user_id"])
    body = resp.json()

    # The URL comes from settings and the id, not from the stored FORGE_SERVICE_BASE_URL.
    assert body["service_url"] == f"http://{project.id}.apps.example.test"
    for key in env:
        assert key not in body
        assert key not in resp.text
    for value in env.values():
        assert value not in resp.text
    assert "service_env_json" not in body


def test_the_create_response_has_a_null_service_url(client, seeded_ids):
    resp = client.post(
        "/api/projects",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={
            "name": "fresh",
            "template_id": seeded_ids["template_id"],
            "team_id": seeded_ids["team_id"],
            "nl_request": "a thing",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["service_url"] is None


# -- one spelling ----------------------------------------------------------------


@pytest.mark.parametrize("traefik", [True, False])
def test_the_three_spellings_agree(db_session, seeded_ids, monkeypatch, traefik):
    """`FORGE_SERVICE_BASE_URL` (what the service is told), the Traefik Host label (what routes it)
    and `service_url` (what a person opens) name the same place, in both modes."""
    monkeypatch.setattr(settings, "traefik_enabled", traefik)
    project = _project(db_session, seeded_ids)
    _deployment(db_session, project, DeploymentStatus.ACTIVE)

    env_base = ensure_service_env(db_session, project)["FORGE_SERVICE_BASE_URL"]
    opened = service_url(db_session, project)
    labels = traefik_service_labels(
        project_id=project.id, deployment_id="dep1", network_name="n", priority=1
    )
    (rule,) = [v for k, v in labels.items() if k.endswith(".rule")]

    assert rule == f"Host(`{project.id}.apps.example.test`)"
    if traefik:
        assert env_base == f"http://{project.id}.apps.example.test"
        assert opened == env_base
        assert rule == f"Host(`{env_base.removeprefix('http://')}`)"
    else:
        assert env_base == f"/api/projects/{project.id}/service"
        assert opened == f"{env_base}/"
