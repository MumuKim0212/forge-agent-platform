"""OIDC authentication end-to-end (ADR 0018).

Runs against a **stub IdP** served by `httpx.MockTransport` (the same
technique `test_github_provider.py` uses for GitHub) plus a locally generated
RSA key, so discovery / JWKS / code exchange / ID-token signatures are all
real protocol mechanics with no network, no Docker, and no actual identity
provider. `oidc._http_client` is the injection seam.

Both modes are exercised: dev-mode behaviour must be observably unchanged,
and OIDC mode must resolve identity through the same single
`deps.get_current_user` seam.
"""

from __future__ import annotations

import json
import time
import urllib.parse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import settings
from app.models.user import User
from app.routers.auth import _safe_next_path
from app.services import oidc, platform_session

ISSUER = "https://idp.example.test"
CLIENT_ID = "forge-platform-test"
CLIENT_SECRET = "not-a-real-client-secret"  # noqa: S105 - test fixture
KID = "test-signing-key"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/oauth2/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/oauth2/token"
JWKS_URI = f"{ISSUER}/oauth2/jwks"


# --- stub IdP ---------------------------------------------------------------


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def other_key() -> rsa.RSAPrivateKey:
    """A key the IdP never published — used to forge a bad signature."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks_for(key: rsa.RSAPrivateKey, *, kid: str = KID) -> dict:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}


# `email_verified` has three states that must stay distinguishable -- absent, explicitly
# null, and a value -- so "not passed" needs a sentinel that is none of them.
ABSENT = object()


def _id_token(
    key: rsa.RSAPrivateKey,
    *,
    sub: str = "idp-sub-1",
    email: str | None = "ada@example.com",
    name: str | None = "Ada Lovelace",
    audience: str = CLIENT_ID,
    issuer: str = ISSUER,
    expires_in: int = 3600,
    issued_at: int | None = None,
    kid: str = KID,
    email_verified=ABSENT,
) -> str:
    now = issued_at if issued_at is not None else int(time.time())
    claims = {"iss": issuer, "sub": sub, "aud": audience, "iat": now, "exp": now + expires_in}
    if email is not None:
        claims["email"] = email
    if name is not None:
        claims["name"] = name
    if email_verified is not ABSENT:
        claims["email_verified"] = email_verified
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


class StubIdp:
    """Serves discovery, JWKS and the token endpoint. `id_token` is whatever
    the test wants the IdP to hand back for the code exchange."""

    def __init__(self, jwks: dict, id_token: str | None = None):
        self.jwks = jwks
        self.id_token = id_token
        self.discovery_requests = 0
        self.token_requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{ISSUER}/.well-known/openid-configuration":
            self.discovery_requests += 1
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": AUTHORIZATION_ENDPOINT,
                    "token_endpoint": TOKEN_ENDPOINT,
                    "jwks_uri": JWKS_URI,
                    "id_token_signing_alg_values_supported": ["RS256"],
                    "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                },
            )
        if url == JWKS_URI:
            return httpx.Response(200, json=self.jwks)
        if url == TOKEN_ENDPOINT:
            self.token_requests.append(dict(urllib.parse.parse_qsl(request.content.decode())))
            if self.id_token is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": "stub-access-token-never-stored",
                    "token_type": "Bearer",
                    "id_token": self.id_token,
                },
            )
        raise AssertionError(f"stub IdP got an unexpected request: {url}")


@pytest.fixture()
def oidc_mode(monkeypatch):
    """Flip the process-wide capability to OIDC (same monkeypatch idiom as
    `test_pipeline_flip.py`) and reset the process-lifetime discovery cache
    around every test."""
    monkeypatch.setattr(settings, "auth_mode", "oidc")
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(settings, "oidc_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "oidc_client_secret", CLIENT_SECRET)
    monkeypatch.setattr(settings, "session_signing_secret", "test-session-secret")
    monkeypatch.setattr(settings, "bootstrap_admin_emails", "")
    oidc.reset_caches()
    yield
    oidc.reset_caches()


@pytest.fixture()
def idp(monkeypatch, signing_key):
    stub = StubIdp(_jwks_for(signing_key), _id_token(signing_key))
    monkeypatch.setattr(
        oidc, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(stub.handler))
    )
    return stub


# --- helpers ----------------------------------------------------------------


def _start_login(client, next_path: str | None = None) -> str:
    """Drive login-start and return the `state` the platform generated."""
    url = "/api/auth/login" + (f"?next={next_path}" if next_path else "")
    response = client.get(url, follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(AUTHORIZATION_ENDPOINT)
    return urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["state"][0]


def _complete_login(client, follow_redirects: bool = False):
    state = _start_login(client)
    return client.get(
        f"/api/auth/callback?code=stub-auth-code&state={state}",
        follow_redirects=follow_redirects,
    )


# --- dev mode is unchanged --------------------------------------------------


def test_dev_mode_header_still_resolves_identity(client, seeded_ids):
    """With AUTH_MODE=dev nothing observable
    changes — this is the behaviour from before OIDC authentication, asserted here explicitly."""
    response = client.get("/api/users/me", headers={"X-Forge-User-Id": seeded_ids["user_id"]})
    assert response.status_code == 200
    assert response.json()["id"] == seeded_ids["user_id"]

    assert client.get("/api/users/me").status_code == 401
    assert client.get("/api/users/me", headers={"X-Forge-User-Id": "nope"}).status_code == 401


def test_dev_mode_has_no_login_endpoint(client):
    assert client.get("/api/auth/login", follow_redirects=False).status_code == 404


def test_oidc_mode_ignores_the_dev_header(client, seeded_ids, oidc_mode, idp):
    """The header stand-in must not remain a second way in once OIDC is on."""
    response = client.get("/api/users/me", headers={"X-Forge-User-Id": seeded_ids["user_id"]})
    assert response.status_code == 401


def test_oidc_mode_without_a_session_is_401(client, oidc_mode, idp):
    assert client.get("/api/users/me").status_code == 401


# --- login flow -------------------------------------------------------------


def test_login_start_redirects_to_the_discovered_idp_with_pkce(client, oidc_mode, idp):
    response = client.get("/api/auth/login", follow_redirects=False)
    assert response.status_code == 302
    query = urllib.parse.parse_qs(urllib.parse.urlparse(response.headers["location"]).query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] and query["state"]
    assert query["redirect_uri"] == ["http://testserver/api/auth/callback"]
    assert query["scope"] == ["openid email profile"]
    # state + verifier travel in a short-lived signed cookie, not a store.
    assert platform_session.login_state_cookie_name() in response.cookies


def test_full_login_provisions_the_user_and_sets_a_session(client, db_session, oidc_mode, idp):
    response = _complete_login(client)
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert platform_session.session_cookie_name() in response.cookies

    user = db_session.query(User).filter_by(oidc_sub="idp-sub-1").one()
    assert (user.email, user.name, user.role) == ("ada@example.com", "Ada Lovelace", "requester")

    me = client.get("/api/users/me")
    assert me.status_code == 200
    assert me.json()["id"] == user.id

    # The code exchange carried the PKCE verifier and never leaked to the
    # browser; no access/refresh token was stored anywhere.
    assert idp.token_requests[0]["code_verifier"]
    assert idp.token_requests[0]["grant_type"] == "authorization_code"


def test_login_preserves_the_intended_destination(client, oidc_mode, idp):
    response = _complete_login_with_next(client, "/projects/abc")
    assert response.headers["location"] == "/projects/abc"


def _complete_login_with_next(client, next_path: str):
    state = _start_login(client, next_path)
    return client.get(f"/api/auth/callback?code=c&state={state}", follow_redirects=False)


@pytest.mark.parametrize("hostile", ["https://evil.test/steal", "//evil.test/steal"])
def test_login_next_cannot_become_an_open_redirect(client, oidc_mode, idp, hostile):
    response = _complete_login_with_next(client, urllib.parse.quote(hostile, safe=""))
    assert response.headers["location"] == "/"


def test_login_next_control_character_cannot_become_an_open_redirect(client, oidc_mode, idp):
    """a real
    login-start -> callback round trip with a control character (tab) in
    `next`. Sent pre-percent-encoded (`%09`) since a raw tab byte cannot sit
    in an HTTP request line at all; the server decodes the query parameter
    back to a real tab before `_safe_next_path` ever sees it, exactly what
    `useSearchParams` hands the frontend copy.

    This assertion does **not** rely on Starlette's `RedirectResponse`
    percent-encoding tab/backslash into the `Location` header (the library
    property that masks today's bug even without this task's fix, per the
    task's Established facts) — it still discriminates because it asserts
    *equality to the default landing page*, not merely "stayed same-origin".
    Without the control-character rule, `next_path` would be the raw
    `"/\t/evil.test"`, Starlette would percent-encode it to
    `"/%09/evil.test"` in the `Location` header, and `"/%09/evil.test" ==
    "/"` is false — so this test fails regardless of what the percent-
    encoding property does or doesn't protect against."""
    response = _complete_login_with_next(client, "/%09/evil.test")
    assert response.headers["location"] == "/"


# --- `_safe_next_path` itself


# Mirrors `frontend/src/pages/LoginPage.test.ts`'s case list side by side
# (see this task's Result): tab, LF, CR, NUL, DEL, and one more C0 control
# character (ESC), so the rule is proven to be "any control character", not
# a hardcoded set of three.
_CONTROL_CHARACTERS = ["\t", "\n", "\r", "\x00", "\x7f", "\x1b"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # empty
        (None, "/"),
        ("", "/"),
        # pass-through, unchanged — including a query string
        ("/projects/abc", "/projects/abc"),
        ("/approvals?status=PENDING&tab=2", "/approvals?status=PENDING&tab=2"),
        # pinned to the leading-`/` rule alone: no `//`, no backslash, no
        # control character
        ("https://evil.test", "/"),
        # pinned to the `//` rule alone: starts with `/`, no backslash, no
        # control character
        ("//evil.test/steal", "/"),
        # pinned to the backslash rule alone: starts with `/`, not `//`, no
        # control character
        ("/\\evil.test", "/"),
        # pinned to the control-character rule alone: starts with `/`, not
        # `//`, no backslash — one case per character in _CONTROL_CHARACTERS
        *[(f"/{ch}/evil.test", "/") for ch in _CONTROL_CHARACTERS],
    ],
)
def test_safe_next_path_rules(raw, expected):
    assert _safe_next_path(raw) == expected


