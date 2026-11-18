"""a `FAILED`
build persists a platform-authored `failure_detail`, in the column and in the
`build.failed` audit metadata, and it reaches the timeline.

Three layers, all without Docker:
  * `mark_failed` / the timeout sweep (the single writer and the one other path);
  * `worker.run_one_build` (the executor raised / `ok=False` with and without a
    summary);
  * `SandboxBuildExecutor.execute` driven through `run_one_build`, with hostile
    markers planted where L3 text and library exception text could leak from, so
    "the rule holds" is asserted on the PERSISTED column, not on an intermediate.

The markers are the point: each rule test plants a unique string in the source
that must not reach the detail and asserts it is absent from the column and from
the audit metadata, next to an assertion that the detail is not empty.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.worker as worker
from app.config import settings
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.project import Project
from app.services import sandbox_build_executor as sbe
from app.services.agent_adapter import AgentAdapterResult
from app.services.build_executor import AlwaysFailBuildExecutor, ExecutionResult
from app.services.build_queue import (
    FAILURE_DETAIL_MAX_CHARS,
    FAILURE_DETAIL_TRUNCATION_MARKER,
    mark_failed,
    sweep_timed_out_builds,
)
from app.services.git_provider import GitProviderError
from app.services.image_assembly import ImageAssemblyError
from app.services.publish import PublishError
from app.services.status import BuildFailureReason, BuildStatus
from tests.harvest_support import with_output

L3_SUMMARY_MARKER = "L3-MARKER-agent-summary-7f3a"
EXC_TEXT_MARKER = "EXC-MARKER-library-text-91bc"
TOKEN = "tok-SECRET-0123456789"


# --- helpers ----------------------------------------------------------------------


def _running_build(db, seeded_ids, *, started_at=None):
    project = Project(
        name="failure-detail-project",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
    )
    db.add(project)
    db.flush()
    build = Build(project_id=project.id, type="create", status=BuildStatus.RUNNING, engine="anthropic")
    build.started_at = started_at or datetime.now(timezone.utc)
    db.add(build)
    db.commit()
    db.refresh(build)
    return build


def _audit_detail(db, build) -> str | None:
    entry = db.query(AuditLog).filter_by(action="build.failed", target=f"build:{build.id}").one()
    return entry.metadata_json["detail"]


def _assert_persisted(db, build, *, reason, contains=(), excludes=()):
    """Column and audit metadata agree, are non-empty, and obey the rule."""
    db.refresh(build)
    assert build.status == BuildStatus.FAILED
    assert build.failure_reason == reason
    detail = build.failure_detail
    assert detail and detail.strip(), "a FAILED build must never carry an empty detail"
    assert _audit_detail(db, build) == detail
    for needle in contains:
        assert needle in detail, (needle, detail)
    for needle in excludes:
        assert needle not in detail, (needle, detail)
    return detail


class _Raising:
    def __init__(self, exc):
        self._exc = exc

    def execute(self, build):
        raise self._exc


class _Returning:
    def __init__(self, result):
        self._result = result

    def execute(self, build):
        return self._result


# --- (a2, a3) the single writer ---------------------------------------------------


def test_mark_failed_without_a_detail_is_a_type_error(db_session, seeded_ids):
    build = _running_build(db_session, seeded_ids)
    with pytest.raises(TypeError):
        mark_failed(db_session, build, reason=BuildFailureReason.AGENT_ERROR)  # type: ignore[call-arg]
    db_session.refresh(build)
    assert build.status == BuildStatus.RUNNING  # nothing was half-written


def test_a_5000_character_detail_is_stored_truncated_with_the_marker(db_session, seeded_ids):
    build = _running_build(db_session, seeded_ids)

    mark_failed(db_session, build, reason=BuildFailureReason.AGENT_ERROR, detail="d" * 5000)

    db_session.refresh(build)
    assert len(build.failure_detail) == 1000 == FAILURE_DETAIL_MAX_CHARS
    assert build.failure_detail.endswith("…[truncated]")
    assert FAILURE_DETAIL_TRUNCATION_MARKER == "…[truncated]"
    assert build.failure_detail.startswith("d" * 100)
    assert _audit_detail(db_session, build) == build.failure_detail  # the audit copy is bounded too


def test_a_detail_at_the_bound_is_stored_as_given(db_session, seeded_ids):
    build = _running_build(db_session, seeded_ids)
    exact = "e" * 1000

    mark_failed(db_session, build, reason=BuildFailureReason.AGENT_ERROR, detail=exact)

    db_session.refresh(build)
    assert build.failure_detail == exact


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_a_blank_detail_never_lands_as_blank(db_session, seeded_ids, blank):
    build = _running_build(db_session, seeded_ids)

    mark_failed(db_session, build, reason=BuildFailureReason.HARVEST_ERROR, detail=blank)

    _assert_persisted(db_session, build, reason="HARVEST_ERROR", contains=["HARVEST_ERROR"])


# --- (b) every FAILED path writes a detail ----------------------------------------


def test_b1_an_executor_that_raised_persists_its_class_name_not_its_text(db_session, seeded_ids):
    build = _running_build(db_session, seeded_ids)

    worker.run_one_build(db_session, build, _Raising(RuntimeError(f"boom {EXC_TEXT_MARKER}")))

    detail = _assert_persisted(
        db_session, build, reason="INFRA_ERROR", excludes=[EXC_TEXT_MARKER, "boom"]
    )
    assert detail == "the build executor raised RuntimeError; see the worker log"


def test_b2_an_ok_false_result_with_a_summary_persists_it(db_session, seeded_ids):
    build = _running_build(db_session, seeded_ids)
    result = ExecutionResult(
        ok=False, reason=BuildFailureReason.ENGINE_UNREACHABLE, summary="the LLM proxy container failed"
    )

    worker.run_one_build(db_session, build, _Returning(result))

    detail = _assert_persisted(db_session, build, reason="ENGINE_UNREACHABLE")
    assert detail == "the LLM proxy container failed"


def test_b3_an_ok_false_result_with_no_summary_still_persists_a_detail_naming_the_code(
    db_session, seeded_ids
):
    build = _running_build(db_session, seeded_ids)

    worker.run_one_build(db_session, build, AlwaysFailBuildExecutor())

    _assert_persisted(db_session, build, reason="AGENT_ERROR", contains=["AGENT_ERROR"])


def test_b3_a_result_with_no_reason_and_a_blank_summary_falls_back_to_agent_error(
    db_session, seeded_ids
):
    build = _running_build(db_session, seeded_ids)

    worker.run_one_build(db_session, build, _Returning(ExecutionResult(ok=False, summary="  ")))

    _assert_persisted(db_session, build, reason="AGENT_ERROR", contains=["AGENT_ERROR"])


def test_b4_the_timeout_sweep_persists_a_detail_stating_the_configured_maximum(
    db_session, seeded_ids
):
    build = _running_build(
        db_session, seeded_ids, started_at=datetime.now(timezone.utc) - timedelta(minutes=500)
    )

    swept = sweep_timed_out_builds(db_session, max_duration_minutes=45)

    assert [b.id for b in swept] == [build.id]
    _assert_persisted(db_session, build, reason="TIMEOUT", contains=["45 minutes"])


def test_a_succeeded_build_has_no_failure_detail(db_session, seeded_ids):
    from app.services.build_executor import PlaceholderBuildExecutor

    build = _running_build(db_session, seeded_ids)

    worker.run_one_build(db_session, build, PlaceholderBuildExecutor())

    db_session.refresh(build)
    assert build.status == BuildStatus.SUCCEEDED
    assert build.failure_detail is None


# --- the executor, driven through the worker --------------------------------------


class _Network:
    def remove(self):
        pass


def _sandbox_executor(
    monkeypatch,
    *,
    usage=None,
    adapter_result=None,
    keep_publish=False,
    network_error=None,
    proxy_start_error=None,
):
    """A `SandboxBuildExecutor` over a fake proxy and adapter. `usage` is the dict
    the fake proxy returns, or an Exception to raise."""
    usage = usage if usage is not None else {
        "tokens": 0,
        "cost_usd": 0.0,
        "budget_exceeded": False,
        "policy_refusals": [],
        "unmetered": False,
        "unmetered_reason": None,
    }

    class _Proxy:
        base_url_for_sandbox = "http://proxy"

        def __init__(self, *a, **k):
            pass

        def start(self, **k):
            if proxy_start_error is not None:
                raise proxy_start_error

        def issue_token(self, *a, **k):
            return TOKEN

        def drain(self, token, *, timeout_seconds):
            return True  # nothing of the token is still running

        def usage(self, token):
            if isinstance(usage, BaseException):
                raise usage
            return dict(usage)

        def revoke(self, *a):
            pass

        def stop(self):
            pass

    class _Adapter:
        def run(self, adapter_input):
            # a success carries the harvested directory the executor publishes from.
            return with_output(
                adapter_result or AgentAdapterResult(ok=True, summary=f"all done {L3_SUMMARY_MARKER}")
            )

    def _create_network(*a, **k):
        if network_error is not None:
            raise network_error
        return _Network()

    docker = SimpleNamespace(networks=SimpleNamespace(create=_create_network))
    monkeypatch.setattr(sbe, "ProxyContainer", _Proxy)
    executor = sbe.SandboxBuildExecutor(docker_client=docker, adapter=_Adapter(), git_provider=object())
    monkeypatch.setattr(executor, "_stage_workspace", lambda *a, **k: None)
    if not keep_publish:
        monkeypatch.setattr(
            executor, "_publish", lambda *a, **k: ExecutionResult(ok=True, summary="published")
        )
    return executor


def _real_publish_executor(monkeypatch, *, publish_exc=None, assemble_exc=None, provider_exc=None):
    """Keeps the real `_publish`; stubs only what it calls out to."""
    executor = _sandbox_executor(monkeypatch, keep_publish=True)
    monkeypatch.setattr(
        executor,
        "_ensure_repo",
        lambda provider, project: SimpleNamespace(full_name="org/p", default_branch="main"),
    )

    def _publish_build(**kwargs):
        if publish_exc is not None:
            raise publish_exc
        return SimpleNamespace(diff_ref="forge-build-x#1", drift=[])

    def _assemble_image(**kwargs):
        if assemble_exc is not None:
            raise assemble_exc
        return "tag"

    monkeypatch.setattr(sbe, "publish_build", _publish_build)
    monkeypatch.setattr(sbe, "assemble_image", _assemble_image)
    if provider_exc is not None:
        executor._git_provider = None

        def _create_provider(_settings):
            raise provider_exc

        monkeypatch.setattr(sbe, "create_provider", _create_provider)
    return executor


def _run(db, build, executor):
    build.status = BuildStatus.RUNNING
    db.commit()
    return worker.run_one_build(db, build, executor)


# --- (c1) HARVEST_ERROR: neither the agent's summary nor the exception's text ------


@pytest.mark.parametrize(
    ("kind", "stage"),
    [
        ("publish", "publish"),
        ("image", "image assembly"),
    ],
)
def test_c1_a_harvest_failure_names_the_stage_and_class_not_the_agent_summary_or_exception_text(
    monkeypatch, db_session, make_build_with_request, kind, stage
):
    if kind == "publish":
        executor = _real_publish_executor(
            monkeypatch, publish_exc=PublishError(f"push rejected {EXC_TEXT_MARKER}")
        )
        exc_class = "PublishError"
    else:
        executor = _real_publish_executor(
            monkeypatch, assemble_exc=ImageAssemblyError(f"docker build said {EXC_TEXT_MARKER}")
        )
        exc_class = "ImageAssemblyError"
    # The agent's (success) summary carries the L3 marker into `_publish`.
    build = make_build_with_request()

    _run(db_session, build, executor)

    detail = _assert_persisted(
        db_session,
        build,
        reason="HARVEST_ERROR",
        contains=[stage, exc_class],
        excludes=[L3_SUMMARY_MARKER, EXC_TEXT_MARKER],
    )
    assert "see the worker log" in detail


def test_c1_a_provider_error_subclass_is_named_by_class(monkeypatch, db_session, make_build_with_request):
    executor = _real_publish_executor(
        monkeypatch, publish_exc=GitProviderError(f"401 for {EXC_TEXT_MARKER}")
    )
    build = make_build_with_request()

    _run(db_session, build, executor)

    _assert_persisted(
        db_session,
        build,
        reason="HARVEST_ERROR",
        contains=["publish", "GitProviderError"],
        excludes=[L3_SUMMARY_MARKER, EXC_TEXT_MARKER],
    )


def test_c1_a_missing_git_provider_names_the_setup_stage_and_class(
    monkeypatch, db_session, make_build_with_request
):
    executor = _real_publish_executor(
        monkeypatch, provider_exc=RuntimeError(f"GIT_TOKEN unset {EXC_TEXT_MARKER}")
    )
    build = make_build_with_request()

    _run(db_session, build, executor)

    _assert_persisted(
        db_session,
        build,
        reason="HARVEST_ERROR",
        contains=["git provider setup", "RuntimeError"],
        excludes=[L3_SUMMARY_MARKER, EXC_TEXT_MARKER],
    )


def test_c1_the_agent_summary_still_reaches_the_log_on_a_harvest_failure(
    monkeypatch, caplog, db_session, make_build_with_request
):
    """Not persisted is not the same as dropped: the operator can still read it."""
    import logging

    executor = _real_publish_executor(monkeypatch, publish_exc=PublishError("x"))
    build = make_build_with_request()

    with caplog.at_level(logging.INFO, logger="app.sandbox_build_executor"):
        _run(db_session, build, executor)

    assert L3_SUMMARY_MARKER in caplog.text


# --- (c2) the adapter's failure: reason code only ---------------------------------


def test_c2_an_adapter_failure_names_the_reason_not_the_adapter_summary(
    monkeypatch, caplog, db_session, make_build_with_request
):
    import logging

    executor = _sandbox_executor(
        monkeypatch,
        adapter_result=AgentAdapterResult(
            ok=False,
            reason=BuildFailureReason.RESOURCE_LIMIT,
            summary=f"the agent wrote: {L3_SUMMARY_MARKER}",
            logs=f"logs {L3_SUMMARY_MARKER}",
        ),
    )
    build = make_build_with_request()

    with caplog.at_level(logging.WARNING, logger="app.sandbox_build_executor"):
        _run(db_session, build, executor)

    _assert_persisted(
        db_session,
        build,
        reason="RESOURCE_LIMIT",
        contains=["RESOURCE_LIMIT"],
        excludes=[L3_SUMMARY_MARKER],
    )
    assert L3_SUMMARY_MARKER in caplog.text  # logged, not persisted


def test_c2_an_adapter_failure_with_no_reason_is_agent_error(
    monkeypatch, db_session, make_build_with_request
):
    executor = _sandbox_executor(
        monkeypatch, adapter_result=AgentAdapterResult(ok=False, summary=L3_SUMMARY_MARKER)
    )
    build = make_build_with_request()

    _run(db_session, build, executor)

    _assert_persisted(
        db_session, build, reason="AGENT_ERROR", contains=["AGENT_ERROR"], excludes=[L3_SUMMARY_MARKER]
    )


# --- (c3) a usage read-back failure: class name only ------------------------------


def test_c3_a_usage_read_back_failure_carries_the_class_not_the_proxy_start_error_text(
    monkeypatch, db_session, make_build_with_request
):
    executor = _sandbox_executor(
        monkeypatch,
        usage=sbe.ProxyStartError(f"usage lookup failed (1): {EXC_TEXT_MARKER} {TOKEN}"),
    )
    build = make_build_with_request()

    _run(db_session, build, executor)

    detail = _assert_persisted(
        db_session,
        build,
        reason="INFRA_ERROR",
        contains=["could not be read back", "ProxyStartError"],
        excludes=[EXC_TEXT_MARKER, TOKEN, "usage lookup failed"],
    )
    assert "cost is unknown" in detail


def test_c3_a_platform_authored_read_back_variant_keeps_its_text(
    monkeypatch, db_session, make_build_with_request
):
    """The other `_UsageReadError` variants are platform templates and stay whole."""
    executor = _sandbox_executor(monkeypatch, usage={"tokens": 1, "cost_usd": 0.0})
    build = make_build_with_request()

    _run(db_session, build, executor)

    _assert_persisted(db_session, build, reason="INFRA_ERROR", contains=["'unmetered'", "stale"])


# --- (c5) what already reached the detail still does ------------------------------


def test_c5_a_policy_refusal_still_reaches_the_detail(monkeypatch, db_session, make_build_with_request):
    refusal = "tool policy: request field 'mcp_servers' is not allowed"
    executor = _sandbox_executor(
        monkeypatch,
        usage={
            "tokens": 3,
            "cost_usd": 0.0,
            "budget_exceeded": False,
            "policy_refusals": [refusal],
            "unmetered": False,
            "unmetered_reason": None,
        },
    )
    build = make_build_with_request()

    _run(db_session, build, executor)

    _assert_persisted(
        db_session,
        build,
        reason="AGENT_ERROR",
        contains=["1 request was refused by tool policy", "mcp_servers"],
    )


def test_c5_a_missing_request_still_reaches_the_detail(monkeypatch, db_session, make_build_with_request):
    executor = _sandbox_executor(monkeypatch)
    build = make_build_with_request(with_request=False)

    _run(db_session, build, executor)

    _assert_persisted(
        db_session, build, reason="INFRA_ERROR", contains=[build.id, "no linked change request"]
    )


def test_c5_an_unmetered_response_reaches_the_detail_with_the_proxys_own_reason(
    monkeypatch, db_session, make_build_with_request
):
    executor = _sandbox_executor(
        monkeypatch,
        usage={
            "tokens": 7,
            "cost_usd": 0.0001,
            "budget_exceeded": False,
            "policy_refusals": [],
            "unmetered": True,
            "unmetered_reason": "response JSON has no usage object",
        },
    )
    build = make_build_with_request()

    _run(db_session, build, executor)

    _assert_persisted(
        db_session,
        build,
        reason="BUDGET_EXCEEDED",
        contains=["could not meter a response", "response JSON has no usage object"],
    )


# --- the sites that used to carry no summary --------------------------------------


def test_a_budget_miss_states_the_budget_and_nothing_from_l3(
    monkeypatch, db_session, make_build_with_request
):
    executor = _sandbox_executor(
        monkeypatch,
        usage={
            "tokens": 250_000,
            "cost_usd": 5.5,
            "budget_exceeded": True,
            "policy_refusals": [],
            "unmetered": False,
            "unmetered_reason": None,
        },
        adapter_result=AgentAdapterResult(ok=False, reason="AGENT_ERROR", summary=L3_SUMMARY_MARKER),
    )
    build = make_build_with_request()

    _run(db_session, build, executor)

    _assert_persisted(
        db_session,
        build,
        reason="BUDGET_EXCEEDED",
        contains=["250000 tokens", "$5.5000", f"{settings.build_token_budget} tokens"],
        excludes=[L3_SUMMARY_MARKER],
    )


def test_a_network_creation_failure_names_the_class_not_the_docker_text(
    monkeypatch, db_session, make_build_with_request
):
    from docker.errors import DockerException

    executor = _sandbox_executor(monkeypatch, network_error=DockerException(f"daemon says {EXC_TEXT_MARKER}"))
    build = make_build_with_request()

    _run(db_session, build, executor)

    _assert_persisted(
        db_session,
        build,
        reason="INFRA_ERROR",
        contains=["network", "DockerException"],
        excludes=[EXC_TEXT_MARKER],
    )


def test_a_proxy_start_failure_is_a_fixed_template_not_the_start_error_text(
    monkeypatch, db_session, make_build_with_request
):
    executor = _sandbox_executor(
        monkeypatch, proxy_start_error=sbe.ProxyStartError(f"container logs: {EXC_TEXT_MARKER}")
    )
    build = make_build_with_request()

    _run(db_session, build, executor)

    _assert_persisted(
        db_session, build, reason="ENGINE_UNREACHABLE", contains=["proxy"], excludes=[EXC_TEXT_MARKER]
    )


# --- (d1) the timeline ------------------------------------------------------------


def test_d1_the_timeline_carries_failure_detail_for_failed_and_null_for_succeeded(
    client, db_session, seeded_ids
):
    failed = _running_build(db_session, seeded_ids)
    project_id = failed.project_id
    ok = Build(project_id=project_id, type="modify", status=BuildStatus.SUCCEEDED)
    db_session.add(ok)
    db_session.commit()
    worker.run_one_build(
        db_session,
        failed,
        _Returning(
            ExecutionResult(
                ok=False, reason=BuildFailureReason.BUDGET_EXCEEDED, summary="the per-build LLM budget was spent"
            )
        ),
    )

    resp = client.get(
        f"/api/projects/{project_id}/timeline", headers={"X-Forge-User-Id": seeded_ids["user_id"]}
    )

    assert resp.status_code == 200
    by_id = {e["data"]["id"]: e["data"] for e in resp.json() if e["kind"] == "build"}
    assert by_id[failed.id]["failure_reason"] == "BUDGET_EXCEEDED"
    assert by_id[failed.id]["failure_detail"] == "the per-build LLM budget was spent"
    assert "failure_detail" in by_id[ok.id] and by_id[ok.id]["failure_detail"] is None
    assert by_id[ok.id]["failure_reason"] is None


def test_d1_build_out_carries_failure_detail(db_session, seeded_ids):
    from app.schemas.build import BuildOut

    build = _running_build(db_session, seeded_ids)
    mark_failed(db_session, build, reason=BuildFailureReason.TIMEOUT, detail="took too long")

    assert BuildOut.model_validate(build).failure_detail == "took too long"
