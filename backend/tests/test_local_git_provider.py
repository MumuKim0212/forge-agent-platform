"""Tests for the local bare-repository git provider and for
publish committing every build branch on top of its base.

Everything runs against REAL bare repositories made by plain `git` under
`tmp_path`: no network, no Docker, nothing mocked at the git layer. The deploy
path is exercised for real too (`execute_deployment` -> the provider's merge),
with only L4 faked.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.models.deployment import DeploymentFailureReason, DeploymentStatus
from app.services.deploy import execute_deployment
from app.services.git_provider import GitProviderError, RepoRef, create_provider
from app.services.local_git_provider import LocalGitProvider
from app.services.publish import publish_build

# Fixtures/helpers of the deploy unit tests, reused as they are (their autouse
# fixture keeps data-service provisioning off the network).
from tests.test_deploy_service import (  # noqa: F401  (fixtures are used by name)
    FakeDeployManager,
    _confirm,
    _fake_data_services,
    _make_build,
    project,
)


def _git(repo, *args, input=None, check=True):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t.t", "--git-dir", str(repo), *args],
        check=check, capture_output=True, text=True, input=input,
    )


def _rev(repo, ref) -> str:
    return _git(repo, "rev-parse", ref).stdout.strip()


def _parents(repo, sha) -> list[str]:
    return _git(repo, "rev-list", "--parents", "-n", "1", sha).stdout.split()[1:]


def _tree(repo, sha) -> dict[str, str]:
    names = _git(repo, "ls-tree", "-r", "--name-only", sha).stdout.split()
    return {n: _git(repo, "show", f"{sha}:{n}").stdout for n in names}


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "git-root"


@pytest.fixture()
def provider(root):
    return LocalGitProvider(root=str(root))


def _publish(provider, repo, tmp_path, files: dict[str, str], branch: str, title="t"):
    workspace = tmp_path / f"ws-{branch}"
    for name, content in files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    skeleton = tmp_path / "skeleton"
    skeleton.mkdir(exist_ok=True)
    return publish_build(
        provider=provider, repo=repo, workspace_dir=str(workspace), input_dir=str(tmp_path / "input"),
        template_skeleton_dir=str(skeleton), protected_paths=[], branch=branch,
        base_branch=repo.default_branch, pr_title=title, pr_body="b",
    )


# -- factory and settings -----------------------------------------------------


def test_create_provider_returns_the_local_provider_for_local(tmp_path):
    settings = Settings(_env_file=None, git_provider="local", local_git_root=str(tmp_path / "r"))

    provider = create_provider(settings)

    assert isinstance(provider, LocalGitProvider)


def test_the_default_root_is_inside_the_backend_directory_and_ignored_by_git():
    settings = Settings(_env_file=None, git_provider="local")

    provider = create_provider(settings)

    backend = Path(__file__).resolve().parents[1]
    assert settings.local_git_root == "local-git"
    assert provider._root == str((backend / "local-git").resolve())
    # `git check-ignore` is the measurement: the default path is not committable.
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "backend/local-git/some-project.git/HEAD"],
        cwd=backend.parent, capture_output=True,
    )
    assert ignored.returncode == 0


def test_the_env_example_documents_the_new_settings():
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")

    assert "# LOCAL_GIT_ROOT=local-git" in example
    assert '"local"' in example


# -- repository creation ------------------------------------------------------


def test_create_repo_makes_a_bare_repository_with_one_empty_commit_on_main(provider, root):
    repo = provider.create_repo(name="proj-1")

    assert repo.full_name == "proj-1"
    assert repo.default_branch == "main"
    assert repo.clone_url == str((root / "proj-1.git").resolve())
    assert _git(repo.clone_url, "rev-parse", "--is-bare-repository").stdout.strip() == "true"
    assert _git(repo.clone_url, "symbolic-ref", "HEAD").stdout.strip() == "refs/heads/main"
    tip = _rev(repo.clone_url, "refs/heads/main")
    assert _git(repo.clone_url, "rev-list", "--count", tip).stdout.strip() == "1"
    assert _git(repo.clone_url, "ls-tree", "-r", tip).stdout == ""  # the empty tree


def test_create_repo_is_create_or_return_and_leaves_no_temporary_directory(provider, root):
    first = provider.create_repo(name="proj-1")
    tip = _rev(first.clone_url, "refs/heads/main")

    second = provider.create_repo(name="proj-1")

    assert second == first
    assert _rev(second.clone_url, "refs/heads/main") == tip  # not re-created
    assert sorted(os.listdir(root)) == ["proj-1.git"]


def test_a_repository_has_no_hooks_not_even_the_samples(provider):
    """created with an empty template, so nothing in it is ever run by a push."""
    repo = provider.create_repo(name="proj-1")

    hooks = Path(repo.clone_url) / "hooks"
    assert (not hooks.exists()) or list(hooks.iterdir()) == []


# -- repository names are confined to the root -------------------------------------


@pytest.mark.parametrize(
    "name",
    ["../escape", "..", ".", "a/b", "a\\b", "", "/abs", "C:\\abs", "C:abs", "a\nb", "x\n", ".hidden", "a b", "..\\..\\x"],
)
def test_a_repository_name_that_is_not_a_plain_name_is_refused(tmp_path, name):
    root = tmp_path / "git-root"
    provider = LocalGitProvider(root=str(root))
    before = sorted(p.name for p in tmp_path.iterdir())

    with pytest.raises(GitProviderError, match="refusing repository name"):
        provider.create_repo(name=name)

    # Nothing was created anywhere under, or next to, the root.
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not root.exists() or os.listdir(root) == []


def test_every_operation_resolves_the_repository_from_its_name_not_from_clone_url(provider, tmp_path):
    outside = tmp_path / "outside.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(outside)], check=True)
    forged = RepoRef(full_name="../outside", clone_url=str(outside), default_branch="main")

    with pytest.raises(GitProviderError, match="refusing repository name"):
        provider.resolve_ref(repo=forged, branch="main")
    with pytest.raises(GitProviderError, match="refusing repository name"):
        provider.push_branch(repo=forged, branch="x", local_path=str(tmp_path))
    with pytest.raises(GitProviderError, match="refusing repository name"):
        provider.merge_pr(repo=forged, pr_number=1)


# -- pull requests ------------------------------------------------------------------


def _branch_on_main(provider, repo, tmp_path, name, files):
    """A branch named `name` holding one commit on top of main's current tip, pushed to the repo."""
    work = tmp_path / f"w-{repo.full_name}-{name}"
    work.mkdir(parents=True)
    gd = work / ".git"
    subprocess.run(["git", "init", "--quiet"], cwd=work, check=True)
    _git(gd, "fetch", "--quiet", repo.clone_url, "+refs/heads/main:refs/forge/base")
    for fname, content in files.items():
        (work / fname).parent.mkdir(parents=True, exist_ok=True)
        (work / fname).write_text(content)
    tree_args = ["git", "--git-dir", str(gd), "--work-tree", str(work)]
    subprocess.run([*tree_args, "add", "-A"], check=True)
    tree = subprocess.run([*tree_args, "write-tree"], check=True, capture_output=True, text=True).stdout.strip()
    commit = _git(gd, "commit-tree", tree, "-p", _rev(gd, "refs/forge/base"), "-m", name).stdout.strip()
    _git(gd, "update-ref", "HEAD", commit)
    provider.push_branch(repo=repo, branch=name, local_path=str(work))
    return commit


