"""Unit tests for `GitHubProvider` against a stubbed HTTP
upstream (`httpx.MockTransport`) — no real GitHub token, no network. Covers
create_repo (create + idempotent existing-repo lookup), push_branch's
token-embedding (never logged/raised in errors), open_pr, resolve_ref, and
get_pr_state, plus error surfacing (never swallowed).

`get_pr_state` tells `open`, `merged` and `closed`-without-merging apart --
GitHub's own `state` says `closed` for the last two -- and names the merge commit.
Every one of those tests answers at the HTTP transport (`httpx.MockTransport`) with the
shape of GitHub's real pull request resource, never at `get_pr_state` itself. A hung git
push is abandoned, against real git talking to a host that never answers.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.services.git_provider import GitProviderError
from app.services.github_provider import GitHubProvider

FAKE_TOKEN = "ghp_fake_test_token_never_real"  # noqa: S105 - test fixture, not a real credential
ORG = "fake-org"


def _repo_payload(name: str) -> dict:
    return {
        "full_name": f"{ORG}/{name}",
        "clone_url": f"https://github.com/{ORG}/{name}.git",
        "default_branch": "main",
    }


def _make_provider(handler) -> GitHubProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    return GitHubProvider(token=FAKE_TOKEN, org=ORG, base_url="https://api.github.test", http_client=client)


def test_create_repo_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/orgs/{ORG}/repos"
        assert request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
        body = json.loads(request.content)
        assert body["name"] == "proj-1"
        assert body["private"] is True
        return httpx.Response(201, json=_repo_payload("proj-1"))

    provider = _make_provider(handler)
    repo = provider.create_repo(name="proj-1")

    assert repo.full_name == f"{ORG}/proj-1"
    assert repo.default_branch == "main"
    # Token embedded in the clone URL for subprocess `git push`, never bare.
    assert f"x-access-token:{FAKE_TOKEN}@" in repo.clone_url


def test_create_repo_existing_falls_back_to_lookup():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(422, json={"message": "name already exists"})
        assert request.method == "GET"
        assert request.url.path == f"/repos/{ORG}/proj-1"
        return httpx.Response(200, json=_repo_payload("proj-1"))

    provider = _make_provider(handler)
    repo = provider.create_repo(name="proj-1")

    assert repo.full_name == f"{ORG}/proj-1"


def test_create_repo_raises_on_failure_never_swallowed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    provider = _make_provider(handler)
    with pytest.raises(GitProviderError):
        provider.create_repo(name="proj-1")


def test_open_pr_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{ORG}/proj-1/pulls"
        body = json.loads(request.content)
        assert body["head"] == "forge-build-abc"
        assert body["base"] == "main"
        return httpx.Response(
            201, json={"number": 7, "html_url": "https://github.test/pr/7", "state": "open"}
        )

    provider = _make_provider(handler)
    from app.services.git_provider import RepoRef

    repo = RepoRef(full_name=f"{ORG}/proj-1", clone_url="unused", default_branch="main")
    pr = provider.open_pr(repo=repo, branch="forge-build-abc", base_branch="main", title="t", body="b")

    assert pr.number == 7
    assert pr.state == "open"


def test_open_pr_raises_on_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    provider = _make_provider(handler)
    from app.services.git_provider import RepoRef

    repo = RepoRef(full_name=f"{ORG}/proj-1", clone_url="unused", default_branch="main")
    with pytest.raises(GitProviderError):
        provider.open_pr(repo=repo, branch="b", base_branch="main", title="t", body="b")


def test_resolve_ref_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{ORG}/proj-1/git/refs/heads/main"
        return httpx.Response(200, json={"object": {"sha": "deadbeef" * 5}})

    provider = _make_provider(handler)
    from app.services.git_provider import RepoRef

    repo = RepoRef(full_name=f"{ORG}/proj-1", clone_url="unused", default_branch="main")
    sha = provider.resolve_ref(repo=repo, branch="main")

    assert sha == "deadbeef" * 5


MERGE_SHA = "cafebabe" * 5
# GitHub fills merge_commit_sha for a pull request that was NOT merged, too (a test merge).
STALE_TEST_MERGE_SHA = "0ddba11e" * 5


def _pr_resource(**over) -> dict:
    """The fields of GitHub's pull request resource that decide the state."""
    resource = {
        "number": 7,
        "html_url": "https://github.test/pr/7",
        "state": "open",
        "merged": False,
        "merged_at": None,
        "merge_commit_sha": STALE_TEST_MERGE_SHA,
    }
    resource.update(over)
    return resource


