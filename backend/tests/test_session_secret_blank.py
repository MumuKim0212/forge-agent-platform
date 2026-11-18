"""a session cookie forged with an EMPTY key is refused, end to end, under
the config an operator would actually write (`SESSION_SIGNING_SECRET=` in an env
file).

The defect: pydantic-settings reads a blank value as `""`, not as unset; the old
`is None` check kept it, and `platform_session._secret()` then HMACed with
`b"".encode()` -- a key anyone can compute, so anyone could mint a session
cookie for any user id.

What runs is the real path: `Settings` parsed from a real env file, installed
where `deps.get_current_user` and `platform_session` read it, and a request
through the app's `get_current_user` seam. The forged cookie is built here by
hand, with `hmac` and a literal `b""` key and the wire format's literal purpose
label, so the test does not share its signer with the code it tests. The control
is a cookie signed the same hand-built way with the key the platform actually
generated: it must be accepted, or "refused" would also be what a broken request
looks like.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

import app.deps as deps_module
from app.config import Settings
from app.services import platform_session

SESSION_COOKIE = "forge_session"  # contract string: the cookie's wire name


def _hand_signed_cookie(user_id: str, key: bytes) -> str:
    now = int(time.time())
    payload = {"uid": user_id, "iat": now, "exp": now + 3600}
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        .decode()
        .rstrip("=")
    )
    signature = hmac.new(key, f"session:{encoded}".encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


@pytest.fixture()
def operator_config(monkeypatch, tmp_path):
    """The platform's config as written by an operator who left the session secret
    blank, in `oidc` mode (a session-cookie mode), installed as the process-wide
    settings every identity-seam consumer reads."""
    env_file = tmp_path / "operator.env"
    env_file.write_text(
        "\n".join(
            [
                "FORGE_ENV=dev",
                "AUTH_MODE=oidc",
                "OIDC_ISSUER=https://idp.example.test",
                "OIDC_CLIENT_ID=forge-test",
                "OIDC_CLIENT_SECRET=not-a-real-secret",
                "SESSION_SIGNING_SECRET=",
                "",
            ]
        ),
        encoding="utf-8",
    )
    cfg = Settings(_env_file=env_file)
    monkeypatch.setattr(deps_module, "settings", cfg)
    monkeypatch.setattr(platform_session, "settings", cfg)
    return cfg


def test_a_cookie_forged_with_an_empty_key_is_refused_end_to_end(client, seeded_ids, operator_config):
    user_id = seeded_ids["user_id"]
    assert operator_config.session_auth_enabled  # the request really goes down the cookie path

    forged = _hand_signed_cookie(user_id, b"")
    client.cookies.set(SESSION_COOKIE, forged)
    response = client.get("/api/users/me")

    assert response.status_code == 401
    assert response.json()["detail"].startswith("Invalid session")


def test_the_control_a_cookie_signed_with_the_generated_key_is_accepted(client, seeded_ids, operator_config):
    """Same request, same user, same hand-built format -- only the key differs. Without
    this the 401 above could be a typo in the URL, the mode, or the cookie format."""
    generated = operator_config.session_signing_secret
    assert generated and generated.strip()

    good = _hand_signed_cookie(seeded_ids["user_id"], generated.encode())
    client.cookies.set(SESSION_COOKIE, good)
    response = client.get("/api/users/me")

    assert response.status_code == 200
    assert response.json()["id"] == seeded_ids["user_id"]


def test_the_operators_blank_value_became_a_generated_key_not_an_empty_one(operator_config):
    assert operator_config.session_signing_secret not in (None, "")
    assert operator_config.session_signing_secret.strip()