def test_pull_request_numbers_increase_per_repository(provider, tmp_path):
    a = provider.create_repo(name="proj-a")
    b = provider.create_repo(name="proj-b")
    for repo in (a, b):
        _branch_on_main(provider, repo, tmp_path, "br1", {"x": "1"})
        _branch_on_main(provider, repo, tmp_path, "br2", {"y": "1"})

    numbers_a = [provider.open_pr(repo=a, branch=br, base_branch="main", title="t", body="b").number for br in ("br1", "br2")]
    number_b = provider.open_pr(repo=b, branch="br1", base_branch="main", title="t", body="b").number

    assert numbers_a == [1, 2]
    assert number_b == 1


def test_open_pr_refuses_a_branch_that_does_not_exist(provider):
    repo = provider.create_repo(name="proj-1")

    with pytest.raises(GitProviderError, match="does not exist"):
        provider.open_pr(repo=repo, branch="nope", base_branch="main", title="t", body="b")


def test_pr_state_is_on_disk_and_survives_a_new_provider_instance(provider, root, tmp_path):
    """The API, the worker and deploy each construct their own provider."""
    repo = provider.create_repo(name="proj-1")
    _branch_on_main(provider, repo, tmp_path, "br1", {"x": "1"})
    pr = provider.open_pr(repo=repo, branch="br1", base_branch="main", title="t", body="b")

    other = LocalGitProvider(root=str(root))  # another process, in effect
    assert other.get_pr_state(repo=repo, pr_number=pr.number).state == "open"
    merged = other.merge_pr(repo=repo, pr_number=pr.number)

    third = LocalGitProvider(root=str(root))
    assert third.get_pr_state(repo=repo, pr_number=pr.number).state == "merged"
    assert provider.get_pr_state(repo=repo, pr_number=pr.number).state == "merged"  # the first one reads it from disk too
    assert third.resolve_ref(repo=repo, branch="main") == merged
    # And the next number continues, rather than restarting at 1.
    _branch_on_main(third, repo, tmp_path, "br2", {"y": "1"})
    assert third.open_pr(repo=repo, branch="br2", base_branch="main", title="t", body="b").number == pr.number + 1


