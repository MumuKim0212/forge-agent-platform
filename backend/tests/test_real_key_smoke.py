"""Real-key end-to-end smoke test:
exercises the full `SandboxBuildExecutor` path — including the publish
step — against the **real** Anthropic API, the **real** pinned agent
sandbox image (with the real Claude Agent SDK), and the **real** GitHub API.

Skips cleanly (not fails) whenever a real key/token isn't available in the
environment — this repository/CI never has one, and that's expected. Set
`ANTHROPIC_API_KEY` + `GITHUB_TOKEN` + `GITHUB_ORG` locally to actually run
it; also requires `forge-llm-proxy:latest` and the real
`forge-agent-sandbox:latest` (`docker build -t forge-agent-sandbox:latest
docker/agent-sandbox`, which pulls Node + the Claude Code CLI + the Claude
Agent SDK — slow the first time) to be built locally, and a real Docker
Engine.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("docker")

import docker  # noqa: E402
from docker.errors import DockerException, ImageNotFound  # noqa: E402

REAL_AGENT_IMAGE = "forge-agent-sandbox:latest"
PROXY_IMAGE = "forge-llm-proxy:latest"


def _docker_available() -> bool:
    try:
        docker.from_env().ping()
        return True
    except DockerException:
        return False


def _real_key_and_images_available() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    if not os.environ.get("GITHUB_TOKEN") or not os.environ.get("GITHUB_ORG"):
        return False
    if not _docker_available():
        return False
    client = docker.from_env()
    try:
        client.images.get(REAL_AGENT_IMAGE)
        client.images.get(PROXY_IMAGE)
        return True
    except (ImageNotFound, DockerException):
        return False


pytestmark = pytest.mark.skipif(
    not _real_key_and_images_available(),
    reason=(
        "requires ANTHROPIC_API_KEY, GITHUB_TOKEN, GITHUB_ORG, a real "
        f"Docker Engine, and the {REAL_AGENT_IMAGE}/{PROXY_IMAGE} images "
        "built locally — skips cleanly everywhere else (this environment "
        "included, since no real GITHUB_TOKEN is available here)"
    ),
)


def test_real_agent_build_succeeds_end_to_end(monkeypatch, db_session, seeded_ids):
    from app.config import settings
    from app.models.build import Build
    from app.models.project import Project
    from app.services.sandbox_build_executor import SandboxBuildExecutor
    from app.services.status import ProjectStatus

    monkeypatch.setattr(settings, "anthropic_api_key", os.environ["ANTHROPIC_API_KEY"])
    monkeypatch.setattr(settings, "agent_sandbox_image", REAL_AGENT_IMAGE)
    monkeypatch.setattr(settings, "github_token", os.environ["GITHUB_TOKEN"])
    monkeypatch.setattr(settings, "github_org", os.environ["GITHUB_ORG"])

    project = Project(
        name="smoke-test-project",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.commit()
    build = Build(project_id=project.id, type="create")
    db_session.add(build)
    db_session.flush()
    # the executor refuses a build with no request.
    from app.models.change_request import ChangeRequest

    db_session.add(
        ChangeRequest(
            project_id=project.id,
            requester_id=seeded_ids["user_id"],
            nl_request="Build a small to-do list service.",
            build_id=build.id,
        )
    )
    db_session.commit()

    executor = SandboxBuildExecutor()

    result = executor.execute(build)

    assert result.ok is True, f"real-key smoke test failed: {result.reason} / {result.summary}"
    assert build.cost_tokens is not None and build.cost_tokens > 0
    assert build.cost_usd is not None and build.cost_usd > 0
    assert build.diff_ref is not None
    assert build.image_ref is not None
