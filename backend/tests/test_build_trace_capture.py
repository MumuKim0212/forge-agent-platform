"""The platform side of the build trace.

- (a) the diff: what `compute_diff` lists and cuts, from the same trees as the change set;
- (b) the executor stores a trace on every path that reached the agent, and a trace failure
  never changes the build's outcome;
- (c) per-call cost and tokens reconcile with `builds.cost_*`, through the REAL proxy app;
- (d) the read endpoint and its access predicate; the timeline's `has_trace` flag;
- (e) deprovisioning removes the trace; the migration round-trips with it;
- (f) an `ast` pin on every file under `app/` that names the trace's storage or its service.

Unit seam, no Docker: the `Harness` of the harvest tests (the executor over fakes). Expected
values are literals; the bounds are not read from the production constants.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.models.build import Build
from app.models.build_trace import BuildTrace
from app.models.project import Project
from app.models.user import User
from app.services import build_trace as bt
from app.services import sandbox_build_executor as sbe
from app.services.agent_adapter import AgentAdapterResult
from app.services.build_trace import compute_diff, normalise_calls
from app.services.change_set import measure_changes
from app.services.llm_proxy import TokenRegistry, create_app
from app.services.proxy_container import ProxyContainer, ProxyStartError
from app.services.publish import PublishError
from app.services.roles import Role, may_read_build_trace
from app.services.status import BuildFailureReason, BuildStatus, ProjectStatus
from app.worker import run_one_build
from tests.harvest_support import make_output_dir
from tests.test_build_trace_proxy import Upstream, json_reply, sse_reply
from tests.test_sandbox_build_executor_harvest import AGENT, STAGED, Harness
from tests.test_sandbox_build_executor_schema import CODE_ONLY, INPUT

LOG_MARKER = "ENGINE-LOG-LINE-that-must-never-be-a-failure-detail"


def tree(files: dict[str, bytes]) -> str:
    return make_output_dir(files)


# --- (a) the diff ---------------------------------------------------------------------------------


def by_path(diff: dict) -> dict[str, dict]:
    return {f["path"]: f for f in diff["files"]}


def test_a_the_diff_lists_added_modified_and_deleted_text_files_as_unified_diffs():
    before = tree({"app/m.py": b"keep\nold line\nkeep too\n", "app/gone.py": b"bye\n", "app/same.py": b"same\n"})
    after = tree({"app/m.py": b"keep\nnew line\nkeep too\n", "app/new.py": b"print('x')\n", "app/same.py": b"same\n"})

    diff = compute_diff(input_dir=before, output_dir=after)

    files = by_path(diff)
    assert list(files) == ["app/gone.py", "app/m.py", "app/new.py"]  # path order; `same.py` is not listed
    assert (diff["files_total"], diff["truncated"]) == (3, False)
    assert files["app/m.py"]["change"] == "modified" and files["app/m.py"]["content"] == "text"
    assert files["app/m.py"]["diff"] == (
        "--- a/app/m.py\n+++ b/app/m.py\n@@ -1,3 +1,3 @@\n keep\n-old line\n+new line\n keep too\n"
    )
    assert files["app/new.py"]["diff"] == "--- /dev/null\n+++ b/app/new.py\n@@ -0,0 +1 @@\n+print('x')\n"
    assert files["app/gone.py"]["diff"] == "--- a/app/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"


def test_a_binary_and_non_utf8_files_are_listed_without_content():
    before = tree({"app/img.bin": b"\x00\x01old", "app/latin.txt": "café\n".encode("latin-1")})
    after = tree(
        {
            "app/img.bin": b"\x00\x01new",  # a NUL byte
            "app/latin.txt": "café au lait\n".encode("latin-1"),  # not UTF-8
            "app/added.dat": b"\xff\xfe\x00\x00",
        }
    )

    files = by_path(compute_diff(input_dir=before, output_dir=after))

    for path in ("app/img.bin", "app/latin.txt", "app/added.dat"):
        assert (files[path]["content"], files[path]["diff"], files[path]["truncated"]) == ("binary", None, False), path
    assert files["app/added.dat"]["change"] == "added" and files["app/img.bin"]["change"] == "modified"


def test_a_a_file_over_the_per_file_bound_is_cut_at_64_kb_and_marked():
    big = "".join(f"line {i:04d} " + "x" * 90 + "\n" for i in range(1000)).encode()  # ~100 KB, 1000 lines
    diff = compute_diff(input_dir=tree({}), output_dir=tree({"app/big.py": big}))

    (entry,) = diff["files"]
    assert entry["content"] == "text" and entry["truncated"] is True and diff["truncated"] is True
    assert len(entry["diff"].encode("utf-8")) == 65_536
    assert entry["diff"].startswith("--- /dev/null\n+++ b/app/big.py\n@@ -0,0 +1,1000 @@\n+line 0000 ")


def test_a_a_file_that_is_too_large_or_has_too_many_lines_is_listed_without_content():
    huge = b"y" * (300 * 1024)  # over the read limit
    many_lines = b"z\n" * 6_000  # small, but over the line limit
    files = by_path(compute_diff(input_dir=tree({}), output_dir=tree({"app/huge.txt": huge, "app/many.txt": many_lines})))

    assert (files["app/huge.txt"]["content"], files["app/huge.txt"]["diff"]) == ("too_large", None)
    assert (files["app/many.txt"]["content"], files["app/many.txt"]["diff"]) == ("too_large", None)


def test_a_the_one_megabyte_total_is_enforced_and_the_files_after_it_say_so():
    one = "".join(f"{i:05d} " + "q" * 94 + "\n" for i in range(600)).encode()  # ~60 KB of text per file
    output = tree({f"app/f{n:02d}.txt": one for n in range(20)})  # 20 x 60 KB > 1 MB

    diff = compute_diff(input_dir=tree({}), output_dir=output)

    sizes = [len((f["diff"] or "").encode("utf-8")) for f in diff["files"]]
    assert sum(sizes) <= 1_048_576
    kinds = [f["content"] for f in diff["files"]]
    assert "total_limit" in kinds and kinds.count("text") >= 15
    assert kinds == sorted(kinds, key=lambda k: k == "total_limit")  # once spent, every later file is marked
    assert diff["truncated"] is True and diff["files_total"] == 20
    cut = [f for f in diff["files"] if f["content"] == "text" and f["truncated"]]
    assert len(cut) == 1  # exactly the file the total ran out in is partially kept


def test_a_the_diff_lists_exactly_the_files_the_change_set_measures():
    before = tree({"app/a.py": b"1\n", "app/b.py": b"2\n", "app/c.py": b"3\n", ".forge/task_input.json": b"{}", "app/x.pyc": b"\x00"})
    after = tree({"app/a.py": b"1\n", "app/b.py": b"2 changed\n", "app/d.py": b"4\n", ".git/HEAD": b"ref\n"})

    changes = measure_changes(input_dir=before, output_dir=after).entries
    diff = compute_diff(input_dir=before, output_dir=after)

    assert [(f["path"], f["change"]) for f in diff["files"]] == [(e["path"], e["change"]) for e in changes]
    assert [(e["path"], e["change"]) for e in changes] == [("app/b.py", "modified"), ("app/c.py", "deleted"), ("app/d.py", "added")]


def test_a_line_endings_and_a_missing_final_newline_stay_visible():
    before = tree({"app/crlf.txt": b"a\r\nb\r\n", "app/tail.txt": b"end\n"})
    after = tree({"app/crlf.txt": b"a\nb\n", "app/tail.txt": b"end"})

    files = by_path(compute_diff(input_dir=before, output_dir=after))

    assert files["app/crlf.txt"]["diff"] and "-a\r\n" in files["app/crlf.txt"]["diff"]
    assert "\\ No newline at end of file" in files["app/tail.txt"]["diff"]


# --- executor harness -------------------------------------------------------------------------------

CALLS = {
    "steps": [
        {
            "ordinal": 1,
            "started_at": "2026-10-10T00:00:00+00:00",
            "ended_at": "2026-10-10T00:00:01+00:00",
            "model": "claude-opus-5-5",
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "tokens": 15,
            "cost_usd": 0.00014,
            "stop_reason": "tool_use",
            "content": "recorded",
            "thinking": [{"chars": 12}],
            "blocks": [
                {"kind": "assistant_text", "text": "CLAIM-TEXT", "chars": 10, "truncated": False},
                {"kind": "tool_use", "id": "toolu_1", "name": "Read", "text": '{"file_path": "/w/a"}', "chars": 21, "truncated": False},
                {"kind": "tool_result", "tool_use_id": "toolu_1", "is_error": False, "text": "REPORTED-BY-L3", "chars": 14, "truncated": False},
            ],
        }
    ],
    "calls_total": 1,
    "content_truncated": False,
    "omitted_calls": 0,
    "omitted_tokens": 0,
    "omitted_cost_usd": 0.0,
}


class TraceProxy:
    """`ProxyContainer` stand-in with a readable (or failing) trace; `usage` is the Harness's."""

    trace_result: object = CALLS  # a dict, or an Exception to raise
    usage_result: object = None  # a dict, an Exception, or None for "measured nothing"

    def __init__(self, *a, **k):
        pass

    base_url_for_sandbox = "http://proxy"

    def start(self, **k):
        pass

    def issue_token(self, *a, **k):
        return "tok-TRACE-0123456789"

    def drain(self, token, *, timeout_seconds):
        return True  # nothing of the token is still running

    def usage(self, token):
        value = type(self).usage_result
        if isinstance(value, BaseException):
            raise value
        return value or {
            "tokens": 15,
            "cost_usd": 0.00014,
            "budget_exceeded": False,
            "policy_refusals": [],
            "unmetered": False,
            "unmetered_reason": None,
        }

    def trace(self, token):
        value = type(self).trace_result
        if isinstance(value, BaseException):
            raise value
        return value

    def revoke(self, *a):
        pass

    def stop(self):
        pass


