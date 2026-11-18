"""the build-image keep-set, the labelled sweep, the rollback window and the
container log caps. No Docker engine: the sweep runs against a fake client (the real-engine proof
of the labels is `test_image_assembly.py::test_a_real_build_image_carries_the_adr_0037_labels`).

Build ids in labels and rows are literals; the instance id is the suite's own
(`tests/instance_scope.py`), and the "other" ids are literals that can never be it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from docker.errors import APIError, ImageNotFound

from app import worker
from app.config import settings
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.services import image_retention, preview_manager
from app.services.container_log_config import capped_log_config
from app.services.deploy import ROLLBACK_WINDOW, decide_rollback
from app.services.image_retention import keep_build_ids, sweep_build_images
from app.services.status import BuildStatus, ProjectStatus
from app.services.worker_lease import LeaseLost
from tests.instance_scope import TEST_INSTANCE_ID

OTHER_INSTANCE = "forge-test-someone-else"
NOW = datetime(2026, 11, 13, 12, 0, 0)  # naive UTC, as the columns are


# -- database rows -------------------------------------------------------------


@pytest.fixture()
def project(db_session, seeded_ids):
    return _project(db_session, seeded_ids, "ret-a")


def _project(db, seeded_ids, name) -> Project:
    row = Project(
        name=name,
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.ACTIVE,
    )
    db.add(row)
    db.commit()
    return row


_clock = {"n": 0}


def _build(db, project, status=BuildStatus.FAILED, **fields) -> Build:
    """A build whose `created_at` is strictly later than the previous call's."""
    _clock["n"] += 1
    row = Build(
        project_id=project.id,
        type="create",
        status=status,
        image_ref=fields.pop("image_ref", None),
        created_at=NOW - timedelta(days=30) + timedelta(minutes=_clock["n"]),
        **fields,
    )
    db.add(row)
    db.commit()
    return row


def _deployment(db, project, build, status, user_id, ended_minutes=None) -> Deployment:
    row = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=user_id,
        status=status,
        started_at=NOW - timedelta(days=1),
        ended_at=None if ended_minutes is None else NOW - timedelta(days=1) + timedelta(minutes=ended_minutes),
    )
    db.add(row)
    db.commit()
    return row


def _superseded(db, project, seeded_ids, n) -> list[Build]:
    """`n` builds, each deployed then superseded, oldest first."""
    builds = []
    for i in range(n):
        build = _build(db, project, BuildStatus.FAILED, image_ref=f"img-sup-{project.name}-{i}")
        _deployment(db, project, build, DeploymentStatus.SUPERSEDED, seeded_ids["user_id"], ended_minutes=i)
        builds.append(build)
    return builds


# -- the keep-set: one test per member -------------------------------------------


@pytest.mark.parametrize("status", [DeploymentStatus.ACTIVE, DeploymentStatus.DEPLOYING])
def test_keep_the_build_of_an_active_or_deploying_deployment(db_session, project, seeded_ids, status):
    deployed = _build(db_session, project, BuildStatus.FAILED)  # not SUCCEEDED, not RUNNING: only the deployment keeps it
    other = _build(db_session, project, BuildStatus.FAILED)
    _deployment(db_session, project, deployed, status, seeded_ids["user_id"])

    keep = keep_build_ids(db_session, NOW)

    assert deployed.id in keep
    assert other.id not in keep


def test_keep_the_latest_succeeded_build_of_each_project(db_session, project, seeded_ids):
    older = _build(db_session, project, BuildStatus.SUCCEEDED)
    newer = _build(db_session, project, BuildStatus.SUCCEEDED)
    failed_after = _build(db_session, project, BuildStatus.FAILED)
    second = _project(db_session, seeded_ids, "ret-b")
    second_only = _build(db_session, second, BuildStatus.SUCCEEDED)

    keep = keep_build_ids(db_session, NOW)

    assert newer.id in keep
    assert second_only.id in keep  # per project, not one for the installation
    assert older.id not in keep
    assert failed_after.id not in keep