def test_the_pr_file_is_one_json_document_inside_the_repository_and_leaves_no_temp_files(provider, tmp_path):
    repo = provider.create_repo(name="proj-1")
    _branch_on_main(provider, repo, tmp_path, "br1", {"x": "1"})
    provider.open_pr(repo=repo, branch="br1", base_branch="main", title="the title", body="b")

    leftovers = [n for n in os.listdir(repo.clone_url) if n.endswith(".tmp")]
    record = json.loads((Path(repo.clone_url) / "forge-pull-requests.json").read_text(encoding="utf-8"))

    assert leftovers == []
    assert record["pull_requests"]["1"] == {
        "number": 1, "branch": "br1", "base": "main", "title": "the title", "state": "open",
    }


def test_get_pr_state_reports_open_with_no_merge_commit_and_the_provider_never_reports_closed(provider, tmp_path):
    repo = provider.create_repo(name="proj-1")
    _branch_on_main(provider, repo, tmp_path, "br1", {"x": "1"})
    pr = provider.open_pr(repo=repo, branch="br1", base_branch="main", title="t", body="b")

    state = provider.get_pr_state(repo=repo, pr_number=pr.number)

    assert (state.state, state.merge_commit, state.number) == ("open", None, pr.number)


def test_get_pr_state_names_the_merge_commit_of_a_fast_forward_a_merge_commit_and_survives_a_new_instance(
    provider, root, tmp_path
):
    repo = provider.create_repo(name="proj-1")
    first = _branch_on_main(provider, repo, tmp_path, "br1", {"a.py": "a\n"})
    _branch_on_main(provider, repo, tmp_path, "br2", {"b.py": "b\n"})
    pr1 = provider.open_pr(repo=repo, branch="br1", base_branch="main", title="t", body="b")
    pr2 = provider.open_pr(repo=repo, branch="br2", base_branch="main", title="t", body="b")

    ff = provider.merge_pr(repo=repo, pr_number=pr1.number)
    two_parent = provider.merge_pr(repo=repo, pr_number=pr2.number)

    other = LocalGitProvider(root=str(root))  # another process, in effect
    one = other.get_pr_state(repo=repo, pr_number=pr1.number)
    two = other.get_pr_state(repo=repo, pr_number=pr2.number)
    assert (one.state, one.merge_commit) == ("merged", first)  # a fast-forward: the branch tip itself
    assert ff == first
    assert (two.state, two.merge_commit) == ("merged", two_parent)
    assert len(_parents(repo.clone_url, two_parent)) == 2
    record = json.loads((Path(repo.clone_url) / "forge-pull-requests.json").read_text(encoding="utf-8"))
    assert record["pull_requests"]["2"]["merge_commit"] == two_parent