@pytest.fixture()
def proxy_cls(monkeypatch):
    """A fresh `TraceProxy` subclass per test (class-level knobs must not leak)."""
    cls = type("TraceProxyForTest", (TraceProxy,), {})
    monkeypatch.setattr(sbe, "ProxyContainer", cls)
    return cls


def harness(monkeypatch, proxy_cls, *, logs=LOG_MARKER, **kw) -> Harness:
    h = Harness(monkeypatch, **kw)
    monkeypatch.setattr(sbe, "ProxyContainer", proxy_cls)  # Harness installed its own; ours wins
    h.executor._adapter = _wrap_logs(h.executor._adapter, logs)
    return h


def _wrap_logs(adapter, logs):
    class _A:
        def run(self, adapter_input):
            return dataclasses.replace(adapter.run(adapter_input), logs=logs)

    return _A()


def row(db, build) -> BuildTrace | None:
    db.expire_all()
    return db.query(BuildTrace).filter_by(build_id=build.id).one_or_none()


def outcome(build) -> tuple:
    return (build.status, build.failure_reason, build.failure_detail, build.cost_tokens, build.cost_usd)


# --- (b) every path that reached the agent stores a trace; the outcome never depends on it ----------


def test_b_a_successful_build_stores_calls_diff_and_log(monkeypatch, proxy_cls, make_build_with_request, db_session):
    h = harness(monkeypatch, proxy_cls)
    build = make_build_with_request()

    done = run_one_build(db_session, build, h.executor)

    assert done.status == BuildStatus.SUCCEEDED
    stored = row(db_session, build)
    assert stored.calls_state == "captured" and stored.calls["calls_total"] == 1
    assert stored.diff_state == "computed"
    assert {f["path"]: f["change"] for f in stored.diff["files"]} == {
        "app/app.py": "modified",
        "app/agent_marker.txt": "added",
        "app/agent_new.py": "added",
        "app/staged_only.py": "deleted",
        "vendor/lib/code.py": "added",  # the agent's own `.forge/` and `.git` are not part of it
    }
    assert stored.log_tail == LOG_MARKER and stored.log_truncated is False
    # The change set on the build and the diff list the same files.
    assert sorted((e["path"], e["change"]) for e in done.changed_files["entries"]) == sorted(
        (f["path"], f["change"]) for f in stored.diff["files"]
    )


