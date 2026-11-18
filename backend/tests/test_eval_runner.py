"""starting an eval run, and the worker step that drives
it -- the run budget with literal figures, one case at a time, resume after a restart, the
stuck-run check, judging wired to the build's own metered cost, and eval-project isolation.

No Docker, no network: the HTTP layer of the judge is a stub, builds are finished by hand the way
the worker's `mark_*` would leave them, and the one test that goes through `run_iteration` uses an
executor double. The engine-up run is `test_eval_run_integration.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.eval_run import EvalCase, EvalRun
from app.models.project import Project
from app.models.user import User
from app.services import eval_runner, project_lifecycle, usage
from app.services.build_executor import ExecutionResult
from app.services.eval_judge import ProbeResponse
from app.services.eval_runner import (
    STUCK_MARGIN_MINUTES,
    EvalStartRefused,
    advance_runs,
    case_cost_budget,
    has_room_for_a_case,
    remaining_budget,
    start_run,
)
from app.services.status import ProjectKind, ProjectStatus
from app.worker import run_iteration
from tests.eval_request_set_support import EVERYTHING, pin_v1_request_set


class RecordingProbes:
    """A probe factory: every probe answers 200 with `EVERYTHING` and records (build id, method, path)."""

    def __init__(self, response: ProbeResponse | None = None):
        self.response = response or ProbeResponse(200, EVERYTHING)
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, build):
        def probe(method, path):
            self.calls.append((build.id, method, path))
            return self.response

        return probe


@pytest.fixture()
def admin(db_session) -> User:
    return db_session.query(User).filter_by(role="admin").first()


@pytest.fixture(autouse=True)
def frozen_request_set(monkeypatch, tmp_path):
    """These tests are about the run, not about the shipped set's content (which grows by version):
    they read a frozen three-case set v1, and `EVERYTHING` satisfies every check of it."""
    pin_v1_request_set(monkeypatch, tmp_path)


@pytest.fixture(autouse=True)
def eval_settings(monkeypatch):
    """An executor is active and the `openai` engine is selected with its pinned model; the
    figures every budget test below reads are set here, in the open."""
    monkeypatch.setattr(settings, "use_sandbox_executor", True)
    monkeypatch.setattr(settings, "openai_api_key", "fake-key-never-used")
    monkeypatch.setattr(settings, "agent_engine_default", "openai")
    monkeypatch.setattr(settings, "openai_model", "gpt-5-mini")
    monkeypatch.setattr(settings, "eval_run_budget_usd", 1.0)
    monkeypatch.setattr(settings, "eval_case_min_budget_usd", 0.05)
    monkeypatch.setattr(settings, "build_cost_budget_usd", 5.0)
    monkeypatch.setattr(settings, "build_max_duration_minutes", 20)
    # pinned, because the ambient default is 1 and the run records what is in force.
    monkeypatch.setattr(settings, "build_repair_max_attempts", 1)


def _start(db, admin) -> EvalRun:
    run = start_run(db, admin=admin)
    db.commit()
    return run


def _cases(db, run) -> list[EvalCase]:
    db.expire_all()
    return list(db.query(EvalCase).filter_by(run_id=run.id).order_by(EvalCase.position))


def _finish(
    db, case: EvalCase, *, status="SUCCEEDED", cost=0.30, tokens=1000, reason=None, minutes=2, repairs=0
):
    """Leave the case's build the way the worker leaves a finished one: terminal status and the
    proxy's metered figures (`cost=None` is 'not measured'), and the repair attempts it ran."""
    build = db.get(Build, case.build_id)
    build.repair_attempts = repairs
    began = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    build.status = status
    build.failure_reason = reason
    build.cost_usd = cost
    build.cost_tokens = None if cost is None else tokens
    build.started_at = began
    build.ended_at = began + timedelta(minutes=minutes)
    db.commit()
    return build


def _step(db, probes=None, **kwargs):
    probes = probes or RecordingProbes()
    advance_runs(db, probe_factory=probes, **kwargs)
    return probes


# --- the budget, as arithmetic ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("budget", "finished", "expected"),
    [
        (1.0, [], 1.0),
        (1.0, [0.30], 0.70),
        (1.0, [0.30, 0.25], 0.45),
        (1.0, [0.0, 0.0], 1.0),  # a MEASURED zero is spend of zero, not "unknown"
        (1.0, [1.2], 0.0),  # an overshoot never leaves a negative remainder
        (1.0, [0.30, None], 0.0),  # an unmeasured case leaves nothing remaining
        (1.0, [None], 0.0),
    ],
)
def test_remaining_is_the_budget_minus_the_metered_cost_of_finished_cases(budget, finished, expected):
    assert remaining_budget(budget, finished) == pytest.approx(expected)


