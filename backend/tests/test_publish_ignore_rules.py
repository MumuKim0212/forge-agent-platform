"""ADR 0032 §Amendment 2026-10-17: ignore rules never split the commit from the image.

Publish commits EVERY file of the harvested tree. Before this, `git add -A` honoured three
sources of ignore rules the platform does not control, and image assembly built from the
directory regardless: a file could be in the deployed image and in no commit.

Real git against real bare repositories under `tmp_path`, no network, no Docker. All three
sources are PLANTED, not assumed: the harvested tree carries its own `.gitignore` (the agent's),
the "host" has a global `core.excludesFile` (a global git config reached through
`GIT_CONFIG_GLOBAL`, which `publish.git_env()` passes on because it copies the environment), and
the platform repository's `info/exclude` is filled by the host's `init.templateDir`, which is how
a host could put rules there. Both providers' real publish paths run: `LocalGitProvider`, and
`GitHubProvider` with its HTTP stubbed and its `clone_url` a local bare repository (its push is a
plain `git push <clone_url>`).

The expected file sets below are LITERALS: they are not computed by anything in `app/`.
"""

from __future__ import annotations

import subprocess

import httpx
import pytest

from app.services.git_provider import RepoRef
from app.services.github_provider import GitHubProvider
from app.services.local_git_provider import LocalGitProvider
from app.services.publish import (
    PublishError,
    _assert_index_is_the_workspace,
    commit_workspace,
    init_platform_repo,
    publish_build,
)

# Everything the tree holds, by what would have kept it out of the commit in earlier versions.
TREE = {
    "Dockerfile": "FROM agent-wrote-this\n",
    ".gitignore": "app/hook.py\n*.log\nignored-dir/\n",
    "app/app.py": "print('app')\n",
    "app/hook.py": "print('listed in the tree's own .gitignore by name')\n",
    "build.log": "matched by a *.log pattern in the tree's own .gitignore\n",
    "ignored-dir/data.txt": "under a directory the tree's own .gitignore lists\n",
    "token.secret": "matched by the host's global core.excludesFile\n",
    "from-template.txt": "matched by info/exclude, which the host's init.templateDir filled\n",
}
EVERY_FILE = {
    "Dockerfile",
    ".gitignore",
    "app/app.py",
    "app/hook.py",
    "build.log",
    "ignored-dir/data.txt",
    "token.secret",
    "from-template.txt",
}
# What plain `git add -A` committed under the same three sources (measured on this host; the
# first test pins it, so the other tests cannot pass against a tree git would have kept whole).
WHAT_PLAIN_ADD_KEPT = {"Dockerfile", ".gitignore", "app/app.py"}


def _write_tree(root, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))  # exact bytes: `write_text` would write CRLF on Windows


@pytest.fixture()
def hostile_git_host(tmp_path, monkeypatch):
    """A host whose git configuration would drop files: a global excludes file, and a template
    directory whose `info/exclude` every new repository inherits. Reached by environment, as a
    real host's configuration is."""
    ignore = tmp_path / "host-global-ignore"
    ignore.write_text("*.secret\n", encoding="utf-8")
    template = tmp_path / "host-template"
    (template / "info").mkdir(parents=True)
    (template / "info" / "exclude").write_text("from-template.txt\n", encoding="utf-8")
    config = tmp_path / "host-gitconfig"
    config.write_text(
        "[core]\n\texcludesFile = " + ignore.as_posix() + "\n[init]\n\ttemplateDir = " + template.as_posix() + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "no-home"))
    return config


def _git(git_dir_or_repo, *args, check=True):
    return subprocess.run(["git", "--git-dir", str(git_dir_or_repo), *args], check=check, capture_output=True, text=True)


def _tree_files(git_dir, ref) -> set[str]:
    return set(_git(git_dir, "ls-tree", "-r", "--name-only", ref).stdout.split("\n")) - {""}


def _bare_with_base(path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "--quiet", "--initial-branch=main"], cwd=path, check=True, capture_output=True)
    empty = subprocess.run(["git", "mktree"], cwd=path, input="", check=True, capture_output=True, text=True).stdout.strip()
    commit = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t.t", "commit-tree", empty, "-m", "base"],
        cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/heads/main", commit], cwd=path, check=True, capture_output=True)