FAILURE_PATHS = {
    "agent reported failure": dict(adapter=AgentAdapterResult(ok=False, reason=BuildFailureReason.AGENT_ERROR, summary="x"), reason=BuildFailureReason.AGENT_ERROR),
    "budget spent": dict(usage={"tokens": 9, "cost_usd": 9.0, "budget_exceeded": True, "policy_refusals": [], "unmetered": False, "unmetered_reason": None}, reason=BuildFailureReason.BUDGET_EXCEEDED),
    "response unmetered": dict(usage={"tokens": 0, "cost_usd": 0.0, "budget_exceeded": False, "policy_refusals": [], "unmetered": True, "unmetered_reason": "no usage object"}, reason=BuildFailureReason.BUDGET_EXCEEDED),
    "policy refusal": dict(usage={"tokens": 0, "cost_usd": 0.0, "budget_exceeded": False, "policy_refusals": ["tool policy: request field 'mcp_servers' is not allowed"], "unmetered": False, "unmetered_reason": None}, reason=BuildFailureReason.AGENT_ERROR),
    "usage unreadable": dict(usage=RuntimeError("docker exec failed"), reason=BuildFailureReason.INFRA_ERROR),
}


@pytest.mark.parametrize("name", sorted(FAILURE_PATHS))
def test_b_every_failure_path_that_reached_the_agent_stores_a_trace_with_no_diff(
    monkeypatch, proxy_cls, make_build_with_request, db_session, name
):
    spec = FAILURE_PATHS[name]
    proxy_cls.usage_result = spec.get("usage")
    h = harness(monkeypatch, proxy_cls, adapter_result=spec.get("adapter"))
    build = make_build_with_request()

    done = run_one_build(db_session, build, h.executor)

    assert (done.status, done.failure_reason) == (BuildStatus.FAILED, spec["reason"])
    stored = row(db_session, build)
    assert stored is not None, "a failure that reached the agent must leave a trace"
    assert stored.calls_state == "captured"
    # The build ended before publish: the trace says so, rather than showing an empty diff.
    assert (stored.diff_state, stored.diff) == ("not_reached", None)
    assert stored.log_tail == LOG_MARKER
    assert LOG_MARKER not in (done.failure_detail or "")  # the log is never a failure detail


def test_b_a_build_whose_adapter_raises_still_stores_the_calls_it_made(monkeypatch, proxy_cls, make_build_with_request, db_session):
    h = harness(monkeypatch, proxy_cls)

    class _Raises:
        def run(self, adapter_input):
            raise RuntimeError("the sandbox runner crashed")

    h.executor._adapter = _Raises()
    build = make_build_with_request()

    done = run_one_build(db_session, build, h.executor)

    assert (done.status, done.failure_reason) == (BuildStatus.FAILED, BuildFailureReason.INFRA_ERROR)
    stored = row(db_session, build)
    assert (stored.calls_state, stored.diff_state, stored.log_tail) == ("captured", "not_reached", None)


def test_b_a_publish_failure_keeps_the_diff_it_computed_before_publishing(monkeypatch, proxy_cls, make_build_with_request, db_session):
    h = harness(monkeypatch, proxy_cls)
    h.publish_error = PublishError("push rejected")
    build = make_build_with_request()

    done = run_one_build(db_session, build, h.executor)

    assert (done.status, done.failure_reason) == (BuildStatus.FAILED, BuildFailureReason.HARVEST_ERROR)
    stored = row(db_session, build)
    assert stored.diff_state == "computed" and "app/agent_new.py" in {f["path"] for f in stored.diff["files"]}


def test_b_a_build_that_never_reached_the_agent_has_no_trace(monkeypatch, proxy_cls, make_build_with_request, db_session):
    h = harness(monkeypatch, proxy_cls)
    build = make_build_with_request(with_request=False)  # refused before any resource exists

    done = run_one_build(db_session, build, h.executor)

    assert done.status == BuildStatus.FAILED
    assert row(db_session, build) is None