def test_the_case_budget_is_the_smaller_of_the_build_setting_and_what_remains():
    assert case_cost_budget(0.70, 5.0) == pytest.approx(0.70)  # capped by remaining
    assert case_cost_budget(0.70, 0.25) == pytest.approx(0.25)  # never above the normal build budget
    assert case_cost_budget(1.0, 5.0) == pytest.approx(1.0)


def test_the_floor_is_inclusive():
    assert has_room_for_a_case(0.05, 0.05) is True
    assert has_room_for_a_case(0.0499, 0.05) is False
    assert has_room_for_a_case(0.0, 0.05) is False


# --- starting ---------------------------------------------------------------------------------


def test_start_records_the_run_the_selection_the_budget_and_one_pending_case_per_request(db_session, admin):
    run = _start(db_session, admin)

    assert run.status == "RUNNING"
    assert (run.engine, run.model) == ("openai", "gpt-5-mini")
    assert run.request_set_version == 1
    assert run.budget_usd == 1.0 and run.spend_usd == 0.0
    assert run.started_by_id == admin.id
    cases = _cases(db_session, run)
    assert [(c.case_id, c.position, c.state) for c in cases] == [
        ("static-page", 0, "PENDING"),
        ("database-page", 1, "PENDING"),
        ("write-then-read-form", 2, "PENDING"),
    ]
    # Starting only records: no project, change request or build exists until the worker acts.
    assert db_session.query(Project).filter_by(kind="eval").count() == 0
    assert db_session.query(Build).count() == 0
    entry = db_session.query(AuditLog).filter_by(action="eval.run_started").one()
    assert entry.actor_id == admin.id and entry.target == f"eval_run:{run.id}"
    assert entry.metadata_json == {
        "engine": "openai",
        "model": "gpt-5-mini",
        "request_set_version": 1,
        "repair_max_attempts": 1,  # the audit entry now names the repair setting in force
        "budget_usd": 1.0,
    }


def test_start_records_the_fingerprint_of_the_prompt_a_create_build_would_get(db_session, admin):
    import hashlib

    from app.services.agent_prompt import compose_system_prompt

    run = _start(db_session, admin)

    expected = hashlib.sha256(compose_system_prompt("create").encode("utf-8")).hexdigest()[:12]
    assert run.prompt_fingerprint == expected
    assert len(run.prompt_fingerprint) == 12


@pytest.mark.parametrize("setting", [0, 1, 2, 3])
def test_start_records_the_effective_repair_setting_zero_included(db_session, admin, monkeypatch, setting):
    monkeypatch.setattr(settings, "build_repair_max_attempts", setting)

    run = _start(db_session, admin)

    assert run.repair_max_attempts == setting  # 0 is a recorded value (loop off), not "absent"
    entry = db_session.query(AuditLog).filter_by(action="eval.run_started").one()
    assert entry.metadata_json["repair_max_attempts"] == setting


def test_the_fingerprint_follows_the_composer_not_a_constant(db_session, admin, monkeypatch):
    from app.services import agent_prompt

    first = _start(db_session, admin)
    first.status = "COMPLETED"
    db_session.commit()
    same = _start(db_session, admin)
    same.status = "COMPLETED"
    db_session.commit()
    # Change what the composer emits (the real constant, not a stub of the function).
    monkeypatch.setattr(agent_prompt, "CODING_HYGIENE_GUIDANCE", agent_prompt.CODING_HYGIENE_GUIDANCE + " Extra.")
    different = _start(db_session, admin)

    assert first.prompt_fingerprint == same.prompt_fingerprint  # same prompt, same fingerprint
    assert different.prompt_fingerprint != first.prompt_fingerprint


def test_a_second_start_while_one_runs_is_refused_as_a_conflict(db_session, admin):
    _start(db_session, admin)

    with pytest.raises(EvalStartRefused) as excinfo:
        start_run(db_session, admin=admin)

    assert excinfo.value.kind == "conflict"


def test_the_database_itself_refuses_two_running_runs(db_session, admin):
    """The partial unique index, under the API's check: a second RUNNING row cannot be inserted."""
    from sqlalchemy.exc import IntegrityError

    _start(db_session, admin)
    db_session.add(
        EvalRun(
            status="RUNNING", engine="openai", model=None, request_set_version=1, budget_usd=1.0, started_by_id=admin.id
        )
    )

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_a_new_run_can_start_once_the_previous_one_has_ended(db_session, admin):
    first = _start(db_session, admin)
    first.status = "COMPLETED"
    db_session.commit()

    second = _start(db_session, admin)

    assert second.id != first.id and second.status == "RUNNING"


