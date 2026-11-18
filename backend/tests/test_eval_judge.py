"""one case, judged in order, first failure recorded.

No Docker: the HTTP layer is a stub that returns each failure shape. The default probe
(`PreviewRouteProbe`) is exercised against a fake `PreviewManager` that records what it was asked,
with real preview tokens, so the token check is the production one.
"""

import ast
import inspect
import json

import pytest

from app.services import eval_judge
from app.services.eval_judge import (
    BODY_READ_LIMIT_BYTES,
    CHECK_TIMEOUT_SECONDS,
    ProbeError,
    ProbeResponse,
    PreviewRouteProbe,
    judge_case,
)
from app.services.eval_request_set import Check
from app.services.preview_manager import PreviewStartError, hash_token, issue_preview_token

OK_PAGE = ProbeResponse(status=200, body=b"<h1>Forge Eval Static Page</h1>")
HEALTHY = ProbeResponse(status=200, body=b'{"status":"ok"}')

CHECKS = (
    Check("GET", "/", 200, "Forge Eval Static Page"),
    Check("POST", "/notes?text=abc", 200, "abc"),
)


class StubProbe:
    """Answers per (method, path); a value that is an exception is raised. Records every call."""

    def __init__(self, answers):
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, path):
        self.calls.append((method, path))
        answer = self.answers[(method, path)]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _judge(probe, *, status="SUCCEEDED", reason=None, checks=CHECKS):
    return judge_case(build_status=status, build_failure_reason=reason, checks=checks, probe=probe)


def _all_good():
    return StubProbe(
        {
            ("GET", "/healthz"): HEALTHY,
            ("GET", "/"): OK_PAGE,
            ("POST", "/notes?text=abc"): ProbeResponse(200, b'{"text": "abc"}'),
        }
    )


def test_a_case_that_satisfies_every_gate_passes_and_records_each_outcome():
    probe = _all_good()

    verdict = _judge(probe)

    assert verdict.result == "PASSED"
    assert verdict.failure_reason is None and verdict.failed_check_index is None
    assert probe.calls == [("GET", "/healthz"), ("GET", "/"), ("POST", "/notes?text=abc")]
    assert [(o["index"], o["ok"]) for o in verdict.outcomes] == [(None, True), (0, True), (1, True)]


@pytest.mark.parametrize("reason", ["AGENT_ERROR", "BUDGET_EXCEEDED", "TIMEOUT", "HARVEST_ERROR"])
def test_a_failed_build_is_judged_on_its_failure_code_and_nothing_is_requested(reason):
    probe = _all_good()

    verdict = _judge(probe, status="FAILED", reason=reason)

    assert (verdict.result, verdict.failure_reason, verdict.failed_check_index) == ("FAILED", reason, None)
    assert probe.calls == []  # the build gate comes first: no request is made for a build that failed
    assert verdict.outcomes == []


def test_a_build_that_is_not_succeeded_never_passes_even_if_a_preview_would_answer():
    for status in ("QUEUED", "RUNNING", "NOT_IMPLEMENTED"):
        verdict = _judge(_all_good(), status=status)
        assert verdict.result == "FAILED"


@pytest.mark.parametrize(
    "healthz",
    [
        ProbeResponse(503, b"down"),
        ProbeResponse(404, b""),
        ProbeResponse(302, b""),
        ProbeError("the build has no live preview"),
        TimeoutError("boom"),  # a probe that raises something unexpected must not escape
    ],
)
def test_a_preview_that_does_not_answer_healthz_with_200_is_unhealthy_and_no_check_is_made(healthz):
    probe = StubProbe({("GET", "/healthz"): healthz})

    verdict = _judge(probe)

    assert (verdict.result, verdict.failure_reason, verdict.failed_check_index) == (
        "FAILED",
        "PREVIEW_UNHEALTHY",
        None,
    )
    assert probe.calls == [("GET", "/healthz")]
    assert len(verdict.outcomes) == 1 and verdict.outcomes[0]["ok"] is False