def test_b_the_diff_is_taken_before_the_protected_path_restore(monkeypatch, make_build_with_request, db_session, tmp_path):
    """Through the real publish, restore included, over a real local bare repo: the agent's edit to
    the pinned Dockerfile is restored in what is pushed, and still shows in the trace's diff."""
    from tests.test_publish import LocalBareRepoProvider, _init_bare_repo
    from tests.test_sandbox_build_executor_changes import settings_skeleton

    bare = tmp_path / "remote.git"
    _init_bare_repo(bare)
    pinned = (settings_skeleton() / "Dockerfile").read_bytes()
    h = Harness(
        monkeypatch,
        output_files={**CODE_ONLY, "Dockerfile": b"FROM evil\n"},
        staged_files={**INPUT, "Dockerfile": pinned},
        git_provider=LocalBareRepoProvider(bare),
    )
    monkeypatch.setattr(sbe, "ProxyContainer", type("P", (TraceProxy,), {}))
    monkeypatch.setattr(sbe, "assemble_image", lambda **k: "tag")
    build = make_build_with_request(build_type="modify")

    done = run_one_build(db_session, build, h.executor)

    assert done.status == BuildStatus.SUCCEEDED and done.protected_path_drift == ["Dockerfile"]
    docker = by_path(row(db_session, build).diff)["Dockerfile"]
    assert docker["change"] == "modified" and "+FROM evil" in docker["diff"]


def test_b_failing_to_read_the_call_record_never_changes_the_build_and_the_trace_says_so(
    monkeypatch, proxy_cls, make_build_with_request, db_session
):
    control = make_build_with_request()
    h = harness(monkeypatch, proxy_cls)
    run_one_build(db_session, control, h.executor)
    expected = outcome(control)
    assert expected[0] == BuildStatus.SUCCEEDED and expected[3] == 15  # the control really succeeded, with cost

    proxy_cls.trace_result = ProxyStartError("trace lookup failed (1): b'boom'")
    build = make_build_with_request(name="second-project")
    done = run_one_build(db_session, build, h.executor)

    assert outcome(done) == expected  # same status, reason, detail and cost as when the trace read works
    stored = row(db_session, build)
    assert (stored.calls_state, stored.calls) == ("unavailable", None)
    assert stored.diff_state == "computed" and stored.log_tail == LOG_MARKER  # the other parts are unaffected


def test_b_a_call_record_that_is_not_a_record_is_unavailable_not_stored_as_given(monkeypatch, proxy_cls, make_build_with_request, db_session):
    proxy_cls.trace_result = {"steps": "not a list"}
    h = harness(monkeypatch, proxy_cls)
    build = make_build_with_request()

    assert run_one_build(db_session, build, h.executor).status == BuildStatus.SUCCEEDED

    assert row(db_session, build).calls_state == "unavailable"


def test_b_failing_to_persist_the_trace_never_changes_the_build_and_leaves_the_session_usable(
    monkeypatch, proxy_cls, make_build_with_request, db_session
):
    control = make_build_with_request()
    run_one_build(db_session, control, harness(monkeypatch, proxy_cls).executor)
    expected = outcome(control)

    # A diff the JSON column cannot hold: the INSERT itself fails, inside the savepoint.
    monkeypatch.setattr(sbe, "compute_diff", lambda **k: {"files": [object()], "files_total": 1, "truncated": False})
    build = make_build_with_request(name="second-project")
    done = run_one_build(db_session, build, harness(monkeypatch, proxy_cls).executor)

    assert outcome(done) == expected
    assert row(db_session, build) is None  # nothing half-written
    assert db_session.get(Build, build.id).status == BuildStatus.SUCCEEDED  # and the session still commits


def test_b_a_proxy_without_the_trace_read_is_a_build_without_a_trace(monkeypatch, proxy_cls, make_build_with_request, db_session):
    """A stale proxy image has no such route; the fakes of the older executor tests have no
    such method. Neither fails the build."""
    monkeypatch.delattr(TraceProxy, "trace")
    h = harness(monkeypatch, proxy_cls)
    build = make_build_with_request()

    assert run_one_build(db_session, build, h.executor).status == BuildStatus.SUCCEEDED
    assert row(db_session, build).calls_state == "unavailable"


def test_b_the_proxy_token_is_replaced_in_the_stored_log(monkeypatch, proxy_cls, make_build_with_request, db_session):
    h = harness(monkeypatch, proxy_cls, logs="env: ANTHROPIC_API_KEY=tok-TRACE-0123456789 done")
    build = make_build_with_request()

    run_one_build(db_session, build, h.executor)

    assert row(db_session, build).log_tail == "env: ANTHROPIC_API_KEY=[proxy-token-redacted] done"


def test_b_the_log_is_stored_as_its_256_kb_tail_and_marked_when_cut(monkeypatch, proxy_cls, make_build_with_request, db_session):
    logs = "head-line\n" + "m" * (300 * 1024) + "\ntail-line"
    h = harness(monkeypatch, proxy_cls, logs=logs)
    build = make_build_with_request()

    run_one_build(db_session, build, h.executor)

    stored = row(db_session, build)
    assert stored.log_truncated is True and len(stored.log_tail.encode("utf-8")) == 262_144
    assert stored.log_tail.endswith("tail-line") and "head-line" not in stored.log_tail


# --- (c) per-call cost and tokens reconcile with the build -------------------------------------------


