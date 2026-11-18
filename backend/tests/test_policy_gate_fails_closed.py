"""Policy gate rule "the gate as a whole fails closed":
an exception anywhere in the gate -- the checks, the persistence of their verdict, the approval
routing -- leaves a platform-authored gate-error verdict at the highest tier, routed to human
approval, and no half-written rows.

The real `SandboxBuildExecutor._publish` -> `_run_policy_gate` path, on the in-memory DB, with only
the external effects (git host, image build, preview) stubbed -- the same seam as
`test_ambiguous_requester.py`. Nothing that matters here is faked: the gate, the savepoint, the
approval routing and the DB constraints are the real ones, and each injected fault is a REAL
failure of the real code (an exception raised after check rows were added; a NOT NULL violation
that the database itself refuses at flush, which is the one that poisons a session). No Docker.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models.approval import ApprovalRequest
from app.models.audit import AuditLog
from app.models.policy_verdict import PolicyCheckResult, PolicyVerdict
from app.models.project import Project
from app.models.user import User
from app.services import policy_gate
from app.services import sandbox_build_executor as sbe
from app.services.policy_checks import CheckResult, PolicyCheckName, PolicyCheckStatus, PolicyVerdictResult
from app.services.status import BuildFailureReason, BuildStatus, ProjectKind, ProjectStatus
from app.worker import run_one_build
from tests.entrypoint_support import place_entrypoint


def _executor(monkeypatch):
    executor = sbe.SandboxBuildExecutor(docker_client=object(), git_provider=object())
    monkeypatch.setattr(
        sbe,
        "publish_build",
        lambda **k: SimpleNamespace(diff_ref="branch#1", drift=[], branch="branch", pr=None),
    )
    monkeypatch.setattr(sbe, "assemble_image", lambda **k: None)
    monkeypatch.setattr(executor, "_ensure_repo", lambda *a, **k: object())
    monkeypatch.setattr(executor, "_base_branch_for", lambda *a, **k: "main")
    monkeypatch.setattr(executor, "_start_preview", lambda build: "")
    return executor


def _publish(executor, build, tmp_path):
    place_entrypoint(tmp_path)  # the entrypoint check runs before the gate
    return executor._publish(build, str(tmp_path), "summary", str(tmp_path))


def _verdicts(db, build):
    return db.query(PolicyVerdict).filter_by(build_id=build.id).all()


def _checks(db, build):
    return (
        db.query(PolicyCheckResult)
        .join(PolicyVerdict, PolicyVerdict.id == PolicyCheckResult.verdict_id)
        .filter(PolicyVerdict.build_id == build.id)
        .all()
    )


def _assert_gate_error_state(db, build, *, expect_request=True):
    """The whole of what a failed gate must leave: ONE verdict at the highest tier holding ONE
    flagged `gate_error` row (none of the six checks' rows, so nothing half-written survived), an
    audit row for it, and a PENDING request the approver queue can see."""
    (verdict,) = _verdicts(db, build)
    assert verdict.risk_tier == "high"
    checks = _checks(db, build)
    assert [(c.check_name, c.result) for c in checks] == [(PolicyCheckName.GATE_ERROR, PolicyCheckStatus.FLAG)]
    assert "failed" in checks[0].summary and "human decision" in checks[0].summary
    assert db.query(AuditLog).filter_by(action="policy_gate.error", target=f"build:{build.id}").count() == 1
    # The ordinary verdict's audit row is not there: it belongs to a verdict that no longer exists.
    assert db.query(AuditLog).filter_by(action="policy_gate.verdict", target=f"build:{build.id}").count() == 0
    requests = db.query(ApprovalRequest).filter_by(build_id=build.id).all()
    if expect_request:
        (request,) = requests
        assert request.status == "PENDING"
        assert request.risk_tier == "high"
        assert request.policy_verdict_id == verdict.id
    else:
        assert requests == []
    return verdict


def _fail_audit_of_the_verdict(monkeypatch):
    """Raises from the very last statement of `run_and_persist_policy_gate`, AFTER the verdict row
    was flushed and all six check rows were added -- the shape the savepoint exists for. Only the
    ordinary verdict's audit write raises: the gate-error record uses the same function."""
    real = policy_gate.write_audit_log

    def _flaky(db, **kwargs):
        if kwargs["action"] == "policy_gate.verdict":
            raise RuntimeError("injected after the check rows were added")
        return real(db, **kwargs)

    monkeypatch.setattr(policy_gate, "write_audit_log", _flaky)


def _poison_the_session_with_a_flush_the_database_refuses(monkeypatch):
    """A real database error at flush: one check row with a NULL `summary` (a NOT NULL column).
    Nothing raises where it is built; the INSERT is refused when the session flushes, which is
    in the approval routing's `flush()` -- and a session whose flush failed is unusable until a
    rollback, so the commit that follows would raise out of the worker."""
    real = policy_gate.build_policy_verdict

    def _bad(**kwargs):
        good = real(**kwargs)
        bad = CheckResult(check_name="poisoned", result=PolicyCheckStatus.PASS, summary=None, findings=[])
        return PolicyVerdictResult(risk_tier=good.risk_tier, checks=[*good.checks, bad])

    monkeypatch.setattr(policy_gate, "build_policy_verdict", _bad)


# --- positive control ---------------------------------------------------------


def test_a_gate_that_does_not_fail_records_its_own_verdict_and_no_gate_error(
    monkeypatch, tmp_path, db_session, make_build_with_request
):
    """The same setup with nothing injected: the gate ran, six real check rows, no gate_error row.
    Without this the tests below could pass against a gate that never ran at all."""
    build = make_build_with_request()
    result = _publish(_executor(monkeypatch), build, tmp_path)
    db_session.commit()

    assert result.ok is True
    (verdict,) = _verdicts(db_session, build)
    names = {c.check_name for c in _checks(db_session, build)}
    assert names == set(PolicyCheckName.ALL)
    assert PolicyCheckName.GATE_ERROR not in names
    assert db_session.query(AuditLog).filter_by(action="policy_gate.error").count() == 0
    assert db_session.query(ApprovalRequest).filter_by(build_id=build.id).count() == 1
    assert verdict.id is not None


# --- the injected failures ----------------------------------------------------


def test_an_exception_after_check_rows_were_added_leaves_no_partial_rows_and_a_gate_error_verdict(
    monkeypatch, tmp_path, db_session, make_build_with_request
):
    build = make_build_with_request()
    _fail_audit_of_the_verdict(monkeypatch)

    result = _publish(_executor(monkeypatch), build, tmp_path)
    db_session.commit()

    assert result.ok is True and result.reason is None  # a gate error is not a failed build
    _assert_gate_error_state(db_session, build)
    # `_publish` sets these on the build just before the gate, unflushed: the savepoint's rollback
    # must not take them with it.
    db_session.refresh(build)
    assert (build.diff_ref, build.image_ref) == ("branch#1", f"forge-build-{build.project_id}-{build.id}")
    # The project is waiting on a human, exactly as it is for any flagged verdict.
    assert db_session.get(Project, build.project_id).status == ProjectStatus.AWAITING_APPROVAL


def test_a_flush_the_database_refuses_does_not_poison_the_session_or_the_commit(
    monkeypatch, tmp_path, db_session, make_build_with_request
):
    build = make_build_with_request()
    _poison_the_session_with_a_flush_the_database_refuses(monkeypatch)

    result = _publish(_executor(monkeypatch), build, tmp_path)
    # The session is usable: this commit is the one that used to raise PendingRollbackError.
    db_session.commit()

    assert result.ok is True
    _assert_gate_error_state(db_session, build)
    assert db_session.query(PolicyCheckResult).filter_by(check_name="poisoned").count() == 0
    db_session.refresh(build)
    assert build.diff_ref == "branch#1"


def test_a_failure_in_the_approval_routing_discards_the_verdict_it_belonged_to(
    monkeypatch, tmp_path, db_session, make_build_with_request
):
    """The routing is inside the same savepoint: a verdict whose request could not be created is
    discarded with it, and the gate-error verdict is routed through the SAME function."""
    build = make_build_with_request()
    real = sbe.create_approval_request_for_verdict
    calls: list[str] = []

    def _routing(session, *, build, verdict, project_id, requester_id):
        calls.append(verdict.risk_tier)
        if len(calls) == 1:
            raise RuntimeError("injected: the routing failed")
        return real(session, build=build, verdict=verdict, project_id=project_id, requester_id=requester_id)

    monkeypatch.setattr(sbe, "create_approval_request_for_verdict", _routing)

    result = _publish(_executor(monkeypatch), build, tmp_path)
    db_session.commit()

    assert result.ok is True
    assert len(calls) == 2 and calls[1] == "high"  # the second call is the gate-error verdict's
    _assert_gate_error_state(db_session, build)
    request = db_session.query(ApprovalRequest).filter_by(build_id=build.id).one()
    # Not a stand-in identity: the ordinary requester resolution ran for it too.
    assert request.requester_id == db_session.query(User).filter_by(role="requester").first().id


def test_the_gate_error_text_names_the_exception_class_and_never_its_message(
    monkeypatch, tmp_path, db_session, make_build_with_request
):
    build = make_build_with_request()
    real = policy_gate.write_audit_log

    def _flaky(db, **kwargs):
        if kwargs["action"] == "policy_gate.verdict":
            raise RuntimeError("SECRET-DETAIL /home/agent/app.py AKIAABCDEFGHIJKLMNOP")
        return real(db, **kwargs)

    monkeypatch.setattr(policy_gate, "write_audit_log", _flaky)

    _publish(_executor(monkeypatch), build, tmp_path)
    db_session.commit()

    (check,) = _checks(db_session, build)
    stored = " ".join([check.summary, str(check.findings)])
    audit = db_session.query(AuditLog).filter_by(action="policy_gate.error").one()
    stored += " " + str(audit.metadata_json)
    assert audit.metadata_json == {
        "risk_tier": "high",
        "stage": policy_gate.GATE_STAGE_CHECKS,
        "error_class": "RuntimeError",
    }
    assert "RuntimeError" in check.summary
    assert "SECRET-DETAIL" not in stored and "AKIA" not in stored and "/home/agent" not in stored
    assert check.findings == [
        {"type": "gate_error", "stage": policy_gate.GATE_STAGE_CHECKS, "error_class": "RuntimeError"}
    ]


def test_a_failure_while_routing_is_named_as_routing(
    monkeypatch, tmp_path, db_session, make_build_with_request
):
    build = make_build_with_request()
    real = sbe.create_approval_request_for_verdict
    seen: list[str] = []

    def _routing(session, **kwargs):
        seen.append(kwargs["verdict"].risk_tier)
        if len(seen) == 1:
            raise RuntimeError("injected")
        return real(session, **kwargs)

    monkeypatch.setattr(sbe, "create_approval_request_for_verdict", _routing)
    _publish(_executor(monkeypatch), build, tmp_path)
    db_session.commit()

    (check,) = _checks(db_session, build)
    assert check.findings[0]["stage"] == policy_gate.GATE_STAGE_ROUTING != policy_gate.GATE_STAGE_CHECKS


# --- the worker loop ------------------------------------------------------------


@pytest.mark.parametrize("injection", ["after_check_rows", "flush_refused"])
def test_the_worker_marks_the_build_succeeded_instead_of_raising_out_of_the_loop(
    monkeypatch, tmp_path, db_session, make_build_with_request, injection
):
    """Through `run_one_build`, as production does it. Before the savepoint a flush the database
    refused left the session unusable and `mark_succeeded`'s commit raised
    `PendingRollbackError` out of the worker's iteration, with the build still RUNNING."""
    build = make_build_with_request()
    if injection == "after_check_rows":
        _fail_audit_of_the_verdict(monkeypatch)
    else:
        _poison_the_session_with_a_flush_the_database_refuses(monkeypatch)
    result = _publish(_executor(monkeypatch), build, tmp_path)

    build.status = BuildStatus.RUNNING
    done = run_one_build(db_session, build, SimpleNamespace(execute=lambda b: result))

    assert done.status == BuildStatus.SUCCEEDED
    _assert_gate_error_state(db_session, done)


# --- the last resort -------------------------------------------------------------


def test_when_the_gate_error_cannot_be_recorded_either_the_build_fails_with_a_reason(
    monkeypatch, tmp_path, db_session, make_build_with_request
):
    """Both attempts fail (the database is what is failing). A SUCCEEDED build with no verdict, no
    request and no reason is the state this exists to prevent, so the build is failed with a
    platform-authored detail -- and the session is still usable, nothing half-written."""
    build = make_build_with_request()
    real = policy_gate.write_audit_log

    def _always(db, **kwargs):
        if kwargs["action"] in ("policy_gate.verdict", "policy_gate.error"):
            raise RuntimeError("SECRET-DETAIL")
        return real(db, **kwargs)

    monkeypatch.setattr(policy_gate, "write_audit_log", _always)

    result = _publish(_executor(monkeypatch), build, tmp_path)
    db_session.commit()

    assert result.ok is False
    assert result.reason == BuildFailureReason.INFRA_ERROR
    assert "policy gate failed (RuntimeError)" in result.summary
    assert "could not be" in result.summary and "no policy verdict" in result.summary
    assert "SECRET-DETAIL" not in result.summary
    assert _verdicts(db_session, build) == []
    assert _checks(db_session, build) == []
    assert db_session.query(ApprovalRequest).filter_by(build_id=build.id).count() == 0

    build.status = BuildStatus.RUNNING
    failed = run_one_build(db_session, build, SimpleNamespace(execute=lambda b: result))
    assert failed.status == BuildStatus.FAILED and failed.failure_reason == BuildFailureReason.INFRA_ERROR


# --- eval builds ------------------------------------------------------------------


def test_an_eval_build_whose_gate_failed_gets_the_verdict_but_no_approval_request(
    monkeypatch, tmp_path, db_session, seeded_ids, make_build_with_request
):
    """an eval build never enters human approval, and a gate
    failure does not change that -- it records the same gate-error verdict and stops there."""
    project = Project(
        name="eval-1234abcd-static-page",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        kind=ProjectKind.EVAL,
    )
    db_session.add(project)
    db_session.commit()
    build = make_build_with_request(project=project)
    _fail_audit_of_the_verdict(monkeypatch)

    result = _publish(_executor(monkeypatch), build, tmp_path)
    db_session.commit()

    assert result.ok is True
    _assert_gate_error_state(db_session, build, expect_request=False)


# --- what a reader sees --------------------------------------------------------------


def test_the_gate_error_is_on_the_timeline_and_in_the_approval_queue(
    monkeypatch, tmp_path, client, db_session, make_build_with_request
):
    build = make_build_with_request()
    _fail_audit_of_the_verdict(monkeypatch)
    _publish(_executor(monkeypatch), build, tmp_path)
    db_session.commit()
    owner = db_session.query(User).filter_by(role="requester").first()
    approver = db_session.query(User).filter_by(role="approver").first()

    timeline = client.get(f"/api/projects/{build.project_id}/timeline", headers={"X-Forge-User-Id": owner.id})
    assert timeline.status_code == 200
    (entry,) = [e for e in timeline.json() if e["kind"] == "policy_verdict"]
    assert entry["data"]["risk_tier"] == "high"
    assert [(c["check_name"], c["result"]) for c in entry["data"]["checks"]] == [("gate_error", "flag")]
    assert "human decision" in entry["data"]["checks"][0]["summary"]

    queue = client.get("/api/approvals", headers={"X-Forge-User-Id": approver.id}).json()
    (row,) = [r for r in queue if r["build_id"] == build.id]
    assert row["risk_tier"] == "high" and row["status"] == "PENDING"
    assert row["flagged_checks"] == ["gate_error"]


def test_the_executor_calls_the_gate_through_one_savepoint(monkeypatch, tmp_path, db_session, make_build_with_request):
    """The nesting itself, observed: the gate's own writes happen with the session one savepoint
    deep, and the gate-error record is a second, separate one."""
    build = make_build_with_request()
    depths: list[int] = []
    real_gate = sbe.run_and_persist_policy_gate

    def _watch(session, build, **kwargs):
        depths.append(session.in_nested_transaction() and 1 or 0)
        return real_gate(session, build, **kwargs)

    monkeypatch.setattr(sbe, "run_and_persist_policy_gate", _watch)
    _publish(_executor(monkeypatch), build, tmp_path)

    assert depths == [1]
    assert db_session.execute(select(func.count()).select_from(PolicyVerdict)).scalar_one() == 1
