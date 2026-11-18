"""the preview container's disk cap -- the watcher's decisions, its record, its thread, and
where the worker starts it. No Docker: a fake engine returns the rows a real one returns (shapes taken
from `test_sandbox_runner_unit.py`'s size-read tests, which were measured on both host shapes) and a fake
`PreviewManager` removes the fake containers; the real-engine proof is
`test_preview_disk_cap_integration.py`.

Every poll here is a plain call: nothing sleeps, nothing writes. The two tests that run the real thread
use a 50 ms interval and an `Event` the fake watcher sets, never a wall-clock guess.
"""

from __future__ import annotations

import ast
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from docker.errors import DockerException, NotFound

from app import worker
from app.config import settings
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.project import Project
from app.services import preview_disk_watch
from app.services.preview_disk_watch import (
    PreviewDiskStop,
    PreviewDiskWatcher,
    PreviewDiskWatchRunner,
    record_preview_disk_stop,
    stop_detail,
)
from app.services.sandbox_runner import judge_disk_reading, writable_layer_bytes_from_row
from app.services.status import BuildStatus, ProjectStatus
from tests.engine_list_shim import ContainerListViaContainers
from tests.instance_scope import TEST_INSTANCE_ID

MIB = 1024 * 1024
LIMIT_MB = 8  # the cap every test here runs under