class ChainProxy:
    """`ProxyContainer` over the REAL proxy app: admin calls through a `TestClient`, and a fake engine
    (the adapter below) driving model calls through the same app, so `usage()` and `trace()` are
    what the real proxy computed."""

    def __init__(self, *a, **k):
        self.upstream = ChainProxy.upstream
        self.registry = TokenRegistry()
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.upstream.app), base_url="http://u.test")
        self.app = TestClient(create_app(self.registry, real_api_key="k", admin_secret="s", http_client=http))
        ChainProxy.current = self

    base_url_for_sandbox = "http://proxy"
    upstream: Upstream
    current: "ChainProxy"

    def _admin(self, method, path, **kw):
        resp = self.app.request(method, path, headers={"X-Forge-Admin-Secret": "s"}, **kw)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def start(self, **k):
        pass

    def issue_token(self, build_id, *, token_budget, cost_budget_usd):
        return self._admin("POST", "/_forge_admin/tokens", json={"build_id": build_id, "token_budget": token_budget, "cost_budget_usd": cost_budget_usd})["token"]

    def drain(self, token, *, timeout_seconds):
        # the real route, so the trace is read from a token the proxy has CLOSED
        return self._admin("POST", f"/_forge_admin/tokens/{token}/drain?timeout_seconds={timeout_seconds}")["drained"]

    def usage(self, token):
        return self._admin("GET", f"/_forge_admin/tokens/{token}/usage")

    def trace(self, token):
        return self._admin("GET", f"/_forge_admin/tokens/{token}/trace")

    def revoke(self, token):
        self.app.delete(f"/_forge_admin/tokens/{token}", headers={"X-Forge-Admin-Secret": "s"})

    def stop(self):
        pass


def test_c_per_call_cost_and_tokens_sum_to_the_builds_figures(monkeypatch, make_build_with_request, db_session):
    cache = {"input_tokens": 1_000, "output_tokens": 100, "cache_read_input_tokens": 5_000, "cache_creation_input_tokens": 400,
             "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 300}}
    ChainProxy.upstream = Upstream(
        [
            sse_reply([("thinking", "hmm"), ("text", "Reading."), ("tool_use", "Read", {"file_path": "/workspace/a"})], usage=cache),
            json_reply([{"type": "text", "text": "All done."}], usage={"input_tokens": 2_000, "output_tokens": 250}),
        ]
    )
    h = Harness(monkeypatch)
    monkeypatch.setattr(sbe, "ProxyContainer", ChainProxy)
    real_adapter = h.executor._adapter

    class _Engine:
        def run(self, adapter_input):
            client, token = ChainProxy.current.app, adapter_input.proxy_token
            post = lambda messages: client.post("/v1/messages", headers={"x-api-key": token}, json={"model": "claude-opus-5-5", "messages": messages})
            assert post([{"role": "user", "content": "build it"}]).status_code == 200
            assert post([
                {"role": "user", "content": "build it"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "file contents"}]},
            ]).status_code == 200
            return real_adapter.run(adapter_input)

    h.executor._adapter = _Engine()
    build = make_build_with_request()

    done = run_one_build(db_session, build, h.executor)

    assert done.status == BuildStatus.SUCCEEDED
    steps = row(db_session, build).calls["steps"]
    assert [s["ordinal"] for s in steps] == [1, 2]
    assert [s["content"] for s in steps] == ["recorded", "recorded"]
    # The sums ARE the build's figures (and neither is zero: the test could not tell otherwise).
    assert done.cost_tokens == sum(s["tokens"] for s in steps) == (1_000 + 400 + 5) + (2_000 + 250)  # the stream's cumulative output (5) supersedes message_start's
    assert done.cost_usd == pytest.approx(sum(s["cost_usd"] for s in steps), rel=1e-12)
    assert done.cost_usd > 0
    # And the content that was new in each call.
    assert [b["kind"] for b in steps[0]["blocks"]] == ["assistant_text", "tool_use"]
    assert steps[0]["thinking"] == [{"chars": 3}] and steps[0]["stop_reason"] == "tool_use"
    assert [(b["kind"], b["text"]) for b in steps[1]["blocks"]] == [("assistant_text", "All done."), ("tool_result", "file contents")]


# --- (d) the read endpoint and its access predicate -----------------------------------------------------


def user(db, *, name, role) -> User:
    found = User(name=name, email=f"{name}@example.com", role=role)
    db.add(found)
    db.flush()
    return found


@pytest.fixture()
def world(db_session, seeded_ids):
    owner = user(db_session, name="trace-owner", role=Role.REQUESTER)
    other = user(db_session, name="trace-other", role=Role.REQUESTER)
    approver = db_session.query(User).filter_by(role=Role.APPROVER).first()
    admin = db_session.query(User).filter_by(role=Role.ADMIN).first()
    project = Project(
        name="traced", owner_id=owner.id, team_id=seeded_ids["team_id"], template_id=seeded_ids["template_id"], status=ProjectStatus.ACTIVE
    )
    db_session.add(project)
    db_session.flush()
    traced = Build(project_id=project.id, type="create", status=BuildStatus.SUCCEEDED)
    untraced = Build(project_id=project.id, type="create", status=BuildStatus.SUCCEEDED)
    db_session.add_all([traced, untraced])
    db_session.flush()
    bt.store_trace(
        db_session,
        traced,
        calls=normalise_calls(CALLS),
        diff_state="computed",
        diff={"files": [{"path": "app/a.py", "change": "added", "content": "text", "diff": "+DIFF-LINE\n", "truncated": False}], "files_total": 1, "truncated": False},
        logs="LOG-TEXT",
    )
    db_session.commit()
    return SimpleNamespace(owner=owner, other=other, approver=approver, admin=admin, project=project, traced=traced, untraced=untraced)