def test_a_wrong_status_fails_that_check_with_its_index_and_later_checks_are_not_made():
    probe = StubProbe({("GET", "/healthz"): HEALTHY, ("GET", "/"): ProbeResponse(500, b"Forge Eval Static Page")})

    verdict = _judge(probe)

    assert (verdict.result, verdict.failure_reason, verdict.failed_check_index) == ("FAILED", "CHECK_FAILED", 0)
    assert probe.calls == [("GET", "/healthz"), ("GET", "/")]
    assert verdict.outcomes[-1]["outcome"] == "expected status 200, got 500"


def test_the_second_check_failing_records_index_one_and_keeps_the_first_outcome():
    probe = _all_good()
    probe.answers[("POST", "/notes?text=abc")] = ProbeResponse(500, b"")

    verdict = _judge(probe)

    assert (verdict.failure_reason, verdict.failed_check_index) == ("CHECK_FAILED", 1)
    assert [o["ok"] for o in verdict.outcomes] == [True, True, False]


def test_a_case_whose_check_substring_is_absent_from_the_body_does_not_pass():
    probe = StubProbe({("GET", "/healthz"): HEALTHY, ("GET", "/"): ProbeResponse(200, b"<h1>Something else</h1>")})

    verdict = _judge(probe, checks=(Check("GET", "/", 200, "Forge Eval Static Page"),))

    assert (verdict.result, verdict.failure_reason, verdict.failed_check_index) == ("FAILED", "CHECK_FAILED", 0)
    assert "expected text was not found" in verdict.outcomes[-1]["outcome"]


def test_a_check_with_no_substring_passes_on_status_alone():
    probe = StubProbe({("GET", "/healthz"): HEALTHY, ("GET", "/"): ProbeResponse(200, b"")})

    verdict = _judge(probe, checks=(Check("GET", "/", 200, None),))

    assert verdict.result == "PASSED"


def test_a_request_that_cannot_be_made_fails_the_check_not_the_judge():
    probe = _all_good()
    probe.answers[("GET", "/")] = ProbeError("timed out")

    verdict = _judge(probe)

    assert (verdict.failure_reason, verdict.failed_check_index) == ("CHECK_FAILED", 0)
    assert verdict.outcomes[-1]["status"] is None
    assert verdict.outcomes[-1]["outcome"] == "the request could not be made: timed out"


def test_only_the_bounded_prefix_of_a_body_is_searched():
    assert BODY_READ_LIMIT_BYTES == 65536  # the bound is part of the contract
    inside = b"x" * (BODY_READ_LIMIT_BYTES - 5) + b"MARKS"
    beyond = b"x" * BODY_READ_LIMIT_BYTES + b"MARKS"
    check = (Check("GET", "/", 200, "MARKS"),)

    found = _judge(StubProbe({("GET", "/healthz"): HEALTHY, ("GET", "/"): ProbeResponse(200, inside)}), checks=check)
    missed = _judge(StubProbe({("GET", "/healthz"): HEALTHY, ("GET", "/"): ProbeResponse(200, beyond)}), checks=check)

    assert found.result == "PASSED"
    assert missed.result == "FAILED" and missed.failure_reason == "CHECK_FAILED"


def test_a_response_body_is_never_stored_in_the_verdict():
    secret = "SECRET-BODY-MARKER-9f3"
    probe = StubProbe(
        {
            ("GET", "/healthz"): ProbeResponse(200, secret.encode()),
            ("GET", "/"): ProbeResponse(200, f"Forge Eval Static Page {secret}".encode()),
        }
    )
    failing = StubProbe(
        {("GET", "/healthz"): HEALTHY, ("GET", "/"): ProbeResponse(404, secret.encode())}
    )

    for verdict in (_judge(probe, checks=CHECKS[:1]), _judge(failing, checks=CHECKS[:1])):
        assert secret not in json.dumps(verdict.outcomes)
        assert secret not in repr(verdict)


def test_the_judge_is_given_the_build_status_and_code_not_the_build():
    """The agent's summary and the trace live on the build and its trace row; the judge's only
    inputs about the build are its status and its failure code."""
    assert list(inspect.signature(judge_case).parameters) == [
        "build_status",
        "build_failure_reason",
        "checks",
        "probe",
    ]
    # No code in the module (docstrings aside) names the summary, the trace, or the build model.
    names = set()
    for node in ast.walk(ast.parse(inspect.getsource(eval_judge))):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add(node.name)
    assert not names & {"agent_summary", "BuildTrace", "build_traces", "Build"}