def test_discovery_is_fetched_once_and_cached(client, oidc_mode, idp):
    _complete_login(client)
    client.cookies.clear()
    _complete_login(client)
    assert idp.discovery_requests == 1


def test_jwks_is_refetched_when_the_idp_rotates_its_signing_key(
    client, oidc_mode, idp, signing_key, other_key
):
    """A process-lifetime JWKS cache alone would reject every login after a
    key rotation until a restart, so an unknown `kid` triggers one refetch.
    (Refetching on a *signature* failure instead would let any forged token
    force an outbound request, so an unknown `kid` is the only trigger.)"""
    _complete_login(client)  # warms the JWKS cache with `signing_key`

    idp.jwks = _jwks_for(other_key, kid="rotated-key")
    idp.id_token = _id_token(
        other_key, sub="idp-sub-rotated", email="rot@example.com", kid="rotated-key"
    )
    client.cookies.clear()
    assert _complete_login(client).status_code == 302


def test_logout_clears_the_session_cookie(client, oidc_mode, idp):
    _complete_login(client)
    assert client.get("/api/users/me").status_code == 200

    response = client.post("/api/auth/logout")
    assert response.status_code == 204
    assert client.get("/api/users/me").status_code == 401


# --- callback rejections ----------------------------------------------------


def test_callback_rejects_a_state_mismatch(client, oidc_mode, idp):
    _start_login(client)
    response = client.get("/api/auth/callback?code=c&state=not-the-state")
    assert response.status_code == 401