def get_trace(client, build, who: User | None):
    headers = {"X-Forge-User-Id": who.id} if who is not None else {}
    return client.get(f"/api/builds/{build.id}/trace", headers=headers)


@pytest.mark.parametrize("who,status", [("owner", 200), ("approver", 200), ("admin", 200), ("other", 403), (None, 401)])
def test_d_who_may_read_a_trace(client, world, who, status):
    resp = get_trace(client, world.traced, getattr(world, who) if who else None)

    assert resp.status_code == status, resp.text


def test_d_the_response_labels_measured_fields_and_claims_apart(client, world):
    body = get_trace(client, world.traced, world.owner).json()

    assert set(body) == {"build_id", "captured_at", "measured", "claims"}
    measured, claims = body["measured"], body["claims"]
    step = measured["calls"]["steps"][0]
    assert (step["ordinal"], step["model"], step["tokens"], step["stop_reason"], step["content"]) == (1, "claude-opus-5-5", 15, "tool_use", "recorded")
    assert step["thinking"] == [{"chars": 12}]
    assert "blocks" not in step  # no content among the measured fields
    assert measured["calls"]["state"] == "captured" and measured["diff"]["state"] == "computed"
    assert measured["diff"]["files"][0]["diff"] == "+DIFF-LINE\n"
    kinds = [(b["kind"], b.get("text")) for b in claims["calls"][0]["blocks"]]
    assert kinds == [("assistant_text", "CLAIM-TEXT"), ("tool_use", '{"file_path": "/w/a"}'), ("tool_result", "REPORTED-BY-L3")]
    assert claims["log"] == {"text": "LOG-TEXT", "truncated": False}
    assert "CLAIM-TEXT" not in json.dumps(measured) and "REPORTED-BY-L3" not in json.dumps(measured)


def test_d_the_access_check_runs_before_any_trace_is_loaded(client, world, monkeypatch):
    import app.routers.builds as builds_router

    def tripwire(*a, **k):
        raise AssertionError("trace content was loaded for a reader who may not see it")

    monkeypatch.setattr(builds_router, "load_trace_view", tripwire)

    assert get_trace(client, world.traced, world.other).status_code == 403
    assert get_trace(client, world.traced, None).status_code == 401


def test_d_an_unknown_build_and_a_build_without_a_trace_are_404(client, world):
    assert get_trace(client, SimpleNamespace(id="no-such-build"), world.owner).status_code == 404
    resp = get_trace(client, world.untraced, world.owner)
    assert resp.status_code == 404 and "no stored trace" in resp.json()["detail"]
    assert get_trace(client, world.untraced, world.other).status_code == 403  # still checked first


def test_d_the_predicate_is_owner_or_approver_and_above_and_fails_closed():
    me = SimpleNamespace(id="u1", role=Role.REQUESTER)

    assert may_read_build_trace(me, "u1") is True  # the owner, whatever their role
    assert may_read_build_trace(me, "u2") is False  # another requester
    assert may_read_build_trace(me, None) is False  # an ownerless project is not everyone's
    assert may_read_build_trace(SimpleNamespace(id="u1", role="superuser"), "u1") is True  # ownership needs no role
    assert may_read_build_trace(SimpleNamespace(id="u3", role="superuser"), "u1") is False  # an unknown role holds nothing
    assert may_read_build_trace(SimpleNamespace(id="u3", role=Role.APPROVER), "u1") is True
    assert may_read_build_trace(SimpleNamespace(id="u3", role=Role.ADMIN), "u1") is True
    assert may_read_build_trace(SimpleNamespace(id="u3", role=None), "u1") is False


def test_d_the_timeline_carries_a_has_trace_flag_and_no_trace_content(client, world):
    resp = client.get(f"/api/projects/{world.project.id}/timeline", headers={"X-Forge-User-Id": world.other.id})

    assert resp.status_code == 200  # the timeline stays open to every authenticated user
    builds = {e["data"]["id"]: e["data"] for e in resp.json() if e["kind"] == "build"}
    assert builds[world.traced.id]["has_trace"] is True
    assert builds[world.untraced.id]["has_trace"] is False
    text = resp.text
    for content in ("CLAIM-TEXT", "REPORTED-BY-L3", "DIFF-LINE", "LOG-TEXT", "toolu_1"):
        assert content not in text


# --- (e) deprovisioning and the migration ---------------------------------------------------------------


def test_e_deprovisioning_removes_the_projects_traces_and_keeps_its_builds_and_other_projects(
    client, db_session, seeded_ids, world, monkeypatch
):
    import app.routers.projects as projects_router
    from app.config import settings
    from tests.test_projects import FakeDockerClient

    monkeypatch.setattr(projects_router, "_docker_client", lambda: FakeDockerClient())
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)  # never reach a real Postgres from here
    bystander_project = Project(
        name="bystander", owner_id=world.owner.id, team_id=seeded_ids["team_id"], template_id=seeded_ids["template_id"], status=ProjectStatus.ACTIVE
    )
    db_session.add(bystander_project)
    db_session.flush()
    bystander = Build(project_id=bystander_project.id, type="create", status=BuildStatus.SUCCEEDED)
    db_session.add(bystander)
    db_session.flush()
    bt.store_trace(db_session, bystander, calls=None, diff_state="not_reached", diff=None, logs=None)
    world.project.status = ProjectStatus.ARCHIVED
    db_session.commit()
    assert row(db_session, world.traced) is not None

    resp = client.post(
        f"/api/projects/{world.project.id}/deprovision",
        headers={"X-Forge-User-Id": world.admin.id},
        json={"project_id": world.project.id},
    )

    assert resp.status_code == 200, resp.text
    assert row(db_session, world.traced) is None
    assert db_session.get(Build, world.traced.id) is not None  # the build itself stays
    assert row(db_session, bystander) is not None  # and so does everyone else's trace