@pytest.fixture(autouse=True)
def _cap(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_disk_limit_mb", LIMIT_MB)
    monkeypatch.setattr(settings, "sandbox_disk_poll_interval_seconds", 0.05)


# -- a fake engine: the two corners of the Docker client the watcher touches ------------------------------


def _labels(build_id="b1", project_id="p1", **overrides):
    labels = {
        "forge.instance": TEST_INSTANCE_ID,
        "forge.role": "preview",
        "forge.preview": "true",
        "forge.build_id": build_id,
        "forge.project_id": project_id,
    }
    for key, value in overrides.items():
        key = key.replace("__", ".")
        if value is None:
            labels.pop(key, None)
        else:
            labels[key] = value
    return labels


class _EngineApi(ContainerListViaContainers):
    def __init__(self, containers):
        self.containers = containers


class FakeEngine:
    """`client.api.containers(...)` (the one bulk size read) and `client.containers.get(...)`.
    The label filter the watcher sends is recorded and NOT applied: the Python check, not the engine,
    must decide what is a preview of this installation."""

    def __init__(self):
        self.rows: list[dict] = []
        self.alive: dict[str, str] = {}  # container name -> id
        self.list_error: Exception | None = None
        self.size_error: Exception | None = None  # only the SIZED list fails; the unsized one answers
        self.list_calls: list[dict] = []
        self.api = _EngineApi(self._list)
        self.containers = SimpleNamespace(get=self._get)

    def add(self, build_id, *, size_rw=None, size_root_fs=141_250_560, container_id=None, labels=None, name=None):
        container_id = container_id or f"id-{build_id}"
        row = {"Id": container_id, "Labels": labels if labels is not None else _labels(build_id)}
        if size_rw is not None:
            row["SizeRw"] = size_rw
        if size_root_fs is not None:
            row["SizeRootFs"] = size_root_fs
        self.rows.append(row)
        self.alive[name or f"forge-preview-{build_id}"] = container_id
        return row

    def set_size(self, build_id, size_rw, *, size_root_fs=141_250_560):
        (row,) = [r for r in self.rows if r["Labels"].get("forge.build_id") == build_id]
        row.pop("SizeRw", None)
        row.pop("SizeRootFs", None)
        if size_rw is not None:
            row["SizeRw"] = size_rw
        if size_root_fs is not None:
            row["SizeRootFs"] = size_root_fs

    def drop(self, build_id):
        """The fake container goes (named `drop`, not `remove`: the removal-site scan counts every
        `.remove(` call in tests/, and this one is a dictionary entry, not a Docker container)."""
        name = f"forge-preview-{build_id}"
        container_id = self.alive.pop(name, None)
        self.rows = [r for r in self.rows if r["Id"] != container_id]

    def _list(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.list_error is not None:
            raise self.list_error
        if kwargs.get("size") and self.size_error is not None:
            raise self.size_error
        if not kwargs.get("size"):
            # An engine that does not walk the layers returns rows with no size fields at all.
            return [{k: v for k, v in r.items() if k not in ("SizeRw", "SizeRootFs")} for r in self.rows]
        return [dict(r) for r in self.rows]

    def _get(self, name_or_id):
        if name_or_id in self.alive:
            return SimpleNamespace(id=self.alive[name_or_id], name=name_or_id)
        for name, container_id in self.alive.items():
            if container_id == name_or_id:
                return SimpleNamespace(id=container_id, name=name)
        raise NotFound(f"no such container: {name_or_id}")


class FakeManager:
    """`PreviewManager.stop_preview`: records the call and, unless told not to, removes the container."""

    def __init__(self, engine, *, removes=True, raises=False):
        self.engine = engine
        self.removes = removes
        self.raises = raises
        self.stopped: list[str] = []

    def stop_preview(self, build_id):
        self.stopped.append(build_id)
        if self.removes:
            self.engine.drop(build_id)
        if self.raises:
            raise RuntimeError("the engine said no")
        return True


@pytest.fixture()
def engine():
    return FakeEngine()


@pytest.fixture()
def manager(engine):
    return FakeManager(engine)


@pytest.fixture()
def watcher(engine, manager):
    return PreviewDiskWatcher(client=engine, preview_manager=manager)


# -- over the cap: torn down through the normal path, and only that one -----------------------------------


def test_a_preview_over_the_cap_is_torn_down_through_stop_preview_and_its_neighbour_is_not(engine, manager, watcher):
    engine.add("over", size_rw=LIMIT_MB * MIB + 1)
    engine.add("fine", size_rw=1 * MIB)

    stops = watcher.poll_once()

    assert manager.stopped == ["over"]
    assert [(s.build_id, s.project_id, s.verdict, s.used_bytes, s.limit_bytes) for s in stops] == [
        ("over", "p1", "over", LIMIT_MB * MIB + 1, LIMIT_MB * MIB)
    ]
    assert "forge-preview-fine" in engine.alive  # the neighbour is still running


def test_the_cap_is_strict_exactly_at_the_limit_is_not_over(engine, manager, watcher):
    engine.add("at", size_rw=LIMIT_MB * MIB)
    engine.add("past", size_rw=LIMIT_MB * MIB + 1)

    watcher.poll_once()

    assert manager.stopped == ["past"]


def test_one_bulk_read_covers_every_preview_and_asks_the_engine_for_sizes_of_this_installations_previews(
    engine, watcher
):
    for n in range(5):
        engine.add(f"b{n}", size_rw=1 * MIB)

    watcher.poll_once()

    (call,) = engine.list_calls  # five previews, ONE call: no per-preview request, no thread per preview
    assert call["size"] is True
    assert call["filters"] == {"label": [f"forge.instance={TEST_INSTANCE_ID}", "forge.role=preview"]}


def test_a_stop_is_a_stop_once_the_container_is_gone_it_is_not_judged_again(engine, manager, watcher):
    engine.add("over", size_rw=LIMIT_MB * MIB + 1)

    assert len(watcher.poll_once()) == 1
    assert watcher.poll_once() == []
    assert manager.stopped == ["over"]


# -- what is positively this installation's preview, and nothing else --------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"forge__instance": "some-other-installation"}, id="another installation"),
        pytest.param({"forge__instance": None}, id="no instance label"),
        pytest.param({"forge__role": "preview-redis"}, id="a preview's Redis"),
        pytest.param({"forge__role": "sandbox"}, id="a build sandbox"),
        pytest.param({"forge__role": "deploy"}, id="a deployed service"),
        pytest.param({"forge__role": None}, id="no role"),
        pytest.param({"forge__preview": None}, id="no preview marker"),
        pytest.param({"forge__preview": "false"}, id="preview marker not true"),
        pytest.param({"forge__build_id": None}, id="no build id"),
    ],
)
def test_a_container_that_is_not_positively_this_installations_preview_is_never_touched(
    engine, manager, watcher, overrides
):
    # The labels are built for THIS row's build id, so the container the watcher would look up by name
    # exists: a check that stopped refusing it would really stop it (and fail here), instead of finding
    # nothing under another row's name.
    engine.add("not-ours", size_rw=100 * MIB, labels=_labels("not-ours", **overrides))  # far over the cap
    engine.add("ours", size_rw=100 * MIB)  # the control: the same overshoot, positively ours

    stops = watcher.poll_once()

    assert manager.stopped == ["ours"]
    assert [s.build_id for s in stops] == ["ours"]
    assert "forge-preview-not-ours" in engine.alive