def test_keep_a_build_with_a_live_preview(db_session, project):
    live = _build(db_session, project, BuildStatus.FAILED, preview_expires_at=NOW + timedelta(minutes=5))
    expired = _build(db_session, project, BuildStatus.FAILED, preview_expires_at=NOW - timedelta(minutes=5))
    none = _build(db_session, project, BuildStatus.FAILED)

    keep = keep_build_ids(db_session, NOW)

    assert live.id in keep
    assert expired.id not in keep
    assert none.id not in keep


def test_keep_a_running_build(db_session, project):
    running = _build(db_session, project, BuildStatus.RUNNING)
    queued = _build(db_session, project, BuildStatus.QUEUED)

    keep = keep_build_ids(db_session, NOW)

    assert running.id in keep
    assert queued.id not in keep


def test_keep_the_three_most_recently_superseded_builds_of_a_project(db_session, project, seeded_ids):
    oldest, second, third, newest = _superseded(db_session, project, seeded_ids, 4)

    keep = keep_build_ids(db_session, NOW)

    assert {second.id, third.id, newest.id} <= keep
    assert oldest.id not in keep


def test_the_superseded_window_counts_per_project_and_not_the_active_build(db_session, project, seeded_ids):
    other = _project(db_session, seeded_ids, "ret-b")
    mine = _superseded(db_session, project, seeded_ids, 3)
    theirs = _superseded(db_session, other, seeded_ids, 3)
    # Re-confirming the ACTIVE build leaves a superseded row of that same build: it is not a
    # rollback target, so it must not use up one of the three slots.
    active = _build(db_session, project, BuildStatus.FAILED)
    _deployment(db_session, project, active, DeploymentStatus.SUPERSEDED, seeded_ids["user_id"], ended_minutes=50)
    _deployment(db_session, project, active, DeploymentStatus.ACTIVE, seeded_ids["user_id"])

    keep = keep_build_ids(db_session, NOW)

    assert {b.id for b in mine + theirs} <= keep


def test_a_build_in_none_of_the_five_is_not_kept(db_session, project, seeded_ids):
    stray = _build(db_session, project, BuildStatus.FAILED, image_ref="img-stray")
    old_success = _build(db_session, project, BuildStatus.SUCCEEDED)
    _build(db_session, project, BuildStatus.SUCCEEDED)  # the latest, so `old_success` is not
    _superseded(db_session, project, seeded_ids, ROLLBACK_WINDOW + 1)

    keep = keep_build_ids(db_session, NOW)

    assert stray.id not in keep
    assert old_success.id not in keep


# -- rollback looks back at most three builds -----------------------------------------


def test_rollback_considers_at_most_three_superseded_builds(db_session, project, seeded_ids):
    oldest, second, third, newest = _superseded(db_session, project, seeded_ids, 4)
    active = _build(db_session, project, BuildStatus.SUCCEEDED)
    _deployment(db_session, project, active, DeploymentStatus.ACTIVE, seeded_ids["user_id"])

    only_the_oldest_has_an_image = lambda ref: ref == oldest.image_ref  # noqa: E731
    refused = decide_rollback(db_session, project=project, image_exists=only_the_oldest_has_an_image)
    assert refused.target is None
    assert refused.refusal.code == "NO_ROLLBACK_TARGET"

    # The control: the same four rows, the image of the third present, is a target -- so the refusal
    # above is the window and not an unrelated refusal.
    reachable = decide_rollback(db_session, project=project, image_exists=lambda ref: ref == third.image_ref)
    assert reachable.target is not None
    assert reachable.target.build_id == third.id


# -- the sweep: a fake engine -----------------------------------------------------------


class FakeImage:
    def __init__(self, build_id, *, instance=TEST_INSTANCE_ID, role="build-image", labelled=True, project="p"):
        labels = {}
        if labelled:
            labels = {"forge.instance": instance, "forge.role": role, "forge.project_id": project}
            if build_id is not None:
                labels["forge.build_id"] = build_id
        self.labels = labels
        self.id = f"sha256:{build_id}-{instance}-{role}"
        self.tags = [f"forge-build-{project}-{build_id}:latest"]


