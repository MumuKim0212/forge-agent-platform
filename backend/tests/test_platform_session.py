"""Unit tests for the stateless session / login-state cookies.
No DB, no
HTTP, no IdP — pure sign/verify behaviour.
"""

from __future__ import annotations

import base64
import json

import pytest

from app.config import settings
from app.services import platform_session as ps

SECRET = "test-session-secret-not-real"


@pytest.fixture(autouse=True)
def fixed_secret(monkeypatch):
    monkeypatch.setattr(settings, "session_signing_secret", SECRET)


def _payload_of(cookie: str) -> dict:
    encoded = cookie.partition(".")[0]
    return json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))


def test_session_round_trip_carries_user_id_iat_and_absolute_expiry():
    cookie = ps.issue_session("user-1", now=1_000)
    session = ps.verify_session(cookie, now=1_000)
    assert session.user_id == "user-1"
    assert session.issued_at == 1_000
    assert session.absolute_expires_at == 1_000 + 7 * 86400


def test_session_payload_never_carries_the_role():
    """`role` is read from the DB on every
    request, so a demotion cannot survive in a cookie the platform cannot
    revoke."""
    payload = _payload_of(ps.issue_session("user-1", now=1_000))
    assert set(payload) == {"uid", "iat", "exp"}
    assert "role" not in payload


@pytest.mark.parametrize("raw", ["payload.éé", "payload.€", "é.signature"])
def test_a_non_ascii_cookie_is_a_session_error_not_a_type_error(raw):
    """`hmac.compare_digest` on two strs raises TypeError for a non-ASCII one, and the cookie is the
    client's. It must be the refusal every other bad cookie gets."""
    with pytest.raises(ps.SessionError):
        ps.verify_session(raw)
    with pytest.raises(ps.SessionError):
        ps.verify_login_state(raw)


def test_missing_cookie_is_rejected():
    with pytest.raises(ps.SessionError):
        ps.verify_session(None)


@pytest.mark.parametrize(
    "mangle",
    [
        lambda c: c[:-1] + ("0" if c[-1] != "0" else "1"),  # flipped signature
        lambda c: "garbage",  # no separator
        lambda c: c.partition(".")[0],  # payload without a signature
        lambda c: "." + c.partition(".")[2],  # signature without a payload
    ],
    ids=["signature-flipped", "no-separator", "payload-only", "signature-only"],
)
def test_tampered_cookie_is_rejected(mangle):
    cookie = ps.issue_session("user-1", now=1_000)
    tampered = mangle(cookie)
    assert tampered != cookie  # a no-op "tamper" would assert nothing
    with pytest.raises(ps.SessionError):
        ps.verify_session(tampered, now=1_000)


def test_forged_payload_without_the_key_is_rejected():
    """Re-encoding the payload with a different user id invalidates the
    signature — a session cookie is not self-asserting."""
    cookie = ps.issue_session("user-1", now=1_000)
    forged_payload = base64.urlsafe_b64encode(
        json.dumps({"uid": "user-2", "iat": 1_000, "exp": 9_999_999}).encode()
    ).decode().rstrip("=")
    with pytest.raises(ps.SessionError):
        ps.verify_session(f"{forged_payload}.{cookie.partition('.')[2]}", now=1_000)


def test_idle_expiry_rejects_after_eight_hours():
    cookie = ps.issue_session("user-1", now=1_000)
    assert ps.verify_session(cookie, now=1_000 + 8 * 3600 - 1)
    with pytest.raises(ps.SessionError):
        ps.verify_session(cookie, now=1_000 + 8 * 3600)


def test_absolute_expiry_rejects_after_seven_days_despite_sliding():
    """The idle window slides on activity; the absolute cap does not move,
    so re-authentication is eventually forced regardless of activity."""
    cookie = ps.issue_session("user-1", now=1_000)
    now = 1_000
    for _ in range(27):  # 27 * 6h = 162h of activity, still inside the 7-day cap
        now += 6 * 3600
        session = ps.verify_session(cookie, now=now)
        cookie = ps.refresh_session(session, now=now)
        assert _payload_of(cookie)["exp"] == 1_000 + 7 * 86400
    with pytest.raises(ps.SessionError):
        ps.verify_session(cookie, now=1_000 + 7 * 86400)


