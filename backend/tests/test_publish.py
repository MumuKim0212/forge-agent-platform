"""Unit/integration tests for `app/services/publish.py`

paths). Git mechanics (init/commit/push) are exercised against real local
bare repos (plain `git`, no network, no real GitHub) — the worker's chosen
approach for testing git plumbing without a real host (acceptance
criteria: "Git commit/branch mechanics may be integration-tested against
local bare repos"). Provider-facing operations (create_repo/open_pr/...)
are stubbed via a fake `GitProvider` double.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from app.services.git_provider import GitProviderError, PullRequestRef, RepoRef
from app.services.publish import (
    PublishError,
    commit_workspace,
    init_platform_repo,
    publish_build,
)


def _init_bare_repo(path) -> None:
    """A bare repository with one empty commit on `main`: publish builds on the
    base branch's tip, so a base has to exist."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", "--initial-branch=main"], cwd=path, check=True, capture_output=True)
    empty = subprocess.run(["git", "mktree"], cwd=path, input="", check=True, capture_output=True, text=True).stdout.strip()
    commit = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t.t", "commit-tree", empty, "-m", "base"],
        cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/heads/main", commit], cwd=path, check=True, capture_output=True)


def _write(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class LocalBareRepoProvider:
    """`GitProvider` double that actually pushes to a local bare repo
    (`local_path` -> `git push <bare_repo_path>`), and fakes only the
    host-side PR bookkeeping. Lets tests assert on real git state (branch
    exists, contains the expected commit) without touching any network."""

    def __init__(self, bare_repo_path) -> None:
        self._bare_repo_path = str(bare_repo_path)
        self.opened_prs: list[dict] = []

    def create_repo(self, *, name: str) -> RepoRef:
        return RepoRef(full_name=name, clone_url=self._bare_repo_path, default_branch="main")

    def push_branch(self, *, repo: RepoRef, branch: str, local_path: str) -> None:
        subprocess.run(
            ["git", "push", repo.clone_url, f"HEAD:refs/heads/{branch}"],
            cwd=local_path,
            check=True,
            capture_output=True,
            text=True,
        )

    def open_pr(self, *, repo, branch, base_branch, title, body) -> PullRequestRef:
        self.opened_prs.append({"branch": branch, "base": base_branch, "title": title, "body": body})
        return PullRequestRef(number=len(self.opened_prs), url="local://pr", state="open")

    def resolve_ref(self, *, repo, branch) -> str:
        out = subprocess.run(
            ["git", "rev-parse", f"refs/heads/{branch}"],
            cwd=self._bare_repo_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()

    def get_pr_state(self, *, repo, pr_number) -> PullRequestRef:
        return PullRequestRef(number=pr_number, url="local://pr", state="open")


class FailingPushProvider:
    """Fails at the push. Its repository is real (publish fetches the base tip first)."""

    def __init__(self, bare_repo_path) -> None:
        self._bare_repo_path = str(bare_repo_path)

    def create_repo(self, *, name: str) -> RepoRef:
        return RepoRef(full_name=name, clone_url=self._bare_repo_path, default_branch="main")

    def push_branch(self, *, repo, branch, local_path) -> None:
        raise GitProviderError("push failed: simulated host rejection")

    def open_pr(self, *, repo, branch, base_branch, title, body):  # pragma: no cover - never reached
        raise AssertionError("open_pr should not be called after push_branch fails")

    def resolve_ref(self, *, repo, branch):  # pragma: no cover
        raise NotImplementedError

    def get_pr_state(self, *, repo, pr_number):  # pragma: no cover
        raise NotImplementedError


def _platform_repo(tmp_path) -> str:
    repo_dir = tmp_path / "platform-repo"
    repo_dir.mkdir()
    return init_platform_repo(str(repo_dir))


def _git_branches(bare_repo) -> str:
    return subprocess.run(
        ["git", "branch", "--list"], cwd=bare_repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _git_log_or_empty(git_dir: str) -> str:
    result = subprocess.run(["git", "--git-dir", git_dir, "log", "--format=%s"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_log(git_dir: str, fmt: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", git_dir, "log", f"--format={fmt}"], check=True, capture_output=True, text=True
    ).stdout


def test_commit_workspace_creates_a_commit(tmp_path):
    workspace = tmp_path / "workspace"
    _write(workspace / "app.py", "print('hi')\n")
    git_dir = _platform_repo(tmp_path)

    commit_workspace(workspace_dir=str(workspace), git_dir=git_dir, message="test commit")

    assert _git_log(git_dir, "%s|%an <%ae>").strip() == "test commit|Forge Bot <forge-bot@forge.local>"
    # The repository is NOT in the published tree: nothing was added to it.
    assert sorted(os.listdir(workspace)) == ["app.py"]


def test_commit_workspace_allows_empty_commit_for_modify_with_only_drift(tmp_path):
    """A modify-type build whose only changes were to protected paths
    (dropped) still needs a committable state to push/PR against."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git_dir = _platform_repo(tmp_path)

    commit_workspace(workspace_dir=str(workspace), git_dir=git_dir, message="empty drift-only build")

    assert "empty drift-only build" in _git_log(git_dir, "%s")


HOOK = '#!/bin/sh\n[ -n "$FORGE_TEST_HOOK_MARKER" ] && echo ran > "$FORGE_TEST_HOOK_MARKER"\nexit 0\n'


def _plant_complete_repo(workspace, dirname: str) -> None:
    """A COMPLETE git repository (made by `git init`) in `workspace/<dirname>`,
    with a `pre-commit` hook that writes $FORGE_TEST_HOOK_MARKER, and a config
    that would also run code (`core.fsmonitor`). `dirname` may be any spelling
    of `.git`: it is made as `.git` and renamed."""
    subprocess.run(["git", "init", "--quiet"], cwd=workspace, check=True, capture_output=True)
    hooks = workspace / ".git" / "hooks"
    (hooks / "pre-commit").write_text(HOOK, newline="\n")
    (hooks / "pre-commit").chmod(0o755)
    with open(workspace / ".git" / "config", "a", newline="\n") as f:
        f.write("[core]\n\tfsmonitor = echo hostile-fsmonitor\n[user]\n\tname = Evil\n\temail = evil@example.invalid\n")
    if dirname != ".git":
        os.rename(workspace / ".git", workspace / dirname)


def _is_case_insensitive(directory) -> bool:
    """Measured on the directory the test will use, not inferred from the platform:
    NTFS and default APFS fold `.GIT` onto `.git`; ext4/overlayfs do not."""
    probe = directory / "CaseProbe"
    probe.write_bytes(b"")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()


def _expect_commit_or_refusal(tmp_path, workspace, dirname, git_dir, message):
    """What `commit_workspace` must do with a complete repository planted in the tree
    as `dirname`, which depends on the filesystem:

    - `.git`, or any spelling on a case-INSENSITIVE filesystem (where it IS `.git`):
      git ignores a `.git` in the work tree by name, and the commit succeeds as Forge Bot;
    - a case variant on a case-SENSITIVE filesystem: `.GIT` is an ordinary directory
      whose contents git's path protection refuses to add (`invalid path '.GIT/HEAD'`),
      so `commit_workspace` raises `PublishError` naming the path: fail closed, nothing
      committed. (In the real pipeline the sanitiser never lets such a directory reach
      here; this is the layer behind it.)

    Returns True if a commit was made. Either way the hook must not have run."""
    if dirname == ".git" or _is_case_insensitive(tmp_path):
        commit_workspace(workspace_dir=str(workspace), git_dir=git_dir, message=message)
        return True
    with pytest.raises(PublishError) as raised:
        commit_workspace(workspace_dir=str(workspace), git_dir=git_dir, message=message)
    assert f"invalid path '{dirname}/" in str(raised.value)
    return False


@pytest.fixture()
def hook_marker(tmp_path, monkeypatch):
    marker = tmp_path / "hook-ran-on-the-host"
    monkeypatch.setenv("FORGE_TEST_HOOK_MARKER", str(marker))
    return marker


def test_the_planted_hook_is_live_the_positive_control(tmp_path, hook_marker):
    """So the tests below are measurements: the same planted repository, committed
    to in place the way publish used to, DOES run the hook on this host."""
    workspace = tmp_path / "workspace"
    _write(workspace / "app.py", "x\n")
    _plant_complete_repo(workspace, ".git")

    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=n", "commit", "-m", "x"],
        cwd=workspace, check=True, capture_output=True,
    )

    assert hook_marker.exists()


@pytest.mark.parametrize("dirname", [".git", ".GIT", ".Git"])
def test_commit_workspace_ignores_a_complete_repository_planted_in_the_tree(tmp_path, hook_marker, dirname):
    """Pinned on its own: this calls `commit_workspace` on a tree that
    DOES contain a valid repository, so the sanitiser's name rule (f2) is not in
    play. The platform's repository is elsewhere, named explicitly, so the planted
    one is neither used nor consulted: no hook runs, the platform's identity
    authors the commit, and the planted repository is left untouched."""
    workspace = tmp_path / "workspace"
    _write(workspace / "app.py", "print('agent output')\n")
    _plant_complete_repo(workspace, dirname)
    git_dir = _platform_repo(tmp_path)

    committed = _expect_commit_or_refusal(tmp_path, workspace, dirname, git_dir, "the real commit")

    assert not hook_marker.exists(), "host git ran a hook that came out of the tree"
    if committed:
        assert _git_log(git_dir, "%s|%an <%ae>").strip() == "the real commit|Forge Bot <forge-bot@forge.local>"
    else:
        assert _git_log_or_empty(git_dir) == ""  # nothing was committed
    # The planted repository was never used: it has no commits.
    planted = subprocess.run(
        ["git", "--git-dir", str(workspace / dirname), "rev-list", "--all", "--count"],
        capture_output=True, text=True,
    )
    assert planted.stdout.strip() in ("", "0")


@pytest.mark.parametrize("dirname", [".git", ".GIT"])
def test_publish_build_never_runs_a_planted_repository_s_hook_and_pushes_the_platform_commit(
    tmp_path, hook_marker, dirname
):
    bare_repo = tmp_path / "bare.git"
    _init_bare_repo(bare_repo)
    skeleton = tmp_path / "skeleton"
    _write(skeleton / "Dockerfile", "FROM pinned\n")
    workspace = tmp_path / "workspace"
    _write(workspace / "app.py", "print('agent output')\n")
    _plant_complete_repo(workspace, dirname)
    provider = LocalBareRepoProvider(bare_repo)
    repo = provider.create_repo(name="proj-1")
    kwargs = dict(
        provider=provider, repo=repo, workspace_dir=str(workspace), input_dir=str(tmp_path / "input"), template_skeleton_dir=str(skeleton),
        protected_paths=["Dockerfile"], branch="forge-build-planted", base_branch="main", pr_title="t", pr_body="b",
    )

    if dirname != ".git" and not _is_case_insensitive(tmp_path):
        # Case-sensitive filesystem: git refuses the ordinary `.GIT` directory, publish
        # fails closed with the path named, and nothing is pushed or opened.
        with pytest.raises(PublishError) as raised:
            publish_build(**kwargs)
        assert f"invalid path '{dirname}/" in str(raised.value)
        assert not hook_marker.exists(), "host git ran a hook that came out of the tree"
        assert _git_branches(bare_repo).replace("*", "").split() == ["main"]  # only the base: nothing pushed
        assert provider.opened_prs == []
        return

    publish_build(**kwargs)

    assert not hook_marker.exists(), "host git ran a hook that came out of the tree"
    sha = provider.resolve_ref(repo=repo, branch="forge-build-planted")
    author = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>", sha], cwd=bare_repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert author == "Forge Bot <forge-bot@forge.local>"


def test_the_platform_repository_is_removed_on_success_and_on_failure(tmp_path, monkeypatch):
    import tempfile

    systemp = tmp_path / "systemp"
    systemp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systemp))
    skeleton = tmp_path / "skeleton"
    _write(skeleton / "Dockerfile", "FROM pinned\n")
    bare_repo = tmp_path / "bare.git"
    _init_bare_repo(bare_repo)

    for provider in (LocalBareRepoProvider(bare_repo), FailingPushProvider(bare_repo)):
        workspace = tmp_path / f"workspace-{type(provider).__name__}"
        _write(workspace / "app.py", "x\n")
        repo = provider.create_repo(name="p")
        try:
            publish_build(
                provider=provider, repo=repo, workspace_dir=str(workspace), input_dir=str(tmp_path / "input"), template_skeleton_dir=str(skeleton),
                protected_paths=["Dockerfile"], branch="b", base_branch="main", pr_title="t", pr_body="b",
            )
        except GitProviderError:
            pass
        assert os.listdir(systemp) == [], type(provider).__name__


def test_publish_build_end_to_end_against_local_bare_repo(tmp_path):
    bare_repo = tmp_path / "bare.git"
    _init_bare_repo(bare_repo)

    skeleton = tmp_path / "skeleton"
    _write(skeleton / "Dockerfile", "FROM pinned\n")

    workspace = tmp_path / "workspace"
    _write(workspace / "app.py", "print('agent output')\n")
    _write(workspace / "Dockerfile", "FROM agent-tampered\n")  # will be restored + reported as drift

    provider = LocalBareRepoProvider(bare_repo)
    repo = provider.create_repo(name="proj-1")

    result = publish_build(
        provider=provider,
        repo=repo,
        workspace_dir=str(workspace),
        input_dir=str(tmp_path / "input"),
        template_skeleton_dir=str(skeleton),
        protected_paths=["Dockerfile"],
        branch="forge-build-abc123",
        base_branch="main",
        pr_title="Forge build abc123",
        pr_body="agent summary",
    )

    assert result.branch == "forge-build-abc123"
    assert result.diff_ref == "forge-build-abc123#1"
    assert result.drift == ["Dockerfile"]
    assert provider.opened_prs == [
        {
            "branch": "forge-build-abc123",
            "base": "main",
            "title": "Forge build abc123",
            "body": "agent summary",
        }
    ]

    # The branch really exists on the bare repo with the restored Dockerfile.
    sha = provider.resolve_ref(repo=repo, branch="forge-build-abc123")
    assert sha  # non-empty commit SHA

    show = subprocess.run(
        ["git", "show", f"{sha}:Dockerfile"],
        cwd=bare_repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert show.stdout == "FROM pinned\n"


def test_publish_build_push_failure_raises_never_swallowed(tmp_path):
    skeleton = tmp_path / "skeleton"
    _write(skeleton / "Dockerfile", "FROM pinned\n")
    workspace = tmp_path / "workspace"
    _write(workspace / "app.py", "print('x')\n")

    bare_repo = tmp_path / "bare.git"
    _init_bare_repo(bare_repo)
    provider = FailingPushProvider(bare_repo)
    repo = provider.create_repo(name="proj-1")

    with pytest.raises(GitProviderError):
        publish_build(
            provider=provider,
            repo=repo,
            workspace_dir=str(workspace),
            input_dir=str(tmp_path / "input"),
            template_skeleton_dir=str(skeleton),
            protected_paths=["Dockerfile"],
            branch="forge-build-x",
            base_branch="main",
            pr_title="t",
            pr_body="b",
        )


def test_publish_build_measures_drift_against_the_input_and_pushes_the_pinned_copy(tmp_path):
    """a modify-shaped build -- the input carries an OLDER pinned Dockerfile the
    agent did not touch -- reports no drift, and the pushed Dockerfile is the CURRENT
    pinned one. Read back from the bare repo with real `git show`."""
    bare_repo = tmp_path / "bare.git"
    _init_bare_repo(bare_repo)
    skeleton = tmp_path / "skeleton"
    _write(skeleton / "Dockerfile", "FROM pinned-v2\n")
    input_dir = tmp_path / "input"
    _write(input_dir / "Dockerfile", "FROM pinned-v1\n")
    _write(input_dir / "app.py", "print('before')\n")
    workspace = tmp_path / "workspace"
    _write(workspace / "Dockerfile", "FROM pinned-v1\n")  # untouched by the agent
    _write(workspace / "app.py", "print('after')\n")
    provider = LocalBareRepoProvider(bare_repo)
    repo = provider.create_repo(name="proj-1")

    result = publish_build(
        provider=provider, repo=repo, workspace_dir=str(workspace), input_dir=str(input_dir),
        template_skeleton_dir=str(skeleton), protected_paths=["Dockerfile"],
        branch="forge-build-m", base_branch="main", pr_title="t", pr_body="b",
    )

    assert result.drift == []
    sha = provider.resolve_ref(repo=repo, branch="forge-build-m")
    show = subprocess.run(
        ["git", "show", f"{sha}:Dockerfile"], cwd=bare_repo, check=True, capture_output=True, text=True
    )
    assert show.stdout == "FROM pinned-v2\n"
    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s", sha], cwd=bare_repo, check=True, capture_output=True, text=True
    ).stdout
    assert "drift" not in subject


# -- the branch is committed on top of the base's tip ------------------------


def _publish_kwargs(tmp_path, provider, repo, **over):
    skeleton = tmp_path / "skeleton"
    _write(skeleton / "Dockerfile", "FROM pinned\n")
    workspace = tmp_path / "workspace"
    _write(workspace / "app.py", "print('x')\n")
    return dict(
        provider=provider, repo=repo, workspace_dir=str(workspace), input_dir=str(tmp_path / "input"),
        template_skeleton_dir=str(skeleton), protected_paths=["Dockerfile"], branch="forge-build-p",
        base_branch="main", pr_title="t", pr_body="b", **over,
    )


def test_publish_commits_on_the_base_tip_and_the_tree_is_still_exactly_the_workspace(tmp_path):
    bare_repo = tmp_path / "bare.git"
    _init_bare_repo(bare_repo)
    base_tip = subprocess.run(
        ["git", "rev-parse", "refs/heads/main"], cwd=bare_repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    provider = LocalBareRepoProvider(bare_repo)
    repo = provider.create_repo(name="p")

    publish_build(**_publish_kwargs(tmp_path, provider, repo))

    sha = provider.resolve_ref(repo=repo, branch="forge-build-p")
    parents = subprocess.run(
        ["git", "rev-list", "--parents", "-n", "1", sha], cwd=bare_repo, check=True, capture_output=True, text=True
    ).stdout.split()[1:]
    names = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", sha], cwd=bare_repo, check=True, capture_output=True, text=True
    ).stdout.split()
    assert parents == [base_tip]
    assert names == ["Dockerfile", "app.py"]  # the empty base contributed nothing to the tree


def test_publish_fails_closed_when_the_base_branch_does_not_exist_rather_than_making_an_orphan(tmp_path):
    bare_repo = tmp_path / "bare.git"
    _init_bare_repo(bare_repo)
    provider = LocalBareRepoProvider(bare_repo)
    repo = provider.create_repo(name="p")

    with pytest.raises(PublishError, match="could not fetch the base branch 'develop'"):
        publish_build(**{**_publish_kwargs(tmp_path, provider, repo), "base_branch": "develop"})

    assert _git_branches(bare_repo).replace("*", "").split() == ["main"]  # nothing was pushed
    assert provider.opened_prs == []


def test_a_failed_base_fetch_never_puts_the_clone_url_into_the_error(tmp_path):
    """The GitHub provider's clone URL embeds the token and is passed to `git fetch` as an
    argument. A clone URL naming a path that is not a repository makes git print that argument
    back verbatim (`fatal: '<url>' does not appear to be a git repository`) -- measured by the
    first assertion, so the test below it reaches the leak it is named for. (A refused network
    connection does NOT show it: git prints the URL without its credentials there.)"""
    secret_url = str(tmp_path / "x-access-token-ghs_SECRETTOKENVALUE123")  # does not exist
    ctl = tmp_path / "control-repo"
    ctl.mkdir()
    ctl_git_dir = init_platform_repo(str(ctl))
    raw = subprocess.run(["git", "--git-dir", ctl_git_dir, "fetch", secret_url], capture_output=True, text=True)
    assert secret_url in raw.stderr  # positive control: git itself would have leaked it
    provider = LocalBareRepoProvider(tmp_path / "unused.git")
    repo = RepoRef(full_name="org/p", clone_url=secret_url, default_branch="main")

    with pytest.raises(PublishError) as raised:
        publish_build(**_publish_kwargs(tmp_path, provider, repo))

    assert "could not fetch the base branch" in str(raised.value)
    assert "ghs_SECRETTOKENVALUE123" not in str(raised.value)
    assert raised.value.__cause__ is None  # the exception whose argv carries the URL is not chained


def test_the_credentials_in_a_clone_url_are_scrubbed_on_their_own_as_well():
    """git may print a URL with its password dropped, or the credential alone; each spelling is scrubbed."""
    from app.services.publish import _redactions

    url = "https://x-access-token:ghs_SECRETTOKENVALUE123@github.com/org/p.git"

    assert set(_redactions(url)) == {url, "x-access-token", "ghs_SECRETTOKENVALUE123"}


# -- git subprocesses on the publish and provider paths are bounded ----------------


def _shortened(monkeypatch, seconds: float) -> None:
    import app.services.publish as publish_module

    monkeypatch.setattr(publish_module, "GIT_TIMEOUT_SECONDS", seconds)


def test_run_git_process_returns_text_output_and_honours_check_and_input(tmp_path):
    from app.services.publish import run_git_process

    ok = run_git_process(["git", "--version"])
    assert ok.returncode == 0 and ok.stdout.startswith("git version")

    # `input` is delivered on stdin: `git mktree` reads an empty listing and prints the empty tree.
    empty = run_git_process(["git", "init", "--bare", "--quiet", str(tmp_path / "r.git")], check=True)
    assert empty.returncode == 0
    tree = run_git_process(["git", "--git-dir", str(tmp_path / "r.git"), "mktree"], input="", check=True)
    assert tree.stdout.strip() == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

    with pytest.raises(subprocess.CalledProcessError) as raised:
        run_git_process(["git", "--git-dir", str(tmp_path / "nowhere"), "rev-parse", "HEAD"], check=True)
    assert raised.value.returncode != 0 and raised.value.stderr  # git's own message is carried


def test_run_git_process_without_check_returns_a_failing_exit_code(tmp_path):
    from app.services.publish import run_git_process

    result = run_git_process(["git", "--git-dir", str(tmp_path / "nowhere"), "rev-parse", "HEAD"])

    assert result.returncode != 0


def test_a_hung_process_is_killed_and_reported_as_a_timeout(monkeypatch, returns_within):
    import sys
    import time

    from app.services.publish import run_git_process

    _shortened(monkeypatch, 0.5)
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        returns_within(15, lambda: run_git_process([sys.executable, "-c", "import time; time.sleep(60)"]))

    assert raised.value.timeout == 0.5
    assert time.monotonic() - started < 15


def test_a_timeout_returns_even_when_a_grandchild_still_holds_the_output(monkeypatch, returns_within):
    """The reason `run_git_process` is not `subprocess.run(timeout=...)`: git's network helper
    inherits git's output handles, so killing git leaves them held. `subprocess.run` then waits
    for them to close and, on Windows, never returns (measured against a stalled HTTP remote:
    still blocked 8 s after the kill). Here the child starts a grandchild that sleeps while
    holding the inherited handles, and the call must still come back."""
    import sys

    from app.services.publish import run_git_process

    holder = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']); time.sleep(60)"
    # Long enough for the child to have started its own child before the kill.
    _shortened(monkeypatch, 2.0)

    with pytest.raises(subprocess.TimeoutExpired):
        returns_within(8, lambda: run_git_process([sys.executable, "-c", holder]))


def test_fetching_from_a_host_that_never_answers_fails_as_a_publish_error(
    tmp_path, monkeypatch, silent_git_remote, returns_within
):
    """Real git against a server that accepts and goes quiet. The step's own failure type,
    the base branch named, no credential in the message, and nothing chained."""
    from app.services.publish import fetch_base_tip

    _shortened(monkeypatch, 1.0)
    ctl = tmp_path / "platform"
    ctl.mkdir()
    git_dir = init_platform_repo(str(ctl))
    url = silent_git_remote.replace("http://", "http://x-access-token:ghs_SECRETTOKENVALUE123@") + "/org/p.git"

    with pytest.raises(PublishError) as raised:
        returns_within(20, lambda: fetch_base_tip(git_dir=git_dir, clone_url=url, base_branch="main"))

    assert "could not fetch the base branch 'main'" in str(raised.value)
    assert "timed out after 1s" in str(raised.value)
    assert "ghs_SECRETTOKENVALUE123" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_a_hung_git_command_inside_publish_is_a_publish_error(
    tmp_path, monkeypatch, silent_git_remote, returns_within
):
    """`_run_git` (the commit mechanics) is bounded too: a fetch against the silent host stands in
    for any git command that stops answering."""
    from app.services.publish import _run_git

    _shortened(monkeypatch, 1.0)
    ctl = tmp_path / "platform"
    ctl.mkdir()
    init_platform_repo(str(ctl))

    with pytest.raises(PublishError, match="timed out after 1s"):
        returns_within(20, lambda: _run_git(["fetch", "--quiet", silent_git_remote + "/org/p.git"], cwd=str(ctl)))


def test_git_env_turns_the_terminal_prompt_off_and_drops_repository_selection(monkeypatch):
    from app.services.publish import git_env

    monkeypatch.setenv("GIT_DIR", "/somewhere/else")
    monkeypatch.setenv("GIT_WORK_TREE", "/somewhere/else")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")

    env = git_env()

    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env


def test_every_git_subprocess_on_the_publish_and_provider_paths_goes_through_the_bounded_runner():
    """The timeout is applied in one place, `run_git_process`; a call that runs git any other way
    would silently be the unbounded one. Reads the three modules' syntax trees."""
    import ast
    from pathlib import Path

    services = Path(__file__).resolve().parents[1] / "app" / "services"
    forbidden = {"run", "Popen", "call", "check_call", "check_output"}
    # `_kill_process_tree` runs `taskkill` (not git) to end git's process tree on Windows,
    # with its own 15 s bound. It is the second and last place these modules start a process.
    runners = {"run_git_process", "_kill_process_tree"}
    offenders = []
    for module in ("publish.py", "github_provider.py", "local_git_provider.py"):
        tree = ast.parse((services / module).read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and node.func.attr in forbidden
                    and not (module == "publish.py" and function.name in runners)
                ):
                    offenders.append(f"{module}:{function.name}:{node.func.attr}")
    assert offenders == []