def test_start_is_refused_when_the_executor_is_inactive_or_the_engine_unavailable(db_session, admin, monkeypatch):
    monkeypatch.setattr(settings, "use_sandbox_executor", False)
    with pytest.raises(EvalStartRefused) as inactive:
        start_run(db_session, admin=admin)
    assert inactive.value.kind == "unavailable" and "USE_SANDBOX_EXECUTOR" in inactive.value.message

    monkeypatch.setattr(settings, "use_sandbox_executor", True)
    # `anthropic` is the selected engine but has no key, while `openai` does: there is no fallback.
    monkeypatch.setattr(settings, "agent_engine_default", "anthropic")
    with pytest.raises(EvalStartRefused) as unavailable:
        start_run(db_session, admin=admin)
    assert unavailable.value.kind == "unavailable" and "anthropic" in unavailable.value.message
    assert db_session.query(EvalRun).count() == 0


# --- one case at a time -----------------------------------------------------------------------


def test_the_worker_queues_the_first_case_as_an_eval_project_owned_by_the_admin(db_session, admin):
    run = _start(db_session, admin)

    _step(db_session)

    first, second, third = _cases(db_session, run)
    assert (first.state, second.state, third.state) == ("RUNNING", "PENDING", "PENDING")
    project = db_session.get(Project, first.project_id)
    assert project.name == f"eval-{run.id[:8]}-static-page"
    assert (project.kind, project.owner_id) == ("eval", admin.id)
    assert project.status == ProjectStatus.BUILD_PENDING
    build = db_session.get(Build, first.build_id)
    assert (build.project_id, build.type, build.status) == (project.id, "create", "QUEUED")
    request = db_session.query(ChangeRequest).filter_by(build_id=build.id).one()
    assert request.requester_id == admin.id
    assert request.nl_request.startswith("Change the home page. The route GET / must return an HTML page")
    # A budget of 1.0 left and a normal ceiling of 5.0: the build carries the smaller.
    assert build.cost_budget_usd == pytest.approx(1.0)


def test_the_next_case_is_not_queued_while_the_previous_build_is_unfinished(db_session, admin):
    run = _start(db_session, admin)
    _step(db_session)
    _step(db_session)
    _step(db_session)

    assert db_session.query(Build).count() == 1
    assert [c.state for c in _cases(db_session, run)] == ["RUNNING", "PENDING", "PENDING"]

    build = db_session.query(Build).one()
    build.status = "RUNNING"  # claimed, still going
    db_session.commit()
    _step(db_session)
    assert db_session.query(Build).count() == 1


def test_a_finished_case_is_judged_then_the_next_is_queued_with_the_remaining_budget(db_session, admin):
    run = _start(db_session, admin)
    probes = _step(db_session)
    first = _cases(db_session, run)[0]
    _finish(db_session, first, cost=0.30, tokens=1234)

    _step(db_session, probes)

    first, second, _ = _cases(db_session, run)
    assert (first.state, first.result, first.failure_reason) == ("DONE", "PASSED", None)
    assert first.cost_usd == pytest.approx(0.30) and first.cost_tokens == 1234
    assert first.duration_seconds == pytest.approx(120.0)
    assert [o["ok"] for o in first.check_outcomes] == [True, True]  # /healthz and the one check
    assert second.state == "RUNNING"
    assert db_session.get(Build, second.build_id).cost_budget_usd == pytest.approx(0.70)  # 1.0 - 0.30
    run = db_session.get(EvalRun, run.id)
    assert run.spend_usd == pytest.approx(0.30)
    # The judge asked the first build's preview for /healthz, then the check.
    assert [(m, p) for _, m, p in probes.calls] == [("GET", "/healthz"), ("GET", "/")]


def test_the_per_build_budget_is_capped_by_remaining_and_never_above_the_normal_budget(
    db_session, admin, monkeypatch
):
    monkeypatch.setattr(settings, "build_cost_budget_usd", 0.25)
    run = _start(db_session, admin)
    _step(db_session)
    first = _cases(db_session, run)[0]
    # 1.0 remaining, but a normal build only ever gets 0.25.
    assert db_session.get(Build, first.build_id).cost_budget_usd == pytest.approx(0.25)
    _finish(db_session, first, cost=0.10)

    _step(db_session)

    second = _cases(db_session, run)[1]
    # 0.90 remaining: still the smaller of the two is the setting.
    assert db_session.get(Build, second.build_id).cost_budget_usd == pytest.approx(0.25)


def test_a_normal_build_carries_no_budget_of_its_own(db_session, admin):
    """The per-build value is NULL for every build the eval runner did not make, i.e. the setting applies."""
    assert Build(project_id="p", type="create").cost_budget_usd is None


