"""The project read's `deploy_controls`
and the confirm / rollback / stop actions are one rule, not two.

The 2026-10-16 review found the frontend deciding by itself which build to offer for confirm
(the newest build of any status, then requiring SUCCEEDED) and when to offer a rollback (any
ACTIVE deployment), and getting both wrong against the backend. So the backend now says what
is actionable, computed by the functions the actions run (`confirm_refusal`, `decide_rollback`).

Every test here is built from one table of states. For each state:

- `test_controls_match_the_table` pins the field values as LITERALS, so a wrong rule that the
  actions happen to share is still caught;
- `test_actions_agree_with_controls` runs the real `confirm_deploy` / `request_rollback` /
  `stop_active_deployment` against the same state and asserts they accept exactly what the
  controls offer and refuse with exactly the code the controls report (no literal: this is the
  agreement the task asks for);
- the two API tests do the same through the routes (`GET /projects/{id}`, the POST confirm and
  rollback), where the refusal travels as the 4xx `detail` prefix.

No Docker: the image check is a plain callable over a set of "images that exist", and the API
tests monkeypatch the routers' two manager seams (`_deploy_manager` for the actions,
`_image_check_manager` for the project read). The one exception is the hung-daemon test, which
points the real client at a local socket that accepts and never answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.approval import ApprovalRequest, ApprovalStatus
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.services.deploy import (
    DeployConflictError,
    DeployControls,
    DeployRefusalCode,
    DeployValidationError,
    confirm_deploy,
    deploy_controls,
    request_rollback,
    stop_active_deployment,
)
from app.services.status import BuildStatus, ProjectKind, ProjectStatus
from tests.fake_admin_dsn import FAKE_ADMIN_DSN

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def at(minute: int) -> datetime:
    return T0 + timedelta(minutes=minute)


@pytest.fixture(autouse=True)
def _configured_postgres(monkeypatch):
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)


@pytest.fixture()
def owner_id(seeded_ids):
    return seeded_ids["user_id"]


@pytest.fixture()
def project(db_session, seeded_ids, owner_id):
    project = Project(
        name="deploy-controls-project",
        owner_id=owner_id,
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.commit()
    return project


class World:
    """One project, its rows, and which images exist. Ids are derived from the keys a state
    uses (`bld-<key>`, `dep-<key>`), so an `Expect` reads as the table it is."""

    def __init__(self, db, project, monkeypatch):
        self.db = db
        self.project = project
        self.monkeypatch = monkeypatch
        self.images: set[str] = set()

    def image_exists(self, image_ref):
        return bool(image_ref) and image_ref in self.images

    def build(self, key, minute, *, status=BuildStatus.SUCCEEDED, eligible=True, image=True):
        build = Build(
            id=f"bld-{key}",
            project_id=self.project.id,
            type="create",
            status=status,
            image_ref=f"img-{key}" if image else None,
            diff_ref=f"forge-build-{key}#1",
            created_at=at(minute),
        )
        if image:
            self.images.add(f"img-{key}")
        self.db.add(build)
        self.db.flush()
        if eligible:
            self.db.add(
                ApprovalRequest(
                    build_id=build.id,
                    project_id=self.project.id,
                    requester_id=self.project.owner_id,
                    risk_tier="low",
                    status=ApprovalStatus.AUTO_APPROVED,
                    required_approvals=1,
                )
            )
        self.db.commit()
        return build.id

    def deployment(self, key, build_key, status, minute, *, ended_minute=None):
        deployment = Deployment(
            id=f"dep-{key}",
            project_id=self.project.id,
            build_id=f"bld-{build_key}",
            confirmed_by_id=self.project.owner_id,
            status=status,
            created_at=at(minute),
            ended_at=at(ended_minute) if ended_minute is not None else None,
        )
        self.db.add(deployment)
        self.db.commit()
        return deployment.id

    def set_project(self, **fields):
        for name, value in fields.items():
            setattr(self.project, name, value)
        self.db.commit()


@dataclass(frozen=True)
class Expect:
    """The literal answer for a state: ids are the `bld-`/`dep-` ids, reasons are the
    `DeployRefusalCode` values spelled out (they cross a boundary: the frontend pins them too)."""

    confirmable: str | None
    confirm_reason: str | None
    rollback_target: str | None
    rollback_reason: str | None
    active: str | None
    in_flight: bool
    # The build a refused confirm is asked about in the API test (None: nothing to ask about).
    probe_build: str | None = None


STATES: dict = {}


def state(name):
    def register(fn):
        STATES[name] = fn
        return fn

    return register


# -- confirm: which build, and why not -----------------------------------------------------------


@state("fresh_project_no_builds")
def _(w):
    return Expect(None, "NO_SUCCESSFUL_BUILD", None, "NO_ROLLBACK_TARGET", None, False)


@state("only_a_failed_build")
def _(w):
    w.build("f", 1, status=BuildStatus.FAILED, eligible=False)
    return Expect(None, "NO_SUCCESSFUL_BUILD", None, "NO_ROLLBACK_TARGET", None, False)


@state("one_eligible_successful_build")
def _(w):
    b = w.build("a", 1)
    return Expect(b, None, None, "NO_ROLLBACK_TARGET", None, False)


@state("newer_build_queued_hides_nothing")
def _(w):
    # The page used to pick the newest build of ANY status and require SUCCEEDED, so a newer
    # queued build hid the confirm for a build the backend accepts.
    older = w.build("a", 1)
    w.build("q", 2, status=BuildStatus.QUEUED, eligible=False)
    return Expect(older, None, None, "NO_ROLLBACK_TARGET", None, True)


@state("newer_build_running_hides_nothing")
def _(w):
    older = w.build("a", 1)
    w.build("r", 2, status=BuildStatus.RUNNING, eligible=False)
    return Expect(older, None, None, "NO_ROLLBACK_TARGET", None, True)


@state("newer_build_failed_hides_nothing")
def _(w):
    older = w.build("a", 1)
    w.build("f", 2, status=BuildStatus.FAILED, eligible=False)
    return Expect(older, None, None, "NO_ROLLBACK_TARGET", None, False)


@state("newer_successful_build_not_yet_approved")
def _(w):
    # The older eligible build is no longer the latest SUCCESSFUL one: a confirm of it is
    # refused, and the new one is not eligible yet.
    w.build("a", 1)
    w.build("b", 2, eligible=False)
    return Expect(None, "NOT_DEPLOY_ELIGIBLE", None, "NO_ROLLBACK_TARGET", None, False, "bld-b")


@state("only_successful_build_not_approved")
def _(w):
    w.build("a", 1, eligible=False)
    return Expect(None, "NOT_DEPLOY_ELIGIBLE", None, "NO_ROLLBACK_TARGET", None, False, "bld-a")


@state("image_missing")
def _(w):
    w.build("a", 1)
    w.images.clear()
    return Expect(None, "IMAGE_MISSING", None, "NO_ROLLBACK_TARGET", None, False, "bld-a")


@state("no_image_ref_at_all")
def _(w):
    w.build("a", 1, image=False)
    return Expect(None, "IMAGE_MISSING", None, "NO_ROLLBACK_TARGET", None, False, "bld-a")


@state("archived_wins_over_a_missing_image")
def _(w):
    w.build("a", 1)
    w.images.clear()
    w.set_project(status=ProjectStatus.ARCHIVED)
    return Expect(None, "PROJECT_ARCHIVED", None, "PROJECT_ARCHIVED", None, False, "bld-a")


@state("deploying_in_progress")
def _(w):
    w.build("a", 1)
    w.deployment("d1", "a", DeploymentStatus.DEPLOYING, 2)
    return Expect(None, "DEPLOYMENT_IN_FLIGHT", None, "DEPLOYMENT_IN_FLIGHT", None, True, "bld-a")


@state("eval_project_wins_over_archived")
def _(w):
    w.build("a", 1)
    w.set_project(kind=ProjectKind.EVAL, status=ProjectStatus.ARCHIVED)
    return Expect(None, "EVAL_PROJECT", None, "PROJECT_ARCHIVED", None, False, "bld-a")


@state("postgres_not_configured")
def _(w):
    w.build("a", 1)
    w.monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)
    return Expect(None, "POSTGRES_UNCONFIGURED", None, "NO_ROLLBACK_TARGET", None, False, "bld-a")


# -- rollback and stop: the target, and why none --------------------------------------------------


@state("first_deploy_active_nothing_to_roll_back_to")
def _(w):
    b = w.build("a", 1)
    w.deployment("d1", "a", DeploymentStatus.ACTIVE, 2)
    # Offering 롤백 here (the old rule: "any ACTIVE deployment") always failed.
    return Expect(b, None, None, "NO_ROLLBACK_TARGET", "dep-d1", False)


@state("second_deploy_has_a_rollback_target")
def _(w):
    w.build("a", 1)
    b = w.build("b", 3)
    w.deployment("d1", "a", DeploymentStatus.SUPERSEDED, 2, ended_minute=4)
    w.deployment("d2", "b", DeploymentStatus.ACTIVE, 4)
    return Expect(b, None, "dep-d1", None, "dep-d2", False)


@state("reconfirming_the_active_build_is_not_a_target")
def _(w):
    # d2 was superseded by a re-confirm of the SAME build: it is the newest SUPERSEDED row, but
    # its build is the ACTIVE one, so the target is the older d1.
    w.build("a", 1)
    b = w.build("b", 3)
    w.deployment("d1", "a", DeploymentStatus.SUPERSEDED, 2, ended_minute=4)
    w.deployment("d2", "b", DeploymentStatus.SUPERSEDED, 4, ended_minute=6)
    w.deployment("d3", "b", DeploymentStatus.ACTIVE, 6)
    return Expect(b, None, "dep-d1", None, "dep-d3", False)


@state("only_superseded_row_is_the_active_build")
def _(w):
    b = w.build("a", 1)
    w.deployment("d1", "a", DeploymentStatus.SUPERSEDED, 2, ended_minute=3)
    w.deployment("d2", "a", DeploymentStatus.ACTIVE, 3)
    return Expect(b, None, None, "NO_ROLLBACK_TARGET", "dep-d2", False)


@state("rollback_target_image_gone")
def _(w):
    w.build("a", 1)
    b = w.build("b", 3)
    w.deployment("d1", "a", DeploymentStatus.SUPERSEDED, 2, ended_minute=4)
    w.deployment("d2", "b", DeploymentStatus.ACTIVE, 4)
    w.images.discard("img-a")
    return Expect(b, None, None, "NO_ROLLBACK_TARGET", "dep-d2", False)


@state("rollback_older_target_when_the_newest_image_is_gone")
def _(w):
    w.build("a", 1)
    w.build("b", 2)
    c = w.build("c", 5)
    w.deployment("d1", "a", DeploymentStatus.SUPERSEDED, 2, ended_minute=3)
    w.deployment("d2", "b", DeploymentStatus.SUPERSEDED, 3, ended_minute=4)
    w.deployment("d3", "c", DeploymentStatus.ACTIVE, 4)
    w.images.discard("img-b")
    return Expect(c, None, "dep-d1", None, "dep-d3", False)


@state("rollback_possible_with_nothing_active_after_a_stop")
def _(w):
    # The backend takes a rollback with no ACTIVE deployment at all (the service was stopped):
    # the old rule ("only while something is ACTIVE") never offered it.
    w.build("a", 1)
    b = w.build("b", 3)
    w.deployment("d1", "a", DeploymentStatus.SUPERSEDED, 2, ended_minute=4)
    w.deployment("d2", "b", DeploymentStatus.STOPPED, 4, ended_minute=5)
    return Expect(b, None, "dep-d1", None, None, False)


@state("archived_with_a_rollback_target")
def _(w):
    w.build("a", 1)
    w.build("b", 3)
    w.deployment("d1", "a", DeploymentStatus.SUPERSEDED, 2, ended_minute=4)
    w.deployment("d2", "b", DeploymentStatus.ACTIVE, 4)
    w.set_project(status=ProjectStatus.ARCHIVED)
    return Expect(None, "PROJECT_ARCHIVED", None, "PROJECT_ARCHIVED", "dep-d2", False, "bld-b")


@state("deploying_while_a_rollback_target_exists")
def _(w):
    w.build("a", 1)
    w.build("b", 3)
    w.deployment("d1", "a", DeploymentStatus.SUPERSEDED, 2, ended_minute=4)
    w.deployment("d2", "b", DeploymentStatus.ACTIVE, 4)
    w.deployment("d3", "b", DeploymentStatus.DEPLOYING, 5)
    return Expect(None, "DEPLOYMENT_IN_FLIGHT", None, "DEPLOYMENT_IN_FLIGHT", "dep-d2", True, "bld-b")


def _controls_of(world) -> DeployControls:
    return deploy_controls(world.db, world.project, image_exists=world.image_exists)


@pytest.mark.parametrize("name", list(STATES))
def test_controls_match_the_table(name, db_session, project, monkeypatch):
    world = World(db_session, project, monkeypatch)
    expect = STATES[name](world)

    assert _controls_of(world) == DeployControls(
        confirmable_build_id=expect.confirmable,
        confirm_blocked_reason=expect.confirm_reason,
        rollback_target_id=expect.rollback_target,
        rollback_blocked_reason=expect.rollback_reason,
        active_deployment_id=expect.active,
        in_flight=expect.in_flight,
    )


# -- the agreement: the same state, the field and the action ---------------------------------------


def _refusal_of(call):
    """(accepted result, None) or (None, the refusal the action raised)."""
    try:
        return call(), None
    except (DeployValidationError, DeployConflictError) as exc:
        return None, exc


class _StopManager:
    def __init__(self):
        self.removed = []

    def remove_service(self, deployment_id):
        self.removed.append(deployment_id)
        return True


@pytest.mark.parametrize("name", list(STATES))
def test_actions_agree_with_controls(name, db_session, project, owner_id, monkeypatch):
    world = World(db_session, project, monkeypatch)
    STATES[name](world)
    db = world.db
    controls = _controls_of(world)

    build_ids = [row[0] for row in db.query(Build.id).filter_by(project_id=project.id).all()]
    successful = [
        row[0]
        for row in db.query(Build.id)
        .filter_by(project_id=project.id, status=BuildStatus.SUCCEEDED)
        .order_by(Build.created_at.desc())
        .all()
    ]

    # confirm: the controls offer exactly the builds the action accepts
    for build_id in build_ids:
        accepted, refusal = _refusal_of(
            lambda: confirm_deploy(
                db,
                project=db.get(Project, project.id),
                build=db.get(Build, build_id),
                actor_id=owner_id,
                image_exists=world.image_exists,
            )
        )
        db.rollback()  # nothing from this probe is kept: the next build meets the same state
        assert (accepted is not None) == (build_id == controls.confirmable_build_id), (
            f"{name}: confirm of {build_id} was "
            f"{'accepted' if accepted is not None else 'refused (' + refusal.code + ')'} "
            f"but the controls offer {controls.confirmable_build_id}"
        )
        if refusal is not None:
            assert refusal.code is not None and str(refusal).startswith(f"{refusal.code}: ")
        # ... and the build the controls speak for is refused for the reason they give
        if refusal is not None and successful and build_id == successful[0]:
            assert refusal.code == controls.confirm_blocked_reason

    # rollback: offered iff accepted; the target and the reason are the action's own
    accepted, refusal = _refusal_of(
        lambda: request_rollback(
            db,
            project=db.get(Project, project.id),
            actor_id=owner_id,
            image_exists=world.image_exists,
        )
    )
    if accepted is not None:
        assert controls.rollback_target_id is not None
        assert accepted.rollback_of == controls.rollback_target_id
        assert accepted.status == DeploymentStatus.DEPLOYING
        assert controls.rollback_blocked_reason is None
    else:
        assert controls.rollback_target_id is None
        assert refusal.code == controls.rollback_blocked_reason
        assert str(refusal).startswith(f"{refusal.code}: ")
    db.rollback()

    # stop: offered iff there is something to stop, and it stops exactly that
    manager = _StopManager()
    accepted, refusal = _refusal_of(
        lambda: stop_active_deployment(
            db, project=db.get(Project, project.id), actor_id=owner_id, manager=manager
        )
    )
    if controls.active_deployment_id is None:
        assert accepted is None and refusal is not None
    else:
        assert accepted is not None and accepted.id == controls.active_deployment_id
        assert manager.removed == [controls.active_deployment_id]
    db.rollback()


# -- the read tolerates a Docker it cannot reach -----------------------------------------------------


def test_an_image_check_that_raises_is_reported_not_raised(db_session, project, monkeypatch):
    world = World(db_session, project, monkeypatch)
    world.build("a", 1)
    world.build("b", 3)
    world.deployment("d1", "a", DeploymentStatus.SUPERSEDED, 2, ended_minute=4)
    world.deployment("d2", "b", DeploymentStatus.ACTIVE, 4)

    def broken(image_ref):
        raise ConnectionError("docker daemon not reachable")

    controls = deploy_controls(db_session, project, image_exists=broken)

    assert controls.confirmable_build_id is None
    assert controls.confirm_blocked_reason == "DOCKER_UNAVAILABLE"
    assert controls.rollback_target_id is None
    assert controls.rollback_blocked_reason == "DOCKER_UNAVAILABLE"
    # what needs no image check is still answered
    assert controls.active_deployment_id == "dep-d2"


def test_an_image_check_that_is_never_reached_is_never_asked(db_session, project, monkeypatch):
    """A project with nothing to deploy must not report Docker as the problem: the image check
    is reached only when everything before it passed, exactly as in `confirm_deploy`."""
    world = World(db_session, project, monkeypatch)
    calls = []

    def must_not_be_called(image_ref):
        calls.append(image_ref)
        raise AssertionError("asked Docker for a project with nothing deployable")

    controls = deploy_controls(db_session, project, image_exists=must_not_be_called)

    assert calls == []
    assert controls.confirm_blocked_reason == "NO_SUCCESSFUL_BUILD"
    assert controls.rollback_blocked_reason == "NO_ROLLBACK_TARGET"


# -- through the routes ------------------------------------------------------------------------------


class _ApiManager:
    def __init__(self, images):
        self.images = images

    def image_exists(self, image_ref):
        return bool(image_ref) and image_ref in self.images

    def remove_service(self, deployment_id):
        return True


@pytest.fixture()
def api_world(db_session, project, monkeypatch):
    import app.routers.deployments as deployments_router

    world = World(db_session, project, monkeypatch)

    def _manager():
        return _ApiManager(world.images)

    # the actions' seam, and the project read's own (it has a different time budget)
    monkeypatch.setattr(deployments_router, "_deploy_manager", _manager)
    monkeypatch.setattr(deployments_router, "_image_check_manager", _manager)
    return world


def _owner(owner_id):
    return {"X-Forge-User-Id": owner_id}


# What status each code travels with (the routers' mapping: a conflict is 409, a rule is 400).
CODE_STATUS = {
    "PROJECT_ARCHIVED": 409,
    "DEPLOYMENT_IN_FLIGHT": 409,
    "EVAL_PROJECT": 409,
    "POSTGRES_UNCONFIGURED": 400,
    "NOT_DEPLOY_ELIGIBLE": 400,
    "NOT_LATEST_SUCCESSFUL": 400,
    "BUILD_NOT_SUCCEEDED": 400,
    "IMAGE_MISSING": 400,
    "NO_ROLLBACK_TARGET": 400,
}


@pytest.mark.parametrize("name", list(STATES))
def test_the_project_read_and_the_confirm_route_agree(name, client, api_world, owner_id):
    expect = STATES[name](api_world)
    project_id = api_world.project.id

    read = client.get(f"/api/projects/{project_id}", headers=_owner(owner_id))
    assert read.status_code == 200
    assert read.json()["deploy_controls"] == {
        "confirmable_build_id": expect.confirmable,
        "confirm_blocked_reason": expect.confirm_reason,
        "rollback_target_id": expect.rollback_target,
        "rollback_blocked_reason": expect.rollback_reason,
        "active_deployment_id": expect.active,
        "in_flight": expect.in_flight,
    }

    if expect.confirmable is not None:
        accepted = client.post(
            f"/api/projects/{project_id}/deployments",
            json={"build_id": expect.confirmable},
            headers=_owner(owner_id),
        )
        assert accepted.status_code == 201
    elif expect.probe_build is not None:
        refused = client.post(
            f"/api/projects/{project_id}/deployments",
            json={"build_id": expect.probe_build},
            headers=_owner(owner_id),
        )
        assert refused.status_code == CODE_STATUS[expect.confirm_reason]
        assert refused.json()["detail"].startswith(f"{expect.confirm_reason}: ")


@pytest.mark.parametrize("name", list(STATES))
def test_the_project_read_and_the_rollback_route_agree(name, client, api_world, owner_id):
    expect = STATES[name](api_world)
    project_id = api_world.project.id

    response = client.post(f"/api/projects/{project_id}/deployments/rollback", headers=_owner(owner_id))

    if expect.rollback_target is not None:
        assert response.status_code == 201
        assert response.json()["rollback_of"] == expect.rollback_target
    else:
        assert response.status_code == CODE_STATUS[expect.rollback_reason]
        assert response.json()["detail"].startswith(f"{expect.rollback_reason}: ")


def test_the_project_read_carries_exactly_the_documented_fields(client, api_world, owner_id):
    api_world.build("a", 1)

    controls = client.get(f"/api/projects/{api_world.project.id}", headers=_owner(owner_id)).json()[
        "deploy_controls"
    ]

    assert set(controls) == {
        "confirmable_build_id",
        "confirm_blocked_reason",
        "rollback_target_id",
        "rollback_blocked_reason",
        "active_deployment_id",
        "in_flight",
    }


def test_the_read_never_asks_docker_when_nothing_is_deployable(client, db_session, project, owner_id, monkeypatch):
    import app.routers.deployments as deployments_router

    def refuse():
        raise AssertionError("the project read built a Docker client for a project with no build")

    monkeypatch.setattr(deployments_router, "_deploy_manager", refuse)
    monkeypatch.setattr(deployments_router, "_image_check_manager", refuse)

    response = client.get(f"/api/projects/{project.id}", headers=_owner(owner_id))

    assert response.status_code == 200
    assert response.json()["deploy_controls"]["confirm_blocked_reason"] == "NO_SUCCESSFUL_BUILD"


def test_the_read_survives_a_docker_client_that_cannot_be_built(
    client, db_session, project, owner_id, monkeypatch
):
    import app.routers.deployments as deployments_router

    World(db_session, project, monkeypatch).build("a", 1)

    def broken():
        raise RuntimeError("DOCKER_HOST is not usable")

    monkeypatch.setattr(deployments_router, "_image_check_manager", broken)

    response = client.get(f"/api/projects/{project.id}", headers=_owner(owner_id))

    assert response.status_code == 200
    controls = response.json()["deploy_controls"]
    assert controls["confirmable_build_id"] is None
    assert controls["confirm_blocked_reason"] == "DOCKER_UNAVAILABLE"


def test_the_create_response_does_not_compute_controls(client, db_session, seeded_ids, owner_id):
    created = client.post(
        "/api/projects",
        json={
            "name": "controls-not-computed",
            "template_id": seeded_ids["template_id"],
            "team_id": seeded_ids["team_id"],
            "nl_request": "build something",
        },
        headers=_owner(owner_id),
    )

    assert created.status_code == 201
    assert created.json()["deploy_controls"] is None


# -- every refusal code, as a literal (a contract string crossing a boundary) ---------------
#
# The frontend words each code (`pages/deployControls.ts`) and pins all eleven as literals
# (`pages/deployControls.test.ts`, `EVERY_CODE`). A code renamed here would otherwise degrade the
# page to its generic sentence and turn nothing red on either side, so the backend pins them too.

# (state, the build a confirm is asked about, the literal code that confirm is refused with)
BUILD_PROBES = [
    # an older eligible build while a newer SUCCEEDED one exists: not the latest successful
    ("newer_successful_build_not_yet_approved", "bld-a", "NOT_LATEST_SUCCESSFUL"),
    ("newer_successful_build_not_yet_approved", "bld-b", "NOT_DEPLOY_ELIGIBLE"),
    # a build that did not succeed
    ("newer_build_queued_hides_nothing", "bld-q", "BUILD_NOT_SUCCEEDED"),
    ("newer_build_running_hides_nothing", "bld-r", "BUILD_NOT_SUCCEEDED"),
    ("newer_build_failed_hides_nothing", "bld-f", "BUILD_NOT_SUCCEEDED"),
    ("only_a_failed_build", "bld-f", "BUILD_NOT_SUCCEEDED"),
    ("only_successful_build_not_approved", "bld-a", "NOT_DEPLOY_ELIGIBLE"),
    ("image_missing", "bld-a", "IMAGE_MISSING"),
    ("archived_wins_over_a_missing_image", "bld-a", "PROJECT_ARCHIVED"),
    ("deploying_in_progress", "bld-a", "DEPLOYMENT_IN_FLIGHT"),
    ("eval_project_wins_over_archived", "bld-a", "EVAL_PROJECT"),
    ("postgres_not_configured", "bld-a", "POSTGRES_UNCONFIGURED"),
]

# (state, the literal code a rollback is refused with)
ROLLBACK_PROBES = [
    ("first_deploy_active_nothing_to_roll_back_to", "NO_ROLLBACK_TARGET"),
    ("only_superseded_row_is_the_active_build", "NO_ROLLBACK_TARGET"),
    ("archived_with_a_rollback_target", "PROJECT_ARCHIVED"),
    ("deploying_while_a_rollback_target_exists", "DEPLOYMENT_IN_FLIGHT"),
]

# Every public `DeployRefusalCode` value, spelled out. KEEP IN STEP WITH THE FRONTEND: the same
# eleven are `EVERY_CODE` in `frontend/src/pages/deployControls.test.ts` and each has a sentence in
# `frontend/src/pages/deployControls.ts`. A code added, renamed or dropped here changes there in the
# same commit.
ALL_REFUSAL_CODES = [
    "PROJECT_ARCHIVED",
    "DEPLOYMENT_IN_FLIGHT",
    "EVAL_PROJECT",
    "POSTGRES_UNCONFIGURED",
    "BUILD_NOT_SUCCEEDED",
    "NOT_DEPLOY_ELIGIBLE",
    "NOT_LATEST_SUCCESSFUL",
    "IMAGE_MISSING",
    "NO_ROLLBACK_TARGET",
    "NO_SUCCESSFUL_BUILD",
    "DOCKER_UNAVAILABLE",
]


def test_the_refusal_codes_are_exactly_these_eleven_and_each_is_its_own_name():
    public = {name: value for name, value in vars(DeployRefusalCode).items() if not name.startswith("_")}

    assert public == {code: code for code in ALL_REFUSAL_CODES}


@pytest.mark.parametrize(("name", "build_id", "code"), BUILD_PROBES)
def test_a_refused_confirm_carries_this_literal_code(name, build_id, code, db_session, project, owner_id, monkeypatch):
    world = World(db_session, project, monkeypatch)
    STATES[name](world)

    with pytest.raises((DeployValidationError, DeployConflictError)) as refused:
        confirm_deploy(
            db_session,
            project=db_session.get(Project, project.id),
            build=db_session.get(Build, build_id),
            actor_id=owner_id,
            image_exists=world.image_exists,
        )

    assert refused.value.code == code
    assert str(refused.value).startswith(f"{code}: ")
    # a conflict is a DeployConflictError (409 at the route), a rule a DeployValidationError (400)
    assert isinstance(refused.value, DeployConflictError) == (CODE_STATUS[code] == 409)


@pytest.mark.parametrize(("name", "code"), ROLLBACK_PROBES)
def test_a_refused_rollback_carries_this_literal_code(name, code, db_session, project, owner_id, monkeypatch):
    world = World(db_session, project, monkeypatch)
    STATES[name](world)

    with pytest.raises((DeployValidationError, DeployConflictError)) as refused:
        request_rollback(
            db_session,
            project=db_session.get(Project, project.id),
            actor_id=owner_id,
            image_exists=world.image_exists,
        )

    assert refused.value.code == code
    assert str(refused.value).startswith(f"{code}: ")


@pytest.mark.parametrize(("name", "build_id", "code"), BUILD_PROBES)
def test_a_refused_confirm_route_answers_with_this_literal_code(name, build_id, code, client, api_world, owner_id):
    STATES[name](api_world)

    response = client.post(
        f"/api/projects/{api_world.project.id}/deployments",
        json={"build_id": build_id},
        headers=_owner(owner_id),
    )

    assert response.status_code == CODE_STATUS[code]
    assert response.json()["detail"].startswith(f"{code}: ")


# -- a Docker the read cannot reach quickly is reported, not waited on ---------------------------------


@pytest.fixture()
def silent_docker_daemon():
    """A local TCP "Docker daemon" that accepts every connection and never answers: how a hung
    daemon looks to docker-py. Yields `.url` (a `DOCKER_HOST` value) and `.connections` (how many
    times anything connected). Connections are held open until teardown."""
    import socket
    import threading
    from types import SimpleNamespace

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    held: list = []

    def _accept_forever():
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            held.append(connection)  # never read, never answered

    threading.Thread(target=_accept_forever, daemon=True).start()
    try:
        yield SimpleNamespace(url=f"tcp://127.0.0.1:{server.getsockname()[1]}", connections=held)
    finally:
        server.close()
        for connection in held:
            connection.close()


def test_the_project_read_builds_a_client_with_a_short_timeout_and_the_actions_do_not(monkeypatch):
    import app.routers.deployments as deployments_router
    from app.services import deploy_manager

    seen: list[dict] = []
    monkeypatch.setattr(deploy_manager.docker, "from_env", lambda **kwargs: seen.append(kwargs) or object())

    deployments_router._image_check_manager()
    assert seen == [{"timeout": deploy_manager.IMAGE_CHECK_TIMEOUT_SECONDS}]
    # a few seconds: far above a healthy daemon, far below docker-py's 60 s per call
    assert 0 < deploy_manager.IMAGE_CHECK_TIMEOUT_SECONDS <= 5

    seen.clear()
    deployments_router._deploy_manager()  # the actions keep docker-py's default client
    assert seen == [{}]


def test_a_hung_docker_daemon_is_reported_unavailable_within_the_timeout_and_asked_once(
    client, db_session, project, owner_id, monkeypatch, silent_docker_daemon, returns_within
):
    """The real client (no seam patched) against a socket that never answers. Both controls need an
    image check (a deployable build and a rollback candidate), so the read would wait once per
    side; it waits once in all: the first failure is remembered."""
    from app.services import deploy_manager

    world = World(db_session, project, monkeypatch)
    world.build("a", 1)
    world.build("b", 3)
    world.deployment("d1", "a", DeploymentStatus.SUPERSEDED, 2, ended_minute=4)
    world.deployment("d2", "b", DeploymentStatus.ACTIVE, 4)
    monkeypatch.setenv("DOCKER_HOST", silent_docker_daemon.url)
    monkeypatch.setattr(deploy_manager, "IMAGE_CHECK_TIMEOUT_SECONDS", 1.0)

    # docker-py's default would be 60 s per call: a read that is not bounded fails this, not hangs.
    response = returns_within(
        20, lambda: client.get(f"/api/projects/{project.id}", headers=_owner(owner_id))
    )

    assert response.status_code == 200
    controls = response.json()["deploy_controls"]
    assert controls["confirmable_build_id"] is None
    assert controls["confirm_blocked_reason"] == "DOCKER_UNAVAILABLE"
    assert controls["rollback_target_id"] is None
    assert controls["rollback_blocked_reason"] == "DOCKER_UNAVAILABLE"
    assert controls["active_deployment_id"] == "dep-d2"  # what needs no image check is still answered
    assert len(silent_docker_daemon.connections) == 1


def test_a_docker_call_that_times_out_is_reported_unavailable_and_not_asked_again(
    client, db_session, project, owner_id, monkeypatch
):
    """The client builds, then asking it times out (requests' own exception, not a DockerException):
    DOCKER_UNAVAILABLE for the confirm side, and the rollback side does not ask a second time."""
    from requests.exceptions import ReadTimeout

    import app.routers.deployments as deployments_router

    world = World(db_session, project, monkeypatch)
    world.build("a", 1)
    world.build("b", 3)
    world.deployment("d1", "a", DeploymentStatus.SUPERSEDED, 2, ended_minute=4)
    world.deployment("d2", "b", DeploymentStatus.ACTIVE, 4)
    asked: list[str] = []

    class HangingManager:
        def image_exists(self, image_ref):
            asked.append(image_ref)
            raise ReadTimeout("read timed out")

    monkeypatch.setattr(deployments_router, "_image_check_manager", lambda: HangingManager())

    response = client.get(f"/api/projects/{project.id}", headers=_owner(owner_id))

    assert response.status_code == 200
    controls = response.json()["deploy_controls"]
    assert controls["confirm_blocked_reason"] == "DOCKER_UNAVAILABLE"
    assert controls["rollback_blocked_reason"] == "DOCKER_UNAVAILABLE"
    assert asked == ["img-b"]
