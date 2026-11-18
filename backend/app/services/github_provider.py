"""GitHub implementation of `GitProvider` (the default provider).

Works against github.com and GitHub Enterprise via a configurable base URL.
Uses the GitHub REST API (via `httpx`) for repo/PR/ref operations, and the
local `git` CLI (via `subprocess`) to push commits — the REST API has no
"push a local working directory" endpoint; `git push` against an
authenticated HTTPS remote URL is the standard mechanism every git host
supports identically, keeping this module swappable.

The token lives only here (constructor arg, sourced from `Settings` by the
caller) — never logged, never included in an exception message, never
written to a sandbox container's environment (the sandbox never talks to
git at all).
"""

from __future__ import annotations

import logging
import subprocess

import httpx

from app.services.git_provider import (
    PR_CLOSED,
    PR_MERGED,
    PR_OPEN,
    GitProviderError,
    PullRequestRef,
    RepoRef,
)
from app.services.publish import _redactions, git_env, run_git_process

logger = logging.getLogger("app.github_provider")

_DEFAULT_BASE_URL = "https://api.github.com"


class GitHubProvider:
    """`GitProvider` for github.com / GitHub Enterprise.

    Args:
        token: a repo-scoped access token. Never logged.
        org: the org/owner repos are created under.
        base_url: REST API base URL (`https://api.github.com` for github.com,
            `https://<host>/api/v3` for Enterprise).
        http_client: injectable `httpx.Client` for tests (stub upstream).
    """

    def __init__(
        self,
        *,
        token: str,
        org: str,
        base_url: str = _DEFAULT_BASE_URL,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._token = token
        self._org = org
        self._base_url = base_url.rstrip("/")
        self._client = http_client or httpx.Client(base_url=self._base_url, timeout=30.0)

    def _auth_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
        }

    # -- GitProvider ------------------------------------------------------

    def create_repo(self, *, name: str) -> RepoRef:
        try:
            resp = self._client.post(
                f"/orgs/{self._org}/repos",
                json={"name": name, "private": True, "auto_init": True},
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            raise GitProviderError(f"create_repo request failed: {exc}") from exc

        if resp.status_code == 422:
            # Already exists (org repo-name conflict) — treat as idempotent
            # lookup, matching "repo per project" (create-or-return).
            return self._get_repo(name)
        if resp.status_code >= 400:
            raise GitProviderError(
                f"create_repo failed ({resp.status_code}): {resp.text}"
            )

        data = resp.json()
        return self._repo_ref_from(data)

    def _get_repo(self, name: str) -> RepoRef:
        try:
            resp = self._client.get(f"/repos/{self._org}/{name}", headers=self._auth_headers())
        except httpx.HTTPError as exc:
            raise GitProviderError(f"get_repo request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise GitProviderError(f"get_repo failed ({resp.status_code}): {resp.text}")
        return self._repo_ref_from(resp.json())

    def _repo_ref_from(self, data: dict) -> RepoRef:
        clone_url = data["clone_url"]
        # Embed the token into the HTTPS clone URL so `git push` (subprocess,
        # no credential helper needed) authenticates non-interactively. The
        # token never appears in logs — only ever passed as part of this
        # in-memory URL string to `git push`'s argv.
        authenticated = clone_url.replace("https://", f"https://x-access-token:{self._token}@", 1)
        return RepoRef(
            full_name=data["full_name"],
            clone_url=authenticated,
            default_branch=data.get("default_branch", "main"),
        )

    def push_branch(self, *, repo: RepoRef, branch: str, local_path: str) -> None:
        try:
            run_git_process(
                ["git", "push", repo.clone_url, f"HEAD:refs/heads/{branch}", "--force-with-lease"],
                cwd=local_path,
                check=True,
                env=git_env(),  # no terminal prompt, no inherited repository selection
            )
        except subprocess.TimeoutExpired as exc:
            # Neither chained nor naming the URL: the exception's argv carries the token.
            raise GitProviderError(
                f"push_branch timed out after {exc.timeout:g}s for branch {branch!r}"
            ) from None
        except subprocess.CalledProcessError as exc:
            # Never echo repo.clone_url (carries the token) into the error -- not in
            # the message, and not through `from exc` either: the exception's argv
            # is the URL, and a logged traceback prints the chain.
            output = exc.stderr or exc.stdout or ""
            for secret in _redactions(repo.clone_url):
                output = output.replace(secret, "<redacted-remote>")
            raise GitProviderError(
                f"push_branch failed for branch {branch!r}: {output.strip()}"
            ) from None

    def open_pr(
        self, *, repo: RepoRef, branch: str, base_branch: str, title: str, body: str
    ) -> PullRequestRef:
        try:
            resp = self._client.post(
                f"/repos/{repo.full_name}/pulls",
                json={"title": title, "body": body, "head": branch, "base": base_branch},
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            raise GitProviderError(f"open_pr request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise GitProviderError(f"open_pr failed ({resp.status_code}): {resp.text}")

        data = resp.json()
        return PullRequestRef(number=data["number"], url=data["html_url"], state=data["state"])

    def resolve_ref(self, *, repo: RepoRef, branch: str) -> str:
        try:
            resp = self._client.get(
                f"/repos/{repo.full_name}/git/refs/heads/{branch}", headers=self._auth_headers()
            )
        except httpx.HTTPError as exc:
            raise GitProviderError(f"resolve_ref request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise GitProviderError(f"resolve_ref failed ({resp.status_code}): {resp.text}")
        return resp.json()["object"]["sha"]

    def get_pr_state(self, *, repo: RepoRef, pr_number: int) -> PullRequestRef:
        """`open`, `merged` or `closed` (unmerged) -- never GitHub's own `state`,
        which says `closed` for a merged PR and an unmerged one alike.
        Merged is read from the PR resource's `merged` / `merged_at`; the merge
        commit is its `merge_commit_sha`."""
        try:
            resp = self._client.get(
                f"/repos/{repo.full_name}/pulls/{pr_number}", headers=self._auth_headers()
            )
        except httpx.HTTPError as exc:
            raise GitProviderError(f"get_pr_state request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise GitProviderError(f"get_pr_state failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        host_state = data.get("state")
        if data.get("merged") is True or data.get("merged_at"):
            state, merge_commit = PR_MERGED, data.get("merge_commit_sha") or None
        elif host_state == "open":
            state, merge_commit = PR_OPEN, None
        elif host_state == "closed":
            state, merge_commit = PR_CLOSED, None
        else:
            raise GitProviderError(
                f"get_pr_state: pull request #{pr_number} has an unrecognised state {host_state!r}"
            )
        return PullRequestRef(
            number=pr_number, url=data.get("html_url") or "", state=state, merge_commit=merge_commit
        )

    def merge_pr(self, *, repo: RepoRef, pr_number: int) -> str:
        try:
            resp = self._client.put(
                f"/repos/{repo.full_name}/pulls/{pr_number}/merge",
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            raise GitProviderError(f"merge_pr request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise GitProviderError(f"merge_pr failed ({resp.status_code}): {resp.text}")
        return resp.json()["sha"]