def test_callback_rejects_a_non_ascii_state_as_a_401_not_a_500(client, oidc_mode, idp):
    # `hmac.compare_digest` on two strs raises TypeError for a non-ASCII one; `state` is the
    # client's, so that was a 500. It must be the same 401 as any other mismatch.
    _start_login(client)
    response = client.get("/api/auth/callback?code=c&state=%C3%A9%E2%82%AC")
    assert response.status_code == 401


def test_a_non_ascii_session_cookie_is_401_not_a_500(client, oidc_mode, idp):
    # The cookie arrives as latin-1 bytes (Starlette decodes the header that way), so send one raw.
    response = client.get(
        "/api/users/me",
        headers={b"cookie": platform_session.session_cookie_name().encode() + b"=payload." + bytes([0xE9, 0xE9])},
    )
    assert response.status_code == 401


def test_callback_rejects_a_missing_login_state_cookie(client, oidc_mode, idp):
    state = _start_login(client)
    client.cookies.clear()
    assert client.get(f"/api/auth/callback?code=c&state={state}").status_code == 401


def test_callback_rejects_an_idp_error_response(client, oidc_mode, idp):
    _start_login(client)
    assert client.get("/api/auth/callback?error=access_denied").status_code == 401


def test_callback_rejects_a_failed_code_exchange(client, oidc_mode, idp):
    idp.id_token = None  # token endpoint answers 400 invalid_grant
    assert _complete_login(client).status_code == 401


