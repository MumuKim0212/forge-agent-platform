"""A build whose preview smoke check measures a failing route may repair its own
output inside the same build, on the same proxy token and budget, and ships ONE attempt.

Unit seam, no Docker: the `Harness` of the harvest tests (fake proxy and network, real
sanitising copy, recorders for publish / image assembly, the real policy gate over the real DB
session) with a scripted adapter (one `Run` per attempt), a fake preview that records which image
is live, a scripted smoke check run through the real `run_smoke_check`, and a clock the tests move.
The real-Docker version is `test_sandbox_build_executor_integration.py`.

Every test turns the loop on itself (`conftest.py` starts each test with it off). Expected values
are literals: the prompt's lines, the tag suffix, the event names.
"""

from __future__ import annotations

import dataclasses
import glob
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models.approval import ApprovalRequest
from app.models.build_trace import BuildTrace
from app.models.policy_verdict import PolicyVerdict
from app.services import sandbox_build_executor as sbe
from app.services.agent_adapter import AgentAdapterResult
from app.services.eval_judge import ProbeError
from app.services.preview_smoke import SmokeResponse
from app.services.proxy_container import ProxyStartError
from app.services.publish import PublishError
from app.services.status import BuildFailureReason, PreviewStartFailure
from tests.harvest_support import make_output_dir
from tests.test_build_trace_capture import ChainProxy
from tests.test_build_trace_proxy import Upstream, json_reply
from tests.test_sandbox_build_executor_harvest import Harness, _snapshot

A0 = {"app/app.py": b"ATTEMPT-0\n", "app/only_in_0.txt": b"zero\n"}
A1 = {"app/app.py": b"ATTEMPT-1\n", "app/only_in_1.txt": b"one\n"}
A2 = {"app/app.py": b"ATTEMPT-2\n"}
NO_ENTRYPOINT = {"app/readme.txt": b"no app module here\n"}

BROKEN = {"/boom": 500, "/ok": 200}
FIXED = {"/boom": 200, "/ok": 200}


def usage(tokens=0, cost=0.0, **over) -> dict:
    return {
        "tokens": tokens,
        "cost_usd": cost,
        "budget_exceeded": False,
        "policy_refusals": [],
        "unmetered": False,
        "unmetered_reason": None,
        **over,
    }


@dataclass
class Run:
    """What one agent run does."""

    files: dict | None = field(default_factory=lambda: dict(A0))
    ok: bool = True
    raises: Exception | None = None
    summary: str = "agent summary"
    logs: str | None = None
    seconds: float = 0.0  # how long the run takes on the harness's clock
    no_output: bool = False


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def prober_for(statuses: dict[str, int]):
    """A smoke prober over a service exposing `statuses` as parameterless GET routes."""
    doc = {"openapi": "3.1.0", "paths": {path: {"get": {"responses": {}}} for path in statuses}}

    def probe(method, path, read_limit, timeout):
        if path == "/openapi.json":
            return SmokeResponse(200, [json.dumps(doc).encode()])
        return SmokeResponse(statuses[path], [b"BODY-MARKER-" + path.encode()])

    return probe


def unreadable_prober(method, path, read_limit, timeout):
    raise ProbeError("the preview did not answer")