def test_a_merge_retried_after_the_record_write_was_lost_still_names_a_merge_commit(provider, tmp_path):
    """The ref moved and the record write did not (a crash between the two): the retry finds the
    branch already contained, settles the record, and the commit it names holds the change."""
    repo = provider.create_repo(name="proj-1")
    commit = _branch_on_main(provider, repo, tmp_path, "br1", {"a.py": "a\n"})
    pr = provider.open_pr(repo=repo, branch="br1", base_branch="main", title="t", body="b")
    provider.merge_pr(repo=repo, pr_number=pr.number)
    pr_file = Path(repo.clone_url) / "forge-pull-requests.json"
    records = json.loads(pr_file.read_text(encoding="utf-8"))
    records["pull_requests"][str(pr.number)].update(state="open")
    records["pull_requests"][str(pr.number)].pop("merge_commit", None)
    pr_file.write_text(json.dumps(records), encoding="utf-8")  # as if the write after update-ref never happened

    retried = provider.merge_pr(repo=repo, pr_number=pr.number)

    state = provider.get_pr_state(repo=repo, pr_number=pr.number)
    assert (state.state, state.merge_commit) == ("merged", retried)
    assert _tree(repo.clone_url, retried) == {"a.py": "a\n"}
    assert retried == commit


def test_a_record_merged_before_merge_commits_were_kept_reports_merged_with_none(provider, tmp_path):
    repo = provider.create_repo(name="proj-1")
    _branch_on_main(provider, repo, tmp_path, "br1", {"a.py": "a\n"})
    pr = provider.open_pr(repo=repo, branch="br1", base_branch="main", title="t", body="b")
    provider.merge_pr(repo=repo, pr_number=pr.number)
    pr_file = Path(repo.clone_url) / "forge-pull-requests.json"
    records = json.loads(pr_file.read_text(encoding="utf-8"))
    records["pull_requests"][str(pr.number)].pop("merge_commit", None)  # the earlier record shape
    pr_file.write_text(json.dumps(records), encoding="utf-8")

    state = provider.get_pr_state(repo=repo, pr_number=pr.number)

    assert (state.state, state.merge_commit) == ("merged", None)


def test_get_pr_state_of_an_unknown_pr_raises(provider):
    repo = provider.create_repo(name="proj-1")

    with pytest.raises(GitProviderError, match="no pull request"):
        provider.get_pr_state(repo=repo, pr_number=9)


# -- merging --------------------------------------------------------------------------


def test_merge_fast_forwards_when_the_base_tip_is_an_ancestor(provider, tmp_path):
    repo = provider.create_repo(name="proj-1")
    commit = _branch_on_main(provider, repo, tmp_path, "br1", {"app.py": "one\n"})
    pr = provider.open_pr(repo=repo, branch="br1", base_branch="main", title="t", body="b")

    sha = provider.merge_pr(repo=repo, pr_number=pr.number)

    assert sha == commit  # fast-forward: no merge commit
    assert _rev(repo.clone_url, "refs/heads/main") == commit


def test_merge_of_a_diverged_branch_makes_a_two_parent_commit_with_both_changes(provider, tmp_path):
    repo = provider.create_repo(name="proj-1")
    first = _branch_on_main(provider, repo, tmp_path, "br1", {"a.py": "a\n"})
    second = _branch_on_main(provider, repo, tmp_path, "br2", {"b.py": "b\n"})
    pr1 = provider.open_pr(repo=repo, branch="br1", base_branch="main", title="t", body="b")
    pr2 = provider.open_pr(repo=repo, branch="br2", base_branch="main", title="t", body="b")

    provider.merge_pr(repo=repo, pr_number=pr1.number)
    sha = provider.merge_pr(repo=repo, pr_number=pr2.number)

    assert _parents(repo.clone_url, sha) == [first, second]
    assert _tree(repo.clone_url, sha) == {"a.py": "a\n", "b.py": "b\n"}
    assert _rev(repo.clone_url, "refs/heads/main") == sha
    assert provider.get_pr_state(repo=repo, pr_number=pr2.number).state == "merged"