def test_e_a_build_has_at_most_one_trace_row(db_session, world):
    from sqlalchemy.exc import IntegrityError

    db_session.add(BuildTrace(build_id=world.traced.id, calls_state="captured", diff_state="computed", log_truncated=False))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_e_the_proxy_container_reads_the_trace_over_its_own_admin_route(monkeypatch):
    proxy = ProxyContainer(client=SimpleNamespace())
    calls = []

    def fake_admin(method, path, *, body=None):
        calls.append((method, path))
        return 0, json.dumps({"steps": []}).encode()

    monkeypatch.setattr(proxy, "_exec_admin_request", fake_admin)
    assert proxy.trace("TKN") == {"steps": []}
    assert calls == [("GET", "/_forge_admin/tokens/TKN/trace")]

    monkeypatch.setattr(proxy, "_exec_admin_request", lambda *a, **k: (1, b"nope"))
    with pytest.raises(ProxyStartError):
        proxy.trace("TKN")


# --- (f) nothing reads the trace except its own display ---------------------------------------------------

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# The pin counts FIVE shapes of "this code touches the trace", per file and per shape, EXACTLY: a file
# holds as many as listed and any other file holds none, so a new use in a file that already has some
# fails as surely as one in a file that has none. (Counting one `import` per file let a
# file that already imported the service grow a consumer silently.)
#
#   storage        the model class `BuildTrace`, and the table name `build_traces` (a name, an
#                  attribute, or inside a string -- raw SQL).
#   module_import  an import of any module called `build_trace` (the model, the schemas, the service).
#   service_name   each name from the service: its definition, each imported alias, each use.
#   proxy_read     the second hop: a `.trace(...)` call, `trace_for` (definition or use), and a string
#                  ending in `/trace` (the admin route, in the proxy and in its client, and the read
#                  endpoint).
#   capture_field  an attribute read or write on a name `trace` -- the executor's `_TraceCapture`
#                  (`trace.logs`, `trace.diff`, ...). DECISION: pinned too, because that object is how
#                  the diff and the log travel inside the pipeline module, and a branch on one of them
#                  is a consumer. Any new `trace.<field>` in `app/` must be argued for here.
#
# A consumer must be argued for in EXPECTED_MENTIONS, with a reason.
SERVICE_NAMES = {
    "compute_diff",
    "normalise_calls",
    "store_trace",
    "load_trace_view",
    "build_ids_with_trace",
    "delete_traces_for_project",
    "bound_log",
}
PROXY_READ_NAMES = {"trace_for"}
CATEGORIES = ("storage", "module_import", "service_name", "proxy_read", "capture_field")

EXPECTED_MENTIONS: dict[str, dict[str, int]] = {
    "models/__init__.py": {"storage": 2, "module_import": 1},  # the import and its `__all__` entry
    "models/build_trace.py": {"storage": 1},  # the `__tablename__`
    "services/build_trace.py": {
        "storage": 9,  # the model import, the insert, and the three queries
        "module_import": 2,  # the model's module and the schemas module
        "service_name": 8,  # its seven definitions and the one call between them (`bound_log` in `store_trace`)
    },
    "routers/builds.py": {
        "module_import": 2,  # the schemas module and the service
        "service_name": 2,  # `load_trace_view`: the import and the one call, after the access check
        "proxy_read": 1,  # the endpoint's own `/trace` path
    },
    "routers/projects.py": {
        "module_import": 1,
        "service_name": 4,  # `build_ids_with_trace` and `delete_traces_for_project`: import + call each
    },
    "services/sandbox_build_executor.py": {
        "module_import": 1,
        "service_name": 6,  # `compute_diff`, `normalise_calls`, `store_trace`: the import tuple and one call each
        "proxy_read": 1,  # its one `proxy.trace(token)`
        # the `_TraceCapture` fields: set while the build runs, read in the `finally` and in `_store_trace`.
        # The repair loop adds 4: it copies the diff pair (`diff_state`, `diff`) into the
        # attempt it belongs to (2) and back onto the capture when that attempt is the one the build
        # describes again (2). A copy of display data from one holder to another: no branch reads it.
        "capture_field": 14,
    },
    "services/proxy_container.py": {"proxy_read": 2},  # `def trace` and its `/trace` path
    "services/llm_proxy.py": {"proxy_read": 3},  # the `/trace` route, `def trace_for`, and the route's `registry.trace_for(...)`
}


def _is_storage_name(value: str) -> bool:
    return value == "BuildTrace" or "build_traces" in value