class RepairHarness:
    def __init__(
        self,
        monkeypatch,
        runs: list[Run],
        *,
        smoke: list | None = None,
        proxy_usage: list[dict] | None = None,
        repairs: int = 1,
        preview_fails: tuple[int, ...] = (),
        publish_fails: tuple[int, ...] = (),
        drain: object = True,
        real_proxy: bool = False,
        trace_model: str = "claude-opus-5-5",
        revoke_raises: BaseException | None = None,
    ) -> None:
        monkeypatch.setattr(settings, "build_repair_max_attempts", repairs)
        self.h = Harness(monkeypatch)
        self.executor = self.h.executor
        self.runs = runs
        self.smoke = smoke if smoke is not None else [FIXED]
        self.proxy_usage = proxy_usage or [usage()]
        self.events: list[str] = []
        self.inputs: list[dict] = []
        self.output_dirs: list[str] = []
        self.removed_images: list[str] = []
        self.live_preview: str | None = None
        self.preview_starts = 0
        self.smoke_calls = 0
        self.clock = Clock()
        self.issued = 0
        self.real_proxy = real_proxy
        harness = self

        monkeypatch.setattr(sbe, "_utcnow", self.clock)

        class _Proxy:
            base_url_for_sandbox = "http://proxy"

            def __init__(self, *a, **k):
                pass

            def start(self, **k):
                pass

            def issue_token(self, *a, **k):
                harness.issued += 1
                return "tok"

            def _current(self):
                return harness.proxy_usage[min(max(len(harness.inputs) - 1, 0), len(harness.proxy_usage) - 1)]

            def drain(self, token, *, timeout_seconds):
                harness.events.append("drain")
                if isinstance(drain, BaseException):
                    raise drain
                return drain

            def usage(self, token):
                harness.events.append("usage")
                return dict(self._current())

            def trace(self, token):
                harness.events.append("trace")
                step = {"ordinal": 1, "model": trace_model, "content": "recorded", "tokens": 1, "cost_usd": 0.0}
                return {"steps": [step], "calls_total": 1}

            def revoke(self, *a):
                harness.events.append("revoke")
                if revoke_raises is not None:
                    raise revoke_raises

            def stop(self):
                harness.events.append("stop")

        if not real_proxy:
            monkeypatch.setattr(sbe, "ProxyContainer", _Proxy)
        else:
            monkeypatch.setattr(sbe, "ProxyContainer", ChainProxy)

        class _Adapter:
            def run(self, adapter_input):
                index = len(harness.inputs)
                harness.inputs.append(
                    {
                        "intent_text": adapter_input.intent_text,
                        "system_prompt": adapter_input.system_prompt,
                        "token": adapter_input.proxy_token,
                        "workspace_dir": adapter_input.workspace_dir,
                        "files": _snapshot(adapter_input.workspace_dir),
                    }
                )
                harness.events.append("adapter")
                run = harness.runs[min(index, len(harness.runs) - 1)]
                harness.clock.advance(run.seconds)
                if harness.real_proxy:
                    client = ChainProxy.current.app
                    reply = client.post(
                        "/v1/messages",
                        headers={"x-api-key": adapter_input.proxy_token},
                        json={"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "go"}]},
                    )
                    assert reply.status_code == 200, reply.text
                if run.raises is not None:
                    raise run.raises
                output_dir = None
                if run.files is not None and not run.no_output:
                    output_dir = make_output_dir(run.files)
                    harness.output_dirs.append(output_dir)
                return AgentAdapterResult(
                    ok=run.ok,
                    reason=None if run.ok else BuildFailureReason.TIMEOUT,
                    summary=run.summary,
                    logs=run.logs,
                    output_dir=output_dir,
                )

        self.executor._adapter = _Adapter()

        for name, fn in (("assemble_image", "assemble"), ("run_and_persist_policy_gate", "gate")):
            recorded = getattr(sbe, name)

            def _wrap(recorded=recorded, fn=fn):
                def wrapper(*args, **kwargs):
                    tag = kwargs.get("tag")
                    harness.events.append(f"{fn}:{tag}" if tag else fn)
                    return recorded(*args, **kwargs)

                return wrapper

            monkeypatch.setattr(sbe, name, _wrap())

        recorded_publish = sbe.publish_build

        def _publish(**kwargs):
            index = len(harness.h.publish_calls)
            harness.events.append("publish")
            harness.h.publish_error = PublishError("push refused") if index in publish_fails else None
            return recorded_publish(**kwargs)

        monkeypatch.setattr(sbe, "publish_build", _publish)

        def _start_preview(build):
            start = harness.preview_starts
            harness.preview_starts += 1
            harness.events.append(f"preview:{build.image_ref}")
            harness.live_preview = None  # a start replaces the build's earlier preview in place
            if start in preview_fails:
                build.preview_start_failure = PreviewStartFailure.CONTAINER_START_FAILED
                return
            harness.live_preview = build.image_ref
            build.preview_url = f"/api/projects/p/preview/b?token=t{start}"
            build.preview_token_hash = f"hash{start}"
            build.preview_expires_at = harness.clock.now + timedelta(minutes=60)

        monkeypatch.setattr(self.executor, "_start_preview", _start_preview)

        def _factory(build):
            harness.events.append("smoke")
            script = harness.smoke[min(harness.smoke_calls, len(harness.smoke) - 1)]
            harness.smoke_calls += 1
            if script is None:
                return unreadable_prober
            return script if callable(script) else prober_for(script)

        self.executor._smoke_prober_factory = _factory

        def _stop_preview(build_id):
            harness.events.append("stop_preview")
            harness.live_preview = None
            return True

        self.executor._preview_manager = SimpleNamespace(stop_preview=_stop_preview)

        def _remove(tag, force=False):
            harness.events.append(f"remove:{tag}")
            harness.removed_images.append(tag)

        self.executor._docker.images = SimpleNamespace(remove=_remove)

        class _Network:
            def remove(self):
                harness.events.append("network.remove")

        self.networks_created = 0

        def _create_network(*a, **k):
            harness.networks_created += 1
            return _Network()

        self.executor._docker.networks = SimpleNamespace(create=_create_network)

    def run(self, build):
        build.started_at = self.clock.now
        return self.executor.execute(build)

    def tag(self, build, suffix: str = "") -> str:
        return f"forge-build-{build.project_id}-{build.id}{suffix}"

    def adapter_runs(self) -> int:
        return len(self.inputs)

    def leaked_trees(self, build) -> list[str]:
        return glob.glob(os.path.join(tempfile.gettempdir(), f"forge-*-{build.id[:8]}-*"))


@pytest.fixture()
def build(make_build_with_request):
    return make_build_with_request()


# --- the predicate and the setting ------------------------------------------------------------


def test_the_executor_has_no_failing_route_comparison_of_its_own():
    """One named predicate (`preview_smoke.route_failed` / `has_failing_route`) answers 'is this a
    failing route'; the loop calls it and states no comparison or outcome word itself."""
    source = open(sbe.__file__, encoding="utf-8").read()
    assert "has_failing_route" in source and "failing_routes" in source
    for forbidden in ("server_error", "no_answer", ">= 500", ">=500", "status >= 5"):
        assert forbidden not in source, forbidden


# --- the trigger --------------------------------------------------------------------


def test_a_clean_smoke_check_ships_the_first_attempt_with_publish_last_and_one_drain(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0)], smoke=[FIXED])

    result = h.run(build)

    assert result.ok is True, result.summary
    assert h.adapter_runs() == 1 and build.repair_attempts == 0
    tag = h.tag(build)
    assert h.events == [
        "adapter",
        "usage",  # the running read: it does not close the token
        f"assemble:{tag}",
        f"preview:{tag}",
        "smoke",
        "drain",
        "usage",  # the final read, after the one drain
        "publish",
        "gate",
        "trace",
        "revoke",
        "stop",
        "network.remove",
    ]
    assert build.image_ref == tag and build.diff_ref == "forge-build-x#1"


