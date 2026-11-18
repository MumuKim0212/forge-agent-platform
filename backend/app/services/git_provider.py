"""Git-host provider interface: the thin abstraction L2 uses to
talk to whichever git host is configured. Exactly the six operations below
(the five named by the ADR, plus `merge_pr`) — nothing else. No module outside a provider implementation may
speak a host-specific API; `publish.py` and `sandbox_build_executor.py` only
ever import `GitProvider`/the dataclasses below, never a concrete provider.

Selection is via `.env` (`GIT_PROVIDER=github`, the default, or `local` for bare
repositories on the host — see `github_provider.py`, `local_git_provider.py`
). Swapping hosts later
means adding one new provider module + config; nothing else moves (ADR
0015 Consequences).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


@dataclass(frozen=True)
class RepoRef:
    """A created/looked-up repository."""

    full_name: str  # e.g. "my-org/my-project"
    clone_url: str  # authenticated or plain HTTPS clone URL
    default_branch: str


PR_OPEN = "open"
PR_MERGED = "merged"
PR_CLOSED = "closed"  # closed WITHOUT being merged


@dataclass(frozen=True)
class PullRequestRef:
    """An opened/looked-up pull request.

    `state` is one of exactly three values, whatever the host calls them
    (Not open is not merged): `open`,
    `merged`, or `closed` — closed WITHOUT merging. GitHub's own REST `state` is
    `closed` for both of the last two, so a provider must look further (its
    `merged` flag) and never pass the host's word through. A caller that sees any
    other value must treat it as a failure, not as merged.

    `merge_commit` is the commit the merge produced and is set only when `state`
    is `merged` (and may still be `None` for a PR a host or an older record
    reports as merged without naming one)."""

    number: int
    url: str
    state: str
    merge_commit: str | None = None


class GitProviderError(RuntimeError):
    """Raised by a provider implementation on any failure (network, auth,
    host-side rejection, ...). Callers (publish.py) map this to a build
    failure reason — never swallowed."""


class GitProvider(Protocol):
    """The provider operations (five, plus `merge_pr`). Keep this interface exactly this thin —
    every convenience method added here raises the cost of the eventual
    host swap."""

    def create_repo(self, *, name: str) -> RepoRef:
        """Create (or return, if already existing) the one repository for a
        project, initialized from the template skeleton.
        Raises `GitProviderError` on failure — never swallowed."""
        ...

    def push_branch(self, *, repo: RepoRef, branch: str, local_path: str) -> None:
        """Push the given local git working directory's current commit(s)
        to `branch` on `repo`, creating the branch on the host if needed."""
        ...

    def open_pr(
        self, *, repo: RepoRef, branch: str, base_branch: str, title: str, body: str
    ) -> PullRequestRef:
        """Open a PR from `branch` onto `base_branch`."""
        ...

    def resolve_ref(self, *, repo: RepoRef, branch: str) -> str:
        """Resolve `branch` to its current commit SHA on the host."""
        ...

    def get_pr_state(self, *, repo: RepoRef, pr_number: int) -> PullRequestRef:
        """Read the current state of a previously opened PR: `state` is
        `open`, `merged` or `closed` (unmerged), never the host's raw word, and
        `merge_commit` is set when merged. Raises `GitProviderError` on
        any failure."""
        ...

    def merge_pr(self, *, repo: RepoRef, pr_number: int) -> str:
        """Merge a previously opened PR (promotion rule:
        "the build's change is merged via the git provider"). Returns the
        merge commit SHA — the project's new `confirmed_ref`. Raises
        `GitProviderError` on any failure (conflict, provider error) —
        callers map this to a deployment failure (`MERGE_FAILED`), never
        swallowed."""
        ...


class GitProviderConfigError(RuntimeError):
    """Raised by `create_provider` when the selected provider is missing
    required configuration (e.g. no token). Surfaced as a build failure,
    never a silent fallback."""


def create_provider(settings) -> GitProvider:  # noqa: ANN001 - avoid importing app.config's Settings type here to keep this module import-light
    """Selects and constructs the configured `GitProvider` from `Settings`
    (`GIT_PROVIDER`: `github`, the default, or `local`). The single seam
    a future host swap touches."""
    if settings.git_provider == "local":
        from app.services.local_git_provider import LocalGitProvider

        root = settings.local_git_root
        if not os.path.isabs(root):
            root = os.path.join(_BACKEND_DIR, root)  # relative = inside the backend directory
        return LocalGitProvider(root=root)
    if settings.git_provider == "github":
        from app.services.github_provider import GitHubProvider

        if not settings.github_token or not settings.github_org:
            raise GitProviderConfigError(
                "GIT_PROVIDER=github requires GITHUB_TOKEN and GITHUB_ORG to be set"
            )
        return GitHubProvider(
            token=settings.github_token,
            org=settings.github_org,
            base_url=settings.github_base_url,
        )
    raise GitProviderConfigError(f"unknown GIT_PROVIDER: {settings.git_provider!r}")