def _get_pr(resource: dict):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers["authorization"]))
        return httpx.Response(200, json=resource)

    from app.services.git_provider import RepoRef

    repo = RepoRef(full_name=f"{ORG}/proj-1", clone_url="unused", default_branch="main")
    pr = _make_provider(handler).get_pr_state(repo=repo, pr_number=7)
    assert seen == [("GET", f"/repos/{ORG}/proj-1/pulls/7", f"Bearer {FAKE_TOKEN}")]
    return pr


def test_an_open_pr_is_open_and_its_test_merge_commit_is_not_a_merge_commit():
    pr = _get_pr(_pr_resource(state="open"))

    assert pr.state == "open"
    assert pr.merge_commit is None  # GitHub's merge_commit_sha on an open PR is a test merge


def test_a_merged_pr_is_merged_with_its_merge_commit_although_github_says_closed():
    pr = _get_pr(
        _pr_resource(state="closed", merged=True, merged_at="2026-10-17T01:02:03Z", merge_commit_sha=MERGE_SHA)
    )

    assert pr.state == "merged"
    assert pr.merge_commit == MERGE_SHA


def test_a_pr_closed_without_merging_is_closed_and_carries_no_merge_commit():
    pr = _get_pr(_pr_resource(state="closed", merged=False, merged_at=None))

    assert pr.state == "closed"
    assert pr.merge_commit is None  # the resource still names a (stale) commit; it must not leak through


def test_merged_at_alone_marks_a_pr_merged():
    resource = _pr_resource(state="closed", merged_at="2026-10-17T01:02:03Z", merge_commit_sha=MERGE_SHA)
    del resource["merged"]

    pr = _get_pr(resource)

    assert pr.state == "merged"
    assert pr.merge_commit == MERGE_SHA


def test_a_merged_pr_that_names_no_merge_commit_reports_none():
    pr = _get_pr(_pr_resource(state="closed", merged=True, merged_at="2026-10-17T01:02:03Z", merge_commit_sha=None))

    assert pr.state == "merged"
    assert pr.merge_commit is None


@pytest.mark.parametrize("state", ["merged", "draft", "", None])
def test_a_state_github_does_not_use_is_an_error_not_a_guess(state):
    with pytest.raises(GitProviderError, match="unrecognised state"):
        _get_pr(_pr_resource(state=state))


def test_get_pr_state_surfaces_a_host_error():
    provider = _make_provider(lambda r: httpx.Response(404, json={"message": "Not Found"}))
    from app.services.git_provider import RepoRef

    repo = RepoRef(full_name=f"{ORG}/proj-1", clone_url="unused", default_branch="main")
    with pytest.raises(GitProviderError, match="404"):
        provider.get_pr_state(repo=repo, pr_number=7)


def test_merge_pr_success():
    """merge returns the merge
    commit SHA (the project's new confirmed_ref)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == f"/repos/{ORG}/proj-1/pulls/7/merge"
        assert request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
        return httpx.Response(200, json={"sha": "cafebabe" * 5, "merged": True})

    provider = _make_provider(handler)
    from app.services.git_provider import RepoRef

    repo = RepoRef(full_name=f"{ORG}/proj-1", clone_url="unused", default_branch="main")
    sha = provider.merge_pr(repo=repo, pr_number=7)

    assert sha == "cafebabe" * 5


def test_merge_pr_raises_on_conflict_never_swallowed():
    """A 405 (not mergeable / conflict) surfaces as GitProviderError —
    mapped to the deployment's MERGE_FAILED by the deploy service."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(405, json={"message": "Pull Request is not mergeable"})

    provider = _make_provider(handler)
    from app.services.git_provider import RepoRef

    repo = RepoRef(full_name=f"{ORG}/proj-1", clone_url="unused", default_branch="main")
    with pytest.raises(GitProviderError):
        provider.merge_pr(repo=repo, pr_number=7)