def test_a_failing_route_starts_a_repair_and_the_repaired_attempt_is_what_ships(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED])

    result = h.run(build)

    assert result.ok is True, result.summary
    assert h.adapter_runs() == 2 and build.repair_attempts == 1
    assert len(h.h.publish_calls) == 1
    assert h.h.publish_calls[0]["files"]["app/app.py"] == b"ATTEMPT-1\n"
    assert build.image_ref == h.tag(build, "-r1")
    assert [r["outcome"] for r in build.smoke_checks["routes"]] == ["ok", "ok"]  # the shipped attempt's measurement
    assert result.agent_summary == "agent summary"


@pytest.mark.parametrize("status, repairs_expected", [(500, 1), (503, 1), (404, 0), (302, 0)])
def test_only_a_5xx_route_triggers_a_repair(monkeypatch, build, status, repairs_expected):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[{"/x": status}, FIXED])

    assert h.run(build).ok is True

    assert build.repair_attempts == repairs_expected
    assert h.adapter_runs() == 1 + repairs_expected


def hung_prober(method, path, read_limit, timeout):
    """A service whose only route never answers (the check's own deadline reads as `no_answer`)."""
    if path == "/openapi.json":
        return SmokeResponse(200, [json.dumps({"paths": {"/slow": {"get": {}}}}).encode()])
    raise ProbeError("the route did not answer in time")


def oversized_500_prober(method, path, read_limit, timeout):
    """A route that fails with a 500 and a body over the check's read limit: it is stored as
    `too_large` (that word wins over the status class), and it is still a failing route."""
    if path == "/openapi.json":
        return SmokeResponse(200, [json.dumps({"paths": {"/big": {"get": {}}}}).encode()])
    return SmokeResponse(500, [b"x" * 70_000])


def test_a_5xx_with_an_oversized_body_still_triggers_a_repair(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[oversized_500_prober, FIXED])

    assert h.run(build).ok is True

    assert build.repair_attempts == 1 and h.adapter_runs() == 2
    assert h.inputs[1]["intent_text"].count("GET /big -> 500") == 1


def test_a_route_that_did_not_answer_triggers_a_repair(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[hung_prober, FIXED])

    assert h.run(build).ok is True

    assert build.repair_attempts == 1 and h.adapter_runs() == 2
    assert h.inputs[1]["intent_text"].split("<measured-failures>" + chr(10))[1].startswith("GET /slow -> no answer" + chr(10))


def test_a_failed_agent_run_is_never_repaired(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(None, ok=False), Run(A1)], smoke=[BROKEN])

    result = h.run(build)

    assert result.ok is False and result.reason == BuildFailureReason.TIMEOUT
    assert h.adapter_runs() == 1 and build.repair_attempts == 0
    assert h.h.publish_calls == [] and h.h.assemble_calls == []


def test_a_recorded_refusal_fails_the_build_before_anything_is_built_and_is_never_repaired(monkeypatch, build):
    refused = usage(10, 0.01, policy_refusals=["tool policy: tool type 'web_search_20250305' is not allowed"])
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN], proxy_usage=[refused])

    result = h.run(build)

    assert result.ok is False and result.reason == BuildFailureReason.AGENT_ERROR
    assert h.adapter_runs() == 1 and build.repair_attempts == 0
    assert h.h.assemble_calls == [] and h.preview_starts == 0 and h.h.publish_calls == []


def test_policy_findings_do_not_trigger_a_repair(monkeypatch, build, db_session):
    leaked = {**A0, "app/leaked.py": b"AWS_ACCESS_KEY_ID = 'AKIAABCDEFGHIJKLMNOP'\n"}
    h = RepairHarness(monkeypatch, [Run(leaked), Run(A1)], smoke=[FIXED])

    result = h.run(build)

    assert result.ok is True
    assert db_session.query(PolicyVerdict).filter_by(build_id=build.id).one().risk_tier == "high"
    assert h.adapter_runs() == 1 and build.repair_attempts == 0


def test_a_missing_entrypoint_is_never_repaired(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(NO_ENTRYPOINT), Run(A1)], smoke=[BROKEN])

    result = h.run(build)

    assert result.ok is False and result.reason == BuildFailureReason.MISSING_ENTRYPOINT
    assert h.adapter_runs() == 1 and build.repair_attempts == 0


def test_a_smoke_check_that_could_not_run_is_never_a_reason_to_repair(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[None])

    result = h.run(build)

    assert result.ok is True and build.smoke_checks is None
    assert h.adapter_runs() == 1 and build.repair_attempts == 0


def test_with_the_loop_off_a_failing_route_changes_nothing_and_the_order_is_the_old_one(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED], repairs=0)

    result = h.run(build)

    assert result.ok is True and result.reason is None
    tag = h.tag(build)
    assert h.events == [
        "adapter",
        "drain",
        "usage",
        "publish",
        f"assemble:{tag}",
        f"preview:{tag}",
        "smoke",
        "gate",
        "trace",
        "revoke",
        "stop",
        "network.remove",
    ]
    assert build.repair_attempts == 0
    assert any(r["outcome"] == "server_error" for r in build.smoke_checks["routes"])  # recorded, acted on by no one
    assert build.image_ref == tag