class FakeImages:
    def __init__(self, images, honour_filters=True):
        self.images = list(images)
        self.honour_filters = honour_filters
        self.removed: list[tuple[str, bool]] = []
        self.remove_errors: dict[str, Exception] = {}
        self.list_error: Exception | None = None
        self.events: list[str] = []

    def list(self, filters=None, **kw):
        if self.list_error is not None:
            raise self.list_error
        if not self.honour_filters:
            return list(self.images)
        wanted = filters["label"]
        return [i for i in self.images if all(i.labels.get(c.split("=")[0]) == c.split("=")[1] for c in wanted)]

    def remove(self, name, force=False, **kw):
        self.events.append(f"remove:{name}")
        if name in self.remove_errors:
            raise self.remove_errors[name]
        self.removed.append((name, force))
        self.images = [i for i in self.images if name not in i.tags and name != i.id]


class FakeClient:
    def __init__(self, images, honour_filters=True):
        self.images = FakeImages(images, honour_filters)


def _tag(build_id, project="p"):
    return f"forge-build-{project}-{build_id}:latest"


def _audits(db):
    return db.query(AuditLog).filter(AuditLog.action == "build_image.removed").all()


def test_a_labelled_image_outside_the_keep_set_is_removed_and_audited(db_session, project):
    stray = _build(db_session, project, BuildStatus.FAILED)
    client = FakeClient([FakeImage(stray.id, project=project.id)])

    removed = sweep_build_images(db_session, client=client, now=NOW)

    assert removed == 1
    assert client.images.removed == [(_tag(stray.id, project.id), False)]  # by tag, never forced
    (entry,) = _audits(db_session)
    assert entry.target == f"build:{stray.id}"
    assert entry.actor_id is None
    assert entry.metadata_json == {
        "image": _tag(stray.id, project.id),
        "build_id": stray.id,
        "project_id": project.id,
    }


def test_a_kept_build_keeps_its_image(db_session, project):
    running = _build(db_session, project, BuildStatus.RUNNING)
    client = FakeClient([FakeImage(running.id)])

    assert sweep_build_images(db_session, client=client, now=NOW) == 0
    assert client.images.removed == []
    assert _audits(db_session) == []


@pytest.mark.parametrize("honour_filters", [True, False])
def test_the_sweep_never_removes_an_unlabelled_image(db_session, project, honour_filters):
    stray = _build(db_session, project, BuildStatus.FAILED)
    # `honour_filters=False` is an engine that hands back everything: the labels are read again.
    client = FakeClient([FakeImage(stray.id, labelled=False)], honour_filters)

    assert sweep_build_images(db_session, client=client, now=NOW) == 0
    assert client.images.removed == []


@pytest.mark.parametrize("honour_filters", [True, False])
def test_the_sweep_never_removes_another_instances_image(db_session, project, honour_filters):
    stray = _build(db_session, project, BuildStatus.FAILED)
    client = FakeClient([FakeImage(stray.id, instance=OTHER_INSTANCE)], honour_filters)

    assert sweep_build_images(db_session, client=client, now=NOW) == 0
    assert client.images.removed == []


@pytest.mark.parametrize("honour_filters", [True, False])
def test_the_sweep_never_removes_an_image_of_another_role(db_session, project, honour_filters):
    stray = _build(db_session, project, BuildStatus.FAILED)
    client = FakeClient([FakeImage(stray.id, role="preview")], honour_filters)

    assert sweep_build_images(db_session, client=client, now=NOW) == 0
    assert client.images.removed == []


def test_an_image_with_no_build_id_label_is_left_alone(db_session):
    client = FakeClient([FakeImage(None)])

    assert sweep_build_images(db_session, client=client, now=NOW) == 0
    assert client.images.removed == []