def _github_provider_over(bare) -> tuple[GitHubProvider, RepoRef]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/pulls"):
            return httpx.Response(201, json={"number": 7, "html_url": "https://github.test/pr/7", "state": "open"})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    provider = GitHubProvider(token="ghp_fake", org="o", base_url="https://api.github.test", http_client=client)
    return provider, RepoRef(full_name="o/p", clone_url=str(bare), default_branch="main")


def _publish_kwargs(tmp_path, workspace, provider, repo, branch):
    skeleton = tmp_path / "skeleton"
    skeleton.mkdir(exist_ok=True)
    (skeleton / "Dockerfile").write_text("FROM pinned\n", encoding="utf-8")
    return dict(
        provider=provider, repo=repo, workspace_dir=str(workspace), input_dir=str(tmp_path / "input"),
        template_skeleton_dir=str(skeleton), protected_paths=["Dockerfile"], branch=branch,
        base_branch="main", pr_title="t", pr_body="b",
    )


# --- the measurement the rest stand on -------------------------------------------


def test_plain_git_add_under_these_three_sources_really_drops_files(tmp_path, hostile_git_host):
    """The positive control: the same tree, the same host configuration, committed the way publish
    used to (`add -A`). If this kept everything, the tests below would prove nothing."""
    workspace = tmp_path / "workspace"
    _write_tree(workspace, TREE)
    repo_dir = tmp_path / "plain-repo"
    repo_dir.mkdir()
    git_dir = init_platform_repo(str(repo_dir))

    subprocess.run(
        ["git", "--git-dir", git_dir, "--work-tree", str(workspace), "add", "-A"],
        cwd=workspace, check=True, capture_output=True,
    )
    kept = set(_git(git_dir, "ls-files").stdout.split("\n")) - {""}

    assert kept == WHAT_PLAIN_ADD_KEPT


# --- the commit ---------------------------------------------------------------


def test_commit_workspace_commits_every_file_whatever_the_ignore_rules(tmp_path, hostile_git_host):
    workspace = tmp_path / "workspace"
    _write_tree(workspace, TREE)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    git_dir = init_platform_repo(str(repo_dir))

    commit_workspace(workspace_dir=str(workspace), git_dir=git_dir, message="m")

    assert _tree_files(git_dir, "HEAD") == EVERY_FILE
    # Not only listed: the ignored file's BYTES are in the commit.
    assert _git(git_dir, "show", "HEAD:app/hook.py").stdout.strip() == "print('listed in the tree's own .gitignore by name')"


def test_a_repository_config_that_names_an_excludes_file_does_not_matter_either(tmp_path, hostile_git_host):
    """The platform repository's OWN config (`$GIT_DIR/config`) is a fourth source: a
    `core.excludesFile` there. `-f` consults no exclude source at all, so this is not special-cased."""
    workspace = tmp_path / "workspace"
    _write_tree(workspace, TREE)
    own_rules = tmp_path / "repo-own-ignore"
    own_rules.write_text("app/app.py\nDockerfile\n", encoding="utf-8")
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    git_dir = init_platform_repo(str(repo_dir))
    _git(git_dir, "config", "core.excludesFile", str(own_rules))

    commit_workspace(workspace_dir=str(workspace), git_dir=git_dir, message="m")

    assert _tree_files(git_dir, "HEAD") == EVERY_FILE


@pytest.mark.parametrize("provider_kind", ["local", "github"])
def test_publish_build_pushes_every_file_through_each_provider(tmp_path, hostile_git_host, provider_kind):
    """Both providers' real publish paths. The branch each one received holds every file."""
    workspace = tmp_path / "workspace"
    _write_tree(workspace, TREE)
    if provider_kind == "local":
        provider = LocalGitProvider(root=str(tmp_path / "git-root"))
        repo = provider.create_repo(name="proj-1")
        bare = repo.clone_url
    else:
        bare = tmp_path / "bare.git"
        _bare_with_base(bare)
        provider, repo = _github_provider_over(bare)

    publish_build(**_publish_kwargs(tmp_path, workspace, provider, repo, "forge-build-ignored"))

    # Dockerfile is a protected path: restored to the pinned copy, still committed. Every other
    # file is the agent's, including the four an ignore rule listed.
    assert _tree_files(bare, "refs/heads/forge-build-ignored") == EVERY_FILE
    assert _git(bare, "show", "refs/heads/forge-build-ignored:Dockerfile").stdout == "FROM pinned\n"
    assert _git(bare, "show", "refs/heads/forge-build-ignored:token.secret").stdout.strip() == (
        "matched by the host's global core.excludesFile"
    )