def test_repair_attempts_are_capped_by_the_setting_and_the_last_completed_attempt_ships(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1), Run(A2)], smoke=[BROKEN], repairs=1)

    assert h.run(build).ok is True

    assert h.adapter_runs() == 2 and build.repair_attempts == 1
    assert h.h.publish_calls[0]["files"]["app/app.py"] == b"ATTEMPT-1\n"
    assert any(r["outcome"] == "server_error" for r in build.smoke_checks["routes"])  # an unfixed route still shows


def test_a_second_repair_starts_when_the_first_did_not_fix_it_and_the_setting_allows(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1), Run(A2)], smoke=[BROKEN, BROKEN, FIXED], repairs=2)

    assert h.run(build).ok is True

    assert h.adapter_runs() == 3 and build.repair_attempts == 2
    assert build.image_ref == h.tag(build, "-r2")
    assert h.h.publish_calls[0]["files"]["app/app.py"] == b"ATTEMPT-2\n"


# --- fresh single-use runs, one token, one drain ---------------------------------


def test_each_attempt_is_a_fresh_workspace_on_the_same_token_and_the_drain_comes_once_after_the_last(monkeypatch, build):
    h = RepairHarness(
        monkeypatch,
        [Run(A0), Run(A1)],
        smoke=[BROKEN, FIXED],
        proxy_usage=[usage(100, 0.10), usage(300, 0.30)],
    )

    assert h.run(build).ok is True

    first, second = h.inputs
    assert first["token"] == second["token"] == "tok" and h.issued == 1
    assert first["workspace_dir"] != second["workspace_dir"]
    assert h.events.count("drain") == 1
    assert h.events.index("drain") > max(i for i, e in enumerate(h.events) if e == "adapter")
    assert (build.cost_tokens, build.cost_usd) == (300, 0.30)  # the proxy's one figure for both attempts


def test_with_the_real_proxy_both_attempts_use_the_token_and_the_trace_sums_to_the_builds_cost(
    monkeypatch, build, db_session
):
    ChainProxy.upstream = Upstream(
        [
            json_reply([{"type": "text", "text": "first"}], usage={"input_tokens": 1_000, "output_tokens": 100}),
            json_reply([{"type": "text", "text": "second"}], usage={"input_tokens": 2_000, "output_tokens": 250}),
        ]
    )
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED], real_proxy=True)

    result = h.run(build)

    assert result.ok is True, result.summary
    assert build.repair_attempts == 1 and h.adapter_runs() == 2
    db_session.expire_all()
    steps = db_session.query(BuildTrace).filter_by(build_id=build.id).one().calls["steps"]
    assert len(steps) == 2  # one model call per attempt, both on the one token
    assert build.cost_tokens == sum(s["tokens"] for s in steps) == 1_100 + 2_250
    assert build.cost_usd == pytest.approx(sum(s["cost_usd"] for s in steps), rel=1e-12)
    assert build.cost_usd > 0


# --- what the agent is told ------------------------------------------------------------


def test_the_repair_workspace_is_the_previous_sanitised_output_with_the_protected_paths_restored(monkeypatch, build):
    tampered = {**A0, "Dockerfile": b"FROM alpine:3.19\nCMD echo TAMPERED\n", ".forge/result.json": b"{}"}
    h = RepairHarness(monkeypatch, [Run(tampered), Run(A1)], smoke=[BROKEN, FIXED])

    assert h.run(build).ok is True

    repair_input = h.inputs[1]["files"]
    assert repair_input["app/app.py"] == b"ATTEMPT-0\n" and repair_input["app/only_in_0.txt"] == b"zero\n"
    assert "app/staged_only.py" not in repair_input  # not the first attempt's staged input
    pinned = open(os.path.join(settings.template_skeleton_dir, "Dockerfile"), "rb").read()
    assert repair_input["Dockerfile"] == pinned and b"TAMPERED" not in repair_input["Dockerfile"]
    assert not any(p == ".forge" or p.startswith(".forge/") for p in repair_input)  # the platform's channel is not carried over


def test_the_repair_prompt_is_the_requesters_request_plus_the_measured_failing_routes(monkeypatch, make_build_with_request):
    build = make_build_with_request(nl_request="Build a room board.")
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[{"/boom": 500, "/ok": 200, "/zzz": 503}, FIXED])

    assert h.run(build).ok is True

    first, second = h.inputs[0]["intent_text"], h.inputs[1]["intent_text"]
    assert second.startswith(first)  # the same requester-marked request, unchanged
    assert "<requester-request>\nBuild a room board.\n</requester-request>" in second
    block = second.split("<measured-failures>\n")[1].split("\n</measured-failures>")[0]
    assert block == "GET /boom -> 500\nGET /zzz -> 503"  # only the failing routes, as measured, in stored order
    assert "<measured-failures>" not in first