def test_the_sweep_removes_only_what_it_may_when_everything_is_in_one_list(db_session, project):
    stray = _build(db_session, project, BuildStatus.FAILED)
    kept = _build(db_session, project, BuildStatus.RUNNING)
    client = FakeClient(
        [
            FakeImage(stray.id, labelled=False),
            FakeImage(stray.id, instance=OTHER_INSTANCE),
            FakeImage(stray.id, role="preview"),
            FakeImage(kept.id),
            FakeImage(stray.id, project=project.id),
        ],
        honour_filters=False,
    )

    assert sweep_build_images(db_session, client=client, now=NOW) == 1
    assert client.images.removed == [(_tag(stray.id, project.id), False)]


def test_an_image_in_use_is_logged_and_retried_next_cycle_never_forced(db_session, project, caplog):
    stray = _build(db_session, project, BuildStatus.FAILED)
    second = _build(db_session, project, BuildStatus.FAILED)
    client = FakeClient([FakeImage(stray.id, project=project.id), FakeImage(second.id, project=project.id)])
    client.images.remove_errors[_tag(stray.id, project.id)] = APIError("conflict: image is being used by a container")
    state: dict = {}

    with caplog.at_level(logging.WARNING, logger="app.image_retention"):
        first = sweep_build_images(db_session, client=client, now=NOW, scan_state=state)

    assert first == 1  # the other image still went
    assert any("not removed" in r.message and stray.id in r.message for r in caplog.records)
    assert [f for _, f in client.images.removed] == [False]
    assert len(_audits(db_session)) == 1  # nothing audited for the one that stayed

    del client.images.remove_errors[_tag(stray.id, project.id)]
    later = NOW + timedelta(seconds=image_retention.SWEEP_INTERVAL_SECONDS)
    assert sweep_build_images(db_session, client=client, now=later, scan_state=state) == 1
    assert all(f is False for _, f in client.images.removed)
    assert len(_audits(db_session)) == 2


def test_an_image_someone_else_already_removed_is_not_audited(db_session, project):
    stray = _build(db_session, project, BuildStatus.FAILED)
    client = FakeClient([FakeImage(stray.id, project=project.id)])
    client.images.remove_errors[_tag(stray.id, project.id)] = ImageNotFound("gone")

    assert sweep_build_images(db_session, client=client, now=NOW) == 0
    assert _audits(db_session) == []


class _Lease:
    def __init__(self, events, lose_on=None):
        self.events = events
        self.calls = 0
        self.lose_on = lose_on

    def require(self):
        self.calls += 1
        self.events.append("lease")
        if self.lose_on == self.calls:
            raise LeaseLost("taken over")


def test_the_lease_is_checked_before_every_removal(db_session, project):
    first = _build(db_session, project, BuildStatus.FAILED)
    second = _build(db_session, project, BuildStatus.FAILED)
    client = FakeClient([FakeImage(first.id), FakeImage(second.id)])
    lease = _Lease(client.images.events)

    assert sweep_build_images(db_session, client=client, lease=lease, now=NOW) == 2

    kinds = [e.split(":")[0] for e in client.images.events]
    assert kinds == ["lease", "remove", "lease", "remove"]


def test_a_lost_lease_removes_nothing_and_ends_the_sweep(db_session, project):
    stray = _build(db_session, project, BuildStatus.FAILED)
    client = FakeClient([FakeImage(stray.id)])
    lease = _Lease(client.images.events, lose_on=1)

    with pytest.raises(LeaseLost):
        sweep_build_images(db_session, client=client, lease=lease, now=NOW)

    assert client.images.removed == []
    assert _audits(db_session) == []


def test_the_sweep_runs_once_an_hour(db_session, project):
    stray = _build(db_session, project, BuildStatus.FAILED)
    client = FakeClient([FakeImage(stray.id, project=project.id)])
    state: dict = {}
    listed = MagicMock(wraps=client.images.list)
    client.images.list = listed

    sweep_build_images(db_session, client=client, now=NOW, scan_state=state)
    sweep_build_images(db_session, client=client, now=NOW + timedelta(minutes=59), scan_state=state)
    assert listed.call_count == 1
    sweep_build_images(db_session, client=client, now=NOW + timedelta(minutes=61), scan_state=state)
    assert listed.call_count == 2