def test_callback_rejects_an_id_token_signed_by_an_unpublished_key(
    client, db_session, oidc_mode, idp, other_key
):
    idp.id_token = _id_token(other_key)
    assert _complete_login(client).status_code == 401
    assert db_session.query(User).filter_by(oidc_sub="idp-sub-1").first() is None


@pytest.mark.parametrize(
    ("kwargs", "case"),
    [
        ({"audience": "some-other-client"}, "wrong-aud"),
        ({"issuer": "https://attacker.example"}, "wrong-iss"),
        ({"expires_in": -60}, "expired"),
        ({"sub": ""}, "no-sub"),
    ],
)
def test_callback_rejects_invalid_id_token_claims(
    client, db_session, oidc_mode, idp, signing_key, kwargs, case
):
    idp.id_token = _id_token(signing_key, **kwargs)
    assert _complete_login(client).status_code == 401
    assert db_session.query(User).count() == 3  # only the dev-seeded rows


def test_callback_rejects_an_id_token_without_an_email_claim(
    client, db_session, oidc_mode, idp, signing_key
):
    idp.id_token = _id_token(signing_key, email=None)
    assert _complete_login(client).status_code == 401
    assert db_session.query(User).filter_by(oidc_sub="idp-sub-1").first() is None


def test_a_rejected_login_never_echoes_the_token(client, oidc_mode, idp, signing_key, caplog):
    """Log the reason, never the token — and the
    error body says nothing either."""
    token = _id_token(signing_key, audience="some-other-client")
    idp.id_token = token
    with caplog.at_level("WARNING"):
        response = _complete_login(client)
    assert response.status_code == 401
    assert response.json()["detail"] == "login failed"
    assert token not in response.text
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in logged
    assert CLIENT_SECRET not in logged
    assert "rejected" in logged  # the reason itself IS logged


# --- session handling -------------------------------------------------------