def test_a_merged_pr_cannot_be_merged_again(provider, tmp_path):
    repo = provider.create_repo(name="proj-1")
    _branch_on_main(provider, repo, tmp_path, "br1", {"a.py": "a\n"})
    pr = provider.open_pr(repo=repo, branch="br1", base_branch="main", title="t", body="b")
    provider.merge_pr(repo=repo, pr_number=pr.number)

    with pytest.raises(GitProviderError, match="not open"):
        provider.merge_pr(repo=repo, pr_number=pr.number)


def test_a_merge_conflict_raises_and_changes_nothing(provider, tmp_path):
    repo = provider.create_repo(name="proj-1")
    _branch_on_main(provider, repo, tmp_path, "br1", {"app.py": "from build one\n"})
    _branch_on_main(provider, repo, tmp_path, "br2", {"app.py": "from build two\n"})
    pr1 = provider.open_pr(repo=repo, branch="br1", base_branch="main", title="t", body="b")
    pr2 = provider.open_pr(repo=repo, branch="br2", base_branch="main", title="t", body="b")
    tip = provider.merge_pr(repo=repo, pr_number=pr1.number)

    with pytest.raises(GitProviderError, match="merge conflict") as raised:
        provider.merge_pr(repo=repo, pr_number=pr2.number)

    assert "app.py" not in str(raised.value)  # L3-authored names stay out of the message
    assert _rev(repo.clone_url, "refs/heads/main") == tip
    assert provider.get_pr_state(repo=repo, pr_number=pr2.number).state == "open"


class _MovesTheBaseBetweenReadAndWrite(LocalGitProvider):
    """Once `armed`, moves `main` immediately before the provider's next `update-ref` of
    it: the base has changed between the merge's read of it and its write."""

    def __init__(self, *, root):
        super().__init__(root=root)
        self.armed = False
        self.moved_to: str | None = None

    def _git(self, git_dir, args, **kwargs):
        if self.armed and args[0] == "update-ref" and args[1] == "refs/heads/main" and self.moved_to is None:
            empty = super()._git(git_dir, ["mktree"], input="").stdout.strip()
            self.moved_to = super()._git(
                git_dir, ["commit-tree", empty, "-m", "someone else moved main"]
            ).stdout.strip()
            super()._git(git_dir, ["update-ref", "refs/heads/main", self.moved_to])
        return super()._git(git_dir, args, **kwargs)


@pytest.mark.parametrize("shape", ["fast-forward", "merge commit"])
def test_a_lost_compare_and_swap_raises_and_does_not_overwrite(root, tmp_path, shape):
    racing = _MovesTheBaseBetweenReadAndWrite(root=str(root))
    repo = racing.create_repo(name="proj-1")
    _branch_on_main(racing, repo, tmp_path, "br1", {"a.py": "a\n"})
    if shape == "merge commit":
        # Make the base diverge from the branch first, so the merge needs a merge commit.
        _branch_on_main(racing, repo, tmp_path, "br2", {"b.py": "b\n"})
        pr_other = racing.open_pr(repo=repo, branch="br2", base_branch="main", title="t", body="b")
        quiet = LocalGitProvider(root=str(root))
        quiet.merge_pr(repo=repo, pr_number=pr_other.number)
    pr = racing.open_pr(repo=repo, branch="br1", base_branch="main", title="t", body="b")
    racing.armed = True

    with pytest.raises(GitProviderError):
        racing.merge_pr(repo=repo, pr_number=pr.number)

    assert racing.moved_to is not None
    assert _rev(repo.clone_url, "refs/heads/main") == racing.moved_to  # the other writer's change survives
    assert racing.get_pr_state(repo=repo, pr_number=pr.number).state == "open"


# -- every git command is bounded, non-interactive and keeps its own repository ------


def _raising_timeouts(monkeypatch, seconds=3):
    import app.services.local_git_provider as local_module

    def hung(args, **kwargs):
        raise subprocess.TimeoutExpired(args, seconds)

    monkeypatch.setattr(local_module, "run_git_process", hung)


def test_a_git_command_that_times_out_is_a_provider_error_naming_the_command(provider, monkeypatch):
    repo = provider.create_repo(name="proj-1")
    _raising_timeouts(monkeypatch, 3)

    with pytest.raises(GitProviderError, match="git rev-parse timed out after 3s"):
        provider.resolve_ref(repo=repo, branch="main")


