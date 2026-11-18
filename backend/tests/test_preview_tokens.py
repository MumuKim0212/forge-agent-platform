"""Unit tests for the signed preview tokens
"signed, expiring preview URL (token bound to the
build, lifetime <= TTL)"). No Docker, no network — pure token mechanics.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.preview_manager import (
    PreviewAccessError,
    PreviewSigningSecretMissingError,
    hash_token,
    issue_preview_token,
    verify_preview_token,
)

BUILD_ID = "build-token-test"


def _issued(ttl_minutes: int = 60) -> tuple[str, str]:
    token, _expires_at = issue_preview_token(BUILD_ID, ttl_minutes=ttl_minutes)
    return token, hash_token(token)


def test_valid_token_verifies():
    token, token_hash = _issued()
    verify_preview_token(BUILD_ID, token, expected_hash=token_hash)  # no raise


def test_expired_token_rejected():
    token, token_hash = _issued(ttl_minutes=-1)  # already expired
    with pytest.raises(PreviewAccessError, match="expired"):
        verify_preview_token(BUILD_ID, token, expected_hash=token_hash)


def test_token_bound_to_build_id():
    """A token minted for one build must not verify for another build id —
    the signature covers the build id (token bound to the build)."""
    token, token_hash = _issued()
    with pytest.raises(PreviewAccessError, match="signature"):
        verify_preview_token("some-other-build", token, expected_hash=token_hash)


def test_tampered_token_rejected():
    token, token_hash = _issued()
    nonce, expires_ts, signature = token.split(".")
    tampered = f"{nonce}.{int(expires_ts) + 99999}.{signature}"  # extend expiry
    with pytest.raises(PreviewAccessError):
        verify_preview_token(BUILD_ID, tampered, expected_hash=token_hash)


def test_malformed_token_rejected():
    _token, token_hash = _issued()
    for bad in ("", "not-a-token", "a.b", "a.b.c.d", "a.not-an-int.c"):
        with pytest.raises(PreviewAccessError):
            verify_preview_token(BUILD_ID, bad, expected_hash=token_hash)


@pytest.mark.parametrize("signature", ["é", "€" * 32, "abéd"])
def test_a_non_ascii_signature_is_an_access_error_not_a_type_error(signature):
    """`hmac.compare_digest` on two strs raises TypeError for a non-ASCII one, and the token is the
    client's -- the preview route turned that into a 500 with no isolation headers."""
    _token, token_hash = _issued()
    nonce, expires_ts, _signature = _token.split(".")
    with pytest.raises(PreviewAccessError, match="signature"):
        verify_preview_token(BUILD_ID, f"{nonce}.{expires_ts}.{signature}", expected_hash=token_hash)


def test_a_non_ascii_stored_hash_is_an_access_error_not_a_type_error():
    """The second compare: reachable with a valid token only when the stored hash is not ASCII (a
    damaged row), and it must still refuse rather than raise."""
    token, _hash = _issued()
    with pytest.raises(PreviewAccessError, match="does not match"):
        verify_preview_token(BUILD_ID, token, expected_hash="é" * 64)


def test_superseded_token_rejected():
    """A structurally valid, unexpired token that doesn't match the build's
    *current* stored hash (e.g. the preview was replaced by a newer build's
    and re-issued) is rejected."""
    old_token, _old_hash = _issued()
    _new_token, new_hash = _issued()
    with pytest.raises(PreviewAccessError, match="does not match"):
        verify_preview_token(BUILD_ID, old_token, expected_hash=new_hash)


def test_no_live_preview_rejected():
    token, _hash = _issued()
    with pytest.raises(PreviewAccessError, match="no live preview"):
        verify_preview_token(BUILD_ID, token, expected_hash=None)


def test_hash_is_not_the_raw_token():
    """Only the hash is ever persisted (Build.preview_token_hash) — it must
    not be usable as (or reveal) the token itself."""
    token, token_hash = _issued()
    assert token_hash != token
    assert token not in token_hash
    with pytest.raises(PreviewAccessError):
        verify_preview_token(BUILD_ID, token_hash, expected_hash=token_hash)


def test_expiry_respects_ttl_setting(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.config import settings

    monkeypatch.setattr(settings, "preview_ttl_minutes", 5)
    _token, expires_at = issue_preview_token(BUILD_ID)
    delta = expires_at - datetime.now(timezone.utc)
    assert timedelta(minutes=4) < delta <= timedelta(minutes=5, seconds=5)


@pytest.mark.parametrize("empty", [None, "", "   "], ids=["none", "empty", "spaces"])
def test_the_signer_refuses_to_work_with_an_empty_key(monkeypatch, empty):
    """defence in depth behind `Settings` (which reads a blank as unset). A
    settings object that reaches the signer with no usable secret must raise, never
    HMAC over `b""` -- a key anyone can compute. It is a configuration fault, so it is
    NOT a `PreviewAccessError` (callers turn that into a quiet 403)."""
    token, token_hash = _issued()  # minted while the secret is fine
    monkeypatch.setattr(settings, "preview_signing_secret", empty)
    with pytest.raises(PreviewSigningSecretMissingError):
        issue_preview_token(BUILD_ID)
    with pytest.raises(PreviewSigningSecretMissingError):
        verify_preview_token(BUILD_ID, token, expected_hash=token_hash)
    assert not issubclass(PreviewSigningSecretMissingError, PreviewAccessError)