def test_a_tampered_session_cookie_is_401(client, oidc_mode, idp):
    _complete_login(client)
    good = client.cookies[platform_session.session_cookie_name()]
    # The replacement byte must differ from the one it replaces — a fixed
    # "0" silently leaves the cookie untouched whenever the hexdigest already
    # ends in "0" (~6% of runs), which both flakes and asserts nothing. Same
    # guard as tests/test_platform_session.py's "signature-flipped" case.
    tampered = good[:-1] + ("0" if good[-1] != "0" else "1")
    assert tampered != good
    client.cookies.set(platform_session.session_cookie_name(), tampered)
    assert client.get("/api/users/me").status_code == 401


def test_an_expired_session_cookie_is_401(client, oidc_mode, idp, monkeypatch):
    _complete_login(client)
    assert client.get("/api/users/me").status_code == 200
    # Idle expiry is what a returning-tomorrow browser hits.
    monkeypatch.setattr(settings, "session_idle_timeout_hours", 0.0)
    assert client.get("/api/users/me").status_code == 401


def test_a_session_for_a_deleted_user_is_401(client, db_session, oidc_mode, idp):
    _complete_login(client)
    db_session.delete(db_session.query(User).filter_by(oidc_sub="idp-sub-1").one())
    db_session.commit()
    assert client.get("/api/users/me").status_code == 401


def test_idle_expiry_slides_on_activity(client, oidc_mode, idp, monkeypatch):
    """The seam re-issues the cookie once the current one is stale, which is
    what makes the 8h idle window sliding rather than fixed. The absolute cap
    is deliberately not moved."""
    _complete_login(client)
    issued = client.cookies[platform_session.session_cookie_name()]

    # No refresh while the cookie is fresh...
    assert platform_session.session_cookie_name() not in client.get("/api/users/me").cookies
    # ...and a re-issued cookie once it is not.
    monkeypatch.setattr(platform_session, "SESSION_REFRESH_AFTER_SECONDS", 0)
    response = client.get("/api/users/me")
    assert response.status_code == 200
    refreshed = response.cookies[platform_session.session_cookie_name()]

    before = platform_session.verify_session(issued)
    after = platform_session.verify_session(refreshed)
    assert after.issued_at >= before.issued_at
    # The absolute cap does not move with activity.
    assert after.absolute_expires_at == before.absolute_expires_at


def test_role_is_read_from_the_db_on_every_request(client, db_session, oidc_mode, idp):
    """`role` is never carried in the session,
    so a promotion/demotion applies from the user's next request on — without
    re-issuing or expiring the cookie."""
    _complete_login(client)
    assert client.get("/api/users/me").json()["role"] == "requester"

    user = db_session.query(User).filter_by(oidc_sub="idp-sub-1").one()
    user.role = "admin"
    db_session.commit()
    assert client.get("/api/users/me").json()["role"] == "admin"


# --- JIT provisioning -------------------------------------------------------


def test_second_login_refreshes_name_and_email_by_sub_not_by_email(
    client, db_session, oidc_mode, idp, signing_key
):
    _complete_login(client)
    user_id = db_session.query(User).filter_by(oidc_sub="idp-sub-1").one().id

    client.cookies.clear()
    idp.id_token = _id_token(signing_key, email="ada.new@example.com", name="Ada L.")
    assert _complete_login(client).status_code == 302

    db_session.expire_all()
    rows = db_session.query(User).filter_by(oidc_sub="idp-sub-1").all()
    assert len(rows) == 1  # a changed email updates the row, never adds one
    assert rows[0].id == user_id
    assert (rows[0].email, rows[0].name) == ("ada.new@example.com", "Ada L.")


def test_a_second_sub_creates_a_second_user(client, db_session, oidc_mode, idp, signing_key):
    _complete_login(client)
    client.cookies.clear()
    idp.id_token = _id_token(signing_key, sub="idp-sub-2", email="bob@example.com", name="Bob")
    assert _complete_login(client).status_code == 302
    assert db_session.query(User).filter(User.oidc_sub.isnot(None)).count() == 2