def test_below_the_floor_the_remaining_cases_are_not_run_and_the_run_stops_on_budget(db_session, admin):
    run = _start(db_session, admin)
    _step(db_session)
    _finish(db_session, _cases(db_session, run)[0], cost=0.96)  # 1.0 - 0.96 = 0.04 < 0.05

    _step(db_session)

    first, second, third = _cases(db_session, run)
    assert first.result == "PASSED"
    for case in (second, third):
        assert (case.state, case.result, case.failure_reason) == ("DONE", "NOT_RUN_BUDGET", "NOT_RUN_BUDGET")
        assert case.build_id is None and case.project_id is None
    run = db_session.get(EvalRun, run.id)
    assert run.status == "STOPPED_BUDGET" and run.ended_at is not None
    assert run.spend_usd == pytest.approx(0.96)
    assert db_session.query(Build).count() == 1  # nothing further was queued


def test_just_above_the_floor_another_case_still_runs_with_what_is_left(db_session, admin):
    run = _start(db_session, admin)
    _step(db_session)
    _finish(db_session, _cases(db_session, run)[0], cost=0.94)  # 0.06 left

    _step(db_session)

    second = _cases(db_session, run)[1]
    assert second.state == "RUNNING"
    assert db_session.get(Build, second.build_id).cost_budget_usd == pytest.approx(0.06)
    assert db_session.get(EvalRun, run.id).status == "RUNNING"


def test_a_finished_case_with_an_unmeasured_cost_leaves_nothing_so_the_run_stops(db_session, admin):
    run = _start(db_session, admin)
    _step(db_session)
    _finish(db_session, _cases(db_session, run)[0], cost=None)

    _step(db_session)

    first, second, third = _cases(db_session, run)
    assert first.cost_usd is None
    assert (second.result, third.result) == ("NOT_RUN_BUDGET", "NOT_RUN_BUDGET")
    assert db_session.get(EvalRun, run.id).status == "STOPPED_BUDGET"


def test_the_remaining_budget_is_read_from_the_builds_metered_cost_not_from_anything_the_engine_reports(
    db_session, admin
):
    """`Build.cost_usd` is the only figure: a different number elsewhere on the build (the
    token count, the failure detail, an agent summary) changes nothing."""
    run = _start(db_session, admin)
    _step(db_session)
    first = _cases(db_session, run)[0]
    build = _finish(db_session, first, cost=0.40, tokens=999_999)
    build.agent_summary = "I spent $0.01 and everything is fine"
    db_session.commit()

    _step(db_session)

    second = _cases(db_session, run)[1]
    assert db_session.get(Build, second.build_id).cost_budget_usd == pytest.approx(0.60)


def test_a_failed_build_is_recorded_with_its_code_and_its_cost_still_counts(db_session, admin):
    run = _start(db_session, admin)
    probes = _step(db_session)
    _finish(db_session, _cases(db_session, run)[0], status="FAILED", reason="BUDGET_EXCEEDED", cost=1.02)

    _step(db_session, probes)

    first, second, third = _cases(db_session, run)
    assert (first.result, first.failure_reason) == ("FAILED", "BUDGET_EXCEEDED")
    assert first.cost_usd == pytest.approx(1.02)
    assert probes.calls == []  # nothing was requested of a build that failed
    assert (second.result, third.result) == ("NOT_RUN_BUDGET", "NOT_RUN_BUDGET")  # 1.0 - 1.02 -> 0
    assert db_session.get(EvalRun, run.id).status == "STOPPED_BUDGET"


def test_a_run_whose_every_case_is_judged_completes(db_session, admin):
    run = _start(db_session, admin)
    probes = RecordingProbes()
    for cost in (0.10, 0.20, 0.15):
        _step(db_session, probes)
        running = [c for c in _cases(db_session, run) if c.state == "RUNNING"]
        assert len(running) == 1
        _finish(db_session, running[0], cost=cost)
    _step(db_session, probes)

    run = db_session.get(EvalRun, run.id)
    assert run.status == "COMPLETED" and run.ended_at is not None and run.detail is None
    assert run.spend_usd == pytest.approx(0.45)
    assert [c.result for c in _cases(db_session, run)] == ["PASSED"] * 3
    assert db_session.query(Build).count() == 3
    ended = db_session.query(AuditLog).filter_by(action="eval.run_ended").one()
    assert ended.metadata_json["status"] == "COMPLETED" and ended.metadata_json["passed"] == 3
    # Nothing is advanced once a run has ended.
    assert advance_runs(db_session, probe_factory=probes) == 0