def test_docker_being_unreachable_is_a_skipped_sweep_not_an_error(db_session, project):
    client = FakeClient([])
    client.images.list_error = ConnectionError("no daemon")

    assert sweep_build_images(db_session, client=client, now=NOW) == 0


def test_run_iteration_runs_the_sweep_only_for_a_caller_that_holds_its_cadence(db_session, monkeypatch):
    from app.services.build_executor import PlaceholderBuildExecutor

    calls = []
    monkeypatch.setattr(worker, "sweep_build_images", lambda db, **kw: calls.append(kw))

    worker.run_iteration(db_session, PlaceholderBuildExecutor())
    assert calls == []

    state: dict = {}
    worker.run_iteration(db_session, PlaceholderBuildExecutor(), image_sweep_state=state)
    assert len(calls) == 1 and calls[0]["scan_state"] is state


# -- builds are labelled and run with forcerm ---------------------------------------------


def test_assemble_image_builds_with_forcerm_and_the_labels_it_was_given(tmp_path):
    from app.services.image_assembly import assemble_image

    skeleton = tmp_path / "skeleton"
    skeleton.mkdir()
    (skeleton / "Dockerfile").write_text("FROM scratch\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    client = MagicMock()
    client.images.build.return_value = (MagicMock(), iter(()))

    assemble_image(
        workspace_dir=str(workspace),
        template_skeleton_dir=str(skeleton),
        tag="t",
        labels={"forge.role": "build-image"},
        docker_client=client,
    )

    kwargs = client.images.build.call_args.kwargs
    assert kwargs["forcerm"] is True
    assert kwargs["labels"] == {"forge.role": "build-image"}


# -- deploy and preview containers: a capped json-file log ---------------------------------


def test_the_log_config_is_json_file_with_the_settings_values(monkeypatch):
    monkeypatch.setattr(settings, "container_log_max_size", "7m")
    monkeypatch.setattr(settings, "container_log_max_files", 5)

    config = capped_log_config()

    assert config["Type"] == "json-file"
    assert config["Config"] == {"max-size": "7m", "max-file": "5"}


def test_the_log_defaults_are_ten_megabytes_and_three_files():
    from app.config import Settings

    fresh = Settings(_env_file=None)
    assert fresh.container_log_max_size == "10m"
    assert fresh.container_log_max_files == 3


@pytest.mark.parametrize("bad", ["", "0", "10mb", "-1m", "m", "1.5m"])
def test_a_log_size_docker_would_refuse_is_refused_at_startup(bad):
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, container_log_max_size=bad)


def test_a_deploy_container_is_created_with_the_capped_log_config(monkeypatch):
    from app.services.deploy_manager import DeployManager

    monkeypatch.setattr(settings, "container_log_max_size", "7m")
    monkeypatch.setattr(settings, "container_log_max_files", 5)

    client = MagicMock()
    network = MagicMock()
    network.name = "net"
    manager = DeployManager(client=client)
    manager._connect_data_services = lambda *a, **k: None
    manager._start_on_network(
        network, project_id="p", deployment_id="d", build_id="b", image_ref="img", env={}
    )

    run = client.containers.run.call_args.kwargs
    assert run["log_config"]["Type"] == "json-file"
    assert run["log_config"]["Config"] == {"max-size": "7m", "max-file": "5"}


def test_a_preview_container_is_created_with_the_capped_log_config(monkeypatch):
    monkeypatch.setattr(settings, "container_log_max_size", "7m")
    monkeypatch.setattr(settings, "container_log_max_files", 5)
    client = MagicMock()
    network = MagicMock()
    network.name = "net"
    monkeypatch.setattr(preview_manager, "issue_preview_token", lambda build_id: ("tok", NOW.replace(tzinfo=timezone.utc)))
    build = MagicMock()
    build.id = "b"
    build.image_ref = "img"
    manager = preview_manager.PreviewManager(client=client)

    manager._start_on_network(network, project_id="p", build=build)

    run = client.containers.run.call_args.kwargs
    assert run["log_config"]["Type"] == "json-file"
    assert run["log_config"]["Config"] == {"max-size": "7m", "max-file": "5"}