def test_email_colliding_with_a_different_sub_fails_the_login(
    client, db_session, oidc_mode, idp, signing_key, caplog
):
    """The login fails with a logged conflict — the
    row is neither reassigned nor duplicated."""
    _complete_login(client)
    original_id = db_session.query(User).filter_by(oidc_sub="idp-sub-1").one().id

    client.cookies.clear()
    idp.id_token = _id_token(signing_key, sub="idp-sub-2", email="ada@example.com", name="Not Ada")
    with caplog.at_level("WARNING"):
        assert _complete_login(client).status_code == 401

    db_session.expire_all()
    assert db_session.query(User).filter_by(email="ada@example.com").one().id == original_id
    assert db_session.query(User).filter_by(oidc_sub="idp-sub-2").first() is None
    assert db_session.query(User).filter_by(name="Not Ada").first() is None
    assert "conflict" in "\n".join(r.getMessage() for r in caplog.records)


def test_email_colliding_with_a_dev_seeded_row_fails_rather_than_adopting_it(
    client, db_session, oidc_mode, idp, signing_key
):
    """ADR 0018 §7: dev identities are discarded, not mapped — a seeded row
    with a matching email has a *different* (absent) sub, so it is a
    conflict, not a row to take over."""
    seeded = db_session.query(User).filter_by(role="approver").one()
    idp.id_token = _id_token(signing_key, sub="idp-sub-9", email=seeded.email)
    assert _complete_login(client).status_code == 401

    db_session.expire_all()
    assert db_session.query(User).filter_by(id=seeded.id).one().oidc_sub is None


# --- bootstrap admin --------------------------------------------------------


def test_bootstrap_admin_email_grants_admin_at_row_creation(
    client, db_session, oidc_mode, idp, monkeypatch
):
    monkeypatch.setattr(settings, "bootstrap_admin_emails", "ADA@example.com, other@x.io")
    _complete_login(client)
    assert db_session.query(User).filter_by(oidc_sub="idp-sub-1").one().role == "admin"


def test_bootstrap_admin_never_re_promotes_a_demoted_admin(
    client, db_session, oidc_mode, idp, monkeypatch
):
    """ADR 0018 §5: the grant applies only at row creation. Re-checking every
    login would silently undo a deliberate demotion."""
    monkeypatch.setattr(settings, "bootstrap_admin_emails", "ada@example.com")
    _complete_login(client)
    user = db_session.query(User).filter_by(oidc_sub="idp-sub-1").one()
    assert user.role == "admin"

    user.role = "requester"
    db_session.commit()

    client.cookies.clear()
    assert _complete_login(client).status_code == 302
    db_session.expire_all()
    assert db_session.query(User).filter_by(oidc_sub="idp-sub-1").one().role == "requester"


def test_a_non_listed_email_gets_the_requester_role(client, db_session, oidc_mode, idp, monkeypatch):
    monkeypatch.setattr(settings, "bootstrap_admin_emails", "someone.else@example.com")
    _complete_login(client)
    assert db_session.query(User).filter_by(oidc_sub="idp-sub-1").one().role == "requester"


# --- email_verified --


@pytest.mark.parametrize(
    "claim",
    [False, "false", "False", "FALSE", 0, "no", ""],
    ids=["false", "str-false", "str-False", "str-FALSE", "zero", "no", "empty-string"],
)
def test_an_unverified_email_is_refused_and_no_row_is_created(
    client, db_session, oidc_mode, idp, signing_key, claim, caplog
):
    """The attack: an IdP that lets a user self-register an unverified address and log in before
    its real owner. Refused through the ordinary login-failure path (401 `login failed`), with a
    logged reason that names no address, and nothing written. A claim that is present but in a
    shape that does not read as "yes" is refused too."""
    idp.id_token = _id_token(signing_key, email_verified=claim)
    users_before = db_session.query(User).count()

    with caplog.at_level("WARNING"):
        response = _complete_login(client)

    assert response.status_code == 401
    assert response.json()["detail"] == "login failed"
    assert platform_session.session_cookie_name() not in response.cookies
    db_session.expire_all()
    assert db_session.query(User).count() == users_before
    assert db_session.query(User).filter_by(oidc_sub="idp-sub-1").first() is None
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "verified" in logged, "the refusal is logged with its reason"
    assert "ada@example.com" not in logged


