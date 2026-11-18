"""Stateless platform session + login-state cookies.

Two signed cookies, no server-side store and **no session table**:

- the **session cookie** (`forge_session`, `__Host-forge_session` in prod) carries the platform user id, an
  issued-at, and an absolute expiry. `role` is deliberately *not* in it — it
  is read from the DB on every request, so a demotion takes effect on the
  user's next request instead of at session expiry (spec: authorization
  state must not be cacheable in a cookie the platform cannot revoke).
- the **login-state cookie** (`forge_login`, `__Host-forge_login` in prod) carries `state` + the PKCE
  `code_verifier` for the few seconds of the IdP redirect, so login-start
  needs no server-side store either.

Both are HMAC-SHA256 signed with `session_signing_secret`, over a payload
that includes a purpose label — a login-state cookie can therefore never be
replayed as a session cookie even though one key signs both.

Accepted limit: a stateless cookie cannot be
invalidated server-side, so logout clears the browser's copy only and a
leaked cookie stays valid until its absolute expiry. A session table is the
fix if server-side revocation is ever required.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from fastapi import Response

from app.config import settings

# Wire names. Everything that reads, writes or clears either cookie goes through
# `session_cookie_name()` / `login_state_cookie_name()` -- never a literal (ADR 0021).
# In prod both take the `__Host-` prefix: a browser
# then refuses to store one that is not `Secure`, has a `Domain`, or is not `Path=/`, and
# a same-site generated service (`{id}.{APPS_BASE_DOMAIN}`) cannot toss a cookie of that
# name at the platform. The bare name sent to a prod instance is simply never read.
_SESSION_COOKIE_BASE = "forge_session"
_LOGIN_STATE_COOKIE_BASE = "forge_login"
_HOST_PREFIX = "__Host-"


def _cookie_name(base: str) -> str:
    return f"{_HOST_PREFIX}{base}" if settings.is_prod else base


def session_cookie_name() -> str:
    return _cookie_name(_SESSION_COOKIE_BASE)


def login_state_cookie_name() -> str:
    return _cookie_name(_LOGIN_STATE_COOKIE_BASE)


# The IdP round-trip is a browser redirect pair; ten minutes is generous for
# a login (incl. an MFA prompt) and short enough that a stolen login cookie
# is worthless.
LOGIN_STATE_TTL_SECONDS = 600

# Don't rewrite the session cookie on literally every request — only once the
# current one is this old. Idle expiry still slides,
# just in coarse steps.
SESSION_REFRESH_AFTER_SECONDS = 300

_SESSION_PURPOSE = "session"
_LOGIN_PURPOSE = "login"


class SessionError(RuntimeError):
    """A session or login-state cookie is missing, malformed, tampered with,
    or expired. Always surfaced as 401 — never a silent downgrade to
    anonymous."""


@dataclass(frozen=True)
class SessionPayload:
    user_id: str
    issued_at: int
    absolute_expires_at: int


@dataclass(frozen=True)
class LoginState:
    state: str
    code_verifier: str
    next_path: str


class SessionSecretMissingError(RuntimeError):
    """The signer was asked to work with no usable `session_signing_secret`.
    `Settings` never hands one over (blank is read as unset, then generated in
    dev or refused in prod); this is the signer's own refusal, so that a
    `Settings` assembled some other way can never fall back to HMAC over an
    empty key -- which anyone can compute, for any user id."""


def _secret() -> bytes:
    secret = settings.session_signing_secret
    if secret is None or not secret.strip():
        raise SessionSecretMissingError(
            "session_signing_secret is empty: refusing to sign or verify a session "
            "cookie with an empty HMAC key."
        )
    return secret.encode()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(purpose: str, encoded_payload: str) -> str:
    return hmac.new(
        _secret(), f"{purpose}:{encoded_payload}".encode(), hashlib.sha256
    ).hexdigest()


def _seal(purpose: str, payload: dict) -> str:
    encoded = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{encoded}.{_sign(purpose, encoded)}"


def _unseal(purpose: str, raw: str | None) -> dict:
    if not raw:
        raise SessionError("cookie missing")
    encoded, _, signature = raw.partition(".")
    if not encoded or not signature:
        raise SessionError("cookie malformed")
    # Bytes, not str: `compare_digest` on a str holding a non-ASCII character raises
    # TypeError, and the cookie is the client's -- that would be a 500, not a refusal.
    if not hmac.compare_digest(signature.encode(), _sign(purpose, encoded).encode()):
        raise SessionError("cookie signature invalid")
    try:
        payload = json.loads(_b64decode(encoded))
    except (ValueError, json.JSONDecodeError) as exc:
        raise SessionError("cookie payload unreadable") from exc
    if not isinstance(payload, dict):
        raise SessionError("cookie payload unreadable")
    return payload


def _now() -> int:
    return int(time.time())


# --- session cookie ---------------------------------------------------------


def issue_session(user_id: str, *, now: int | None = None) -> str:
    now = now if now is not None else _now()
    absolute = now + int(settings.session_absolute_max_days * 86400)
    return _seal(_SESSION_PURPOSE, {"uid": user_id, "iat": now, "exp": absolute})


def refresh_session(payload: SessionPayload, *, now: int | None = None) -> str:
    """Slide the idle window without moving the absolute cap: a new
    issued-at, the *same* `exp` (re-authentication is
    required after the cap regardless of activity)."""
    now = now if now is not None else _now()
    return _seal(
        _SESSION_PURPOSE,
        {"uid": payload.user_id, "iat": now, "exp": payload.absolute_expires_at},
    )


def verify_session(raw: str | None, *, now: int | None = None) -> SessionPayload:
    payload = _unseal(_SESSION_PURPOSE, raw)
    try:
        session = SessionPayload(
            user_id=str(payload["uid"]),
            issued_at=int(payload["iat"]),
            absolute_expires_at=int(payload["exp"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SessionError("session payload incomplete") from exc

    now = now if now is not None else _now()
    if now >= session.absolute_expires_at:
        raise SessionError("session past its absolute expiry")
    if now - session.issued_at >= int(settings.session_idle_timeout_hours * 3600):
        raise SessionError("session idle-expired")
    return session


def should_refresh_session(payload: SessionPayload, *, now: int | None = None) -> bool:
    now = now if now is not None else _now()
    return now - payload.issued_at >= SESSION_REFRESH_AFTER_SECONDS


def set_session_cookie(response: Response, value: str) -> None:
    _set_cookie(
        response,
        session_cookie_name(),
        value,
        max_age=int(settings.session_absolute_max_days * 86400),
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        session_cookie_name(), path="/", httponly=True, samesite="lax", secure=settings.is_prod
    )


# --- login-state cookie -----------------------------------------------------


def issue_login_state(
    *, state: str, code_verifier: str, next_path: str, now: int | None = None
) -> str:
    now = now if now is not None else _now()
    return _seal(
        _LOGIN_PURPOSE,
        {
            "state": state,
            "cv": code_verifier,
            "next": next_path,
            "exp": now + LOGIN_STATE_TTL_SECONDS,
        },
    )


def verify_login_state(raw: str | None, *, now: int | None = None) -> LoginState:
    payload = _unseal(_LOGIN_PURPOSE, raw)
    try:
        login = LoginState(
            state=str(payload["state"]),
            code_verifier=str(payload["cv"]),
            next_path=str(payload["next"]),
        )
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SessionError("login-state payload incomplete") from exc

    now = now if now is not None else _now()
    if now >= expires_at:
        raise SessionError("login-state expired")
    return login


def set_login_state_cookie(response: Response, value: str) -> None:
    _set_cookie(response, login_state_cookie_name(), value, max_age=LOGIN_STATE_TTL_SECONDS)


def clear_login_state_cookie(response: Response) -> None:
    response.delete_cookie(
        login_state_cookie_name(), path="/", httponly=True, samesite="lax", secure=settings.is_prod
    )


def _set_cookie(response: Response, name: str, value: str, *, max_age: int) -> None:
    """httpOnly (never readable by JavaScript — ADR 0018 §2 rejected the
    localStorage shape for exactly this reason), `SameSite=Lax` so the
    post-IdP top-level redirect still carries it, `Path=/`, and `Secure`
    only in prod (dev is plain http://localhost)."""
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.is_prod,
    )