# --- the net under the flag ----------------------------------------------------------


def _index_built_the_old_way(tmp_path, workspace):
    repo_dir = tmp_path / "old-way"
    repo_dir.mkdir()
    git_dir = init_platform_repo(str(repo_dir))
    subprocess.run(
        ["git", "--git-dir", git_dir, "--work-tree", str(workspace), "add", "-A"],
        cwd=workspace, check=True, capture_output=True,
    )
    return git_dir


def test_the_check_refuses_an_index_that_is_not_the_workspace_and_names_only_counts(tmp_path, hostile_git_host):
    workspace = tmp_path / "workspace"
    _write_tree(workspace, TREE)
    git_dir = _index_built_the_old_way(tmp_path, workspace)

    with pytest.raises(PublishError) as raised:
        _assert_index_is_the_workspace(git_dir=git_dir, workspace_dir=str(workspace))

    message = str(raised.value)
    assert "5 left out of the commit, 0 in it that are not in the tree" in message
    # The paths are agent-chosen and are not put into platform-authored text.
    assert not any(name in message for name in ("hook.py", "token.secret", "ignored-dir", "build.log"))


def test_the_check_passes_for_an_index_that_is_the_workspace(tmp_path, hostile_git_host):
    """The control for the test above: the same tree, committed with `-f`, is accepted."""
    workspace = tmp_path / "workspace"
    _write_tree(workspace, TREE)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    git_dir = init_platform_repo(str(repo_dir))
    subprocess.run(
        ["git", "--git-dir", git_dir, "--work-tree", str(workspace), "add", "-f", "-A"],
        cwd=workspace, check=True, capture_output=True,
    )

    _assert_index_is_the_workspace(git_dir=git_dir, workspace_dir=str(workspace))  # does not raise


def test_the_check_refuses_an_entry_in_the_index_that_the_tree_does_not_hold(tmp_path, hostile_git_host):
    workspace = tmp_path / "workspace"
    _write_tree(workspace, {"app/app.py": "x\n", "gone.txt": "y\n"})
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    git_dir = init_platform_repo(str(repo_dir))
    subprocess.run(
        ["git", "--git-dir", git_dir, "--work-tree", str(workspace), "add", "-f", "-A"],
        cwd=workspace, check=True, capture_output=True,
    )
    (workspace / "gone.txt").unlink()

    with pytest.raises(PublishError) as raised:
        _assert_index_is_the_workspace(git_dir=git_dir, workspace_dir=str(workspace))

    assert "0 left out of the commit, 1 in it that are not in the tree" in str(raised.value)


def test_publish_does_not_push_when_the_files_to_commit_are_not_the_files_of_the_tree(
    tmp_path, hostile_git_host, monkeypatch
):
    """End to end through `publish_build`: if the add ever stops committing everything, nothing
    is pushed and no pull request is opened."""
    import app.services.publish as publish_module

    real = publish_module._run_git

    def _plain_add(args, *, cwd):
        return real([a for a in args if a != "-f"], cwd=cwd)

    monkeypatch.setattr(publish_module, "_run_git", _plain_add)
    workspace = tmp_path / "workspace"
    _write_tree(workspace, TREE)
    provider = LocalGitProvider(root=str(tmp_path / "git-root"))
    repo = provider.create_repo(name="proj-1")

    with pytest.raises(PublishError, match="not the files of the harvested tree"):
        publish_build(**_publish_kwargs(tmp_path, workspace, provider, repo, "forge-build-refused"))

    assert provider._load_prs(repo.clone_url)["pull_requests"] == {}
    assert _git(repo.clone_url, "branch", "--list").stdout.replace("*", "").split() == ["main"]

