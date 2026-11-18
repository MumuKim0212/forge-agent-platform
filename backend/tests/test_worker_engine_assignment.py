"""ADR 0035 §Decision 5: the worker decides a build's engine when it CLAIMS it.

Driven through `run_iteration` with a recording executor that says it runs an engine
(`uses_engine`), over the in-memory DB. No Docker, no sandbox: what is asserted is the order
of events (claim -> resolve -> record -> execute) and what a build looks like when the selected
engine is not available.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.project import Project
from app.services.build_executor import ExecutionResult, PlaceholderBuildExecutor
from app.services.status import BuildFailureReason, BuildStatus, ProjectStatus
from app.worker import _select_executor, run_iteration


class RecordingExecutor:
    """Stands in for `SandboxBuildExecutor`: it runs an engine, and records what the build
    carried at the moment it was handed over."""

    uses_engine = True

    def __init__(self) -> None:
        self.seen: list[tuple[str, str | None, str | None]] = []

    def execute(self, build: Build) -> ExecutionResult:
        self.seen.append((build.id, build.engine, build.model))
        return ExecutionResult(ok=True, summary="ran")


def _queue_build(db_session, seeded_ids, name: str) -> Build:
    project = Project(
        name=name,
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    build = Build(project_id=project.id, type="create", status=BuildStatus.QUEUED)
    db_session.add(build)
    db_session.commit()
    return build


def _keys(monkeypatch, *, anthropic=None, openai=None, default=None, model="gpt-5-mini"):
    monkeypatch.setattr(settings, "anthropic_api_key", anthropic)
    monkeypatch.setattr(settings, "openai_api_key", openai)
    monkeypatch.setattr(settings, "agent_engine_default", default)
    monkeypatch.setattr(settings, "openai_model", model)


def test_the_engine_and_model_are_recorded_on_the_build_before_the_executor_sees_it(
    db_session, seeded_ids, monkeypatch
):
    _keys(monkeypatch, anthropic="a", openai="o", default="openai", model="gpt-5-nano")
    build = _queue_build(db_session, seeded_ids, "p1")
    executor = RecordingExecutor()

    ran = run_iteration(db_session, executor)

    assert ran.id == build.id
    assert executor.seen == [(build.id, "openai", "gpt-5-nano")]
    db_session.refresh(build)
    assert (build.engine, build.model, build.status) == ("openai", "gpt-5-nano", BuildStatus.SUCCEEDED)


def test_an_anthropic_build_records_the_engine_and_no_model(db_session, seeded_ids, monkeypatch):
    _keys(monkeypatch, anthropic="a", openai="o", default="anthropic")
    build = _queue_build(db_session, seeded_ids, "p2")
    executor = RecordingExecutor()

    run_iteration(db_session, executor)

    assert executor.seen == [(build.id, "anthropic", None)]


def test_the_engine_is_resolved_per_claim_so_a_changed_selection_applies_to_the_next_build(
    db_session, seeded_ids, monkeypatch
):
    """The point of resolving at claim, not at startup: ONE executor instance (as the worker
    holds for its lifetime) runs two builds across a change of selection, and each carries the
    engine selected when it was claimed."""
    _keys(monkeypatch, anthropic="a", openai="o", default="anthropic")
    first = _queue_build(db_session, seeded_ids, "p3a")
    executor = RecordingExecutor()
    run_iteration(db_session, executor)

    monkeypatch.setattr(settings, "agent_engine_default", "openai")
    second = _queue_build(db_session, seeded_ids, "p3b")
    run_iteration(db_session, executor)

    assert executor.seen == [(first.id, "anthropic", None), (second.id, "openai", "gpt-5-mini")]


def test_a_finished_build_is_never_switched(db_session, seeded_ids, monkeypatch):
    _keys(monkeypatch, anthropic="a", openai="o", default="anthropic")
    build = _queue_build(db_session, seeded_ids, "p4")
    executor = RecordingExecutor()
    run_iteration(db_session, executor)

    monkeypatch.setattr(settings, "agent_engine_default", "openai")
    run_iteration(db_session, executor)  # nothing queued: nothing to claim

    db_session.refresh(build)
    assert build.engine == "anthropic"


# --- the selected engine is not available: fail before anything starts, never fall back -------


def test_an_unavailable_selected_engine_fails_the_build_before_the_executor_is_reached(
    db_session, seeded_ids, monkeypatch
):
    _keys(monkeypatch, anthropic="a-key-that-must-not-be-used", openai=None, default="openai")
    build = _queue_build(db_session, seeded_ids, "p5")
    executor = RecordingExecutor()

    ran = run_iteration(db_session, executor)

    assert ran.id == build.id
    assert executor.seen == []  # no sandbox, proxy or token was ever started for it
    db_session.refresh(build)
    assert build.status == BuildStatus.FAILED
    assert build.failure_reason == BuildFailureReason.ENGINE_UNREACHABLE
    assert "openai" in build.failure_detail and "OPENAI_API_KEY" in build.failure_detail
    assert "not run on another engine" in build.failure_detail
    assert "a-key-that-must-not-be-used" not in build.failure_detail
    assert build.engine == "openai" and build.model is None  # it names the engine it was meant for


def test_the_unavailable_failure_is_audited_with_the_same_detail(db_session, seeded_ids, monkeypatch):
    _keys(monkeypatch, anthropic="a", openai=None, default="openai")
    build = _queue_build(db_session, seeded_ids, "p6")

    run_iteration(db_session, RecordingExecutor())

    db_session.refresh(build)
    failed = db_session.query(AuditLog).filter_by(action="build.failed", target=f"build:{build.id}").one()
    assert failed.metadata_json["reason"] == "ENGINE_UNREACHABLE"
    assert failed.metadata_json["detail"] == build.failure_detail


def test_the_other_engine_is_never_tried_when_the_selected_one_is_unavailable(db_session, seeded_ids, monkeypatch):
    """Both directions: it is the keyed engine that must NOT run the build."""
    for selected, keyed in (("openai", "anthropic"), ("anthropic", "openai")):
        _keys(
            monkeypatch,
            anthropic="a" if keyed == "anthropic" else None,
            openai="o" if keyed == "openai" else None,
            default=selected,
        )
        build = _queue_build(db_session, seeded_ids, f"p7-{selected}")
        executor = RecordingExecutor()

        run_iteration(db_session, executor)

        db_session.refresh(build)
        assert executor.seen == [], selected
        assert (build.status, build.engine) == (BuildStatus.FAILED, selected)


# --- executors that run no engine are untouched ---------------------------------------------


def test_the_placeholder_executor_never_has_an_engine_assigned(db_session, seeded_ids, monkeypatch):
    _keys(monkeypatch)  # nothing configured at all: an engine decision here would fail the build
    build = _queue_build(db_session, seeded_ids, "p8")

    run_iteration(db_session, PlaceholderBuildExecutor())

    db_session.refresh(build)
    assert build.status == BuildStatus.SUCCEEDED
    assert (build.engine, build.model) == (None, None)


# --- the executor choice at startup is engine-neutral -----------------------------------------


@pytest.mark.parametrize(
    ("flag", "anthropic", "openai", "sandboxed"),
    [
        (True, None, "o", True),
        (True, "a", None, True),
        (True, None, None, False),
        (False, "a", "o", False),
    ],
)
def test_select_executor_needs_the_flag_and_at_least_one_engine(monkeypatch, flag, anthropic, openai, sandboxed):
    from app.services import sandbox_build_executor as sbe

    class _Stub:
        uses_engine = True

    monkeypatch.setattr(sbe, "SandboxBuildExecutor", _Stub)
    monkeypatch.setattr(settings, "use_sandbox_executor", flag)
    _keys(monkeypatch, anthropic=anthropic, openai=openai)

    assert isinstance(_select_executor(), _Stub) is sandboxed