def test_idle_timeout_is_configurable(monkeypatch):
    monkeypatch.setattr(settings, "session_idle_timeout_hours", 1.0)
    cookie = ps.issue_session("user-1", now=0)
    assert ps.verify_session(cookie, now=3_599)
    with pytest.raises(ps.SessionError):
        ps.verify_session(cookie, now=3_600)


def test_absolute_cap_is_configurable(monkeypatch):
    monkeypatch.setattr(settings, "session_absolute_max_days", 1.0)
    assert _payload_of(ps.issue_session("user-1", now=0))["exp"] == 86400


def test_should_refresh_only_once_the_cookie_is_stale():
    session = ps.verify_session(ps.issue_session("user-1", now=1_000), now=1_000)
    assert ps.should_refresh_session(session, now=1_000) is False
    assert ps.should_refresh_session(session, now=1_000 + ps.SESSION_REFRESH_AFTER_SECONDS) is True


def test_login_state_round_trip():
    cookie = ps.issue_login_state(
        state="st", code_verifier="cv", next_path="/projects", now=1_000
    )
    login = ps.verify_login_state(cookie, now=1_000)
    assert (login.state, login.code_verifier, login.next_path) == ("st", "cv", "/projects")


def test_login_state_expires():
    cookie = ps.issue_login_state(state="st", code_verifier="cv", next_path="/", now=1_000)
    with pytest.raises(ps.SessionError):
        ps.verify_login_state(cookie, now=1_000 + ps.LOGIN_STATE_TTL_SECONDS)


def test_login_and_session_cookies_are_not_interchangeable():
    """One key signs both, so the purpose label is what stops a login-state
    cookie being replayed as a session (and vice versa)."""
    session_cookie = ps.issue_session("user-1", now=1_000)
    login_cookie = ps.issue_login_state(state="s", code_verifier="c", next_path="/", now=1_000)
    with pytest.raises(ps.SessionError):
        ps.verify_login_state(session_cookie, now=1_000)
    with pytest.raises(ps.SessionError):
        ps.verify_session(login_cookie, now=1_000)


def test_a_different_secret_invalidates_existing_cookies(monkeypatch):
    cookie = ps.issue_session("user-1", now=1_000)
    monkeypatch.setattr(settings, "session_signing_secret", "some-other-secret")
    with pytest.raises(ps.SessionError):
        ps.verify_session(cookie, now=1_000)


def test_cookie_flags_are_httponly_lax_and_secure_only_in_prod(monkeypatch):
    from fastapi import Response

    response = Response()
    ps.set_session_cookie(response, "value")
    header = response.headers["set-cookie"]
    assert "HttpOnly" in header
    assert "SameSite=lax" in header
    assert "Path=/" in header
    assert "Secure" not in header  # dev is plain http://localhost

    monkeypatch.setattr(settings, "forge_env", "prod")
    prod_response = Response()
    ps.set_session_cookie(prod_response, "value")
    assert "Secure" in prod_response.headers["set-cookie"]


@pytest.mark.parametrize("empty", [None, "", "   "], ids=["none", "empty", "spaces"])
def test_the_signer_refuses_to_work_with_an_empty_key(monkeypatch, empty):
    """defence in depth behind `Settings` (which reads a blank as unset). If a
    settings object ever reaches the signer with no usable secret, it must raise, not
    fall back to HMAC over `b""` -- a key anyone can compute. Both directions: it
    neither mints nor accepts a cookie."""
    good_cookie = ps.issue_session("user-1", now=1_000)  # signed while the secret is fine
    monkeypatch.setattr(settings, "session_signing_secret", empty)
    with pytest.raises(ps.SessionSecretMissingError):
        ps.issue_session("user-1", now=1_000)
    with pytest.raises(ps.SessionSecretMissingError):
        ps.issue_login_state(state="s", code_verifier="c", next_path="/", now=1_000)
    with pytest.raises(ps.SessionSecretMissingError):
        ps.verify_session(good_cookie, now=1_000)
