"""ADR 0032 §Amendment 2026-10-28: the commit holds the BYTES of the harvested tree, not only
its files. The commit and the image hold the same file set; `git add` also rewrites
content -- by attribute (a `.gitattributes` the agent wrote, `info/attributes`, the host's
attributes file) and by host config (`core.autocrlf`, a `filter` driver) -- while the image is
built from the raw directory. A reviewer reading the PR would then read other bytes than the ones
that run.

Real git against real bare repositories under `tmp_path`, no network, no Docker. The "host" is
PLANTED, as in `test_publish_ignore_rules.py`: a global git configuration reached through
`GIT_CONFIG_GLOBAL` (never the developer's own) that sets `core.autocrlf=true` and a `filter` driver
`x` whose clean step upper-cases the file. Every expected byte string below is a LITERAL, and the
committed bytes are read back from the bare repository with `cat-file`, not through anything in `app/`.
"""

from __future__ import annotations

import subprocess

import pytest

import app.services.publish as publish_module
from app.services.local_git_provider import LocalGitProvider
from app.services.publish import PublishError, _assert_index_is_the_workspace, init_platform_repo, publish_build
from tests.test_publish_ignore_rules import _publish_kwargs

# Each lever: the `.gitattributes` the agent writes at the root, and files whose bytes `git add`
# WOULD change under it (the control below proves that, per lever).
LEVERS = {
    "text-eol-crlf": ("* text eol=crlf\n", {"app/a.txt": b"one\r\ntwo\r\n", "app/b.py": b"x = 1\r\n"}),
    "working-tree-encoding": ("*.txt working-tree-encoding=UTF-16\n", {"notes.txt": "héllo\n".encode("utf-16")}),
    "ident": ("* ident\n", {"v.txt": b"$Id: 0123456789abcdef $\n", "w.txt": b"$Id$\n"}),
    "filter": ("* filter=x\n", {"s.txt": b"hello world\n", "app/t.py": b"print('lower')\n"}),
    "text-auto": ("* text=auto\n", {"a.txt": b"one\r\ntwo\r\n"}),
    "legacy-crlf": ("* crlf\n", {"a.txt": b"one\r\ntwo\r\n"}),
    "macro": ("[attr]mine text eol=crlf\n* mine\n", {"a.txt": b"one\r\ntwo\r\n"}),
}
UNTOUCHED = {"plain.bin": b"\x00\x01\x02\xff", "app/app.py": b"print('app')\n"}