# --- the default probe: the preview's token-checked route ---------------------------------------


class FakeManager:
    def __init__(self, *, response=(200, b"hello", "text/html"), error=None):
        self.response, self.error, self.calls = response, error, []

    def proxy(self, build_id, **kwargs):
        self.calls.append((build_id, kwargs))
        if self.error:
            raise self.error
        return self.response


def _probe(manager, *, build_id="b-1", url="/api/projects/p/preview/b-1?token={token}", token_ok=True):
    token, _ = issue_preview_token(build_id)
    return PreviewRouteProbe(
        build_id=build_id,
        preview_url=url.format(token=token) if url else url,
        token_hash=hash_token(token if token_ok else token + "x"),
        preview_manager=manager,
    )


def test_the_default_probe_forwards_through_the_preview_with_a_timeout_and_a_bounded_read():
    assert CHECK_TIMEOUT_SECONDS == 10.0
    manager = FakeManager(response=(200, b"x" * (BODY_READ_LIMIT_BYTES + 100), None))

    response = _probe(manager)("POST", "/notes?text=a")

    assert manager.calls == [
        (
            "b-1",
            {"method": "POST", "path": "/notes?text=a", "timeout_seconds": 10.0, "max_body_bytes": 65536},
        )
    ]
    assert response.status == 200 and len(response.body) == BODY_READ_LIMIT_BYTES


def test_a_token_that_does_not_match_the_stored_hash_is_refused_before_anything_is_forwarded():
    manager = FakeManager()

    with pytest.raises(ProbeError, match="token was refused"):
        _probe(manager, token_ok=False)("GET", "/")

    assert manager.calls == []


@pytest.mark.parametrize("url", [None, "", "/api/projects/p/preview/b-1", "/api/projects/p/preview/b-1?token="])
def test_a_build_with_no_preview_address_or_no_token_in_it_is_refused(url):
    manager = FakeManager()
    probe = PreviewRouteProbe(build_id="b-1", preview_url=url, token_hash="h", preview_manager=manager)

    with pytest.raises(ProbeError):
        probe("GET", "/")

    assert manager.calls == []


def test_an_unreachable_preview_is_a_probe_error_that_carries_no_container_output():
    manager = FakeManager(error=PreviewStartError("preview proxy exec failed (exit 1): b'LEAKED-APP-OUTPUT'"))

    with pytest.raises(ProbeError) as excinfo:
        _probe(manager)("GET", "/")

    assert "LEAKED-APP-OUTPUT" not in str(excinfo.value)


# --- the bounded read, in the script the preview container runs ------------------------------------


def test_the_exec_script_reads_at_most_the_bound_and_is_unchanged_without_one():
    """`max_body_bytes` reaches `resp.read(N)` inside the container. Run for real against a local
    server (no Docker): the body is cut at the bound, the status and content type are intact, and
    with no bound the script is byte-for-byte what it was before this task."""
    import http.server
    import socket
    import subprocess
    import sys
    import threading

    from app.services import preview_manager

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"0123456789" * 100)

        def log_message(self, *a):
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}/"
        bounded = preview_manager._build_proxy_exec_script(url, "GET", 5.0, 16)
        unbounded = preview_manager._build_proxy_exec_script(url, "GET", 5.0)
        out_bounded = subprocess.run([sys.executable, "-c", bounded], capture_output=True, timeout=10)
        out_unbounded = subprocess.run([sys.executable, "-c", unbounded], capture_output=True, timeout=10)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert preview_manager._parse_proxy_output(out_bounded.stdout) == (200, b"0123456789012345", "text/plain")
    assert len(preview_manager._parse_proxy_output(out_unbounded.stdout)[1]) == 1000
    assert "resp.read()" in unbounded and "e.read()" in unbounded  # exactly the old script
    assert "resp.read(16)" in bounded and "e.read(16)" in bounded