def test_nothing_but_the_measured_routes_reaches_the_repair_prompt(monkeypatch, build):
    """A response body, a log line, the agent's summary, the proxy's call record and a file's content are
    all in scope when the repair prompt is composed; none of them is in it."""
    first = Run(
        {**A0, "app/notes.txt": b"FILE-MARKER\n"}, summary="SUMMARY-MARKER", logs="LOG-MARKER-0123 from the sandbox"
    )
    h = RepairHarness(monkeypatch, [first, Run(A1)], smoke=[BROKEN, FIXED], trace_model="TRACE-MARKER")

    result = h.run(build)

    assert result.ok is True and build.repair_attempts == 1
    repair_prompt = h.inputs[1]["intent_text"]
    for marker in ("BODY-MARKER", "SUMMARY-MARKER", "LOG-MARKER", "TRACE-MARKER", "FILE-MARKER", "server_error"):
        assert marker not in repair_prompt, marker
    assert "GET /boom -> 500" in repair_prompt  # and the one thing that should be there is


def test_the_system_prompt_of_a_repair_is_the_one_every_build_gets(monkeypatch, build):
    from app.services.agent_prompt import compose_system_prompt

    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED])

    assert h.run(build).ok is True

    assert h.inputs[0]["system_prompt"] == h.inputs[1]["system_prompt"] == compose_system_prompt("create")


# --- the limits ----------------------------------------------------------------------------


def test_a_repair_starts_when_the_largest_earlier_attempt_fits_in_what_is_left(monkeypatch, build):
    monkeypatch.setattr(settings, "build_cost_budget_usd", 5.0)
    h = RepairHarness(monkeypatch, [Run(A0, seconds=300), Run(A1)], smoke=[BROKEN, FIXED], proxy_usage=[usage(1_000, 1.0), usage(2_000, 2.0)])

    assert h.run(build).ok is True

    assert h.adapter_runs() == 2  # $1.00 spent, $4.00 left, an attempt cost $1.00; 5 of 20 minutes used


def test_no_repair_starts_when_the_largest_earlier_attempt_does_not_fit_the_budget_left(monkeypatch, build):
    monkeypatch.setattr(settings, "build_cost_budget_usd", 1.5)
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED], proxy_usage=[usage(1_000, 1.0), usage(2_000, 2.0)])

    result = h.run(build)

    assert result.ok is True and h.adapter_runs() == 1 and build.repair_attempts == 0  # $0.50 left, an attempt cost $1.00
    assert any(r["outcome"] == "server_error" for r in build.smoke_checks["routes"])


def test_no_repair_starts_when_the_longest_earlier_attempt_does_not_fit_the_wall_clock_left(monkeypatch, build):
    monkeypatch.setattr(settings, "build_max_duration_minutes", 20)
    h = RepairHarness(monkeypatch, [Run(A0, seconds=15 * 60), Run(A1)], smoke=[BROKEN, FIXED])

    result = h.run(build)

    assert result.ok is True and h.adapter_runs() == 1 and build.repair_attempts == 0  # 5 minutes left, an attempt took 15


def test_a_repair_that_fits_the_wall_clock_exactly_starts(monkeypatch, build):
    monkeypatch.setattr(settings, "build_max_duration_minutes", 20)
    h = RepairHarness(monkeypatch, [Run(A0, seconds=10 * 60), Run(A1)], smoke=[BROKEN, FIXED])

    assert h.run(build).ok is True

    assert h.adapter_runs() == 2  # 10 minutes left, an attempt took 10


def test_no_repair_starts_when_the_tokens_left_do_not_fit_either(monkeypatch, build):
    monkeypatch.setattr(settings, "build_token_budget", 1_500)
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED], proxy_usage=[usage(1_000, 0.1), usage(2_000, 0.2)])

    assert h.run(build).ok is True

    assert h.adapter_runs() == 1  # 500 tokens left, an attempt used 1,000


def test_no_repair_starts_for_a_build_with_no_start_time_to_measure_the_wall_clock_from(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED])

    result = h.executor.execute(build)  # `run` would set `started_at`

    assert result.ok is True and h.adapter_runs() == 1


# --- the outcome rules: the last completed attempt ships ------------------------------------


def test_a_repair_whose_agent_failed_is_discarded_and_the_first_attempt_ships(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(None, ok=False)], smoke=[BROKEN])

    result = h.run(build)

    assert result.ok is True and result.reason is None
    assert build.repair_attempts == 1  # it was started
    assert h.h.publish_calls[0]["files"]["app/app.py"] == b"ATTEMPT-0\n"
    assert build.image_ref == h.tag(build) and h.live_preview == h.tag(build)
    assert any(r["outcome"] == "server_error" for r in build.smoke_checks["routes"])


def test_a_repair_that_loses_the_entrypoint_is_discarded_and_the_first_attempt_ships(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(NO_ENTRYPOINT)], smoke=[BROKEN])

    result = h.run(build)

    assert result.ok is True and build.repair_attempts == 1
    assert h.h.publish_calls[0]["files"]["app/app.py"] == b"ATTEMPT-0\n"
    assert h.preview_starts == 1  # the discarded attempt never reached an image or a preview
    # The change set was measured for the discarded attempt before its entrypoint was checked; the
    # build carries the shipped attempt's again.
    paths = {entry["path"] for entry in build.changed_files["entries"]}
    assert "app/only_in_0.txt" in paths and "app/readme.txt" not in paths