# -- the engine stops reporting sizes: fail closed ---------------------------------------------------------


def test_a_preview_the_engine_reports_no_size_for_is_torn_down_after_five_polls_not_before(engine, manager, watcher):
    engine.add("blind", size_rw=None, size_root_fs=None)  # neither SizeRw nor SizeRootFs: an engine that does not say

    for poll in range(4):
        assert watcher.poll_once() == [], f"poll {poll + 1}"
    assert manager.stopped == []

    (stop,) = watcher.poll_once()  # the fifth in a row

    assert manager.stopped == ["blind"]
    assert (stop.verdict, stop.used_bytes) == ("blind", None)


def test_one_readable_poll_resets_the_blind_count(engine, manager, watcher):
    engine.add("flaky", size_rw=None, size_root_fs=None)
    for _ in range(4):
        watcher.poll_once()
    engine.set_size("flaky", 1 * MIB)
    watcher.poll_once()  # readable: the count starts over
    engine.set_size("flaky", None, size_root_fs=None)

    for _ in range(4):
        assert watcher.poll_once() == []

    assert manager.stopped == []
    assert len(watcher.poll_once()) == 1  # five again, and only now


def test_an_empty_layer_the_engine_omits_the_size_for_is_zero_not_blind(engine, manager, watcher):
    """overlay2 omits `SizeRw` for a layer with nothing in it (measured). A preview that has not
    written anything yet is not an engine that stopped reporting."""
    engine.add("empty", size_rw=None, size_root_fs=141_250_560)

    for _ in range(12):
        assert watcher.poll_once() == []

    assert manager.stopped == []


def test_a_failed_bulk_read_counts_against_every_preview_the_last_good_read_showed(engine, manager, watcher):
    engine.add("a", size_rw=1 * MIB)
    engine.add("b", size_rw=1 * MIB)
    assert watcher.poll_once() == []  # a good read: both are known
    engine.list_error = DockerException("the engine did not answer")

    for _ in range(4):
        assert watcher.poll_once() == []
    stops = watcher.poll_once()

    assert sorted(manager.stopped) == ["a", "b"]
    assert {s.verdict for s in stops} == {"blind"}


def test_a_bulk_read_that_works_again_clears_the_failures(engine, manager, watcher):
    engine.add("a", size_rw=1 * MIB)
    watcher.poll_once()
    engine.list_error = DockerException("down")
    for _ in range(4):
        watcher.poll_once()
    engine.list_error = None
    watcher.poll_once()  # answers, and `a` is readable
    engine.list_error = DockerException("down")

    for _ in range(4):
        assert watcher.poll_once() == []

    assert manager.stopped == []


def test_a_failed_read_with_nothing_known_judges_nothing_and_does_not_raise(engine, manager, watcher):
    engine.list_error = RuntimeError("engine refused the connection")

    assert watcher.poll_once() == []
    assert manager.stopped == []


# -- a preview the watcher has never seen is judged when the sized read fails ----------------------


def test_a_preview_that_first_appears_while_the_sized_read_fails_is_torn_down_after_five_polls(
    engine, manager, watcher
):
    """The sized read has failed from the very first poll (or the worker just restarted), so no good read
    ever showed this preview. Judging only what an earlier good read showed left it running uncapped until
    its TTL; the unsized list names it, and it is counted like any other."""
    engine.size_error = DockerException("the engine's size computation is not answering")
    engine.add("never-seen", size_rw=1 * MIB)

    for poll in range(4):
        assert watcher.poll_once() == [], f"poll {poll + 1}"
    assert manager.stopped == []

    (stop,) = watcher.poll_once()  # the fifth unreadable poll in a row

    assert manager.stopped == ["never-seen"]
    assert (stop.build_id, stop.verdict, stop.used_bytes) == ("never-seen", "blind", None)