def _name_hits(node: ast.AST) -> list[tuple[str, int]]:
    """The (category, line) pairs one AST node contributes."""
    hits: list[tuple[str, int]] = []
    line = getattr(node, "lineno", 0)
    if isinstance(node, ast.Name):
        if _is_storage_name(node.id):
            hits.append(("storage", line))
        if node.id in SERVICE_NAMES:
            hits.append(("service_name", line))
        if node.id in PROXY_READ_NAMES:
            hits.append(("proxy_read", line))
    elif isinstance(node, ast.Attribute):
        if _is_storage_name(node.attr):
            hits.append(("storage", line))
        if node.attr in SERVICE_NAMES:
            hits.append(("service_name", line))
        if node.attr in PROXY_READ_NAMES:
            hits.append(("proxy_read", line))
        if isinstance(node.value, ast.Name) and node.value.id == "trace":
            hits.append(("capture_field", line))
    elif isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "trace":
            hits.append(("proxy_read", line))
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        if _is_storage_name(node.value):
            hits.append(("storage", line))
        if node.value.endswith("/trace"):
            hits.append(("proxy_read", line))
    elif isinstance(node, ast.alias):
        last = node.name.rsplit(".", 1)[-1]
        if _is_storage_name(node.name):
            hits.append(("storage", line))
        if last == "build_trace":
            hits.append(("module_import", line))
        if node.name in SERVICE_NAMES:
            hits.append(("service_name", line))
    elif isinstance(node, ast.ImportFrom):
        if (node.module or "").rsplit(".", 1)[-1] == "build_trace":
            hits.append(("module_import", line))
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.name in SERVICE_NAMES:
            hits.append(("service_name", line))
        if node.name in PROXY_READ_NAMES or node.name == "trace":
            hits.append(("proxy_read", line))
    return hits


def _docstrings(tree_: ast.AST) -> set[int]:
    """ids of the string constants that are docstrings: prose about the trace is not a use of it."""
    ids: set[int] = set()
    for node in ast.walk(tree_):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _mentions(path: Path) -> dict[str, list[int]]:
    tree_ = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    prose = _docstrings(tree_)
    found: dict[str, list[int]] = {}
    for node in ast.walk(tree_):
        if id(node) in prose:
            continue
        for category, line in _name_hits(node):
            found.setdefault(category, []).append(line)
    return found


def mention_deviations(app_dir: Path, expected: dict[str, dict[str, int]]) -> dict[str, str]:
    found = {p.relative_to(app_dir).as_posix(): _mentions(p) for p in sorted(app_dir.rglob("*.py"))}
    out: dict[str, str] = {}
    for rel in sorted({r for r, hits in found.items() if hits} | set(expected)):
        for category in CATEGORIES:
            want = expected.get(rel, {}).get(category, 0)
            lines = found.get(rel, {}).get(category, [])
            if want != len(lines):
                out[f"{rel}:{category}"] = f"expected {want}, found {len(lines)} (lines {sorted(lines)})"
    return out


def test_f_only_the_expected_files_touch_the_trace_in_the_expected_ways():
    deviations = mention_deviations(APP_DIR, EXPECTED_MENTIONS)
    assert deviations == {}, (
        "the build trace is display-only: a new reader, in any shape, must be argued "
        f"for in EXPECTED_MENTIONS, not slipped in: {deviations}"
    )


def planted(tmp_path, source: str) -> dict[str, str]:
    (tmp_path / "gate.py").write_text(source, encoding="utf-8")
    return mention_deviations(tmp_path, {})


def test_f_the_pin_sees_a_planted_model_reader(tmp_path):
    """Non-vacuity: the scan counts an import of the model's module and a use of the class."""
    found = planted(tmp_path, "from app.models.build_trace import BuildTrace\n\ndef check(db):\n    return db.query(BuildTrace).all()\n")

    assert found == {
        "gate.py:storage": "expected 0, found 2 (lines [1, 4])",
        "gate.py:module_import": "expected 0, found 1 (lines [1])",
    }


def test_f_the_pin_sees_a_raw_sql_read(tmp_path):
    found = planted(tmp_path, "def read(db):\n    return db.execute('SELECT log_tail FROM build_traces')\n")

    assert list(found) == ["gate.py:storage"]


def test_f_a_new_use_of_a_service_function_through_an_import_the_file_already_has_is_seen(tmp_path):
    """The shape the first pin missed: the file's import is already expected, and only a USE is new."""
    source = (
        "from app.services.build_trace import load_trace_view\n\n"
        "def decide(db, build_id):\n    return load_trace_view(db, build_id) is not None\n"
    )
    (tmp_path / "gate.py").write_text(source, encoding="utf-8")
    already_expected = {"gate.py": {"module_import": 1, "service_name": 1}}  # the import alone, as the executor's is

    found = mention_deviations(tmp_path, already_expected)

    assert found == {"gate.py:service_name": "expected 1, found 2 (lines [1, 4])"}


def test_f_a_name_imported_from_the_service_is_counted_even_without_being_called(tmp_path):
    found = planted(tmp_path, "from app.services.build_trace import compute_diff, bound_log\n")

    assert found == {
        "gate.py:module_import": "expected 0, found 1 (lines [1])",
        "gate.py:service_name": "expected 0, found 2 (lines [1, 1])",
    }


def test_f_a_trace_read_of_the_proxy_hop_is_seen_in_any_module(tmp_path):
    found = planted(tmp_path, "def check(proxy, token):\n    return proxy.trace(token)['steps']\n")

    assert found == {"gate.py:proxy_read": "expected 0, found 1 (lines [2])"}


def test_f_trace_for_and_the_admin_route_are_seen(tmp_path):
    by_name = planted(tmp_path, "def check(registry, token):\n    return registry.trace_for(token)\n")
    by_route = planted(tmp_path, "URL = '/_forge_admin/tokens/x/trace'\n")

    assert list(by_name) == ["gate.py:proxy_read"] and list(by_route) == ["gate.py:proxy_read"]


def test_f_a_read_of_the_executors_capture_fields_is_seen(tmp_path):
    found = planted(tmp_path, "def decide(trace):\n    return trace.diff_state == 'computed'\n")

    assert found == {"gate.py:capture_field": "expected 0, found 1 (lines [2])"}