def test_a_repair_whose_preview_cannot_start_is_discarded_and_the_first_attempts_preview_is_restored(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN], preview_fails=(1,))

    result = h.run(build)

    assert result.ok is True and build.repair_attempts == 1
    assert h.h.publish_calls[0]["files"]["app/app.py"] == b"ATTEMPT-0\n"
    tag0, tag1 = h.tag(build), h.tag(build, "-r1")
    assert [e for e in h.events if e.startswith("preview:")] == [f"preview:{tag0}", f"preview:{tag1}", f"preview:{tag0}"]
    assert h.live_preview == tag0 and build.image_ref == tag0
    assert build.preview_url == "/api/projects/p/preview/b?token=t2" and build.preview_start_failure is None
    assert tag1 in h.removed_images and tag0 not in h.removed_images


def test_a_repair_whose_publish_fails_is_discarded_and_the_first_attempt_is_published_instead(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED], publish_fails=(0,))

    result = h.run(build)

    assert result.ok is True and build.repair_attempts == 1
    assert [call["files"]["app/app.py"] for call in h.h.publish_calls] == [b"ATTEMPT-1\n", b"ATTEMPT-0\n"]
    assert build.image_ref == h.tag(build) and h.live_preview == h.tag(build)
    assert build.diff_ref == "forge-build-x#1"
    assert h.tag(build, "-r1") in h.removed_images


def test_a_first_attempt_that_cannot_be_published_still_fails_the_build(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0)], smoke=[FIXED], publish_fails=(0,))

    result = h.run(build)

    assert result.ok is False and result.reason == BuildFailureReason.HARVEST_ERROR
    assert build.image_ref is None and build.preview_url is None and h.live_preview is None
    assert h.tag(build) in h.removed_images


def test_a_recorded_refusal_during_the_repair_fails_the_build_and_ships_nothing(monkeypatch, build):
    refused = usage(20, 0.02, policy_refusals=["tool policy: tool type 'web_search_20250305' is not allowed"])
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED], proxy_usage=[usage(10, 0.01), refused])

    result = h.run(build)

    assert result.ok is False and result.reason == BuildFailureReason.AGENT_ERROR
    assert "refused by tool policy" in result.summary
    assert h.adapter_runs() == 2 and h.h.publish_calls == []
    assert build.image_ref is None and build.preview_url is None and h.live_preview is None
    assert h.tag(build) in h.removed_images  # the first attempt, which had already passed, is cleaned up


def test_an_unmetered_response_during_the_repair_fails_the_build_as_budget_exceeded(monkeypatch, build):
    unmetered = usage(20, 0.02, unmetered=True, unmetered_reason="a streamed reply carried no usage")
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED], proxy_usage=[usage(10, 0.01), unmetered])

    result = h.run(build)

    assert result.ok is False and result.reason == BuildFailureReason.BUDGET_EXCEEDED
    assert "could not meter a response" in result.summary
    assert h.h.publish_calls == [] and h.live_preview is None


def test_a_drain_that_fails_after_the_repair_fails_the_build_with_the_cost_left_unknown(monkeypatch, build):
    h = RepairHarness(
        monkeypatch,
        [Run(A0), Run(A1)],
        smoke=[BROKEN, FIXED],
        proxy_usage=[usage(10, 0.01), usage(20, 0.02)],
        drain=ProxyStartError("drain failed (1)"),
    )

    result = h.run(build)

    assert result.ok is False and result.reason == BuildFailureReason.INFRA_ERROR
    assert "cost could not be read back" in result.summary or "cost is unknown" in result.summary
    assert (build.cost_tokens, build.cost_usd) == (None, None)  # never a short figure
    assert h.h.publish_calls == [] and h.live_preview is None


def test_a_repair_that_ran_the_budget_out_is_discarded_and_does_not_fail_a_build_that_had_passed(monkeypatch, build):
    h = RepairHarness(
        monkeypatch,
        [Run(A0), Run(A1)],
        smoke=[BROKEN],
        proxy_usage=[usage(10, 0.01), usage(500, 4.99, budget_exceeded=True)],
    )

    result = h.run(build)

    assert result.ok is True and build.repair_attempts == 1
    assert h.h.publish_calls[0]["files"]["app/app.py"] == b"ATTEMPT-0\n"
    assert (build.cost_tokens, build.cost_usd) == (500, 4.99)  # what the proxy measured, both attempts


def test_a_first_attempt_that_ran_the_budget_out_still_fails_the_build(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0)], smoke=[BROKEN], proxy_usage=[usage(500, 5.2, budget_exceeded=True)])

    result = h.run(build)

    assert result.ok is False and result.reason == BuildFailureReason.BUDGET_EXCEEDED
    assert h.preview_starts == 0 and h.h.publish_calls == []


# --- one of each, made from the shipped attempt ------------------------------------------------------------


def test_a_two_attempt_build_has_one_pr_one_gate_run_one_approval_one_live_preview_and_one_image(
    monkeypatch, build, db_session
):
    h = RepairHarness(monkeypatch, [Run(A0, summary="FIRST-SUMMARY"), Run(A1, summary="SECOND-SUMMARY")], smoke=[BROKEN, FIXED])

    result = h.run(build)

    assert result.ok is True
    assert len(h.h.publish_calls) == 1 and h.h.publish_calls[0]["pr_body"] == "SECOND-SUMMARY"
    assert len(h.h.gate_calls) == 1 and h.h.gate_calls[0]["files"]["app/app.py"] == b"ATTEMPT-1\n"
    assert db_session.query(PolicyVerdict).filter_by(build_id=build.id).count() == 1
    assert db_session.query(ApprovalRequest).filter_by(build_id=build.id).count() == 1
    assert h.preview_starts == 2 and h.live_preview == h.tag(build, "-r1")
    assert build.preview_url == "/api/projects/p/preview/b?token=t1"
    assert h.removed_images == [h.tag(build)]  # the superseded attempt's image; the shipped one stays