def test_a_failing_check_marks_the_case_failed_with_the_check_index(db_session, admin):
    run = _start(db_session, admin)
    _step(db_session)
    _finish(db_session, _cases(db_session, run)[0], cost=0.10)

    _step(db_session, RecordingProbes(ProbeResponse(200, b"a page with none of the expected text")))

    first = _cases(db_session, run)[0]
    assert (first.result, first.failure_reason, first.failed_check_index) == ("FAILED", "CHECK_FAILED", 0)


# --- the run never switches engine, and the set must not change under it ------------------------


def test_a_run_ends_failed_rather_than_queue_a_case_on_a_different_engine(db_session, admin, monkeypatch):
    run = _start(db_session, admin)
    _step(db_session)
    _finish(db_session, _cases(db_session, run)[0], cost=0.10)
    monkeypatch.setattr(settings, "anthropic_api_key", "fake-anthropic-key")
    monkeypatch.setattr(settings, "agent_engine_default", "anthropic")  # the selection moves

    _step(db_session)

    run = db_session.get(EvalRun, run.id)
    assert run.status == "FAILED" and "never switches engine or model" in run.detail
    assert db_session.query(Build).count() == 1  # the second case was not queued


# --- the case records the repair attempts its build ran ---------------------------------


@pytest.mark.parametrize(
    ("status", "reason", "repairs", "probe_body", "outcome"),
    [
        ("SUCCEEDED", None, 2, None, ("PASSED", None)),
        ("SUCCEEDED", None, 1, b"a page with none of the expected text", ("FAILED", "CHECK_FAILED")),
        ("FAILED", "AGENT_ERROR", 3, None, ("FAILED", "AGENT_ERROR")),
        ("SUCCEEDED", None, 0, None, ("PASSED", None)),
    ],
    ids=["passed-after-2", "check-failed-after-1", "build-failed-after-3", "passed-with-none"],
)
def test_a_judged_case_copies_the_builds_repair_attempts_in_every_outcome(
    db_session, admin, status, reason, repairs, probe_body, outcome
):
    run = _start(db_session, admin)
    _step(db_session)
    _finish(db_session, _cases(db_session, run)[0], status=status, reason=reason, repairs=repairs)

    _step(db_session, RecordingProbes(ProbeResponse(200, probe_body)) if probe_body else None)

    first = _cases(db_session, run)[0]
    assert (first.result, first.failure_reason) == outcome
    assert first.repair_attempts == repairs  # 0 is stored as 0 (a measurement), never as NULL


def test_a_case_judged_as_engine_changed_still_records_its_attempts_like_its_cost(db_session, admin):
    run = _start(db_session, admin)
    _step(db_session)
    first = _cases(db_session, run)[0]
    _finish(db_session, first, cost=0.30, repairs=2)
    _record_engine(db_session, first, "anthropic", "claude-x")

    _step(db_session)

    first = _cases(db_session, run)[0]
    assert first.failure_reason == "ENGINE_CHANGED"
    assert first.repair_attempts == 2


def test_a_case_that_was_not_run_or_is_still_pending_or_running_has_no_attempts_not_zero(db_session, admin):
    run = _start(db_session, admin)
    _step(db_session)
    first = _cases(db_session, run)[0]
    assert first.state == "RUNNING"
    assert [c.repair_attempts for c in _cases(db_session, run)] == [None, None, None]  # queued, not built
    _finish(db_session, first, cost=0.96, repairs=1)  # leaves 0.04, under the floor

    _step(db_session)

    first, second, third = _cases(db_session, run)
    assert first.repair_attempts == 1
    assert (second.result, third.result) == ("NOT_RUN_BUDGET", "NOT_RUN_BUDGET")
    assert second.repair_attempts is None and third.repair_attempts is None


def test_a_case_in_flight_when_the_run_is_stuck_has_no_attempts(db_session, admin):
    run = _start(db_session, admin)
    _step(db_session)
    _age(db_session, run, 31)

    _step(db_session)

    first = _cases(db_session, run)[0]
    assert first.failure_reason == "RUN_STUCK" and first.repair_attempts is None


# --- the run stops rather than spend under a repair setting it did not record -------------


def test_a_run_whose_worker_runs_another_repair_setting_is_failed_before_any_build_is_queued(
    db_session, admin, monkeypatch
):
    run = _start(db_session, admin)  # recorded 1
    monkeypatch.setattr(settings, "build_repair_max_attempts", 0)  # the worker was restarted alone

    _step(db_session)

    run = db_session.get(EvalRun, run.id)
    assert run.status == "FAILED" and run.ended_at is not None
    assert "BUILD_REPAIR_MAX_ATTEMPTS=1" in run.detail and "running with 0" in run.detail
    assert "restart both" in run.detail.lower()
    assert db_session.query(Build).count() == 0  # nothing was queued under the wrong setting
    assert [c.state for c in _cases(db_session, run)] == ["PENDING", "PENDING", "PENDING"]


