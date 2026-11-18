"""Publish step.

After a successful agent run, L2:
  1. Restores protected paths in
     the workspace to the pinned template copies, dropping any
     agent changes to them and recording drift measured against the build's
     own input tree (`app/services/protected_paths.py`).
  2. Commits the workspace to a local git working copy and pushes
     it to a per-build branch via the configured `GitProvider`.
  3. Opens a PR (branch -> the project's base branch) and returns
     `branch + PR` as `Build.diff_ref`.

Git mechanics (init/commit/push) run via the local `git` CLI (subprocess)
against the project's already-cloned working directory — this keeps the
git-mechanics layer testable against local bare repos with no GitPython
dependency and no real host required (an implementation choice).
Only the repo-create/PR-open/ref-resolve operations
go through the `GitProvider` (host-specific).

ADR 0032: the directory it is given is the executor's SANITISED copy of the
agent's output (`app/services/harvest.py`) -- regular files and directories
only, no `.git` of the agent's, no `.forge/`. Two independent layers keep L3's
git metadata away from host git. (1) The sanitiser drops any entry whose name
is `.git` as a filesystem sees it (case, trailing dots/spaces). (2) STRUCTURAL,
and the one that holds when (1) misses a spelling: the platform's repository is
created in its own fresh directory (`init_platform_repo`), every command names
it with `--git-dir`/`--work-tree`, and the push runs with that directory as its
working directory. Git is therefore never run in, and never discovers a
repository from, the published tree, so a `.git` that survived into it cannot
select the repository, its hooks or its configuration (found on NTFS, where a
complete repository renamed `.GIT` ran its `pre-commit` on the host).

the commit is made ON TOP OF the base branch's tip, for every provider.
Publish fetches that tip from `repo.clone_url` into the platform's own fresh
repository (`fetch_base_tip`) and makes it the commit's only parent
(`commit_workspace(parent=...)`). The fetch only writes objects into the platform's
git directory; nothing is checked out from the fetched history, and the tree
committed is still exactly the sanitised workspace. A branch with no shared history
could not be opened as a PR on GitHub and could not be merged by any provider.

The sandbox never talks to git (airlock model) — this module runs
entirely in L2, operating on the workspace directory it is given after the L3
container has already been destroyed.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.services.git_provider import GitProvider, GitProviderError, PullRequestRef, RepoRef
from app.services.harvest import remove_tree
from app.services.protected_paths import restore_protected_paths

logger = logging.getLogger("app.publish")


class PublishError(RuntimeError):
    """Raised on any publish-step failure. Callers map this to
    HARVEST_ERROR — never
    swallowed."""


@dataclass(frozen=True)
class PublishResult:
    """Successful publish outcome."""

    diff_ref: str  # "<branch>#<pr_number>" — stored on Build.diff_ref
    branch: str
    pr: PullRequestRef
    drift: list[str] = field(default_factory=list)  # declared protected entries the agent changed, then restored


# Environment variables by which a caller's environment could point git at a
# repository other than the one a command names. Stripped from every git
# subprocess here: the repository is chosen by this module, explicitly, and by
# nothing else. (The operator's global git configuration is left alone.)
_REPOSITORY_SELECTING_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
)


# how long any one git subprocess on the provider/publish path may run
# before it is killed and reported as that step's own failure. Generous for a
# fetch or push of one small repository over a slow link, finite so that a host
# that accepts the connection and then says nothing cannot hold the worker (one
# build at a time) forever. A module constant, not a setting: nothing here is
# worth an operator's tuning until a real host needs it. `run_git_process` reads
# it at call time, so a test shortens it here.
GIT_TIMEOUT_SECONDS = 120.0


def git_env() -> dict:
    """The environment every git subprocess on this path runs with: the caller's,
    minus whatever could point git at another repository, and with the terminal
    prompt off so a credential question fails the command instead of
    waiting on a terminal nobody is at."""
    env = {k: v for k, v in os.environ.items() if k not in _REPOSITORY_SELECTING_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kills `proc` and everything it started. Never raises: it runs on a failure
    path, and the caller still reaps `proc` itself. The direct child is killed last in every
    case, so a tree kill that could not run still ends git."""
    try:
        if os.name == "nt":
            # `/T` walks the parent chain at call time, so git must still be running (it is:
            # this is the timeout path). Bounded: a `taskkill` that hangs must not hold the caller.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001 - already gone, or no such tool; the direct kill below still runs
        logger.debug("could not kill git's process tree for pid %s", proc.pid, exc_info=True)
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 - already gone
        pass