# --- nothing leaks --------------------------------


def _assert_nothing_leaked(h: RepairHarness, build) -> None:
    assert h.leaked_trees(build) == []
    for path in h.output_dirs + [i["workspace_dir"] for i in h.inputs]:
        assert not os.path.exists(path), path


def test_every_temp_tree_of_every_attempt_is_removed_after_a_repaired_build(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED])

    assert h.run(build).ok is True

    _assert_nothing_leaked(h, build)


def test_every_temp_tree_is_removed_after_a_discarded_repair(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1, ok=False)], smoke=[BROKEN])

    assert h.run(build).ok is True

    _assert_nothing_leaked(h, build)


def test_every_temp_tree_is_removed_when_the_build_fails_after_a_repair(monkeypatch, build):
    refused = usage(20, 0.02, policy_refusals=["tool policy: tool type 'web_search_20250305' is not allowed"])
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED], proxy_usage=[usage(), refused])

    assert h.run(build).ok is False

    _assert_nothing_leaked(h, build)


def test_every_temp_tree_is_removed_when_something_unexpected_is_raised_mid_loop(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(raises=KeyboardInterrupt())], smoke=[BROKEN])

    with pytest.raises(KeyboardInterrupt):
        h.run(build)

    _assert_nothing_leaked(h, build)
    assert h.live_preview is None  # the first attempt's preview does not outlive a build that did not ship


def test_with_the_loop_on_the_order_is_preview_check_then_the_next_attempts_preview_check_then_the_gate(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED])

    assert h.run(build).ok is True

    tag0, tag1 = h.tag(build), h.tag(build, "-r1")
    steps = [e for e in h.events if e.split(":")[0] in ("adapter", "assemble", "preview", "smoke", "drain", "publish", "gate")]
    assert steps == [
        "adapter",
        f"assemble:{tag0}",
        f"preview:{tag0}",
        "smoke",
        "adapter",
        f"assemble:{tag1}",
        f"preview:{tag1}",
        "smoke",
        "drain",
        "publish",
        "gate",
    ]


# --- builds.repair_routes: why each repair attempt started --------------------------------------------

BOOM_500 = [{"attempt": 1, "routes": [{"path": "/boom", "status": 500, "outcome": "server_error"}]}]


def test_a_started_repair_stores_the_routes_it_was_told_to_fix_with_its_attempt_number(monkeypatch, build, db_session):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED])

    assert h.run(build).ok is True

    # The first attempt's smoke fact is gone (the shipped attempt's replaces it); the reason survives.
    assert [r["outcome"] for r in build.smoke_checks["routes"]] == ["ok", "ok"]
    assert build.repair_routes == BOOM_500 and build.repair_attempts == 1
    # What the agent was told is what is stored: the one failing route, not the passing /ok.
    assert h.inputs[1]["intent_text"].split("<measured-failures>\n")[1].split("\n</measured-failures>")[0] == "GET /boom -> 500"
    db_session.commit()
    db_session.expire_all()
    assert build.repair_routes == BOOM_500  # and it survives a round trip through the column


def test_two_repairs_store_two_entries_each_with_its_own_routes_and_the_count_agrees(monkeypatch, build):
    first = {"/a": 500, "/b": 200}
    second = {"/a": 503, "/c": 500, "/b": 200}
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1), Run(A2)], smoke=[first, second, FIXED], repairs=2)

    assert h.run(build).ok is True

    assert build.repair_routes == [
        {"attempt": 1, "routes": [{"path": "/a", "status": 500, "outcome": "server_error"}]},
        {
            "attempt": 2,
            "routes": [
                {"path": "/a", "status": 503, "outcome": "server_error"},
                {"path": "/c", "status": 500, "outcome": "server_error"},
            ],
        },
    ]
    assert len(build.repair_routes) == build.repair_attempts == 2


def test_a_route_that_did_not_answer_is_stored_with_a_null_status_and_its_outcome_word(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[hung_prober, FIXED])

    assert h.run(build).ok is True

    assert build.repair_routes == [{"attempt": 1, "routes": [{"path": "/slow", "status": None, "outcome": "no_answer"}]}]


def test_the_stored_routes_are_the_same_list_the_prompt_was_made_from_and_no_longer_than_the_check_records(monkeypatch, build):
    from app.services.preview_smoke import MAX_ROUTES

    many = {f"/r{i:03d}": 500 for i in range(MAX_ROUTES + 5)}
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[many, FIXED])

    assert h.run(build).ok is True

    (entry,) = build.repair_routes
    assert 1 <= len(entry["routes"]) <= MAX_ROUTES
    prompt_block = h.inputs[1]["intent_text"].split("<measured-failures>\n")[1].split("\n</measured-failures>")[0]
    assert [f"GET {r['path']} -> {r['status']}" for r in entry["routes"]] == prompt_block.split("\n")