def test_a_push_that_times_out_is_a_provider_error_naming_the_branch(provider, tmp_path, monkeypatch):
    repo = provider.create_repo(name="proj-1")
    _raising_timeouts(monkeypatch, 3)

    with pytest.raises(GitProviderError, match="push_branch timed out after 3s for branch 'br1'"):
        provider.push_branch(repo=repo, branch="br1", local_path=str(tmp_path))


def test_a_merge_that_times_out_changes_nothing_and_leaves_the_pr_open(provider, tmp_path, monkeypatch):
    repo = provider.create_repo(name="proj-1")
    _branch_on_main(provider, repo, tmp_path, "br1", {"a.py": "a\n"})
    pr = provider.open_pr(repo=repo, branch="br1", base_branch="main", title="t", body="b")
    main_before = _rev(repo.clone_url, "refs/heads/main")
    import app.services.local_git_provider as local_module

    real = local_module.run_git_process

    def hangs_on_update_ref(args, **kwargs):
        if "update-ref" in args:
            raise subprocess.TimeoutExpired(args, 3)
        return real(args, **kwargs)

    monkeypatch.setattr(local_module, "run_git_process", hangs_on_update_ref)

    with pytest.raises(GitProviderError, match="timed out"):
        provider.merge_pr(repo=repo, pr_number=pr.number)

    monkeypatch.setattr(local_module, "run_git_process", real)
    assert _rev(repo.clone_url, "refs/heads/main") == main_before
    assert provider.get_pr_state(repo=repo, pr_number=pr.number).state == "open"


def test_every_git_command_runs_non_interactively_and_without_inherited_repository_selection(
    provider, tmp_path, monkeypatch
):
    import app.services.local_git_provider as local_module

    repo = provider.create_repo(name="proj-1")
    _branch_on_main(provider, repo, tmp_path, "br1", {"a.py": "a\n"})  # before the environment is poisoned
    work = tmp_path / "w-proj-1-br1"
    calls = []
    real = local_module.run_git_process

    def spy(args, **kwargs):
        calls.append((args, kwargs))
        return real(args, **kwargs)

    monkeypatch.setattr(local_module, "run_git_process", spy)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-the-repository"))
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")

    provider.push_branch(repo=repo, branch="br1-again", local_path=str(work))  # the push
    provider.resolve_ref(repo=repo, branch="br1")  # a read
    pr = provider.open_pr(repo=repo, branch="br1", base_branch="main", title="t", body="b")
    provider.merge_pr(repo=repo, pr_number=pr.number)  # merge-base, update-ref

    assert len(calls) >= 5
    for args, kwargs in calls:
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0", args
        assert "GIT_DIR" not in kwargs["env"], args


# -- publish commits on top of the base -----------------------


def test_a_create_build_s_branch_commit_has_the_base_tip_as_its_only_parent(provider, tmp_path):
    repo = provider.create_repo(name="proj-1")
    base = _rev(repo.clone_url, "refs/heads/main")

    result = _publish(provider, repo, tmp_path, {"app.py": "print('v1')\n", "pkg/m.py": "m\n"}, "forge-build-1")

    sha = provider.resolve_ref(repo=repo, branch=result.branch)
    assert _parents(repo.clone_url, sha) == [base]
    assert _tree(repo.clone_url, sha) == {"app.py": "print('v1')\n", "pkg/m.py": "m\n"}


def test_a_modify_build_s_branch_commit_is_on_the_confirmed_tip_and_keeps_only_the_workspace(provider, tmp_path):
    repo = provider.create_repo(name="proj-1")
    first = _publish(provider, repo, tmp_path, {"app.py": "v1\n", "old.py": "old\n"}, "forge-build-1")
    confirmed = provider.merge_pr(repo=repo, pr_number=first.pr.number)

    # The modify build's workspace: the confirmed tree, with old.py deleted and app.py edited.
    second = _publish(provider, repo, tmp_path, {"app.py": "v2\n"}, "forge-build-2")

    sha = provider.resolve_ref(repo=repo, branch=second.branch)
    assert _parents(repo.clone_url, sha) == [confirmed]
    assert _tree(repo.clone_url, sha) == {"app.py": "v2\n"}  # tree is the workspace, whatever the parent held