def test_a_preview_started_between_failed_reads_joins_the_count_from_its_first_poll(engine, manager, watcher):
    engine.add("old", size_rw=1 * MIB)
    assert watcher.poll_once() == []  # a good read: `old` is known
    engine.size_error = DockerException("down")
    for _ in range(3):
        watcher.poll_once()  # `old` is at 3
    engine.add("new", size_rw=1 * MIB)  # appears while reads fail

    assert watcher.poll_once() == []  # `old` is at 4, `new` at 1
    stops = watcher.poll_once()  # `old` is at 5, `new` at 2

    assert [s.build_id for s in stops] == ["old"]
    assert manager.stopped == ["old"]
    for _ in range(2):
        assert watcher.poll_once() == []  # `new` is at 3, 4
    assert [s.build_id for s in watcher.poll_once()] == ["new"]  # and at 5 it goes too


def test_when_the_sized_read_answers_again_a_never_seen_preview_is_judged_by_its_size(engine, manager, watcher):
    engine.size_error = DockerException("down")
    engine.add("late", size_rw=1 * MIB)
    for _ in range(4):
        watcher.poll_once()
    engine.size_error = None
    watcher.poll_once()  # readable: the count starts over
    engine.size_error = DockerException("down again")

    for _ in range(4):
        assert watcher.poll_once() == []

    assert manager.stopped == []


def test_a_preview_that_leaves_the_list_while_reads_fail_stops_being_counted(engine, manager, watcher):
    engine.size_error = DockerException("down")
    engine.add("leaves", size_rw=1 * MIB)
    for _ in range(3):
        watcher.poll_once()
    engine.drop("leaves")  # a TTL sweep took it
    engine.add("leaves", size_rw=1 * MIB, container_id="id-leaves-2")  # and the same build's name is reused later

    assert watcher.poll_once() == []
    assert watcher.poll_once() == []
    assert manager.stopped == []  # the new container starts its own count at 1, not at the old one's 4


def test_the_unsized_list_is_the_cheap_one_it_carries_the_same_filter_and_the_same_bound(engine, watcher):
    engine.size_error = DockerException("down")
    engine.add("p", size_rw=1 * MIB)

    watcher.poll_once()

    sized, unsized = engine.list_calls
    assert sized["size"] is True and unsized["size"] is False
    assert unsized["filters"] == {"label": [f"forge.instance={TEST_INSTANCE_ID}", "forge.role=preview"]}
    assert engine.api.list_timeouts == [5.0, 5.0]  # both reads are bounded; the bound itself is pinned in the sandbox runner's tests


def test_an_unsized_list_does_not_make_a_container_that_is_not_ours_a_preview_to_judge(engine, manager, watcher):
    engine.size_error = DockerException("down")
    engine.add("not-ours", size_rw=1 * MIB, labels=_labels("not-ours", forge__instance="some-other-installation"))
    engine.add("ours", size_rw=1 * MIB)

    for _ in range(5):
        watcher.poll_once()

    assert manager.stopped == ["ours"]
    assert "forge-preview-not-ours" in engine.alive


# -- a teardown that does not finish is not recorded, and is tried again ------------------------------------


def test_a_container_that_cannot_be_removed_is_not_reported_stopped_and_the_next_poll_tries_again(engine):
    manager = FakeManager(engine, removes=False)
    watcher = PreviewDiskWatcher(client=engine, preview_manager=manager)
    engine.add("stuck", size_rw=100 * MIB)

    assert watcher.poll_once() == []  # `stop_preview` ran and the container is still there: nothing to record
    assert watcher.poll_once() == []
    assert manager.stopped == ["stuck", "stuck"]

    manager.removes = True
    (stop,) = watcher.poll_once()  # now it goes, and it is recorded exactly once
    assert stop.build_id == "stuck"
    assert watcher.poll_once() == []


def test_a_stop_preview_that_raises_does_not_escape_the_poll(engine):
    manager = FakeManager(engine, removes=False, raises=True)
    watcher = PreviewDiskWatcher(client=engine, preview_manager=manager)
    engine.add("explodes", size_rw=100 * MIB)

    assert watcher.poll_once() == []
    assert manager.stopped == ["explodes"]


def test_a_preview_that_was_already_gone_is_not_this_watchers_to_record(engine, manager, watcher):
    engine.add("raced", size_rw=100 * MIB)
    real_list = engine._list

    def list_then_lose_the_container(**kwargs):
        rows = real_list(**kwargs)
        engine.alive.pop("forge-preview-raced")  # the TTL sweep removed it between the read and the stop
        return rows

    engine.api.containers = list_then_lose_the_container

    assert watcher.poll_once() == []
    assert manager.stopped == []  # nothing to stop, nothing to record: the sweep that removed it clears its own fields