def test_the_drift_is_caught_before_the_next_case_and_the_finished_case_is_judged_first(
    db_session, admin, monkeypatch
):
    run = _start(db_session, admin)  # recorded 1
    _step(db_session)
    _finish(db_session, _cases(db_session, run)[0], cost=0.10, repairs=1)
    monkeypatch.setattr(settings, "build_repair_max_attempts", 3)

    _step(db_session)

    run = db_session.get(EvalRun, run.id)
    assert run.status == "FAILED" and "BUILD_REPAIR_MAX_ATTEMPTS=1" in run.detail and "running with 3" in run.detail
    first, second, _ = _cases(db_session, run)
    assert (first.state, first.result, first.repair_attempts) == ("DONE", "PASSED", 1)
    assert second.state == "PENDING"
    assert db_session.query(Build).count() == 1


def test_a_run_whose_worker_agrees_queues_the_next_case(db_session, admin):
    run = _start(db_session, admin)
    _step(db_session)
    _finish(db_session, _cases(db_session, run)[0], cost=0.10)

    _step(db_session)

    run = db_session.get(EvalRun, run.id)
    assert run.status == "RUNNING" and run.detail is None
    assert [c.state for c in _cases(db_session, run)] == ["DONE", "RUNNING", "PENDING"]
    assert db_session.query(Build).count() == 2


def test_a_run_with_no_recorded_setting_is_never_guarded(db_session, admin, monkeypatch):
    """A run that predates the recorded setting recorded nothing: NULL is not 0, so no worker setting contradicts it."""
    run = _start(db_session, admin)
    run.repair_max_attempts = None
    db_session.commit()
    monkeypatch.setattr(settings, "build_repair_max_attempts", 1)  # would differ from a NULL read as 0

    _step(db_session)

    run = db_session.get(EvalRun, run.id)
    assert run.status == "RUNNING" and run.detail is None
    assert db_session.query(Build).count() == 1 and _cases(db_session, run)[0].state == "RUNNING"


def test_the_drift_check_does_not_touch_a_run_that_has_already_finished(db_session, admin, monkeypatch):
    run = _start(db_session, admin)
    run.status = "COMPLETED"
    db_session.commit()
    monkeypatch.setattr(settings, "build_repair_max_attempts", 3)

    assert advance_runs(db_session, probe_factory=RecordingProbes()) == 0

    run = db_session.get(EvalRun, run.id)
    assert run.status == "COMPLETED" and run.detail is None


# --- a case is never reported under an engine it did not run on --------------------------------


def _record_engine(db, case, engine, model):
    build = db.get(Build, case.build_id)
    build.engine, build.model = engine, model
    db.commit()


def test_a_case_whose_build_ran_on_another_engine_fails_as_engine_changed_and_is_not_judged(db_session, admin):
    """The admin switched engines after the case was queued and before the worker claimed it."""
    run = _start(db_session, admin)  # openai / gpt-5-mini
    probes = _step(db_session)
    first = _cases(db_session, run)[0]
    _finish(db_session, first, cost=0.30, tokens=900)
    _record_engine(db_session, first, "anthropic", "claude-x")

    _step(db_session, probes)

    first = _cases(db_session, run)[0]
    assert (first.state, first.result, first.failure_reason) == ("DONE", "FAILED", "ENGINE_CHANGED")
    assert probes.calls == []  # nothing was asked of a preview that was not the run's engine's
    assert first.cost_usd == pytest.approx(0.30) and first.cost_tokens == 900  # real spend still counts
    assert db_session.get(EvalRun, run.id).spend_usd == pytest.approx(0.30)


@pytest.mark.parametrize(
    ("build_engine", "build_model", "expected"),
    [
        ("openai", "gpt-5-mini", True),  # the run's own engine and model
        ("openai", "gpt-5", False),  # same engine, another model
        ("anthropic", "gpt-5-mini", False),  # another engine, even with the same model string
        ("anthropic", None, False),
        (None, None, True),  # nothing recorded: not a difference
        ("openai", None, True),  # recorded at a claim that failed (engine unavailable): same engine
    ],
)
def test_ran_on_the_runs_engine_compares_engine_and_model(build_engine, build_model, expected):
    run = EvalRun(engine="openai", model="gpt-5-mini")
    build = Build(project_id="p", type="create", engine=build_engine, model=build_model)

    assert eval_runner.ran_on_the_runs_engine(run, build) is expected


