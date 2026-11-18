"""Engine-up: an eval run, end to end.

What is REAL here: the run and its rows, the worker's step (`run_iteration` -> `advance_runs`),
the normal build path for each case (the claim, `assign_engine`, `SandboxBuildExecutor`, the
adapter, the staged `agent_runner_openai.py`, `forge-agent-sandbox-openai:latest`, the packaged
`forge-llm-proxy:latest` on its `openai` dialect, the harvest, the LOCAL git provider, the image
assembly and the policy gate), a REAL preview container of the assembled image on the real
per-preview Postgres and Redis, the judge's `/healthz` and platform checks made through the
preview's token-checked route (`PreviewRouteProbe` -> `PreviewManager.proxy`), and the preview
teardown. What is FAKE: the OpenAI upstream (the stub of `test_openai_engine_up_integration.py`,
which records every request). No real OpenAI call is made and no real key exists in this file.

The request set for the run is a two-case file written to `tmp_path` (the loader's path is
redirected), so the run stays small:

  1. `home-page`: `GET /` must contain text the template's own page serves -> PASSES against the
     real preview. The stub agent only writes a marker file, so the page is the template's.
  2. `absent-text`: `GET /` must contain text that is not on that page -> FAILS `CHECK_FAILED` at
     check index 1, with check 0 passing, so the judge is shown to discriminate on a real response.

Asserted: both cases judged in order, one build each; the run COMPLETED with 1 passed of 2; the
spend is the proxy's metered cost (worked out by hand: 0.000510 per build, see the stub); the
second build carries 1.0 - 0.00051 as its per-build budget (the remaining-budget arithmetic on a
real figure); the previews are gone; the eval projects are `kind=eval` and owned by the admin.

Skips cleanly without Docker or the images. Build them from `backend/`:
    docker build -t forge-llm-proxy:latest -f docker/llm-proxy/Dockerfile .
    docker build -t forge-agent-sandbox-openai:latest -f docker/agent-sandbox-openai/Dockerfile .
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("docker")

import docker  # noqa: E402
from docker.errors import DockerException, ImageNotFound, NotFound  # noqa: E402

from app.config import settings  # noqa: E402
from app.models.build import Build  # noqa: E402
from app.models.eval_run import EvalCase, EvalRun  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import eval_request_set  # noqa: E402
from app.services.eval_runner import start_run  # noqa: E402
from app.services.local_git_provider import LocalGitProvider  # noqa: E402
from app.services.preview_manager import PreviewManager  # noqa: E402
from app.services.sandbox_build_executor import SandboxBuildExecutor  # noqa: E402
from app.worker import run_iteration  # noqa: E402
from tests.test_openai_engine_up_integration import (  # noqa: E402
    AGENT_IMAGE,
    PROXY_IMAGE,
    REAL_KEY,
    UPSTREAM_IMAGE,
    _StubUpstream,
)


def _images_available() -> bool:
    try:
        client = docker.from_env()
        client.ping()
        for image in (AGENT_IMAGE, PROXY_IMAGE, UPSTREAM_IMAGE, "postgres:18-alpine", "redis:8-alpine"):
            client.images.get(image)
        return True
    except (ImageNotFound, DockerException):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _images_available(),
        reason=(
            f"requires Docker and {AGENT_IMAGE}, {PROXY_IMAGE}, {UPSTREAM_IMAGE}, postgres:18-alpine "
            "and redis:8-alpine built/pulled locally"
        ),
    ),
]

REQUEST_SET = {
    "version": 7,
    "cases": [
        {
            "id": "home-page",
            "request": "Build a small to-do list service.",
            "checks": [
                {"method": "GET", "path": "/", "expect_status": 200, "expect_substring": "forge generic-webtool"}
            ],
        },
        {
            "id": "absent-text",
            "request": "Build a small to-do list service.",
            "checks": [
                {"method": "GET", "path": "/", "expect_status": 200, "expect_substring": "forge generic-webtool"},
                {
                    "method": "GET",
                    "path": "/",
                    "expect_status": 200,
                    "expect_substring": "this text is nowhere on the template page",
                },
            ],
        },
    ],
}


@pytest.fixture(scope="module", autouse=True)
def _real_data_services(real_service_postgres_admin_dsn):
    """The template's service fails at import without `DATABASE_URL`, so a real preview needs the
    per-preview Postgres + Redis that `start_preview` provisions: point it at the module's own
    throwaway Postgres, never whatever `backend/.env` holds."""
    yield


@pytest.fixture()
def docker_client():
    return docker.from_env()


@pytest.fixture()
def finished_run(docker_client, db_session, tmp_path, monkeypatch):
    upstream = _StubUpstream(docker_client)
    request_set_file = tmp_path / "request_set.json"
    request_set_file.write_text(json.dumps(REQUEST_SET), encoding="utf-8")
    monkeypatch.setattr(eval_request_set, "REQUEST_SET_PATH", request_set_file)
    monkeypatch.setattr(settings, "use_sandbox_executor", True)
    monkeypatch.setattr(settings, "openai_api_key", REAL_KEY)
    monkeypatch.setattr(settings, "openai_upstream_base_url", upstream.url)
    monkeypatch.setattr(settings, "openai_agent_sandbox_image", AGENT_IMAGE)
    monkeypatch.setattr(settings, "agent_engine_default", "openai")
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    monkeypatch.setattr(settings, "eval_run_budget_usd", 1.0)
    monkeypatch.setattr(settings, "eval_case_min_budget_usd", 0.05)
    monkeypatch.setattr(settings, "build_cost_budget_usd", 5.0)

    admin = db_session.query(User).filter_by(role="admin").first()
    run = start_run(db_session, admin=admin)
    db_session.commit()

    previews = PreviewManager(client=docker_client)
    executor = SandboxBuildExecutor(
        docker_client=docker_client,
        git_provider=LocalGitProvider(root=str(tmp_path / "git-root")),
        preview_manager=previews,
    )
    ticks = []
    try:
        # Tick 1: queue case 0, claim it, build it. Tick 2: judge case 0 (preview checks), queue and
        # build case 1. Tick 3: judge case 1 and complete the run. Tick 4: nothing left to do.
        for _ in range(4):
            ticks.append(run_iteration(db_session, executor, preview_manager=previews))
        db_session.expire_all()
        yield {"run": db_session.get(EvalRun, run.id), "admin": admin, "ticks": ticks, "upstream": upstream}
    finally:
        upstream.stop()
        for build in db_session.query(Build).all():
            for name in (f"forge-preview-{build.id}",):
                try:
                    docker_client.containers.get(name).remove(v=True, force=True)
                except (NotFound, DockerException):
                    pass
            if build.image_ref:
                try:
                    docker_client.images.get(build.image_ref).remove(force=True)
                except DockerException:
                    pass


def test_an_eval_run_judges_each_case_against_a_real_preview_within_its_budget(finished_run, db_session, docker_client):
    run, admin, ticks = finished_run["run"], finished_run["admin"], finished_run["ticks"]

    # The run: both cases judged, one passed.
    assert run.status == "COMPLETED", run.detail
    assert (run.engine, run.model, run.request_set_version) == ("openai", "gpt-5-mini", 7)
    cases = db_session.query(EvalCase).filter_by(run_id=run.id).order_by(EvalCase.position).all()
    first, second = cases
    assert [(c.case_id, c.state, c.result) for c in cases] == [
        ("home-page", "DONE", "PASSED"),
        ("absent-text", "DONE", "FAILED"),
    ]

    # The passing case: /healthz and the check were answered by the real preview.
    assert [(o["index"], o["status"], o["ok"]) for o in first.check_outcomes] == [(None, 200, True), (0, 200, True)]
    assert first.check_outcomes[1]["outcome"] == "status 200 as expected and the expected text was found"

    # The failing case: the first check passed, the second named text the page does not have.
    assert (second.failure_reason, second.failed_check_index) == ("CHECK_FAILED", 1)
    assert [o["ok"] for o in second.check_outcomes] == [True, True, False]
    assert "expected text was not found" in second.check_outcomes[2]["outcome"]
    assert "forge generic-webtool" not in json.dumps(second.check_outcomes)  # no body is stored

    # Cost: the proxy's metered figures (0.000510 and 1470 tokens per build, worked out by hand in
    # test_openai_engine_up_integration), copied to the cases and summed on the run.
    assert [round(c.cost_usd, 9) for c in cases] == [0.00051, 0.00051]
    assert [c.cost_tokens for c in cases] == [1470, 1470]
    assert run.spend_usd == pytest.approx(0.00102, abs=1e-12)

    # Budget: the first build carries the whole run budget, the second the remainder.
    builds = [db_session.get(Build, c.build_id) for c in cases]
    assert builds[0].cost_budget_usd == pytest.approx(1.0)
    assert builds[1].cost_budget_usd == pytest.approx(1.0 - 0.00051)
    assert [(b.status, b.engine, b.model) for b in builds] == [("SUCCEEDED", "openai", "gpt-5-mini")] * 2

    # Isolation: eval projects owned by the admin, named for the run and the case.
    projects = [db_session.get(Project, c.project_id) for c in cases]
    assert [p.name for p in projects] == [f"eval-{run.id[:8]}-home-page", f"eval-{run.id[:8]}-absent-text"]
    assert {(p.kind, p.owner_id) for p in projects} == {("eval", admin.id)}

    # The previews were torn down after judging: columns cleared, containers gone.
    for build in builds:
        assert build.preview_url is None and build.preview_token_hash is None
        with pytest.raises(NotFound):
            docker_client.containers.get(f"forge-preview-{build.id}")

    # The fourth tick found nothing to do; the first three each ran something.
    assert [t is not None for t in ticks] == [True, True, False, False]
    # And the stub upstream saw only the builds' own calls: two per build, nothing for the judge.
    assert len(finished_run["upstream"].requests()) == 4