def test_two_successive_builds_open_and_merge_through_the_local_provider(provider, tmp_path):
    """The second build is published before the first merges, so its merge is a real
    two-parent merge, not a fast-forward."""
    repo = provider.create_repo(name="proj-1")
    first = _publish(provider, repo, tmp_path, {"a.py": "a\n"}, "forge-build-1")
    second = _publish(provider, repo, tmp_path, {"b.py": "b\n"}, "forge-build-2")
    assert first.diff_ref == "forge-build-1#1"
    assert second.diff_ref == "forge-build-2#2"

    one = provider.merge_pr(repo=repo, pr_number=first.pr.number)
    two = provider.merge_pr(repo=repo, pr_number=second.pr.number)

    assert one == provider.resolve_ref(repo=repo, branch="forge-build-1")  # fast-forward
    assert len(_parents(repo.clone_url, two)) == 2  # a real merge
    assert _rev(repo.clone_url, "refs/heads/main") == two
    # The workspace of each build was only its own file, so the merged tree is the union.
    assert _tree(repo.clone_url, two) == {"a.py": "a\n", "b.py": "b\n"}


# -- through the real deploy path -------------------------------------------------------


def test_confirmed_ref_advances_for_two_successive_builds_through_deploy(db_session, project, provider, tmp_path):
    repo = provider.create_repo(name=project.id)
    # Both published before either merges; deploy then merges them one after the other.
    r1 = _publish(provider, repo, tmp_path, {"a.py": "a\n"}, "forge-build-1")
    r2 = _publish(provider, repo, tmp_path, {"b.py": "b\n"}, "forge-build-2")
    results = []
    for result in (r1, r2):
        build = _make_build(db_session, project, build_id=result.branch, diff_ref=result.diff_ref)
        deployment = _confirm(db_session, project, build)
        execute_deployment(db_session, deployment, manager=FakeDeployManager(), git_provider=provider)
        db_session.refresh(deployment)
        db_session.refresh(project)
        results.append((deployment.status, project.confirmed_ref))

    assert [status for status, _ in results] == [DeploymentStatus.ACTIVE, DeploymentStatus.ACTIVE]
    first_ref, second_ref = (ref for _, ref in results)
    assert first_ref != second_ref
    assert second_ref == _rev(repo.clone_url, "refs/heads/main")
    assert _tree(repo.clone_url, second_ref) == {"a.py": "a\n", "b.py": "b\n"}


def test_confirmed_ref_is_recovered_when_the_worker_died_after_the_provider_merge_and_before_the_commit(
    db_session, project, provider, tmp_path, monkeypatch
):
    """The first deploy of a project: the provider merged, the database commit that
    would have set `confirmed_ref` never happened. Without the fix the retry saw "not open",
    skipped the merge, and left `confirmed_ref` None, so the next modify build staged an empty
    workspace. The crash is a real failure at that commit, once; the retry is a second confirm."""
    repo = provider.create_repo(name=project.id)
    result = _publish(provider, repo, tmp_path, {"a.py": "a\n"}, "forge-build-1")
    build = _make_build(db_session, project, build_id=result.branch, diff_ref=result.diff_ref)
    first = _confirm(db_session, project, build)

    # the merge now comes after the container is healthy and routed, so the
    # first commit is no longer the merge's. Re-aimed: the commit that dies is the first one
    # made once the provider reports the PR merged, i.e. exactly the `confirmed_ref` commit.
    real_commit = db_session.commit
    armed = {"on": True}
    first_manager = FakeDeployManager()

    def dies_once_at_the_confirmed_ref_commit():
        merged = provider.get_pr_state(repo=repo, pr_number=1).state == "merged"
        if armed["on"] and merged:
            armed["on"] = False
            raise RuntimeError("simulated worker death after the provider merge")
        return real_commit()

    monkeypatch.setattr(db_session, "commit", dies_once_at_the_confirmed_ref_commit)
    execute_deployment(db_session, first, manager=first_manager, git_provider=provider)
    monkeypatch.setattr(db_session, "commit", real_commit)

    db_session.refresh(first)
    db_session.refresh(project)
    assert armed["on"] is False  # the simulated death really happened, at the merge's commit
    assert first.status == DeploymentStatus.FAILED  # the died-in-the-middle attempt
    assert len(first_manager.started) == 1  # the container had started (the merge is after the health gate) ...
    assert first_manager.removed == [first.id]  # ... and was cleaned up again
    assert provider.get_pr_state(repo=repo, pr_number=1).state == "merged"  # the provider did merge it
    assert project.confirmed_ref is None  # and the platform never learned the commit

    retry = _confirm(db_session, project, build)
    manager = FakeDeployManager()
    execute_deployment(db_session, retry, manager=manager, git_provider=provider)

    db_session.refresh(retry)
    db_session.refresh(project)
    assert retry.status == DeploymentStatus.ACTIVE
    main_tip = _rev(repo.clone_url, "refs/heads/main")
    assert project.confirmed_ref == main_tip
    assert _tree(repo.clone_url, project.confirmed_ref) == {"a.py": "a\n"}  # a modify build would clone this, not nothing
    assert len(manager.started) == 1