def test_a_container_that_now_carries_the_name_is_not_the_one_that_was_over_the_cap(engine, manager, watcher):
    engine.add("replaced", size_rw=100 * MIB, container_id="old-id")
    real_list = engine._list

    def list_then_replace(**kwargs):
        rows = real_list(**kwargs)
        engine.alive["forge-preview-replaced"] = "new-id"  # the same build's preview was started again
        return rows

    engine.api.containers = list_then_replace

    assert watcher.poll_once() == []
    assert manager.stopped == []  # the new container is not judged by the old one's size


# -- the verdict and the row reading are the production ones, not copies -------------------------------------------------


@pytest.mark.parametrize(
    ("used", "blind_polls", "expected"),
    [
        (0, 0, (None, 0)),
        (8 * MIB, 3, (None, 0)),  # at the limit, a readable poll: carry on and the blind count is reset
        (8 * MIB + 1, 3, ("over", 0)),
        (None, 0, (None, 1)),
        (None, 3, (None, 4)),
        (None, 4, ("blind", 5)),
        (None, 9, ("blind", 10)),
    ],
)
def test_judge_disk_reading_is_the_one_verdict(used, blind_polls, expected):
    assert judge_disk_reading(used, limit_bytes=8 * MIB, blind_polls=blind_polls) == expected


def test_writable_layer_bytes_from_row_is_the_one_reading():
    assert writable_layer_bytes_from_row({"SizeRw": 5_242_880, "SizeRootFs": 141_250_560}) == 5_242_880
    assert writable_layer_bytes_from_row({"SizeRootFs": 141_250_560}) == 0  # overlay2, empty layer
    assert writable_layer_bytes_from_row({}) is None
    assert writable_layer_bytes_from_row({"SizeRw": -1, "SizeRootFs": 1}) is None
    assert writable_layer_bytes_from_row({"SizeRw": True, "SizeRootFs": 1}) is None
    assert writable_layer_bytes_from_row({"SizeRw": "12", "SizeRootFs": 1}) is None


def _watch_source() -> tuple[str, ast.Module]:
    source = Path(preview_disk_watch.__file__).read_text(encoding="utf-8")
    return source, ast.parse(source)


def test_the_watcher_reads_rows_and_judges_with_the_runners_functions_and_has_no_copy_of_either():
    source, tree = _watch_source()
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "app.services.sandbox_runner"
        for alias in node.names
    }
    string_literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}

    assert {"judge_disk_reading", "writable_layer_bytes_from_row"} <= imported
    assert not {"SizeRw", "SizeRootFs"} & string_literals  # the row's fields are named in ONE place
    assert "_DISK_BLIND_POLL_LIMIT" not in source  # nor is the blind rule restated


def test_the_watcher_tears_down_only_through_stop_preview_never_by_removing_or_killing_anything_itself():
    _, tree = _watch_source()
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "stop_preview" in called
    assert not {"remove", "kill", "stop", "prune"} & called


# -- the record: where the project's timeline shows it --------------------------------------------------------