def test_the_loop_off_stores_null_even_though_a_failing_route_was_measured(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED], repairs=0)

    assert h.run(build).ok is True

    assert any(r["outcome"] == "server_error" for r in build.smoke_checks["routes"])  # a failing route is on the record
    assert build.repair_routes is None and build.repair_attempts == 0


def test_a_clean_first_attempt_stores_null_not_an_empty_list(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[FIXED])

    assert h.run(build).ok is True

    assert build.smoke_checks is not None and build.repair_routes is None and build.repair_attempts == 0


def test_a_first_attempt_that_failed_stores_null(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(None, ok=False), Run(A1)], smoke=[BROKEN])

    result = h.run(build)

    assert result.ok is False and h.adapter_runs() == 1
    assert build.repair_routes is None and build.repair_attempts == 0


def test_a_repair_that_did_not_fit_the_budget_stores_null(monkeypatch, build):
    monkeypatch.setattr(settings, "build_cost_budget_usd", 1.5)
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED], proxy_usage=[usage(1_000, 1.0), usage(2_000, 2.0)])

    assert h.run(build).ok is True

    assert h.adapter_runs() == 1 and build.repair_routes is None and build.repair_attempts == 0


def test_a_discarded_repair_keeps_its_entry_and_the_first_attempt_ships(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(None, ok=False)], smoke=[BROKEN])

    result = h.run(build)

    assert result.ok is True
    assert h.h.publish_calls[0]["files"]["app/app.py"] == b"ATTEMPT-0\n"  # the repair did not ship
    assert build.repair_attempts == 1 and build.repair_routes == BOOM_500


def test_a_second_repair_discarded_after_the_first_stood_keeps_both_entries(monkeypatch, build):
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1), Run(None, ok=False)], smoke=[BROKEN, BROKEN], repairs=2)

    assert h.run(build).ok is True

    assert h.h.publish_calls[0]["files"]["app/app.py"] == b"ATTEMPT-1\n"
    assert build.repair_attempts == 2
    assert [entry["attempt"] for entry in build.repair_routes] == [1, 2]


def test_a_build_the_loop_fails_after_a_repair_started_keeps_the_entry_like_the_count_does(monkeypatch, build):
    refused = usage(20, 0.02, policy_refusals=["tool policy: tool type 'web_search_20250305' is not allowed"])
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED], proxy_usage=[usage(10, 0.01), refused])

    result = h.run(build)

    assert result.ok is False and result.reason == BuildFailureReason.AGENT_ERROR
    assert build.repair_attempts == 1 and build.repair_routes == BOOM_500


def test_a_failed_drain_after_a_repair_keeps_the_entry_like_the_count_does(monkeypatch, build):
    h = RepairHarness(
        monkeypatch,
        [Run(A0), Run(A1)],
        smoke=[BROKEN, FIXED],
        proxy_usage=[usage(10, 0.01), usage(20, 0.02)],
        drain=ProxyStartError("drain failed (1)"),
    )

    result = h.run(build)

    assert result.ok is False and result.reason == BuildFailureReason.INFRA_ERROR
    assert build.repair_attempts == 1 and build.repair_routes == BOOM_500


def test_an_unmetered_response_after_a_repair_keeps_the_entry_like_the_count_does(monkeypatch, build):
    unmetered = usage(20, 0.02, unmetered=True, unmetered_reason="a streamed reply carried no usage")
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED], proxy_usage=[usage(10, 0.01), unmetered])

    result = h.run(build)

    assert result.ok is False and result.reason == BuildFailureReason.BUDGET_EXCEEDED
    assert build.repair_attempts == 1 and build.repair_routes == BOOM_500


def test_nothing_but_the_measured_routes_is_stored(monkeypatch, build):
    """A response body, a log line, the agent summary, the proxy's call record and a file's content
    are all in scope when the entry is written; none of it is in it, and its keys are exactly three."""
    first = Run(
        {**A0, "app/notes.txt": b"FILE-MARKER\n"}, summary="SUMMARY-MARKER", logs="LOG-MARKER-0123 from the sandbox"
    )
    h = RepairHarness(monkeypatch, [first, Run(A1, summary="SUMMARY-MARKER-2")], smoke=[BROKEN, FIXED], trace_model="TRACE-MARKER")

    result = h.run(build)

    assert result.ok is True
    stored = json.dumps(build.repair_routes)
    for marker in ("BODY-MARKER", "SUMMARY-MARKER", "LOG-MARKER", "TRACE-MARKER", "FILE-MARKER"):
        assert marker not in stored, marker
    assert build.repair_routes == BOOM_500
    (entry,) = build.repair_routes
    assert set(entry) == {"attempt", "routes"} and all(set(route) == {"path", "status", "outcome"} for route in entry["routes"])


def test_the_entry_is_written_from_the_failing_routes_list_and_from_nothing_else():
    """The one write is `repair_entries.append(...)` with the `routes` that `failing_routes(...)` returned
    (the list the prompt is composed from); no other statement assigns the column."""
    source = open(sbe.__file__, encoding="utf-8").read()
    (written,) = [line for line in source.splitlines() if "repair_entries.append(" in line]
    assert '"routes": copy.deepcopy(routes)' in written
    assert source.count("routes = failing_routes(current.smoke_checks)") == 1
    assigned = [line.strip() for line in source.splitlines() if "repair_routes =" in line]
    assert assigned == ["build.repair_routes = copy.deepcopy(repair_entries)"]