def test_a_build_that_ran_on_the_runs_engine_is_judged_as_before(db_session, admin):
    run = _start(db_session, admin)
    probes = _step(db_session)
    first = _cases(db_session, run)[0]
    _finish(db_session, first, cost=0.30)
    _record_engine(db_session, first, "openai", "gpt-5-mini")

    _step(db_session, probes)

    first = _cases(db_session, run)[0]
    assert (first.result, first.failure_reason) == ("PASSED", None)
    assert len(probes.calls) == 2


def test_a_claim_that_failed_for_an_unavailable_engine_keeps_the_builds_own_failure_code(db_session, admin):
    run = _start(db_session, admin)
    _step(db_session)
    first = _cases(db_session, run)[0]
    _finish(db_session, first, status="FAILED", cost=None, reason="ENGINE_UNREACHABLE")
    _record_engine(db_session, first, "openai", None)  # what `assign_engine` writes when the key is gone

    _step(db_session)

    first = _cases(db_session, run)[0]
    assert first.failure_reason == "ENGINE_UNREACHABLE"


def test_a_run_ends_failed_when_the_request_set_version_changed(db_session, admin):
    from app.services.eval_request_set import RequestSet

    run = _start(db_session, admin)

    advance_runs(db_session, request_set=RequestSet(version=2, cases=()), probe_factory=RecordingProbes())

    run = db_session.get(EvalRun, run.id)
    assert run.status == "FAILED" and "version 1" in run.detail and "version 2" in run.detail


# --- resume after a restart -------------------------------------------------------------------


class _Crash(BaseException):
    """Not an `Exception`: models the process dying in the middle of a step."""


def test_a_restarted_worker_resumes_from_the_rows_and_no_case_is_judged_twice(db_session, admin):
    run = _start(db_session, admin)
    _step(db_session)
    _finish(db_session, _cases(db_session, run)[0], cost=0.10)

    # The worker dies while judging the first case: the probe is reached, the process is gone
    # before anything is committed.
    def dying_factory(build):
        def probe(method, path):
            raise _Crash()

        return probe

    with pytest.raises(_Crash):
        advance_runs(db_session, probe_factory=dying_factory)
    db_session.rollback()
    assert [c.state for c in _cases(db_session, run)] == ["RUNNING", "PENDING", "PENDING"]  # nothing was recorded

    # A new process: a new session on the same database, nothing in memory.
    restarted = sessionmaker(bind=db_session.get_bind())()
    try:
        probes = RecordingProbes()
        advance_runs(restarted, probe_factory=probes)  # judges case 0 once, queues case 1
        judged_once = list(probes.calls)
        assert [(m, p) for _, m, p in judged_once] == [("GET", "/healthz"), ("GET", "/")]
        assert len({b for b, _, _ in judged_once}) == 1

        advance_runs(restarted, probe_factory=probes)  # nothing new to judge: case 1's build is queued
        advance_runs(restarted, probe_factory=probes)

        assert probes.calls == judged_once  # case 0 was not judged again
        states = [(c.case_id, c.state, c.result) for c in _cases(restarted, run)]
        assert states == [
            ("static-page", "DONE", "PASSED"),
            ("database-page", "RUNNING", None),
            ("write-then-read-form", "PENDING", None),
        ]
        assert restarted.query(Build).count() == 2  # one build per started case, never a duplicate
    finally:
        restarted.close()


# --- stuck runs -------------------------------------------------------------------------------


def _age(db, run, minutes):
    run = db.get(EvalRun, run.id)
    run.last_progress_at = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    db.commit()


def test_a_run_with_no_progress_past_the_build_timeout_plus_a_margin_is_failed(db_session, admin):
    assert STUCK_MARGIN_MINUTES == 10
    run = _start(db_session, admin)
    _step(db_session)
    _age(db_session, run, 31)  # 20-minute build timeout + 10-minute margin = 30

    _step(db_session)

    run = db_session.get(EvalRun, run.id)
    assert run.status == "FAILED" and run.ended_at is not None
    assert "20 minutes" in run.detail and "10-minute margin" in run.detail
    first = _cases(db_session, run)[0]
    assert (first.state, first.result, first.failure_reason) == ("DONE", "FAILED", "RUN_STUCK")
    assert [c.state for c in _cases(db_session, run)[1:]] == ["PENDING", "PENDING"]


def test_a_run_inside_the_window_is_not_failed(db_session, admin):
    run = _start(db_session, admin)
    _step(db_session)
    _age(db_session, run, 29)

    _step(db_session)

    assert db_session.get(EvalRun, run.id).status == "RUNNING"


def test_time_waiting_behind_other_builds_does_not_count_once_the_build_has_started(db_session, admin):
    run = _start(db_session, admin)
    _step(db_session)
    _age(db_session, run, 45)
    build = db_session.query(Build).one()
    build.status = "RUNNING"
    build.started_at = datetime.now(timezone.utc) - timedelta(minutes=5)  # claimed five minutes ago
    db_session.commit()

    _step(db_session)

    assert db_session.get(EvalRun, run.id).status == "RUNNING"