def test_a_pr_the_provider_cannot_name_a_merge_commit_for_fails_closed_on_a_first_deploy(
    db_session, project, provider, tmp_path
):
    """A record merged before merge commits were kept, a project with no confirmed ref: there is
    nothing to base the next build on, so the deployment fails rather than stage an empty one."""
    repo = provider.create_repo(name=project.id)
    result = _publish(provider, repo, tmp_path, {"a.py": "a\n"}, "forge-build-1")
    provider.merge_pr(repo=repo, pr_number=result.pr.number)
    pr_file = Path(repo.clone_url) / "forge-pull-requests.json"
    records = json.loads(pr_file.read_text(encoding="utf-8"))
    records["pull_requests"][str(result.pr.number)].pop("merge_commit", None)
    pr_file.write_text(json.dumps(records), encoding="utf-8")
    build = _make_build(db_session, project, build_id=result.branch, diff_ref=result.diff_ref)
    deployment = _confirm(db_session, project, build)
    manager = FakeDeployManager()

    execute_deployment(db_session, deployment, manager=manager, git_provider=provider)

    db_session.refresh(deployment)
    db_session.refresh(project)
    assert deployment.status == DeploymentStatus.FAILED
    assert deployment.failure_reason == DeploymentFailureReason.MERGE_FAILED
    assert project.confirmed_ref is None
    assert manager.started == []


def test_a_merge_conflict_ends_as_merge_failed_and_confirmed_ref_does_not_move(db_session, project, provider, tmp_path):
    repo = provider.create_repo(name=project.id)
    r1 = _publish(provider, repo, tmp_path, {"app.py": "from build one\n"}, "forge-build-1")
    r2 = _publish(provider, repo, tmp_path, {"app.py": "from build two\n"}, "forge-build-2")
    build1 = _make_build(db_session, project, build_id="forge-build-1", diff_ref=r1.diff_ref)
    first = _confirm(db_session, project, build1)
    execute_deployment(db_session, first, manager=FakeDeployManager(), git_provider=provider)
    db_session.refresh(project)
    confirmed = project.confirmed_ref
    assert confirmed is not None

    build2 = _make_build(db_session, project, build_id="forge-build-2", diff_ref=r2.diff_ref)
    second = _confirm(db_session, project, build2)
    manager = FakeDeployManager()
    execute_deployment(db_session, second, manager=manager, git_provider=provider)

    db_session.refresh(second)
    db_session.refresh(project)
    assert second.status == DeploymentStatus.FAILED
    assert second.failure_reason == DeploymentFailureReason.MERGE_FAILED
    assert project.confirmed_ref == confirmed
    assert _rev(repo.clone_url, "refs/heads/main") == confirmed
    # the conflict only shows when the merge is tried, after the container
    # started (was `manager.started == []`); the container is removed again and the PR is open.
    assert [entry["deployment_id"] for entry in manager.started] == [second.id]
    assert manager.removed == [second.id]
    assert provider.get_pr_state(repo=repo, pr_number=2).state == "open"
