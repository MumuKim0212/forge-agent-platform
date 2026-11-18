"""Unit tests for `app/services/git_provider.py`'s `create_provider`
factory — the single seam a future host swap touches.
No network, no real token."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services.git_provider import GitProviderConfigError, create_provider
from app.services.github_provider import GitHubProvider


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_create_provider_selects_github_by_default():
    settings = _settings(github_token="tok", github_org="org")
    provider = create_provider(settings)
    assert isinstance(provider, GitHubProvider)


def test_create_provider_raises_when_token_missing():
    settings = _settings(github_org="org")
    with pytest.raises(GitProviderConfigError):
        create_provider(settings)


def test_create_provider_raises_when_org_missing():
    settings = _settings(github_token="tok")
    with pytest.raises(GitProviderConfigError):
        create_provider(settings)


def test_create_provider_raises_on_unknown_provider():
    settings = _settings(git_provider="bitbucket", github_token="tok", github_org="org")
    with pytest.raises(GitProviderConfigError):
        create_provider(settings)


def test_the_three_pull_request_states_are_these_words_and_a_ref_carries_a_merge_commit_only_when_given():
    """`open` / `merged` / `closed` (closed WITHOUT merging) are the whole vocabulary the
    deploy step branches on; the words cross a module boundary, so they are pinned as literals."""
    from app.services.git_provider import PR_CLOSED, PR_MERGED, PR_OPEN, PullRequestRef

    assert (PR_OPEN, PR_MERGED, PR_CLOSED) == ("open", "merged", "closed")
    assert PullRequestRef(number=1, url="u", state="open").merge_commit is None
    assert PullRequestRef(number=1, url="u", state="merged", merge_commit="abc").merge_commit == "abc"