@pytest.fixture()
def attribute_host(tmp_path, monkeypatch):
    """A host whose git would rewrite content: `core.autocrlf=true`, a global attributes file that
    sets `text eol=crlf` on `*.cfg`, and a `filter` driver `x` (clean: upper-case)."""
    attributes = tmp_path / "host-global-attributes"
    attributes.write_bytes(b"*.cfg text eol=crlf\n")
    config = tmp_path / "host-gitconfig"
    config.write_text(
        "[core]\n\tautocrlf = true\n\tattributesFile = " + attributes.as_posix() + "\n"
        '[filter "x"]\n\tclean = tr a-z A-Z\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "no-home"))
    return config


def _write_bytes_tree(root, files: dict[str, bytes]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _lever_tree(lever: str) -> dict[str, bytes]:
    attributes, files = LEVERS[lever]
    return {".gitattributes": attributes.encode("utf-8"), **UNTOUCHED, **files}


def _blob(git_dir_or_repo, spec: str) -> bytes:
    return subprocess.run(
        ["git", "--git-dir", str(git_dir_or_repo), "cat-file", "blob", spec], check=True, capture_output=True
    ).stdout


def _committed(git_dir_or_repo, ref: str) -> dict[str, bytes]:
    names = subprocess.run(
        ["git", "--git-dir", str(git_dir_or_repo), "ls-tree", "-r", "-z", "--name-only", ref],
        check=True, capture_output=True,
    ).stdout.decode("utf-8").split("\0")
    return {name: _blob(git_dir_or_repo, f"{ref}:{name}") for name in names if name}


def _on_disk(root) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _plain_staged(tmp_path, workspace, name="plain"):
    """What `git add -f -A` stages from `workspace` with nothing neutralised: (name -> staged bytes, git dir)."""
    repo_dir = tmp_path / name
    repo_dir.mkdir()
    git_dir = init_platform_repo(str(repo_dir))
    subprocess.run(
        ["git", "--git-dir", git_dir, "--work-tree", str(workspace), "add", "-f", "-A"],
        cwd=workspace, check=True, capture_output=True,
    )
    listed = subprocess.run(
        ["git", "--git-dir", git_dir, "ls-files", "-z"], check=True, capture_output=True
    ).stdout.decode("utf-8").split("\0")
    return {name: _blob(git_dir, f":{name}") for name in listed if name}, git_dir


def _publish(tmp_path, workspace, branch="forge-build-bytes"):
    provider = LocalGitProvider(root=str(tmp_path / "git-root"))
    repo = provider.create_repo(name="proj-1")
    publish_build(**_publish_kwargs(tmp_path, workspace, provider, repo, branch))
    return provider, repo


# --- the positive control: without this, the tests below could pass against a git that never rewrote ---


@pytest.mark.parametrize("lever", sorted(LEVERS))
def test_plain_add_really_rewrites_the_bytes_under_each_lever(tmp_path, attribute_host, lever):
    workspace = tmp_path / "workspace"
    _write_bytes_tree(workspace, _lever_tree(lever))

    staged, _ = _plain_staged(tmp_path, workspace)

    changed = {name for name, content in _on_disk(workspace).items() if staged[name] != content}
    assert changed, f"git add did not change any file under {lever!r}; this lever proves nothing"


def test_plain_add_under_the_host_config_alone_rewrites_crlf(tmp_path, attribute_host):
    workspace = tmp_path / "workspace"
    _write_bytes_tree(workspace, {"a.txt": b"one\r\ntwo\r\n", "conf.cfg": b"k=v\n"})

    staged, _ = _plain_staged(tmp_path, workspace)

    assert staged["a.txt"] == b"one\ntwo\n"  # core.autocrlf=true
    assert staged["conf.cfg"] == b"k=v\n"  # the host's attributes file sets eol=crlf, which only acts on checkout


# --- the commit holds the harvested bytes ---------------------------------------------------------


@pytest.mark.parametrize("lever", sorted(LEVERS))
def test_publish_commits_the_workspace_bytes_under_each_attribute_lever(tmp_path, attribute_host, lever):
    workspace = tmp_path / "workspace"
    tree = _lever_tree(lever)
    _write_bytes_tree(workspace, tree)

    _, repo = _publish(tmp_path, workspace)

    committed = _committed(repo.clone_url, "refs/heads/forge-build-bytes")
    # `Dockerfile` is protected and restored from the pinned skeleton; the workspace now holds it, so
    # the comparison is against the directory as publish left it (the bytes the image is built from).
    assert committed == _on_disk(workspace)
    # And the agent's literal bytes survived, for every file it wrote (the Dockerfile aside).
    for name, content in tree.items():
        assert committed[name] == content, name
    # The agent's `.gitattributes` is committed as a file, as it wrote it; it just has no effect.
    assert committed[".gitattributes"] == LEVERS[lever][0].encode("utf-8")


def test_a_nested_gitattributes_has_no_effect_either(tmp_path, attribute_host):
    workspace = tmp_path / "workspace"
    tree = {"app/.gitattributes": b"*.txt text eol=crlf\n*.py filter=x\n", "app/a.txt": b"one\r\n", "app/b.py": b"print(1)\n"}
    _write_bytes_tree(workspace, tree)

    _, repo = _publish(tmp_path, workspace)

    committed = _committed(repo.clone_url, "refs/heads/forge-build-bytes")
    assert {name: committed[name] for name in tree} == tree


def test_the_hosts_autocrlf_and_attributes_file_do_not_change_committed_bytes(tmp_path, attribute_host):
    workspace = tmp_path / "workspace"
    tree = {
        "crlf.txt": b"one\r\ntwo\r\n",
        "lf.txt": b"one\ntwo\n",
        "mixed.txt": b"one\r\ntwo\nthree\r\n",
        "conf.cfg": b"k=v\r\n",  # matched by the host's global attributes file
        "plain.bin": b"\x00\x01\r\n\xff",
    }
    _write_bytes_tree(workspace, tree)

    _, repo = _publish(tmp_path, workspace)

    committed = _committed(repo.clone_url, "refs/heads/forge-build-bytes")
    assert {name: committed[name] for name in tree} == tree


def test_an_info_attributes_a_host_template_put_in_the_new_repository_has_no_effect(tmp_path, attribute_host):
    """`init.templateDir` fills `$GIT_DIR/info/attributes` of every repository `git init` makes, and
    that file outranks the tree's own `.gitattributes`."""
    template = tmp_path / "host-template"
    (template / "info").mkdir(parents=True)
    (template / "info" / "attributes").write_bytes(b"* text eol=crlf filter=x ident\n")
    with open(attribute_host, "a", encoding="utf-8") as handle:
        handle.write("[init]\n\ttemplateDir = " + template.as_posix() + "\n")
    workspace = tmp_path / "workspace"
    tree = {"a.txt": b"one\r\n", "b.txt": b"hello\n", "c.txt": b"$Id: 12 $\n"}
    _write_bytes_tree(workspace, tree)

    staged, git_dir = _plain_staged(tmp_path, workspace)
    assert staged != tree  # the control: the template's attributes really do rewrite on a plain add
    assert (tmp_path / "plain" / ".git" / "info" / "attributes").read_bytes() == b"* text eol=crlf filter=x ident\n"

    _, repo = _publish(tmp_path, workspace)

    committed = _committed(repo.clone_url, "refs/heads/forge-build-bytes")
    assert {name: committed[name] for name in tree} == tree


# --- the check that proves it ---------------------------------------------------------------------


def test_the_check_refuses_an_index_whose_bytes_are_not_the_workspace_and_names_only_counts(tmp_path, attribute_host):
    workspace = tmp_path / "workspace"
    _write_bytes_tree(workspace, _lever_tree("text-eol-crlf"))
    _, git_dir = _plain_staged(tmp_path, workspace)  # the same file SET, other bytes

    with pytest.raises(PublishError) as raised:
        _assert_index_is_the_workspace(git_dir=git_dir, workspace_dir=str(workspace))

    message = str(raised.value)
    assert "the bytes to commit are not the bytes of the harvested tree" in message
    assert "2 of 5 files would be committed changed" in message
    # The paths are agent-chosen and are not put into platform-authored text.
    assert not any(name in message for name in ("a.txt", "b.py", "plain.bin", "app"))


def test_the_check_passes_for_an_index_that_is_the_workspace_bytes(tmp_path, attribute_host):
    """The control for the test above: the same tree staged with attributes neutralised."""
    workspace = tmp_path / "workspace"
    _write_bytes_tree(workspace, _lever_tree("text-eol-crlf"))
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    git_dir = init_platform_repo(str(repo_dir))
    publish_module._write_neutral_attributes(git_dir)
    subprocess.run(
        ["git", "--git-dir", git_dir, "--work-tree", str(workspace), "add", "-f", "-A"],
        cwd=workspace, check=True, capture_output=True,
    )

    _assert_index_is_the_workspace(git_dir=git_dir, workspace_dir=str(workspace))  # does not raise


@pytest.mark.parametrize("lever", sorted(LEVERS))
def test_publish_pushes_nothing_when_the_staged_bytes_are_not_the_workspace(tmp_path, attribute_host, monkeypatch, lever):
    """With the neutralising step switched off, plain `add` rewrites content under each lever and
    the check -- the only thing left -- refuses: nothing is pushed, no pull request is opened. A
    check that only compared file lists would let every one of these through."""
    monkeypatch.setattr(publish_module, "_write_neutral_attributes", lambda git_dir: None)
    workspace = tmp_path / "workspace"
    _write_bytes_tree(workspace, _lever_tree(lever))
    provider = LocalGitProvider(root=str(tmp_path / "git-root"))
    repo = provider.create_repo(name="proj-1")

    with pytest.raises(PublishError, match="the bytes to commit are not the bytes of the harvested tree"):
        publish_build(**_publish_kwargs(tmp_path, workspace, provider, repo, "forge-build-refused"))

    assert provider._load_prs(repo.clone_url)["pull_requests"] == {}
    branches = subprocess.run(
        ["git", "--git-dir", repo.clone_url, "branch", "--list"], check=True, capture_output=True, text=True
    ).stdout.replace("*", "").split()
    assert branches == ["main"]


# --- the other git-side transformations -------------------------------------------------------------


def test_a_link_in_the_tree_is_compared_by_its_target(tmp_path, attribute_host):
    """Harvest refuses links, so none reaches publish; the check still handles one, because git
    stores a link as a blob of its target text. Skipped where this host cannot make a link."""
    workspace = tmp_path / "workspace"
    _write_bytes_tree(workspace, {"target.txt": b"hi\n"})
    try:
        (workspace / "link.txt").symlink_to("target.txt")
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create a symbolic link")
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    git_dir = init_platform_repo(str(repo_dir))
    publish_module._write_neutral_attributes(git_dir)
    subprocess.run(
        ["git", "--git-dir", git_dir, "--work-tree", str(workspace), "add", "-f", "-A"],
        cwd=workspace, check=True, capture_output=True,
    )

    _assert_index_is_the_workspace(git_dir=git_dir, workspace_dir=str(workspace))  # does not raise

    assert _blob(git_dir, ":link.txt") == b"target.txt"


@pytest.mark.parametrize("name", ['"quoted.txt', "with space.txt", "café.txt", "two\nlines.txt"])
def test_unusual_file_names_are_compared_too(tmp_path, attribute_host, name):
    """The names `hash-object --stdin-paths` cannot take on a line of their own are hashed one by
    one; the check must still see their bytes. A name with a line break cannot be made on Windows."""
    workspace = tmp_path / "workspace"
    try:
        _write_bytes_tree(workspace, {name: b"one\r\ntwo\r\n", ".gitattributes": b"* text eol=lf\n"})
    except OSError:
        pytest.skip("this filesystem cannot hold that file name")
    staged, git_dir = _plain_staged(tmp_path, workspace)  # the control: plain add rewrites it
    assert staged[name] == b"one\ntwo\n"

    with pytest.raises(PublishError, match="1 of 2 files would be committed changed"):
        _assert_index_is_the_workspace(git_dir=git_dir, workspace_dir=str(workspace))


def test_the_one_by_one_hash_gives_the_same_ids_as_the_batch(tmp_path, attribute_host, monkeypatch):
    """The names a line cannot carry (a quote, a line break) cannot be made on every host, so the
    path that hashes a file by itself is run here for ordinary names and compared with the batch."""
    workspace = tmp_path / "workspace"
    tree = {"a.txt": b"one\r\ntwo\r\n", "app/b.py": b"x = 1\n", ".gitattributes": b"* text eol=lf\n"}
    _write_bytes_tree(workspace, tree)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    git_dir = init_platform_repo(str(repo_dir))
    names = sorted(tree)

    batch = publish_module._workspace_object_ids(git_dir=git_dir, workspace_dir=str(workspace), names=names)
    monkeypatch.setattr(publish_module, "_needs_a_call_of_its_own", lambda name: True)
    one_by_one = publish_module._workspace_object_ids(git_dir=git_dir, workspace_dir=str(workspace), names=names)

    assert one_by_one == batch
    # and they are the ids of the bytes as written (a.txt keeps its CRLF), not of a converted copy
    expected = subprocess.run(
        ["git", "hash-object", "--stdin"], input=tree["a.txt"], check=True, capture_output=True
    ).stdout.decode().strip()
    assert batch["a.txt"] == expected
