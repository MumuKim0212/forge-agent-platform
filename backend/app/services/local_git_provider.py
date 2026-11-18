"""Local bare-repository implementation of `GitProvider`.

For an installation with no git host: one bare repository per project under a
single root directory (`LOCAL_GIT_ROOT`), on the host, nothing leaving it. The
same six operations as the GitHub provider, so publish, modify staging and deploy
do not know which one they are talking to.

Storage
- `<root>/<name>.git` is the repository; `clone_url` is its absolute path. It is
  created with an EMPTY template (`--template=`), so no hook file exists, sample
  or otherwise: nothing in it is ever run by a later push.
- `create_repo` makes one empty initial commit on `main`, so a base always exists
  (publish commits every build on top of its base tip).
- Pull requests are records in ONE JSON file inside the repository
  (`forge-pull-requests.json`, beside `HEAD`, where git ignores it), replaced
  atomically on every change. They are on disk and nowhere else, because the API,
  the worker and deploy each construct their own provider instance.

Merging (`merge_pr`)
- The merge commit is written into the pull request's record with `state: merged`,
  so `get_pr_state` can name it later: a retry after a crash between this
  merge and the platform's own commit sets `confirmed_ref` from it.
- If the base tip is an ancestor of the branch tip, the base ref is fast-forwarded.
- Otherwise the merge is computed with `git merge-tree --write-tree` (in the
  repository, no work tree, nothing checked out) and committed with `commit-tree`
  as a two-parent commit.
- Either way the base ref is moved with `update-ref <ref> <new> <expected-old>`,
  so a base that moved between the read and the write fails the merge instead of
  being overwritten. A conflict raises `GitProviderError`, which deploy already
  maps to `MERGE_FAILED`.

Repository names come from `project.id`, which is generated, but this module does
not rely on that: `_repo_path` refuses any name that is not a plain single path
component and checks the result is directly inside the root.

The repository's contents are L3-derived (earlier builds' sanitised output). Every
git command here runs with `--git-dir` naming a repository this module made, with
no work tree, with repository-selecting environment variables removed. The merge
runs no user-configured merge driver because none is configured: drivers come from
configuration, never from the tree.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time

from app.services.git_provider import PR_MERGED, GitProviderError, PullRequestRef, RepoRef
from app.services.publish import git_env, run_git_process

logger = logging.getLogger("app.local_git_provider")

_DEFAULT_BRANCH = "main"
_PR_FILE = "forge-pull-requests.json"
_IDENTITY = ["-c", "user.name=Forge Bot", "-c", "user.email=forge-bot@forge.local"]
# One path component: starts with a letter or digit, then letters, digits, `.`, `_`, `-`.
# No separator, no leading dot (so neither `.` nor `..`), no drive prefix.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class LocalGitProvider:
    """`GitProvider` over bare repositories under `root`.

    Args:
        root: the directory holding every repository (created if missing).
    """

    def __init__(self, *, root: str) -> None:
        self._root = os.path.realpath(root)

    # -- paths --------------------------------------------------------------

    def _repo_path(self, name: str) -> str:
        """The repository directory for `name`, refused unless it is directly
        inside the root. Two independent checks: the name must be one plain path
        component, and the joined path must resolve to a direct child of the root."""
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise GitProviderError(f"refusing repository name {name!r}: not a plain name")
        path = os.path.realpath(os.path.join(self._root, f"{name}.git"))
        if os.path.normcase(os.path.dirname(path)) != os.path.normcase(self._root):
            raise GitProviderError(f"refusing repository name {name!r}: outside the repository root")
        return path

    def _existing(self, repo: RepoRef) -> str:
        path = self._repo_path(repo.full_name)
        if not os.path.isdir(path):
            raise GitProviderError(f"no repository {repo.full_name!r}")
        return path

    # -- git ----------------------------------------------------------------

    def _git(self, git_dir: str, args: list[str], *, input: str | None = None,
             ok_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
        try:
            result = run_git_process(
                ["git", *_IDENTITY, "--git-dir", git_dir, *args],
                cwd=self._root, env=git_env(), input=input,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitProviderError(f"git {args[0]} timed out after {exc.timeout:g}s") from None
        except OSError as exc:
            raise GitProviderError(f"git could not be run: {exc}") from exc
        if result.returncode not in ok_codes:
            raise GitProviderError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()}")
        return result

    # -- pull request records -------------------------------------------------

    def _pr_file(self, git_dir: str) -> str:
        return os.path.join(git_dir, _PR_FILE)

    def _load_prs(self, git_dir: str) -> dict:
        try:
            with open(self._pr_file(git_dir), encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {"next_number": 1, "pull_requests": {}}
        except (OSError, ValueError) as exc:
            raise GitProviderError(f"pull request records are unreadable: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("pull_requests"), dict):
            raise GitProviderError("pull request records are unreadable: unexpected shape")
        return data

    def _save_prs(self, git_dir: str, data: dict) -> None:
        """Atomic replace: write a sibling temp file, flush it to disk, then
        `os.replace`. A reader sees the old file or the new one, never half."""
        fd, tmp = tempfile.mkstemp(prefix=".prs-", suffix=".tmp", dir=git_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(5):
                try:
                    os.replace(tmp, self._pr_file(git_dir))
                    break
                except PermissionError:  # Windows: a reader has the target open at this instant
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise GitProviderError(f"pull request records could not be written: {exc}") from exc

    def _pr(self, git_dir: str, pr_number: int) -> tuple[dict, dict]:
        data = self._load_prs(git_dir)
        record = data["pull_requests"].get(str(pr_number))
        if record is None:
            raise GitProviderError(f"no pull request #{pr_number}")
        return data, record

    # -- GitProvider ----------------------------------------------------------

    def create_repo(self, *, name: str) -> RepoRef:
        path = self._repo_path(name)
        if not os.path.isdir(path):
            self._create_bare_repo(name, path)
        return RepoRef(full_name=name, clone_url=path, default_branch=_DEFAULT_BRANCH)

    def _create_bare_repo(self, name: str, path: str) -> None:
        """Built under a temporary name and renamed into place, so a repository
        another process can see is always a complete one, and two processes
        creating the same project's repository cannot interleave."""
        os.makedirs(self._root, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=".creating-", dir=self._root)
        try:
            self._git(tmp, ["init", "--bare", "--quiet", "--template=", f"--initial-branch={_DEFAULT_BRANCH}"])
            empty_tree = self._git(tmp, ["mktree"], input="").stdout.strip()
            commit = self._git(
                tmp, ["commit-tree", empty_tree, "-m", "forge: empty initial commit"]
            ).stdout.strip()
            self._git(tmp, ["update-ref", f"refs/heads/{_DEFAULT_BRANCH}", commit])
            try:
                os.rename(tmp, path)
            except OSError:
                if not os.path.isdir(path):
                    raise
                # Lost a race with another process creating the same repository.
        except (OSError, GitProviderError) as exc:
            raise GitProviderError(f"create_repo failed for {name!r}: {exc}") from exc
        finally:
            if os.path.isdir(tmp):
                _remove_tree(tmp)

    def push_branch(self, *, repo: RepoRef, branch: str, local_path: str) -> None:
        # The destination is derived from the repository name, never from
        # `repo.clone_url`, so a hand-built RepoRef cannot point a push elsewhere.
        path = self._existing(repo)
        try:
            run_git_process(
                ["git", "push", "--quiet", path, f"HEAD:refs/heads/{branch}"],
                cwd=local_path, check=True, env=git_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise GitProviderError(
                f"push_branch timed out after {exc.timeout:g}s for branch {branch!r}"
            ) from None
        except subprocess.CalledProcessError as exc:
            raise GitProviderError(
                f"push_branch failed for branch {branch!r}: {(exc.stderr or exc.stdout).strip()}"
            ) from exc

    def open_pr(
        self, *, repo: RepoRef, branch: str, base_branch: str, title: str, body: str
    ) -> PullRequestRef:
        path = self._existing(repo)
        for ref in (branch, base_branch):
            if not self._rev_or_none(path, f"refs/heads/{ref}"):
                raise GitProviderError(f"open_pr failed: branch {ref!r} does not exist")
        data = self._load_prs(path)
        number = int(data.get("next_number", 1))
        data["next_number"] = number + 1
        data["pull_requests"][str(number)] = {
            "number": number, "branch": branch, "base": base_branch, "title": title, "state": "open",
        }
        self._save_prs(path, data)
        return PullRequestRef(number=number, url=f"local:{repo.full_name}#{number}", state="open")

    def _rev_or_none(self, git_dir: str, ref: str) -> str | None:
        result = self._git(
            git_dir, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], ok_codes=(0, 1)
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def resolve_ref(self, *, repo: RepoRef, branch: str) -> str:
        path = self._existing(repo)
        sha = self._rev_or_none(path, f"refs/heads/{branch}")
        if sha is None:
            raise GitProviderError(f"resolve_ref failed: branch {branch!r} does not exist")
        return sha

    def get_pr_state(self, *, repo: RepoRef, pr_number: int) -> PullRequestRef:
        """The record's state (`open` or `merged`; this provider has no way to close
        a pull request unmerged), and for a merged one the commit `merge_pr`
        recorded. A record merged before the commit was recorded reports none."""
        path = self._existing(repo)
        _, record = self._pr(path, pr_number)
        state = record["state"]
        return PullRequestRef(
            number=pr_number,
            url=f"local:{repo.full_name}#{pr_number}",
            state=state,
            merge_commit=(record.get("merge_commit") or None) if state == PR_MERGED else None,
        )

    def merge_pr(self, *, repo: RepoRef, pr_number: int) -> str:
        path = self._existing(repo)
        data, record = self._pr(path, pr_number)
        if record["state"] != "open":
            raise GitProviderError(f"pull request #{pr_number} is {record['state']}, not open")

        base_ref = f"refs/heads/{record['base']}"
        base_tip = self._rev_or_none(path, base_ref)
        branch_tip = self._rev_or_none(path, f"refs/heads/{record['branch']}")
        if base_tip is None or branch_tip is None:
            raise GitProviderError(f"merge_pr failed: pull request #{pr_number} has a missing branch")

        if self._is_ancestor(path, branch_tip, base_tip):
            # Already contained (e.g. the ref moved, then the record write failed
            # on an earlier attempt): nothing to move, only the record to settle.
            new_tip = base_tip
        else:
            if self._is_ancestor(path, base_tip, branch_tip):
                new_tip = branch_tip  # fast-forward
            else:
                new_tip = self._merge_commit(path, record, base_tip, branch_tip)
            # Compare-and-swap: fails if the base ref is no longer at `base_tip`.
            self._git(path, ["update-ref", base_ref, new_tip, base_tip])

        record["state"] = PR_MERGED
        record["merge_commit"] = new_tip  # what a later get_pr_state reports
        self._save_prs(path, data)
        return new_tip

    def _is_ancestor(self, git_dir: str, ancestor: str, descendant: str) -> bool:
        result = self._git(git_dir, ["merge-base", "--is-ancestor", ancestor, descendant], ok_codes=(0, 1))
        return result.returncode == 0

    def _merge_commit(self, git_dir: str, record: dict, base_tip: str, branch_tip: str) -> str:
        result = self._git(
            git_dir, ["merge-tree", "--write-tree", base_tip, branch_tip], ok_codes=(0, 1)
        )
        if result.returncode == 1:
            # The conflicted paths are L3-authored names; they stay out of the message.
            raise GitProviderError(
                f"merge conflict: branch {record['branch']!r} does not merge cleanly into {record['base']!r}"
            )
        tree = result.stdout.splitlines()[0].strip() if result.stdout.strip() else ""
        if not tree:
            raise GitProviderError("merge_pr failed: git produced no merge tree")
        return self._git(
            git_dir,
            ["commit-tree", tree, "-p", base_tip, "-p", branch_tip,
             "-m", f"Merge {record['branch']} into {record['base']} (PR #{record['number']})"],
        ).stdout.strip()


def _remove_tree(path: str) -> None:
    """Removes a directory git has marked read-only (object files, on Windows)."""
    import shutil

    def _clear_readonly(func, p, _exc_info):
        os.chmod(p, 0o700)
        func(p)

    shutil.rmtree(path, onerror=_clear_readonly)
