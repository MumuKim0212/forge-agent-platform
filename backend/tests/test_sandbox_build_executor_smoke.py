"""the executor runs the preview smoke check, and nothing it does can fail the build.

Unit seam, no Docker: the `Harness` of the harvest tests with a `_start_preview` that marks
the preview as started, and a recording prober in place of the preview route. The check is
informational: an exception anywhere in it leaves `Build.smoke_checks` NULL ("not measured"), the
build's result unchanged, and the policy gate still run.

The check is no longer inert when it measures a failing route, because the build may
then repair its own output. These tests pin the statement that still holds with the repair loop OFF
(`BUILD_REPAIR_MAX_ATTEMPTS=0`, which `_harness` sets explicitly): the check changes nothing. What a
failing route does with the loop on is `test_sandbox_build_executor_repair.py`.
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.services import sandbox_build_executor as sbe
from app.services.preview_smoke import SmokeResponse
from tests.test_sandbox_build_executor_harvest import Harness
from tests.test_sandbox_build_executor_schema import CODE_ONLY, INPUT

DOC = {
    "openapi": "3.1.0",
    "paths": {
        "/ok": {"get": {"responses": {}}},
        "/boom": {"get": {"responses": {}}},
        "/items/{id}": {"get": {"responses": {}}},
        "/make": {"post": {"responses": {}}},
    },
}
EXPECTED = {
    "routes": [
        {"path": "/boom", "status": 500, "outcome": "server_error"},
        {"path": "/ok", "status": 200, "outcome": "ok"},
    ],
    "skipped": 1,
    "truncated": False,
}


class Prober:
    def __init__(self, *, fail_with=None):
        self.calls: list[tuple[str, str]] = []
        self.fail_with = fail_with

    def __call__(self, method, path, read_limit, timeout):
        self.calls.append((method, path))
        if self.fail_with is not None:
            raise self.fail_with
        if path == "/openapi.json":
            return SmokeResponse(200, [json.dumps(DOC).encode()])
        return SmokeResponse(500 if path == "/boom" else 200, [b"body"])


def _harness(monkeypatch, *, preview_started=True, prober=None, order=None):
    # explicit, not left to the suite's default -- these are the statements about the check
    # alone, in the order it has with the repair loop off (publish, image, preview, check, gate).
    monkeypatch.setattr(settings, "build_repair_max_attempts", 0)
    h = Harness(monkeypatch, output_files=CODE_ONLY, staged_files=INPUT)
    h.prober = prober or Prober()
    h.factory_builds = []

    def start_preview(build):
        if order is not None:
            order.append("preview")
        if preview_started:
            build.preview_url = "/api/projects/p/preview/b?token=t"
            build.preview_token_hash = "h"

    def factory(build):
        if order is not None:
            order.append("smoke")
        h.factory_builds.append(build)
        return h.prober

    monkeypatch.setattr(h.executor, "_start_preview", start_preview)
    h.executor._smoke_prober_factory = factory
    return h


def test_a_started_preview_is_smoke_checked_and_the_fact_is_stored_on_the_build(monkeypatch, make_build_with_request):
    h = _harness(monkeypatch)
    build = make_build_with_request()

    result = h.executor.execute(build)

    assert result.ok is True, result.summary
    assert build.smoke_checks == EXPECTED
    assert h.factory_builds == [build]
    assert {method for method, _ in h.prober.calls} == {"GET"}


def test_with_the_repair_loop_off_the_check_runs_after_the_preview_starts_and_before_the_policy_gate(
    monkeypatch, make_build_with_request
):
    """Renamed from "the check runs after the preview starts and before the policy gate". With
    the loop on, the order is preview, check, (repair attempts: preview, check), gate -- pinned in
    `test_sandbox_build_executor_repair.py`."""
    order: list[str] = []
    h = _harness(monkeypatch, order=order)
    real_gate = sbe.run_and_persist_policy_gate

    def gate(session, build, **kwargs):
        order.append("gate")
        return real_gate(session, build, **kwargs)

    monkeypatch.setattr(sbe, "run_and_persist_policy_gate", gate)

    assert h.executor.execute(make_build_with_request()).ok is True

    assert order == ["preview", "smoke", "gate"]


def test_a_build_whose_preview_did_not_start_is_not_probed_and_has_null(monkeypatch, make_build_with_request):
    h = _harness(monkeypatch, preview_started=False)
    build = make_build_with_request()

    result = h.executor.execute(build)

    assert result.ok is True
    assert build.smoke_checks is None  # NULL, not {"routes": []}
    assert h.factory_builds == [] and h.prober.calls == []


@pytest.mark.parametrize("failure", ["prober", "factory", "runner", "unwritable"])
def test_an_exception_anywhere_in_the_check_leaves_null_the_result_unchanged_and_the_gate_run(
    monkeypatch, make_build_with_request, failure
):
    h = _harness(monkeypatch, prober=Prober(fail_with=RuntimeError("boom")) if failure == "prober" else None)
    if failure == "factory":

        def broken_factory(build):
            raise RuntimeError("factory")

        h.executor._smoke_prober_factory = broken_factory
    elif failure == "runner":

        def broken_runner(prober):
            raise ValueError("parser")

        monkeypatch.setattr(sbe, "run_smoke_check", broken_runner)
    elif failure == "unwritable":
        # A fact the database cannot store: the flush of the write itself fails.
        monkeypatch.setattr(sbe, "run_smoke_check", lambda prober: {"routes": [object()], "skipped": 0, "truncated": False})
    build = make_build_with_request()

    result = h.executor.execute(build)

    assert result.ok is True, result.summary
    assert result.reason is None
    assert build.smoke_checks is None
    assert len(h.gate_calls) == 1  # the gate ran, and committed its verdict against a usable session


def test_with_the_repair_loop_off_a_failing_route_does_not_change_the_result_or_the_gates_work(
    monkeypatch, make_build_with_request
):
    """This used to say "a failing route changes nothing" without a condition. It is true only
    with `BUILD_REPAIR_MAX_ATTEMPTS=0` now: with the loop on, a failing route starts a repair attempt
    (`test_a_failing_route_starts_a_repair_and_the_repaired_attempt_is_what_ships`)."""
    h = _harness(monkeypatch)
    build = make_build_with_request()

    result = h.executor.execute(build)

    assert any(r["outcome"] == "server_error" for r in build.smoke_checks["routes"])
    assert result.ok is True and result.reason is None
    assert len(h.gate_calls) == 1
    assert build.repair_attempts == 0


def test_the_check_returns_nothing_so_no_caller_can_branch_on_it(monkeypatch, make_build_with_request):
    h = _harness(monkeypatch)

    assert h.executor._run_smoke_check(make_build_with_request()) is None