def test_an_unverified_bootstrap_admin_address_gets_no_row_and_no_admin_grant(
    client, db_session, oidc_mode, idp, signing_key, monkeypatch
):
    """The case the rule exists for: the listed admin address, claimed by someone who has not
    proved they own it, must not become an admin row."""
    monkeypatch.setattr(settings, "bootstrap_admin_emails", "ada@example.com")
    idp.id_token = _id_token(signing_key, email_verified=False)

    assert _complete_login(client).status_code == 401

    db_session.expire_all()
    assert db_session.query(User).filter_by(email="ada@example.com").first() is None
    assert db_session.query(User).filter_by(oidc_sub="idp-sub-1").first() is None


def test_an_unverified_email_does_not_refresh_an_existing_row(
    client, db_session, oidc_mode, idp, signing_key
):
    """"No row is created OR updated": a later login that carries `email_verified: false` must
    not rewrite the name and email of the row it would have matched by `sub`."""
    assert _complete_login(client).status_code == 302
    client.cookies.clear()
    idp.id_token = _id_token(
        signing_key, email="new-address@example.com", name="Renamed", email_verified=False
    )

    assert _complete_login(client).status_code == 401

    db_session.expire_all()
    user = db_session.query(User).filter_by(oidc_sub="idp-sub-1").one()
    assert (user.email, user.name) == ("ada@example.com", "Ada Lovelace")


@pytest.mark.parametrize(
    "claim",
    [ABSENT, True, "true", "True", "TRUE", None],
    ids=["absent", "true", "str-true", "str-True", "str-TRUE", "null"],
)
def test_an_absent_null_or_true_email_verified_provisions_as_before(
    client, db_session, oidc_mode, idp, signing_key, claim, monkeypatch
):
    """An absent claim must stay accepted (Entra ID and others never send it), and a `true` --
    boolean or the string some IdPs send -- is the normal case. The listed bootstrap address
    still gets its grant on each."""
    monkeypatch.setattr(settings, "bootstrap_admin_emails", "ada@example.com")
    idp.id_token = _id_token(signing_key, email_verified=claim)

    assert _complete_login(client).status_code == 302

    user = db_session.query(User).filter_by(oidc_sub="idp-sub-1").one()
    assert user.email == "ada@example.com"
    assert user.role == "admin"


# --- the seam is the only thing that changed --------------------------------


def test_oidc_session_works_on_an_existing_protected_endpoint(
    client, db_session, seeded_ids, oidc_mode, idp
):
    """`get_current_user`'s 8 call sites are untouched — a session cookie has
    to work on them with no per-router auth code. `POST /projects` is one."""
    _complete_login(client)
    response = client.post(
        "/api/projects",
        json={
            "name": "oidc-project",
            "template_id": seeded_ids["template_id"],
            "team_id": seeded_ids["team_id"],
            "nl_request": "build a thing",
        },
    )
    assert response.status_code == 201
    user = db_session.query(User).filter_by(oidc_sub="idp-sub-1").one()
    assert response.json()["owner_id"] == user.id


def test_identity_free_preview_route_stays_identity_free_in_oidc_mode(client, oidc_mode, idp):
    """ADR 0016: the preview route authenticates by signed token only. In
    OIDC mode it must still not demand a session — a 404 for an unknown build
    (not a 401) proves the request got past identity entirely."""
    assert client.get("/api/projects/nope/preview/nope?token=x").status_code == 404
