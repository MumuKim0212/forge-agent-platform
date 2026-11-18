"""the executor MEASURES the agent's change set and records it on the build.

Unit seam, no Docker: the `Harness` of the harvest tests. The measurement is
between the staged INPUT tree and the sanitised tree, before protected-path restore
(so an attempted Dockerfile edit is in it) and before the schema refusal (so a
refused modify build carries what it tried to change).
"""

from __future__ import annotations


from app.config import settings
from app.services import sandbox_build_executor as sbe
from app.services.build_queue import mark_failed
from app.services.status import BuildFailureReason
from tests.test_sandbox_build_executor_harvest import Harness
from tests.test_sandbox_build_executor_schema import CODE_ONLY, INPUT, MARKER, SCHEMA_CHANGES

PINNED_DOCKERFILE = b"FROM pinned\n"


def test_b1_a_successful_build_persists_the_change_set_and_the_drift(monkeypatch, make_build_with_request):
    h = Harness(monkeypatch, output_files=CODE_ONLY, staged_files=INPUT)
    h.publish_drift = ["Dockerfile"]  # what restore reported, through the recorder
    build = make_build_with_request(build_type="modify")

    result = h.executor.execute(build)

    assert result.ok is True, result.summary
    assert build.changed_files == {
        "entries": [
            {"path": "app/app.py", "change": "modified"},
            {"path": "app/feature.py", "change": "added"},
        ],
        "totals": {"added": 1, "modified": 1, "deleted": 0},
        "truncated": False,
    }
    assert build.protected_path_drift == ["Dockerfile"]


def test_b1_a_build_that_changed_nothing_records_an_empty_measurement_not_null(monkeypatch, make_build_with_request):
    h = Harness(monkeypatch, output_files=INPUT, staged_files=INPUT)
    build = make_build_with_request(build_type="modify")

    assert h.executor.execute(build).ok is True

    assert build.changed_files == {
        "entries": [],
        "totals": {"added": 0, "modified": 0, "deleted": 0},
        "truncated": False,
    }
    assert build.protected_path_drift == []


def test_b1_the_adapters_own_dot_forge_and_git_are_not_counted_as_changes(monkeypatch, make_build_with_request):
    """The staged input carries `.forge/` (the adapter wrote it); the sanitised output has
    none. That is not a deletion."""
    staged = {**INPUT, ".forge/task_input.json": b"{}"}
    h = Harness(monkeypatch, output_files={**INPUT, ".git/HEAD": b"ref\n"}, staged_files=staged)
    build = make_build_with_request(build_type="modify")

    assert h.executor.execute(build).ok is True

    assert build.changed_files["entries"] == []


def test_b1_an_edit_to_the_dockerfile_is_in_the_change_set_and_in_drift_and_the_pinned_copy_is_pushed(
    monkeypatch, make_build_with_request, tmp_path
):
    """(a2) through the real executor, real sanitiser, real publish (restore included) and
    a real local bare repo."""
    import subprocess

    from tests.test_publish import LocalBareRepoProvider, _init_bare_repo

    bare = tmp_path / "remote.git"
    _init_bare_repo(bare)
    provider = LocalBareRepoProvider(bare)
    pinned = (settings_skeleton() / "Dockerfile").read_bytes()
    staged = {**INPUT, "Dockerfile": pinned}  # a confirmed ref carries the pinned manifest
    output = {**CODE_ONLY, "Dockerfile": b"FROM evil\n"}  # the agent edited it
    h = Harness(monkeypatch, output_files=output, staged_files=staged, git_provider=provider)
    monkeypatch.setattr(sbe, "assemble_image", lambda **k: "tag")
    build = make_build_with_request(build_type="modify")

    result = h.executor.execute(build)

    assert result.ok is True, result.summary
    assert {"path": "Dockerfile", "change": "modified"} in build.changed_files["entries"]
    assert build.protected_path_drift == ["Dockerfile"]
    sha = provider.resolve_ref(repo=provider.create_repo(name="x"), branch=f"forge-build-{build.id}")
    pushed = subprocess.run(
        ["git", "show", f"{sha}:Dockerfile"], cwd=bare, check=True, capture_output=True
    ).stdout
    assert b"evil" not in pushed
    # Unchanged: the pushed Dockerfile is the pinned one (git may normalise line endings).
    assert pushed.replace(b"\r\n", b"\n") == pinned.replace(b"\r\n", b"\n")


