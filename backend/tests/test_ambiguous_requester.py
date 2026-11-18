"""every function that maps a
build to its requester refuses ambiguity instead of choosing, and an approval
request is never created from an ambiguous build.

`change_requests.build_id` is unique, so the in-memory schema cannot hold two
rows for one build. These tests use `pre_constraint_db` (a file DB migrated to
the revision before the constraint) -- the real query runs against real rows,
nothing is monkeypatched. Each refusal test has a positive control beside it
that proves the same setup does the thing when there is one row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.approval import ApprovalRequest
from app.models.policy_verdict import PolicyVerdict
from app.services import sandbox_build_executor as sbe
from app.services import usage
from app.services.approval import (
    AmbiguousRequesterError,
    requester_ids_for_builds,
    resolve_requester_for_build,
)
from app.services.status import BuildFailureReason, BuildStatus
from app.worker import run_one_build
from tests.entrypoint_support import place_entrypoint


# --- (b1) requester_ids_for_builds -------------------------------------------


def test_one_row_per_build_returns_the_map_unchanged(pre_constraint_db):
    db = pre_constraint_db
    a, _ = db.make_build(requesters=1, name="a", offset=0)
    b, _ = db.make_build(requesters=1, name="b", offset=1)
    c, _ = db.make_build(requesters=1, name="c", offset=2)

    result = requester_ids_for_builds(db.session, [a.id, b.id, c.id])

    assert result == {a.id: db.users[0].id, b.id: db.users[1].id, c.id: db.users[2].id}
    assert len({db.users[0].id, db.users[1].id, db.users[2].id}) == 3  # the three really differ


def test_a_build_with_no_row_is_absent_not_an_error(pre_constraint_db):
    db = pre_constraint_db
    a, _ = db.make_build(requesters=1, name="a")

    assert requester_ids_for_builds(db.session, [a.id, "no-such-build"]) == {a.id: db.users[0].id}
    assert requester_ids_for_builds(db.session, []) == {}


def test_two_rows_for_one_build_raise_and_name_that_build(pre_constraint_db):
    db = pre_constraint_db
    ok, _ = db.make_build(requesters=1, name="ok")
    bad, _ = db.make_build(requesters=2, name="bad")

    with pytest.raises(AmbiguousRequesterError) as raised:
        requester_ids_for_builds(db.session, [ok.id, bad.id])

    error = raised.value
    assert error.build_id == bad.id
    assert error.count == 2
    message = str(error)
    assert bad.id in message and "2 change requests" in message
    assert ok.id not in message
    # Never a requester id nor request text: this message can reach failure_detail.
    assert not any(user.id in message for user in db.users)
    assert "SECRET-REQUEST" not in message


def test_three_rows_report_three(pre_constraint_db):
    db = pre_constraint_db
    bad, _ = db.make_build(requesters=3)

    with pytest.raises(AmbiguousRequesterError) as raised:
        requester_ids_for_builds(db.session, [bad.id])

    assert raised.value.count == 3


def test_several_ambiguous_builds_are_counted_in_the_message(pre_constraint_db):
    db = pre_constraint_db
    one, _ = db.make_build(requesters=2, name="one")
    two, _ = db.make_build(requesters=2, name="two")

    with pytest.raises(AmbiguousRequesterError) as raised:
        requester_ids_for_builds(db.session, [one.id, two.id])

    assert raised.value.other_builds == 1
    assert raised.value.build_id == min(one.id, two.id)  # deterministic, not row order
    assert "1 other build" in str(raised.value)


# --- (b2) resolve_requester_for_build ----------------------------------------


def test_resolve_with_no_row_falls_back_to_the_owner(pre_constraint_db):
    db = pre_constraint_db
    build, _ = db.make_build(requesters=0)

    assert resolve_requester_for_build(db.session, build_id=build.id, project_owner_id="the-owner") == "the-owner"


def test_resolve_with_one_row_returns_that_requester_not_the_owner(pre_constraint_db):
    db = pre_constraint_db
    build, _ = db.make_build(requesters=1)

    resolved = resolve_requester_for_build(db.session, build_id=build.id, project_owner_id="the-owner")

    assert resolved == db.users[0].id
    assert resolved != "the-owner"


def test_resolve_with_two_rows_raises_rather_than_choosing_or_falling_back(pre_constraint_db):
    db = pre_constraint_db
    build, _ = db.make_build(requesters=2)

    with pytest.raises(AmbiguousRequesterError) as raised:
        resolve_requester_for_build(db.session, build_id=build.id, project_owner_id="the-owner")

    assert raised.value.build_id == build.id and raised.value.count == 2


# --- usage: propagates, is not reported as unattributed ----------------------


def test_cost_attribution_refuses_an_ambiguous_build_instead_of_crediting_one_requester(pre_constraint_db):
    db = pre_constraint_db
    window = usage.resolve_month_window("2026-03")
    ended = datetime(2026, 3, 10, 12, tzinfo=timezone.utc)

    clean, _ = db.make_build(requesters=1, name="clean")
    clean.cost_usd, clean.ended_at = 2.0, ended
    db.session.commit()
    # Positive control: with only the clean build the same call attributes it.
    lines = usage.user_usage(db.session, window)
    assert {line.subject_id: line.spend_usd for line in lines if line.subject_id and line.spend_usd} == {db.users[0].id: 2.0}

    bad, _ = db.make_build(requesters=2, name="bad")
    bad.cost_usd, bad.ended_at = 5.0, ended + timedelta(days=1)
    db.session.commit()

    with pytest.raises(AmbiguousRequesterError) as raised:
        usage.user_usage(db.session, window)
    assert raised.value.build_id == bad.id


# --- (c1) the executor at gate time -------------------------------------------


def _publishing_executor(monkeypatch):
    """A real `SandboxBuildExecutor._publish` -> `_run_policy_gate` path with
    only the external effects stubbed (git host, image build, preview). The
    policy gate and the requester resolution are the real ones."""
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


def test_one_request_reaches_the_gate_and_creates_an_approval_request_with_that_requester(
    monkeypatch, tmp_path, pre_constraint_db
):
    """The positive control for the test below: same setup, one row."""
    db = pre_constraint_db
    build, _ = db.make_build(requesters=1)
    executor = _publishing_executor(monkeypatch)

    place_entrypoint(tmp_path)  # the entrypoint check runs before the gate
    result = executor._publish(build, str(tmp_path), "summary", str(tmp_path))
    db.session.commit()

    assert result.ok is True
    assert db.session.query(PolicyVerdict).filter_by(build_id=build.id).count() == 1
    approval = db.session.query(ApprovalRequest).filter_by(build_id=build.id).one()
    assert approval.requester_id == db.users[0].id


def test_an_ambiguous_build_gets_no_approval_request_and_ends_failed_infra_error(
    monkeypatch, tmp_path, pre_constraint_db
):
    db = pre_constraint_db
    build, _ = db.make_build(requesters=2)
    executor = _publishing_executor(monkeypatch)

    place_entrypoint(tmp_path)  # the entrypoint check runs before the gate
    result = executor._publish(build, str(tmp_path), "summary", str(tmp_path))
    db.session.commit()

    assert result.ok is False
    assert result.reason == BuildFailureReason.INFRA_ERROR
    # The gate itself ran (a verdict exists), so the missing request is the
    # ambiguity's doing and not a gate that never got there.
    assert db.session.query(PolicyVerdict).filter_by(build_id=build.id).count() == 1
    assert db.session.query(ApprovalRequest).count() == 0

    # Through the worker, as production does it: the build row is FAILED/INFRA_ERROR.
    build.status = BuildStatus.RUNNING
    failed = run_one_build(db.session, build, SimpleNamespace(execute=lambda b: result))
    assert failed.status == BuildStatus.FAILED
    assert failed.failure_reason == BuildFailureReason.INFRA_ERROR
    detail = failed.failure_detail
    assert build.id in detail and "2 change requests" in detail and "ambiguous" in detail
    assert not any(user.id in detail for user in db.users)
    assert "SECRET-REQUEST" not in detail
    assert db.session.query(ApprovalRequest).count() == 0
