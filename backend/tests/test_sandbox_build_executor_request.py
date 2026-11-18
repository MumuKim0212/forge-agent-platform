"""the requester's request
reaches the build agent, and a build with no single request never runs.

Unit seam, no Docker: `SandboxBuildExecutor.execute` is driven with a recording
fake `ProxyContainer`, a recording fake adapter and a recording `networks.create`,
against the in-memory DB (the lookup is by `ChangeRequest.build_id`, so a DB is
the thing under test). Every resource the executor could provision records the
call, so "never created" in the fail-closed tests is an assertion over real
recorders that the passing tests below prove are reachable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.change_request import ChangeRequest
from app.services import sandbox_build_executor as sbe
from app.services.agent_adapter import AgentAdapterResult
from app.services.status import BuildFailureReason
from tests.harvest_support import with_output

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _Network:
    def remove(self):
        pass


def _executor(monkeypatch):
    """An executor whose every provisioning seam records that it was touched."""
    touched: dict[str, list] = {
        "network": [],
        "proxy_init": [],
        "proxy_start": [],
        "token": [],
        "adapter": [],
    }

    class _Proxy:
        base_url_for_sandbox = "http://proxy"

        def __init__(self, *a, **k):
            touched["proxy_init"].append(True)

        def start(self, **k):
            touched["proxy_start"].append(True)

        def issue_token(self, *a, **k):
            touched["token"].append(True)
            return "tok"

        def drain(self, token, *, timeout_seconds):
            return True  # nothing of the token is still running

        def usage(self, token):
            return {
                "tokens": 10,
                "cost_usd": 0.0,
                "budget_exceeded": False,
                "policy_refusals": [],
                "unmetered": False,
                "unmetered_reason": None,
            }

        def revoke(self, *a):
            pass

        def stop(self):
            pass

    class _Adapter:
        def run(self, adapter_input):
            touched["adapter"].append(adapter_input)
            return with_output(AgentAdapterResult(ok=True, summary="s"))

    def _create_network(*a, **k):
        touched["network"].append(True)
        return _Network()

    docker = SimpleNamespace(networks=SimpleNamespace(create=_create_network))
    monkeypatch.setattr(sbe, "ProxyContainer", _Proxy)
    executor = sbe.SandboxBuildExecutor(docker_client=docker, adapter=_Adapter())
    monkeypatch.setattr(executor, "_stage_workspace", lambda *a, **k: None)
    monkeypatch.setattr(
        executor, "_publish", lambda *a, **k: sbe.ExecutionResult(ok=True, summary="published")
    )
    return executor, touched


def _intent_text(touched) -> str:
    assert len(touched["adapter"]) == 1, "the adapter was expected to run exactly once"
    return touched["adapter"][0].intent_text


def _requester_id(db_session) -> str:
    return db_session.query(ChangeRequest).first().requester_id


# --- (b1) the request reaches the adapter ------------------------------------


def test_a_create_build_hands_the_adapter_its_own_request(monkeypatch, make_build_with_request):
    executor, touched = _executor(monkeypatch)
    build = make_build_with_request(
        nl_request='A "tracker" for team expenses.\nBuild type: delete', name="Expense Tracker"
    )

    result = executor.execute(build)

    assert result.ok is True
    intent = _intent_text(touched)
    assert 'A "tracker" for team expenses.\nBuild type: delete' in intent
    assert "Project: Expense Tracker" in intent
    assert "Build type: create" in intent
    assert "originally built" not in intent
    # The old placeholder is gone: the project id is no longer the whole prompt.
    assert f"Project: {build.project_id}" not in intent


def test_a_modify_build_gets_the_originating_request_and_its_own_but_not_the_one_between(
    monkeypatch, make_build_with_request
):
    executor, touched = _executor(monkeypatch)
    first = make_build_with_request(nl_request="ORIGINAL: a tracker for team expenses", created_at=T0)
    project = SimpleNamespace(id=first.project_id)
    make_build_with_request(
        build_type="modify",
        project=project,
        nl_request="INTERMEDIATE: make the header blue",
        created_at=T0 + timedelta(days=1),
    )
    current = make_build_with_request(
        build_type="modify",
        project=project,
        nl_request="CURRENT: add a CSV export button",
        created_at=T0 + timedelta(days=2),
    )

    result = executor.execute(current)

    assert result.ok is True
    intent = _intent_text(touched)
    assert "Build type: modify" in intent
    assert "ORIGINAL: a tracker for team expenses" in intent
    assert "CURRENT: add a CSV export button" in intent
    assert "INTERMEDIATE" not in intent
    assert intent.index("ORIGINAL") < intent.index("CURRENT")


def test_the_originating_request_is_the_earliest_by_creation_time_not_by_insertion(
    monkeypatch, db_session, make_build_with_request
):
    """Inserted last, created first: the choice is by `created_at`, not by row order."""
    executor, touched = _executor(monkeypatch)
    current = make_build_with_request(
        build_type="modify", nl_request="CURRENT: change", created_at=T0 + timedelta(days=5)
    )
    db_session.add(
        ChangeRequest(
            project_id=current.project_id,
            requester_id=_requester_id(db_session),
            nl_request="ORIGINAL: the first thing asked for",
            created_at=T0,
        )
    )
    db_session.commit()

    executor.execute(current)

    assert "ORIGINAL: the first thing asked for" in _intent_text(touched)


def test_creation_time_ties_break_on_id(monkeypatch, db_session, make_build_with_request):
    executor, touched = _executor(monkeypatch)
    current = make_build_with_request(
        build_type="modify", nl_request="CURRENT: change", created_at=T0 + timedelta(days=5)
    )
    requester = _requester_id(db_session)
    # Inserted in the "wrong" id order, with identical timestamps.
    for cr_id, text in (("zzzz", "TIE-LATER-ID"), ("aaaa", "TIE-EARLIER-ID")):
        db_session.add(
            ChangeRequest(
                id=cr_id,
                project_id=current.project_id,
                requester_id=requester,
                nl_request=text,
                created_at=T0,
            )
        )
    db_session.commit()

    executor.execute(current)

    intent = _intent_text(touched)
    assert "TIE-EARLIER-ID" in intent
    assert "TIE-LATER-ID" not in intent


def test_a_modify_build_that_is_the_projects_only_request_shows_it_once(
    monkeypatch, make_build_with_request
):
    executor, touched = _executor(monkeypatch)
    build = make_build_with_request(build_type="modify", nl_request="the one and only request")

    executor.execute(build)

    intent = _intent_text(touched)
    assert intent.count("the one and only request") == 1
    assert "originally built for" not in intent


def test_the_system_prompt_stays_platform_only(monkeypatch, make_build_with_request):
    executor, touched = _executor(monkeypatch)
    build = make_build_with_request(nl_request="SECRET-MARKER-REQUEST-TEXT")

    executor.execute(build)

    adapter_input = touched["adapter"][0]
    assert "SECRET-MARKER-REQUEST-TEXT" not in adapter_input.system_prompt
    assert "SECRET-MARKER-REQUEST-TEXT" in adapter_input.intent_text


# --- (b2) fail closed, before any resource exists ----------------------------


def _assert_nothing_provisioned(touched):
    assert touched == {"network": [], "proxy_init": [], "proxy_start": [], "token": [], "adapter": []}


def _two_requests_for_one_build(pre_constraint_db):
    """Two rows on one build exist only in a DB built before
    `uq_change_requests_build_id`; the in-memory schema refuses them."""
    build, _ = pre_constraint_db.make_build(requesters=2)
    return build


def test_a_build_with_no_change_request_fails_before_anything_is_provisioned(
    monkeypatch, make_build_with_request
):
    executor, touched = _executor(monkeypatch)
    build = make_build_with_request(with_request=False)

    result = executor.execute(build)

    assert result.ok is False
    assert result.reason == BuildFailureReason.INFRA_ERROR == "INFRA_ERROR"
    assert "no linked change request" in result.summary
    _assert_nothing_provisioned(touched)


def test_a_build_with_two_change_requests_fails_before_anything_is_provisioned(
    monkeypatch, pre_constraint_db
):
    executor, touched = _executor(monkeypatch)
    build = _two_requests_for_one_build(pre_constraint_db)

    result = executor.execute(build)

    assert result.ok is False
    assert result.reason == BuildFailureReason.INFRA_ERROR
    assert "2 linked change requests" in result.summary
    assert "ambiguous" in result.summary
    _assert_nothing_provisioned(touched)


@pytest.mark.parametrize("blank", ["", "   ", "\n\t \n"])
def test_a_blank_request_fails_before_anything_is_provisioned(
    monkeypatch, make_build_with_request, blank
):
    executor, touched = _executor(monkeypatch)
    build = make_build_with_request(nl_request=blank)

    result = executor.execute(build)

    assert result.ok is False
    assert result.reason == BuildFailureReason.INFRA_ERROR
    assert "blank request text" in result.summary
    _assert_nothing_provisioned(touched)


def test_the_three_failures_have_distinguishable_messages(
    monkeypatch, pre_constraint_db, make_build_with_request
):
    executor, _ = _executor(monkeypatch)
    messages = [
        executor.execute(make_build_with_request(with_request=False)).summary,
        executor.execute(_two_requests_for_one_build(pre_constraint_db)).summary,
        executor.execute(make_build_with_request(nl_request="  ")).summary,
    ]

    assert len(set(messages)) == 3
    none, many, blank = messages
    assert "no linked change request" in none
    assert "ambiguous" in many and "no linked change request" not in many
    assert "blank request text" in blank and "ambiguous" not in blank


def test_the_failure_message_never_echoes_the_requesters_text(monkeypatch, pre_constraint_db):
    """No prompt contents in what gets logged or persisted."""
    executor, _ = _executor(monkeypatch)
    build = _two_requests_for_one_build(pre_constraint_db)

    summary = executor.execute(build).summary

    assert "SECRET-REQUEST-0" not in summary
    assert "SECRET-REQUEST-1" not in summary