def _project_and_build(db, seeded_ids, *, with_preview=True):
    project = Project(
        name="disk-cap-project",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db.add(project)
    db.commit()
    build = Build(project_id=project.id, type="create", status=BuildStatus.SUCCEEDED)
    db.add(build)
    db.flush()
    if with_preview:
        build.preview_url = "/api/projects/x/preview/y?token=t"
        build.preview_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
        build.preview_token_hash = "hash"
    db.commit()
    db.refresh(project)
    db.refresh(build)
    return project, build


def _stop(build, *, verdict="over", used=LIMIT_MB * MIB + 5, limit=LIMIT_MB * MIB):
    return PreviewDiskStop(
        build_id=build.id, project_id=build.project_id, verdict=verdict, used_bytes=used, limit_bytes=limit
    )


def _audits(db, action):
    return list(db.query(AuditLog).filter(AuditLog.action == action))


def test_an_over_the_cap_stop_clears_the_preview_and_writes_one_project_audit_line_that_says_why(
    db_session, seeded_ids
):
    project, build = _project_and_build(db_session, seeded_ids)

    record_preview_disk_stop(db_session, _stop(build))

    db_session.refresh(build)
    assert (build.preview_url, build.preview_expires_at, build.preview_token_hash) == (None, None, None)
    (entry,) = _audits(db_session, "preview.stopped_disk_limit")
    assert entry.actor_id is None
    assert entry.target == f"project:{project.id}"  # the target the project timeline already shows
    assert entry.metadata_json == {
        "build_id": build.id,
        "limit_bytes": LIMIT_MB * MIB,
        "used_bytes": LIMIT_MB * MIB + 5,
        "detail": "the preview was stopped because its container wrote more than the 8 MiB disk limit",
    }


def test_a_blind_stop_has_its_own_action_and_sentence(db_session, seeded_ids):
    project, build = _project_and_build(db_session, seeded_ids)

    record_preview_disk_stop(db_session, _stop(build, verdict="blind", used=None))

    (entry,) = _audits(db_session, "preview.stopped_disk_unreadable")
    assert entry.target == f"project:{project.id}"
    assert entry.metadata_json["used_bytes"] is None
    assert entry.metadata_json["detail"] == (
        "the preview was stopped because the container engine stopped reporting its disk use, "
        "and a limit that cannot be read cannot be enforced"
    )
    assert _audits(db_session, "preview.stopped_disk_limit") == []


def test_the_sentence_is_platform_text_with_numbers_only(db_session, seeded_ids):
    _, build = _project_and_build(db_session, seeded_ids)

    sentence = stop_detail(_stop(build))

    assert build.id not in sentence  # not even a build id: the timeline entry already sits on its project
    assert sentence.isascii()


def test_the_stop_shows_on_the_projects_timeline(client, db_session, seeded_ids):
    project, build = _project_and_build(db_session, seeded_ids)
    record_preview_disk_stop(db_session, _stop(build))

    timeline = client.get(
        f"/api/projects/{project.id}/timeline", headers={"X-Forge-User-Id": seeded_ids["user_id"]}
    ).json()

    lines = [e for e in timeline if e["kind"] == "audit_log" and e["data"]["action"] == "preview.stopped_disk_limit"]
    assert len(lines) == 1
    (build_entry,) = [e for e in timeline if e["kind"] == "build"]
    assert build_entry["data"]["preview_url"] is None  # and the link to the stopped preview is no longer offered


def test_a_build_with_no_row_is_not_an_error_and_writes_nothing(db_session):
    gone = PreviewDiskStop(build_id="no-such-build", project_id="p", verdict="over", used_bytes=1, limit_bytes=1)

    record_preview_disk_stop(db_session, gone)

    assert _audits(db_session, "preview.stopped_disk_limit") == []


def test_a_failed_commit_leaves_neither_the_cleared_columns_nor_the_entry_and_the_retry_writes_one(
    db_session, seeded_ids, monkeypatch
):
    """The commit that fails is the one that carries the audit entry. If the columns were cleared in a
    commit of their own first, they would stay cleared with no line to say why; one transaction leaves
    neither."""
    _, build = _project_and_build(db_session, seeded_ids)
    real_commit = db_session.commit
    failing = {"on": True}

    def commit_fails_while_the_entry_is_in_it():
        if failing["on"] and any(isinstance(o, AuditLog) for o in db_session.new):
            raise RuntimeError("database is locked")
        real_commit()

    monkeypatch.setattr(db_session, "commit", commit_fails_while_the_entry_is_in_it)
    stop = _stop(build)

    with pytest.raises(RuntimeError, match="database is locked"):
        record_preview_disk_stop(db_session, stop)
    db_session.refresh(build)
    assert build.preview_url is not None  # rolled back together
    assert _audits(db_session, "preview.stopped_disk_limit") == []

    failing["on"] = False
    record_preview_disk_stop(db_session, stop)  # the retry
    db_session.refresh(build)
    assert build.preview_url is None
    assert len(_audits(db_session, "preview.stopped_disk_limit")) == 1


# -- the runner: one thread, a record that is retried -----------------------------------------------------------


class CountingWatcher:
    """Stands in for `PreviewDiskWatcher`: counts polls, returns what it is told, sets `enough` at `after`."""

    def __init__(self, *, after=3, stops=(), fail_first=0):
        self.polls = 0
        self.enough = threading.Event()
        self._after = after
        self._stops = list(stops)
        self._fail_first = fail_first

    def poll_once(self):
        self.polls += 1
        if self.polls >= self._after:
            self.enough.set()
        if self.polls <= self._fail_first:
            raise RuntimeError("a cycle blew up")
        out, self._stops = self._stops, []
        return out


def _factory_for(db_session, monkeypatch):
    monkeypatch.setattr(db_session, "close", lambda: None)
    return lambda: db_session


def test_a_cycle_records_what_the_watcher_stopped(db_session, seeded_ids, monkeypatch):
    project, build = _project_and_build(db_session, seeded_ids)
    runner = PreviewDiskWatchRunner(CountingWatcher(stops=[_stop(build)]), _factory_for(db_session, monkeypatch))

    runner.run_cycle()

    db_session.refresh(build)
    assert build.preview_url is None
    assert len(_audits(db_session, "preview.stopped_disk_limit")) == 1


def test_a_record_that_could_not_be_written_is_kept_and_retried_every_cycle_never_lost(
    db_session, seeded_ids, monkeypatch
):
    _, build = _project_and_build(db_session, seeded_ids)
    healthy = {"db": False}

    def factory():
        if not healthy["db"]:
            raise RuntimeError("the database is down")
        return db_session

    monkeypatch.setattr(db_session, "close", lambda: None)
    runner = PreviewDiskWatchRunner(CountingWatcher(stops=[_stop(build)]), factory)

    runner.run_cycle()  # the preview is already gone and the record cannot be made
    runner.run_cycle()
    assert _audits(db_session, "preview.stopped_disk_limit") == []

    healthy["db"] = True
    runner.run_cycle()
    runner.run_cycle()  # and nothing is written twice

    assert len(_audits(db_session, "preview.stopped_disk_limit")) == 1


def test_a_record_that_fails_inside_a_working_session_is_retried_next_cycle_and_written_once(
    db_session, seeded_ids, monkeypatch
):
    """The other way a record is lost: the session opens, and the write itself raises (a locked database)."""
    _, build = _project_and_build(db_session, seeded_ids)
    monkeypatch.setattr(db_session, "close", lambda: None)
    real_commit = db_session.commit
    failures = []

    def commit_fails_once():
        if not failures:
            failures.append(1)
            raise RuntimeError("database is locked")
        real_commit()

    monkeypatch.setattr(db_session, "commit", commit_fails_once)
    runner = PreviewDiskWatchRunner(CountingWatcher(stops=[_stop(build)]), lambda: db_session)

    runner.run_cycle()  # the write fails: kept
    assert _audits(db_session, "preview.stopped_disk_limit") == []
    runner.run_cycle()  # retried, and it lands
    runner.run_cycle()  # and is not written again

    assert len(_audits(db_session, "preview.stopped_disk_limit")) == 1


def test_the_thread_polls_repeatedly_until_stopped_and_then_not_at_all(db_session, monkeypatch):
    watcher = CountingWatcher(after=3)
    runner = PreviewDiskWatchRunner(watcher, _factory_for(db_session, monkeypatch))

    runner.start()
    try:
        assert watcher.enough.wait(10), "the watcher thread did not poll three times"
        assert runner.alive
    finally:
        runner.stop()

    assert not runner.alive
    polls = watcher.polls
    threading.Event().wait(0.3)  # six intervals of nothing
    assert watcher.polls == polls


def test_a_cycle_that_raises_does_not_end_the_thread(db_session, monkeypatch, caplog):
    watcher = CountingWatcher(after=4, fail_first=2)
    runner = PreviewDiskWatchRunner(watcher, _factory_for(db_session, monkeypatch))

    with caplog.at_level("ERROR", logger="app.preview_disk_watch"):
        runner.start()
        try:
            assert watcher.enough.wait(10), "the thread stopped polling after a failed cycle"
        finally:
            runner.stop()

    assert watcher.polls >= 4
    assert sum("a cycle failed" in r.getMessage() for r in caplog.records) == 2
    assert not any("watcher thread ended" in r.getMessage() for r in caplog.records)  # a stop() is not a death


# -- where the worker starts it ---------------------------------------------------------------------------------


class _StopLoop(BaseException):
    """Leaves `main()`'s endless loop, as a `KeyboardInterrupt` would (the loop only catches `Exception`)."""


class _SandboxLikeExecutor:
    uses_engine = False
    starts_previews = True


def _drive_main(monkeypatch, executor, iteration, *, order=None):
    monkeypatch.setattr(worker, "assert_schema_at_head", lambda engine: None)
    monkeypatch.setattr(worker, "_select_executor", lambda: executor)
    monkeypatch.setattr(worker, "_log_disk_cap_startup_banner", lambda executor: None)
    monkeypatch.setattr(worker, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(worker, "_bind_instance_id", lambda: "forge-test-preview-watch")
    monkeypatch.setattr(worker, "_new_reconciler", lambda: None)
    monkeypatch.setattr(
        worker, "_recover_at_start", lambda reconciler, state, lease=None: order.append("recover") if order is not None else None
    )
    monkeypatch.setattr(worker, "run_iteration", iteration)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)


def _watch_threads():
    return [t for t in threading.enumerate() if t.name == "forge-preview-disk-watch"]


def test_the_watcher_keeps_polling_while_the_workers_own_thread_is_inside_a_build(monkeypatch):
    """The reason it is a thread. `run_iteration` is blocked for the whole build; a watcher driven by
    the loop's sweeps would not poll once in that time. Here the 'build' does not return until the watcher
    has polled three times, so a watcher that only runs between work items cannot get past it."""
    watcher = CountingWatcher(after=3)
    monkeypatch.setattr(preview_disk_watch, "PreviewDiskWatcher", lambda: watcher)
    observed: list[bool] = []

    def a_long_build(db, executor, **kwargs):
        # Always leaves the loop: an assertion raised HERE would be an `Exception`, which the worker
        # loop logs and survives, so a watcher that never polls would hang this test, not fail it.
        observed.append(watcher.enough.wait(10))
        raise _StopLoop

    _drive_main(monkeypatch, _SandboxLikeExecutor(), a_long_build)

    with pytest.raises(_StopLoop):
        worker.main()

    assert observed == [True], "the preview watcher did not poll three times while the build was running"
    assert watcher.polls >= 3
    assert _watch_threads() == []  # and main() stopped it on the way out, whatever way it left


def test_the_watcher_starts_after_the_start_up_recovery_and_before_the_first_claim(monkeypatch):
    order: list[str] = []

    class Recording(CountingWatcher):
        def __init__(self):
            super().__init__(after=1)
            order.append("watcher built")

    monkeypatch.setattr(preview_disk_watch, "PreviewDiskWatcher", Recording)

    def first_iteration(db, executor, **kwargs):
        order.append("iteration")
        raise _StopLoop

    _drive_main(monkeypatch, _SandboxLikeExecutor(), first_iteration, order=order)

    with pytest.raises(_StopLoop):
        worker.main()

    assert order == ["recover", "watcher built", "iteration"]


def test_an_executor_that_starts_no_previews_gets_no_watcher_and_no_docker(monkeypatch):
    from app.services.build_executor import PlaceholderBuildExecutor

    def must_not_be_built():
        pytest.fail("a worker that starts no previews must not build the preview watcher (or touch Docker)")

    monkeypatch.setattr(preview_disk_watch, "PreviewDiskWatcher", must_not_be_built)

    def one_iteration(db, executor, **kwargs):
        raise _StopLoop

    _drive_main(monkeypatch, PlaceholderBuildExecutor(), one_iteration)

    with pytest.raises(_StopLoop):
        worker.main()

    assert _watch_threads() == []


def test_a_watcher_that_cannot_be_built_is_an_error_in_the_log_and_not_a_dead_worker(monkeypatch, caplog):
    def cannot_connect():
        raise DockerException("no daemon")

    monkeypatch.setattr(preview_disk_watch, "PreviewDiskWatcher", cannot_connect)
    ran: list[int] = []

    def one_iteration(db, executor, **kwargs):
        ran.append(1)
        raise _StopLoop

    _drive_main(monkeypatch, _SandboxLikeExecutor(), one_iteration)

    with caplog.at_level("ERROR", logger="app.worker"):
        with pytest.raises(_StopLoop):
            worker.main()

    assert ran == [1]  # the worker went on
    assert any("previews are NOT being watched" in r.getMessage() for r in caplog.records)