def test_push_branch_error_never_leaks_token(tmp_path):
    """A push failure's error message must not contain the token-embedded
    clone URL (never log secrets/tokens)."""
    from app.services.git_provider import RepoRef

    workdir = tmp_path / "workspace"
    workdir.mkdir()
    import subprocess

    subprocess.run(["git", "init"], cwd=workdir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=workdir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=workdir, check=True, capture_output=True)
    (workdir / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=workdir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "x"], cwd=workdir, check=True, capture_output=True)

    provider = _make_provider(lambda r: httpx.Response(200))
    repo = RepoRef(
        full_name=f"{ORG}/proj-1",
        clone_url=f"https://x-access-token:{FAKE_TOKEN}@github.test/does/not/exist.git",
        default_branch="main",
    )

    with pytest.raises(GitProviderError) as exc_info:
        provider.push_branch(repo=repo, branch="b", local_path=str(workdir))

    assert FAKE_TOKEN not in str(exc_info.value)


def _committed_workdir(tmp_path):
    import subprocess

    workdir = tmp_path / "workspace"
    workdir.mkdir()
    for args in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "a@b.c"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=workdir, check=True, capture_output=True)
    (workdir / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=workdir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=workdir, check=True, capture_output=True)
    return workdir


def test_a_failed_push_scrubs_the_url_git_echoes_and_does_not_chain_the_exception(tmp_path):
    """git echoes a remote that is not a repository back verbatim (the first assertion measures
    it), and the `CalledProcessError` it would be chained from carries the URL as its argv."""
    import subprocess

    from app.services.git_provider import RepoRef

    workdir = _committed_workdir(tmp_path)
    secret_url = str(tmp_path / f"x-access-token-{FAKE_TOKEN}")  # does not exist
    raw = subprocess.run(["git", "push", secret_url, "HEAD:refs/heads/b"], cwd=workdir, capture_output=True, text=True)
    assert FAKE_TOKEN in raw.stderr  # positive control: git itself would have leaked it
    repo = RepoRef(full_name=f"{ORG}/proj-1", clone_url=secret_url, default_branch="main")

    with pytest.raises(GitProviderError) as exc_info:
        _make_provider(lambda r: httpx.Response(200)).push_branch(repo=repo, branch="b", local_path=str(workdir))

    assert "push_branch failed for branch 'b'" in str(exc_info.value)
    assert FAKE_TOKEN not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_a_push_to_a_host_that_never_answers_fails_as_a_provider_error(
    tmp_path, monkeypatch, silent_git_remote, returns_within
):
    """real git, a server that accepts and goes quiet. The step's own failure type, the
    branch named, the token nowhere, nothing chained (the exception's argv is the URL)."""
    import app.services.publish as publish_module
    from app.services.git_provider import RepoRef

    monkeypatch.setattr(publish_module, "GIT_TIMEOUT_SECONDS", 1.0)
    workdir = _committed_workdir(tmp_path)
    url = silent_git_remote.replace("http://", f"http://x-access-token:{FAKE_TOKEN}@") + "/org/p.git"
    repo = RepoRef(full_name=f"{ORG}/proj-1", clone_url=url, default_branch="main")
    provider = _make_provider(lambda r: httpx.Response(200))

    with pytest.raises(GitProviderError) as exc_info:
        returns_within(20, lambda: provider.push_branch(repo=repo, branch="b", local_path=str(workdir)))

    assert "push_branch timed out after 1s for branch 'b'" in str(exc_info.value)
    assert FAKE_TOKEN not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_push_runs_with_the_terminal_prompt_off_and_no_inherited_repository_selection(tmp_path, monkeypatch):
    import app.services.github_provider as github_module
    from app.services.git_provider import RepoRef

    workdir = _committed_workdir(tmp_path)  # before the environment is poisoned
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-the-repository"))
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    calls = []
    real = github_module.run_git_process

    def spy(args, **kwargs):
        calls.append(kwargs)
        return real(args, **kwargs)

    monkeypatch.setattr(github_module, "run_git_process", spy)
    repo = RepoRef(full_name=f"{ORG}/proj-1", clone_url=str(tmp_path / "does-not-exist.git"), default_branch="main")

    with pytest.raises(GitProviderError):
        _make_provider(lambda r: httpx.Response(200)).push_branch(repo=repo, branch="b", local_path=str(workdir))

    assert len(calls) == 1
    assert calls[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_DIR" not in calls[0]["env"]