def run_git_process(
    args: list[str],
    *,
    cwd: str | None = None,
    env: dict | None = None,
    input: str | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """`subprocess.run` for git, bounded by `GIT_TIMEOUT_SECONDS`.

    Raises `subprocess.TimeoutExpired` (its `timeout` is the limit that applied)
    once the limit passes, with git killed and reaped; with `check`, raises
    `subprocess.CalledProcessError` on a non-zero exit, as `subprocess.run` does.
    Output comes back as text (UTF-8, undecodable bytes replaced).

    **Why not `subprocess.run(timeout=...)`, which is what this replaces.** It
    kills the child it started and then drains the stdout/stderr pipes until they
    close. A hung `git fetch`/`git push` over HTTPS is a `git` that has started
    `git-remote-https`, and the helper inherited those pipes: killing `git` leaves
    the helper holding them open, and on Windows the drain never returns -- the
    timeout expires and the caller is blocked anyway (measured: against a server
    that accepts the connection and never answers, `run(timeout=2)` was still
    blocked 8 seconds after the kill). Output therefore goes to temporary files,
    which nothing can hold open, and the wait is on the process alone. A helper that
    outlives `git` is orphaned until its own connection gives up; it no longer holds
    a caller. Stdin is closed (`input` is written through a file), so git cannot
    wait on a terminal either.

    **the helper does not outlive `git` any more.** A timeout (or an interrupted
    wait, a worker stopping on SIGTERM) kills git's whole process tree, not git alone
    (`_kill_process_tree`): the reconciler removes Docker objects by label and cannot
    reach a host process, so this is the only place a hung `git-remote-https` can be
    stopped. Measured on this Windows host with a server that accepts and never answers:
    `proc.kill()` left git's child (git for Windows runs `git.exe` under `git.exe`)
    alive, `taskkill /T` removed the chain."""
    with contextlib.ExitStack() as files:
        out = files.enter_context(tempfile.TemporaryFile())
        err = files.enter_context(tempfile.TemporaryFile())
        stdin = subprocess.DEVNULL
        if input is not None:
            stdin = files.enter_context(tempfile.TemporaryFile())
            stdin.write(input.encode("utf-8"))
            stdin.flush()
            stdin.seek(0)
        # POSIX: git leads its own session, so its process group is exactly git and the helpers
        # it starts (`_kill_process_tree` signals the group). Windows has no groups to signal;
        # `taskkill /T` follows the parent chain instead.
        group_kwargs = {} if os.name == "nt" else {"start_new_session": True}
        proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=stdin, stdout=out, stderr=err, **group_kwargs)
        timeout = GIT_TIMEOUT_SECONDS
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired(args, timeout) from None
        finally:
            if proc.poll() is None:  # timed out, or the wait was interrupted
                _kill_process_tree(proc)
                proc.wait()
        out.seek(0)
        err.seek(0)
        stdout = out.read().decode("utf-8", errors="replace").replace("\r\n", "\n")
        stderr = err.read().decode("utf-8", errors="replace").replace("\r\n", "\n")
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, args, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def _run_git(args: list[str], *, cwd: str) -> str:
    try:
        result = run_git_process(["git", *args], cwd=cwd, env=git_env(), check=True)
    except subprocess.CalledProcessError as exc:
        raise PublishError(f"git {' '.join(args)} failed: {exc.stderr or exc.stdout}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PublishError(f"git {' '.join(args)} timed out after {exc.timeout:g}s") from None
    return result.stdout


def init_platform_repo(repo_dir: str) -> str:
    """Creates the platform's own git repository in `repo_dir` -- a fresh,
    empty, platform-made directory that is NOT the published tree -- with the
    `Forge Bot` identity, and returns its git directory.

    ADR 0032: the repository that publish commits into and pushes
    from never lives inside, and is never discovered from, the tree the agent
    wrote. Every later git command names this git directory with `--git-dir` and
    the tree with `--work-tree`, and `push_branch` is given `repo_dir` as its
    working directory, so a `.git` of ANY spelling that survived into the tree
    (`.GIT` on a case-insensitive filesystem, a name the sanitiser's rule missed)
    cannot select the repository, its hooks or its configuration."""
    _run_git(["init", "--quiet"], cwd=repo_dir)
    git_dir = os.path.join(repo_dir, ".git")
    _run_git(["--git-dir", git_dir, "config", "user.email", "forge-bot@forge.local"], cwd=repo_dir)
    _run_git(["--git-dir", git_dir, "config", "user.name", "Forge Bot"], cwd=repo_dir)
    return git_dir


def _redactions(url: str) -> list[str]:
    """Every spelling of a remote URL that must not reach an error message: the
    URL itself, and its credentials (`user:token`, bare `token`) as git may print
    them on their own or with the password dropped."""
    secrets = [url]
    try:
        parts = urlsplit(url)
        for piece in (parts.password, parts.username):
            if piece:
                secrets.append(piece)
    except ValueError:
        pass
    return [s for s in secrets if s]


def fetch_base_tip(*, git_dir: str, clone_url: str, base_branch: str) -> str:
    """fetches the tip of `base_branch` from `clone_url` into the
    platform repository at `git_dir` and returns its commit SHA. Writes objects
    and one private ref (`refs/forge/base`) into `git_dir` and nothing else:
    nothing is checked out and no work tree is involved.

    `clone_url` may embed the GitHub token, so git's output is scrubbed of the URL
    and of its credentials before it is raised, and the original exception (whose
    argv carries the URL) is never chained. A base branch that does not exist is
    an error, not an orphan commit: that silently reintroduces the unmergeable
    branch this step exists to prevent."""
    args = [
        "git", "--git-dir", git_dir, "fetch", "--quiet", "--no-tags", "--no-recurse-submodules",
        clone_url, f"+refs/heads/{base_branch}:refs/forge/base",
    ]
    try:
        run_git_process(args, env=git_env(), cwd=os.path.dirname(git_dir), check=True)
    except subprocess.TimeoutExpired as exc:
        # Not chained, and the URL is not named: the exception's argv carries it
        # (and, for GitHub, the token).
        raise PublishError(
            f"could not fetch the base branch {base_branch!r} to build on: "
            f"git fetch timed out after {exc.timeout:g}s"
        ) from None
    except subprocess.CalledProcessError as exc:
        output = exc.stderr or exc.stdout or ""
        for secret in _redactions(clone_url):
            output = output.replace(secret, "<redacted-remote>")
        raise PublishError(
            f"could not fetch the base branch {base_branch!r} to build on: {output.strip()}"
        ) from None
    return _run_git(
        ["--git-dir", git_dir, "rev-parse", "--verify", "refs/forge/base^{commit}"],
        cwd=os.path.dirname(git_dir),
    ).strip()


def _workspace_files(workspace_dir: str) -> set[str]:
    """Every file under `workspace_dir` as git names it (relative, `/`-separated). A symlink
    counts as a file whether it points at a file or a directory (git records both as one entry
    and `os.walk` lists a directory link among the directories); a directory counts for nothing,
    git has no entry for an empty one.

    A directory named `.git` (any case, any depth) is not walked: git never records one, by name,
    so it is not an ignore rule's doing and not something this comparison can ask of the commit.
    What keeps one out of the tree is the sanitiser (ADR 0032 layer 1), and what keeps it from
    selecting the repository is `--git-dir` (layer 2); `commit_workspace` committing past a
    planted one is the behaviour `test_commit_workspace_ignores_a_complete_repository_planted_in_the_tree`
    pins."""
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(workspace_dir):
        dirnames[:] = [name for name in dirnames if name.casefold() != ".git"]
        rel = os.path.relpath(dirpath, workspace_dir)
        prefix = "" if rel == "." else rel.replace(os.sep, "/") + "/"
        found.update(prefix + name for name in filenames)
        found.update(prefix + name for name in dirnames if os.path.islink(os.path.join(dirpath, name)))
    return found


# ADR 0032 §Amendment 2026-10-28: what `$GIT_DIR/info/attributes` says while publish adds
# the tree. Every attribute through which `git add` rewrites content is UNSET for every path.
# `info/attributes` outranks every other source (the tree's own `.gitattributes` at any depth,
# `core.attributesFile`, the system file), and a line of it that names an attribute with a minus
# sign wins over any value another source gave it, a macro included. What each one does on `add`:
#   text          end-of-line conversion (`text`, `text=auto`, `eol=...`, and the host's
#                 `core.autocrlf`/`core.eol`/`core.safecrlf`, which only act on a path whose `text`
#                 is not unset; the legacy `crlf` attribute is read only when `text` is undefined, so
#                 it needs no line of its own -- removing `-text` is what turns its test red)
#   filter        the clean driver the host configured under that name (git-lfs, any `sed`)
#   ident         `$Id: <anything> $` collapsed to `$Id$`
#   working-tree-encoding   re-encoding of the file into UTF-8
_NEUTRAL_ATTRIBUTES = "* -text -filter -ident -working-tree-encoding\n"


def _write_neutral_attributes(git_dir: str) -> None:
    """Replaces `$GIT_DIR/info/attributes` -- which a host's `init.templateDir` can have filled --
    with `_NEUTRAL_ATTRIBUTES`, so that `git add` commits the bytes of every file as they are."""
    info_dir = os.path.join(git_dir, "info")
    os.makedirs(info_dir, exist_ok=True)
    with open(os.path.join(info_dir, "attributes"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_NEUTRAL_ATTRIBUTES)


def _needs_a_call_of_its_own(name: str) -> bool:
    """`hash-object --stdin-paths` reads one name per line and unquotes one that starts with `"`, so
    a name holding a line break, starting with a quote or not encodable as UTF-8 is hashed on its own."""
    if "\n" in name or "\r" in name or name.startswith('"'):
        return True
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


NL = "\n"


def _workspace_object_ids(*, git_dir: str, workspace_dir: str, names: list[str]) -> dict[str, str]:
    """The id git would give each of `names` (files of `workspace_dir`) if it stored the bytes on
    disk and nothing else: `hash-object --no-filters`, which consults no attribute and no
    `core.autocrlf`. A symlink is hashed by its target text, as git stores it. Writes no object."""
    tree = ["--git-dir", git_dir, "--work-tree", workspace_dir]
    ids: dict[str, str] = {}
    batch = sorted(
        name
        for name in names
        if not os.path.islink(os.path.join(workspace_dir, name)) and not _needs_a_call_of_its_own(name)
    )
    if batch:
        out = _run_git_with_input(
            [*tree, "hash-object", "--no-filters", "--stdin-paths"], cwd=workspace_dir, text=NL.join(batch) + NL
        )
        shas = out.split(NL)[: len(batch)]
        if len(shas) != len(batch) or not all(shas):
            raise PublishError("git hash-object did not return one id per file of the harvested tree")
        ids.update(zip(batch, shas))
    for name in names:
        if name in ids:
            continue
        path = os.path.join(workspace_dir, name)
        if os.path.islink(path):
            target = os.readlink(path)
            ids[name] = _run_git_with_input(
                [*tree, "hash-object", "--no-filters", "--stdin"], cwd=workspace_dir, text=target
            ).strip()
        else:
            ids[name] = _run_git([*tree, "hash-object", "--no-filters", "--", path], cwd=workspace_dir).strip()
    return ids


def _run_git_with_input(args: list[str], *, cwd: str, text: str) -> str:
    """`_run_git` with `text` on standard input."""
    try:
        result = run_git_process(["git", *args], cwd=cwd, env=git_env(), input=text, check=True)
    except UnicodeEncodeError as exc:  # a symlink target that is not UTF-8 text
        raise PublishError("a link of the harvested tree could not be read to be compared") from exc
    except subprocess.CalledProcessError as exc:
        raise PublishError(f"git {' '.join(args)} failed: {exc.stderr or exc.stdout}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PublishError(f"git {' '.join(args)} timed out after {exc.timeout:g}s") from None
    return result.stdout


def _assert_index_is_the_workspace(*, git_dir: str, workspace_dir: str) -> None:
    """what is about to be committed (the index) must be exactly the directory the
    image is built from -- the same files and, for each, the same BYTES. Raises
    `PublishError` -- naming only the counts: the paths are agent-chosen and stay out of
    platform-authored text -- when a file is missing from the index, the index holds an
    entry the directory does not, or an entry's blob is not the file's bytes.

    `add -f -A` already makes ignore rules irrelevant and `_write_neutral_attributes` makes
    attributes and `core.autocrlf` irrelevant; this is what is left to catch anything else that
    keeps a path out of the commit, puts one in it or changes what is stored: a name git refuses,
    an embedded repository (a directory holding its own `.git` is recorded as ONE entry, not its
    files), a conversion neither of those reached, a future change to the flags above. It compares
    what is in the index with what is on disk rather than re-deriving git's rules, so it needs no
    list of them. The bytes are compared as object ids: the index's, against `hash-object
    --no-filters` of the file on disk (git's own hash, so a SHA-256 repository works as well)."""
    listed = _run_git(["--git-dir", git_dir, "ls-files", "-z", "--cached", "--stage"], cwd=os.path.dirname(git_dir))
    indexed: dict[str, str] = {}
    for entry in listed.split(chr(0)):
        if entry:
            meta, _, path = entry.partition("\t")
            indexed[path] = meta.split(" ")[1]
    on_disk = _workspace_files(workspace_dir)
    missing, extra = on_disk - set(indexed), set(indexed) - on_disk
    if missing or extra:
        raise PublishError(
            "the files to commit are not the files of the harvested tree "
            f"({len(missing)} left out of the commit, {len(extra)} in it that are not in the tree)"
        )
    actual = _workspace_object_ids(git_dir=git_dir, workspace_dir=workspace_dir, names=sorted(on_disk))
    changed = sum(1 for name in on_disk if indexed[name] != actual[name])
    if changed:
        raise PublishError(
            "the bytes to commit are not the bytes of the harvested tree "
            f"({changed} of {len(on_disk)} files would be committed changed)"
        )


def commit_workspace(
    *, workspace_dir: str, git_dir: str, message: str, parent: str | None = None
) -> None:
    """Commits everything in `workspace_dir` as a single commit into the
    platform repository at `git_dir` (from `init_platform_repo`), using
    `--git-dir`/`--work-tree` so that nothing inside `workspace_dir` is ever
    consulted to find the repository. A build's workspace is always a fresh
    directory, never reused.

    With `parent` the commit's only parent is that SHA, which must
    already be an object in `git_dir` (`fetch_base_tip`). The index is built from
    the workspace alone, so the tree is exactly the workspace whatever the parent
    held. Without it the commit is a root commit (kept for the unit tests of the
    commit mechanics; publish always passes a parent)."""
    tree = ["--git-dir", git_dir, "--work-tree", workspace_dir]
    # ADR 0032 §Amendment 2026-10-17: EVERY file of the tree is committed, whatever any
    # ignore rule says. `add -A` alone honours three sources the platform does not control -- a
    # `.gitignore` inside the harvested tree (agent-written), the host's `core.excludesFile` and
    # `$XDG_CONFIG_HOME/git/ignore`, and `info/exclude` of the repository `git init` just made
    # (which a host `init.templateDir` can fill) -- and image assembly builds from the directory.
    # An agent that wrote `app/hook.py` and a `.gitignore` listing it would ship a file in the
    # deployed image that the PR, the reviewed diff and `confirmed_ref` never contain, and the next
    # modify build would silently drop it. `-f` is the whole fix and not "clear one setting": with
    # `-f` git does not consult any exclude source at all (measured with all three planted: plain
    # `add -A` kept 2 of 7 files, `add -A -f` all 7, an empty `core.excludesFile` alone only the
    # global one). The check after it is the net under that, not a second mechanism: whatever else
    # might keep a file out of the index, publish refuses rather than commit a tree that is not
    # the directory.
    #
    # ADR 0032 §Amendment 2026-10-28: the same goes for BYTES. `add` also converts content,
    # by attribute (a `.gitattributes` in the tree -- agent-written -- `$GIT_DIR/info/attributes`,
    # the host's `core.attributesFile` and system file: `text eol=...`, `working-tree-encoding`,
    # `ident`, `filter=<a driver the host configured>`) and by host config (`core.autocrlf`), and the
    # image is built from the raw file. `info/attributes` is therefore replaced with one line that
    # unsets each of those for every path (`_NEUTRAL_ATTRIBUTES`); the agent's `.gitattributes`
    # is still committed as a file, it just has no effect here. The check below compares bytes.
    _write_neutral_attributes(git_dir)
    _run_git([*tree, "add", "-f", "-A"], cwd=workspace_dir)
    _assert_index_is_the_workspace(git_dir=git_dir, workspace_dir=workspace_dir)
    # An empty commit is allowed: a modify-type build that produced no net diff
    # (all changes were to protected paths, dropped above) still needs a valid
    # commit to push and open a PR against — the drift report is what
    # signals "nothing agent-authored actually landed".
    tree_sha = _run_git([*tree, "write-tree"], cwd=workspace_dir).strip()
    parent_args = ["-p", parent] if parent else []
    commit_sha = _run_git(
        [*tree, "commit-tree", tree_sha, *parent_args, "-m", message], cwd=workspace_dir
    ).strip()
    # HEAD is what `push_branch` pushes (`HEAD:refs/heads/<branch>`).
    _run_git([*tree, "update-ref", "HEAD", commit_sha], cwd=workspace_dir)


def publish_build(
    *,
    provider: GitProvider,
    repo: RepoRef,
    workspace_dir: str,
    input_dir: str,
    template_skeleton_dir: str,
    protected_paths: list[str],
    branch: str,
    base_branch: str,
    pr_title: str,
    pr_body: str,
) -> PublishResult:
    """The full publish step for one build: restore protected paths, commit on
    top of the base branch's tip, push the per-build branch, open the PR. `input_dir` is the build's own input
    tree (the staged directory), the baseline drift is measured against. Raises `PublishError` /
    `GitProviderError` on any failure — the caller (sandbox_build_executor)
    maps these to a build failure reason, never silently."""
    drift = restore_protected_paths(
        workspace_dir=workspace_dir,
        input_dir=input_dir,
        template_skeleton_dir=template_skeleton_dir,
        protected_paths=protected_paths,
    )

    commit_message = f"forge build: {branch}" + (
        f" (drift dropped: {', '.join(drift)})" if drift else ""
    )
    # The platform's git repository lives in its own directory, outside the
    # published tree, and is removed whichever way this returns.
    repo_dir = tempfile.mkdtemp(prefix="forge-gitdir-")
    try:
        git_dir = init_platform_repo(repo_dir)
        base_tip = fetch_base_tip(git_dir=git_dir, clone_url=repo.clone_url, base_branch=base_branch)
        commit_workspace(
            workspace_dir=workspace_dir, git_dir=git_dir, message=commit_message, parent=base_tip
        )

        try:
            provider.push_branch(repo=repo, branch=branch, local_path=repo_dir)
            pr = provider.open_pr(
                repo=repo,
                branch=branch,
                base_branch=base_branch,
                title=pr_title,
                body=pr_body,
            )
        except GitProviderError:
            raise  # already descriptive; let the caller's except clause catch this type
        except Exception as exc:  # noqa: BLE001 - any other publish failure is still surfaced
            raise PublishError(f"publish failed: {exc}") from exc
    finally:
        remove_tree(repo_dir, what="platform git repository")

    return PublishResult(
        diff_ref=f"{branch}#{pr.number}",
        branch=branch,
        pr=pr,
        drift=drift,
    )