def settings_skeleton():
    from pathlib import Path

    return Path(settings.template_skeleton_dir)


def test_b2_a_schema_refused_modify_build_still_persists_its_changed_files_and_names_none_in_the_detail(
    monkeypatch, make_build_with_request, db_session
):
    h = Harness(monkeypatch, output_files=SCHEMA_CHANGES["adds-a-migration"], staged_files=INPUT)
    build = make_build_with_request(build_type="modify")

    result = h.executor.execute(build)

    assert result.ok is False and result.reason == BuildFailureReason.AGENT_ERROR
    paths = {e["path"]: e["change"] for e in build.changed_files["entries"]}
    assert paths[f"app/migrations/versions/{MARKER}_add_column.py"] == "added"
    assert paths["app/app.py"] == "modified"
    assert build.changed_files["totals"] == {"added": 2, "modified": 1, "deleted": 0}
    assert build.protected_path_drift == []
    assert h.publish_calls == []  # still refused before anything was published
    # the platform-authored detail holds no file name the agent chose.
    mark_failed(db_session, build, reason=result.reason, detail=result.summary)
    assert MARKER not in build.failure_detail
    assert "feature.py" not in build.failure_detail and "app.py" not in build.failure_detail


def test_b2_a_refused_build_that_also_edited_the_dockerfile_records_the_drift_it_tried(
    monkeypatch, make_build_with_request
):
    pinned = (settings_skeleton() / "Dockerfile").read_bytes()
    h = Harness(
        monkeypatch,
        output_files={**SCHEMA_CHANGES["edits-a-migration"], "Dockerfile": b"FROM evil\n"},
        staged_files={**INPUT, "Dockerfile": pinned},
    )
    build = make_build_with_request(build_type="modify")

    result = h.executor.execute(build)

    assert result.ok is False
    assert build.protected_path_drift == ["Dockerfile"]
    assert {"path": "Dockerfile", "change": "modified"} in build.changed_files["entries"]


def test_b3_a_build_that_fails_before_harvest_leaves_both_fields_null(monkeypatch, make_build_with_request):
    from tests.test_sandbox_build_executor_usage import _usage

    h = Harness(monkeypatch, usage=_usage(tokens=7, cost_usd=99.0, budget_exceeded=True))
    build = make_build_with_request(build_type="modify")

    result = h.executor.execute(build)

    assert result.ok is False and result.reason == BuildFailureReason.BUDGET_EXCEEDED
    assert build.changed_files is None
    assert build.protected_path_drift is None
    assert h.sanitise_calls == []


def test_b3_a_harvest_that_never_produced_output_leaves_both_fields_null(monkeypatch, make_build_with_request):
    h = Harness(monkeypatch, make_output=False)
    build = make_build_with_request()

    result = h.executor.execute(build)

    assert result.ok is False and result.reason == BuildFailureReason.HARVEST_ERROR
    assert build.changed_files is None and build.protected_path_drift is None


def test_a_measurement_that_cannot_read_a_file_fails_closed_naming_only_the_stage(
    monkeypatch, make_build_with_request
):
    h = Harness(monkeypatch, output_files=CODE_ONLY, staged_files=INPUT)

    def _boom(**k):
        raise OSError("C:\\secret\\path\\the-l3-chose: unreadable")

    monkeypatch.setattr(sbe, "measure_changes", _boom)
    build = make_build_with_request(build_type="modify")

    result = h.executor.execute(build)

    assert result.ok is False and result.reason == BuildFailureReason.HARVEST_ERROR
    assert result.summary == (
        "harvesting the build's output failed at measuring the agent's changes (OSError); see the worker log"
    )
    assert build.changed_files is None and build.protected_path_drift is None
    assert h.publish_calls == []


def test_the_paths_never_reach_the_log_or_the_commit_text(monkeypatch, make_build_with_request, caplog):
    name = "app/AGENT-CHOSEN-PATH-5d1e.py"
    h = Harness(monkeypatch, output_files={**CODE_ONLY, name: b"x\n"}, staged_files=INPUT)
    build = make_build_with_request(build_type="modify")

    with caplog.at_level("DEBUG"):
        result = h.executor.execute(build)

    assert result.ok is True
    assert "AGENT-CHOSEN-PATH-5d1e" not in (result.agent_summary or "")
    assert "AGENT-CHOSEN-PATH-5d1e" not in caplog.text