# --- through the worker -----------------------------------------------------------------------


class _MeteringExecutor:
    """What the real executor does to the build, in miniature: it writes the proxy's cost onto
    the build and reports success; the worker's `mark_succeeded` does the rest."""

    def __init__(self, cost):
        self.cost, self.seen = cost, []

    def execute(self, build):
        self.seen.append((build.project_id, build.cost_budget_usd))
        build.cost_usd = self.cost
        build.cost_tokens = 500
        return ExecutionResult(ok=True, summary="done", agent_summary="the agent says hello")


def test_run_iteration_drives_a_run_through_the_normal_build_queue(db_session, admin):
    run = _start(db_session, admin)
    executor, probes = _MeteringExecutor(cost=0.30), RecordingProbes()

    first_tick = run_iteration(db_session, executor, eval_probe_factory=probes)  # queues case 0, claims and runs it
    second_tick = run_iteration(db_session, executor, eval_probe_factory=probes)  # judges it, queues and runs case 1

    assert first_tick is not None and second_tick is not None
    assert [budget for _, budget in executor.seen] == [pytest.approx(1.0), pytest.approx(0.70)]
    first, second, third = _cases(db_session, run)
    assert (first.state, first.result) == ("DONE", "PASSED")
    assert second.state == "RUNNING" and third.state == "PENDING"
    assert db_session.get(EvalRun, run.id).spend_usd == pytest.approx(0.30)
    # Both builds ran as the eval projects' own builds, each in a project of its own.
    assert len({p for p, _ in executor.seen}) == 2


# --- isolation --------------------------------------------------------------------------------


def _eval_project_with_cost(db, admin, cost, month_start):
    run = _start(db, admin)
    _step(db)
    case = _cases(db, run)[0]
    build = db.get(Build, case.build_id)
    build.status, build.cost_usd = "SUCCEEDED", cost
    build.ended_at = month_start + timedelta(days=1)
    db.commit()
    return db.get(Project, case.project_id), build


def test_an_eval_projects_cost_counts_in_the_usage_rollups_for_the_project_and_the_admin(db_session, admin):
    window = usage.resolve_month_window("2026-07")

    project, _ = _eval_project_with_cost(db_session, admin, 0.42, window.starts_at)

    project_line = usage.project_usage_for(db_session, window, project)
    assert project_line.spend_usd == pytest.approx(0.42) and project_line.build_count == 1
    admin_line = usage.user_usage_for(db_session, window, admin)
    assert admin_line.spend_usd == pytest.approx(0.42)
    assert usage.platform_usage(db_session, window).total_spend_usd == pytest.approx(0.42)
    assert [l.subject_id for l in usage.project_usage(db_session, window)].count(project.id) == 1


def test_the_zombie_sweep_never_selects_an_eval_project(db_session, admin, seeded_ids):
    run = _start(db_session, admin)
    _step(db_session)
    eval_project = db_session.get(Project, _cases(db_session, run)[0].project_id)
    normal = Project(
        name="long-idle-normal",
        owner_id=admin.id,
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
    )
    db_session.add(normal)
    db_session.flush()
    long_ago = datetime.now(timezone.utc) - timedelta(days=400)
    eval_project.last_active_at = long_ago
    normal.last_active_at = long_ago
    db_session.commit()

    candidates = project_lifecycle.idle_candidates(db_session, cutoff=datetime.now(timezone.utc) - timedelta(days=90))

    assert [p.id for p in candidates] == [normal.id]  # the same idleness, but only the normal one
    assert eval_project.kind == ProjectKind.EVAL


def test_an_eval_run_falls_back_to_an_existing_team_for_an_admin_with_none(db_session, admin):
    """A local-mode bootstrap admin has no team; `projects.team_id` is NOT NULL."""
    admin.team_id = None
    db_session.commit()
    run = _start(db_session, admin)

    _step(db_session)

    project = db_session.get(Project, _cases(db_session, run)[0].project_id)
    assert project.team_id is not None


# --- settings ---------------------------------------------------------------------------------


def test_the_eval_settings_default_to_the_owners_values_and_are_documented():
    from pathlib import Path

    from app.config import Settings

    assert Settings.model_fields["eval_run_budget_usd"].default == 1.0
    assert Settings.model_fields["eval_case_min_budget_usd"].default == 0.05
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    assert "# EVAL_RUN_BUDGET_USD=1.0" in example
    assert "# EVAL_CASE_MIN_BUDGET_USD=0.05" in example
