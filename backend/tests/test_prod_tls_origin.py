"""prod needs an `https` platform origin, `__Host-` cookies,
and an OIDC callback that never comes from the request. (The forward-auth scheme tests live
beside the other label tests, `test_preview_auth.py`.)

Every literal here is a wire string (cookie names, attribute spellings, the setting's name in
an error): none is derived from the code under test."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import Response

from app.config import PlatformPublicUrlError, Settings, settings
from app.models.user import User
from app.routers.auth import _redirect_uri
from app.services import platform_session
from app.services.roles import Role

# A minimal prod construction that otherwise starts (same literals test_config.py uses).
PROD_BASE = {
    "_env_file": None,
    "forge_env": "prod",
    "database_url": "postgresql://u:p@db.example.test/forge",
    "oidc_issuer": "https://idp.example.test",
    "oidc_client_id": "forge-test",
    "oidc_client_secret": "not-a-real-secret",
    "service_env_encryption_key": "-6Q6OIXC_6ezyiOPJFDC0Qsv6tXNJNPatQb77E0Y2xY=",
    "session_signing_secret": "prod-session-secret-0123456789-abcdefghijklmnop",
    "preview_signing_secret": "prod-preview-secret-9876543210-ponmlkjihgfedcba",
}


def _prod(**overrides) -> Settings:
    kwargs = {**PROD_BASE, **overrides}
    return Settings(**{k: v for k, v in kwargs.items() if v is not ...})


# --- the public-origin setting ----------------------------------------------------


def test_prod_with_an_https_origin_starts():
    s = _prod(platform_public_url="https://forge.example.test")
    assert s.platform_public_url == "https://forge.example.test"


def test_prod_with_an_https_origin_and_port_starts():
    s = _prod(platform_public_url="https://forge.example.test:8443")
    assert s.platform_public_url == "https://forge.example.test:8443"


def test_prod_refuses_an_unset_public_url():
    with pytest.raises(PlatformPublicUrlError) as exc_info:
        _prod(platform_public_url=...)
    message = str(exc_info.value)
    assert "PLATFORM_PUBLIC_URL" in message
    assert "requires PLATFORM_PUBLIC_URL to be set" in message


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "spaces"])
def test_prod_reads_a_blank_public_url_as_unset(blank):
    with pytest.raises(PlatformPublicUrlError) as exc_info:
        _prod(platform_public_url=blank)
    assert "requires PLATFORM_PUBLIC_URL to be set" in str(exc_info.value)


def test_prod_refuses_an_http_origin():
    with pytest.raises(PlatformPublicUrlError) as exc_info:
        _prod(platform_public_url="http://forge.example.test")
    message = str(exc_info.value)
    assert "PLATFORM_PUBLIC_URL" in message
    assert "https:// origin" in message


@pytest.mark.parametrize(
    "value", ["https://forge.example.test/", "https://forge.example.test/app"], ids=["slash", "path"]
)
def test_prod_refuses_an_origin_with_a_path(value):
    with pytest.raises(PlatformPublicUrlError) as exc_info:
        _prod(platform_public_url=value)
    assert "path, query or fragment" in str(exc_info.value)


@pytest.mark.parametrize(
    "value",
    ["https://forge.example.test?x=1", "https://forge.example.test#frag"],
    ids=["query", "fragment"],
)
def test_prod_refuses_an_origin_with_a_query_or_fragment(value):
    with pytest.raises(PlatformPublicUrlError) as exc_info:
        _prod(platform_public_url=value)
    assert "path, query or fragment" in str(exc_info.value)


@pytest.mark.parametrize("value", ["forge.example.test", "ftp://forge.example.test", "HTTPS://x.test"])
def test_prod_refuses_a_value_that_is_not_an_http_origin(value):
    with pytest.raises(PlatformPublicUrlError) as exc_info:
        _prod(platform_public_url=value)
    assert "is not an origin" in str(exc_info.value)


def test_prod_refuses_credentials_in_the_origin():
    with pytest.raises(PlatformPublicUrlError) as exc_info:
        _prod(platform_public_url="https://user:pw@forge.example.test")
    assert "credentials" in str(exc_info.value)


def test_prod_refuses_a_bad_port():
    with pytest.raises(PlatformPublicUrlError) as exc_info:
        _prod(platform_public_url="https://forge.example.test:99999")
    assert "invalid port" in str(exc_info.value)


def test_prod_refuses_an_origin_with_no_host():
    with pytest.raises(PlatformPublicUrlError) as exc_info:
        _prod(platform_public_url="https://")
    assert "names no host" in str(exc_info.value)


def test_dev_does_not_need_a_public_url():
    s = Settings(_env_file=None, forge_env="dev")
    assert s.platform_public_url is None


def test_dev_accepts_any_scheme_but_not_a_path():
    s = Settings(_env_file=None, forge_env="dev", platform_public_url="http://localhost:5173")
    assert s.platform_public_url == "http://localhost:5173"
    with pytest.raises(PlatformPublicUrlError):
        Settings(_env_file=None, forge_env="dev", platform_public_url="http://localhost:5173/x")


def test_the_forward_auth_scheme_defaults_to_http_and_refuses_anything_else():
    assert Settings(_env_file=None, forge_env="dev").traefik_forward_auth_scheme == "http"
    with pytest.raises(ValueError):
        Settings(_env_file=None, forge_env="dev", traefik_forward_auth_scheme="ftp")


# --- OIDC callback URL ------------------------------------------------------------


def _request_at(internal_url: str):
    """Stands in for a request that reached the API on its internal address."""
    from app.main import app

    return SimpleNamespace(app=app, url_for=lambda name: internal_url)


def test_prod_derives_the_oidc_callback_from_the_public_origin_not_the_request(monkeypatch):
    monkeypatch.setattr(settings, "forge_env", "prod")
    monkeypatch.setattr(settings, "platform_public_url", "https://forge.example.test")
    monkeypatch.setattr(settings, "oidc_redirect_url", None)
    uri = _redirect_uri(_request_at("http://127.0.0.1:8000/api/auth/callback"))
    assert uri == "https://forge.example.test/api/auth/callback"


def test_an_explicit_oidc_redirect_url_still_wins_in_prod(monkeypatch):
    monkeypatch.setattr(settings, "forge_env", "prod")
    monkeypatch.setattr(settings, "platform_public_url", "https://forge.example.test")
    monkeypatch.setattr(settings, "oidc_redirect_url", "https://sso.example.test/cb")
    assert _redirect_uri(_request_at("http://127.0.0.1:8000/x")) == "https://sso.example.test/cb"


def test_dev_still_derives_the_oidc_callback_from_the_request(monkeypatch):
    monkeypatch.setattr(settings, "oidc_redirect_url", None)
    monkeypatch.setattr(settings, "platform_public_url", "https://forge.example.test")
    uri = _redirect_uri(_request_at("http://localhost:8000/api/auth/callback"))
    assert uri == "http://localhost:8000/api/auth/callback"


# --- cookie names and attributes --------------------------------------------------


def _attributes(set_cookie: str) -> tuple[str, dict[str, str | None]]:
    """`name=value; A; B=c` -> (name, {a: None, b: "c"}), attribute names lower-cased."""
    head, *attrs = [part.strip() for part in set_cookie.split(";")]
    name = head.partition("=")[0]
    parsed: dict[str, str | None] = {}
    for attr in attrs:
        key, sep, value = attr.partition("=")
        parsed[key.lower()] = value if sep else None
    return name, parsed


def _set_cookie_headers(response: Response) -> list[str]:
    return response.headers.getlist("set-cookie")


def _in_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(settings, "forge_env", mode)
    monkeypatch.setattr(settings, "session_absolute_max_days", 7.0)


def test_dev_session_cookie_name_and_attributes(monkeypatch):
    _in_mode(monkeypatch, "dev")
    response = Response()
    platform_session.set_session_cookie(response, "v")
    (header,) = _set_cookie_headers(response)
    name, attrs = _attributes(header)
    assert name == "forge_session"
    assert attrs == {"max-age": "604800", "path": "/", "httponly": None, "samesite": "lax"}


def test_prod_session_cookie_is_host_prefixed_with_exact_attributes(monkeypatch):
    _in_mode(monkeypatch, "prod")
    response = Response()
    platform_session.set_session_cookie(response, "v")
    (header,) = _set_cookie_headers(response)
    name, attrs = _attributes(header)
    assert name == "__Host-forge_session"
    # No Domain, Path=/, Secure: the three things a browser demands of a __Host- cookie.
    assert attrs == {
        "max-age": "604800",
        "path": "/",
        "httponly": None,
        "samesite": "lax",
        "secure": None,
    }


def test_dev_login_cookie_name_and_attributes(monkeypatch):
    _in_mode(monkeypatch, "dev")
    response = Response()
    platform_session.set_login_state_cookie(response, "v")
    (header,) = _set_cookie_headers(response)
    name, attrs = _attributes(header)
    assert name == "forge_login"
    assert attrs == {"max-age": "600", "path": "/", "httponly": None, "samesite": "lax"}


def test_prod_login_cookie_is_host_prefixed_with_exact_attributes(monkeypatch):
    _in_mode(monkeypatch, "prod")
    response = Response()
    platform_session.set_login_state_cookie(response, "v")
    (header,) = _set_cookie_headers(response)
    name, attrs = _attributes(header)
    assert name == "__Host-forge_login"
    assert attrs == {
        "max-age": "600",
        "path": "/",
        "httponly": None,
        "samesite": "lax",
        "secure": None,
    }


@pytest.mark.parametrize(
    ("mode", "session_name", "login_name", "secure"),
    [
        ("dev", "forge_session", "forge_login", False),
        ("prod", "__Host-forge_session", "__Host-forge_login", True),
    ],
)
def test_clearing_targets_the_same_names_and_attributes_as_setting(
    monkeypatch, mode, session_name, login_name, secure
):
    """A clear that used another name would leave the real cookie in the browser (logout
    that does not log out); one that dropped `Secure` or `Path=/` is ignored by a browser
    for a `__Host-` cookie."""
    _in_mode(monkeypatch, mode)
    response = Response()
    platform_session.clear_session_cookie(response)
    platform_session.clear_login_state_cookie(response)
    cleared = {}
    for header in _set_cookie_headers(response):
        name, attrs = _attributes(header)
        cleared[name] = attrs
    assert set(cleared) == {session_name, login_name}
    for attrs in cleared.values():
        assert attrs["max-age"] == "0"
        assert attrs["path"] == "/"
        assert "domain" not in attrs
        assert ("secure" in attrs) is secure


# --- cookie tossing ---------------------------------------------------------------


@pytest.fixture()
def prod_session_mode(monkeypatch):
    monkeypatch.setattr(settings, "forge_env", "prod")
    monkeypatch.setattr(settings, "auth_mode", "oidc")
    monkeypatch.setattr(settings, "session_signing_secret", "t-session-secret-for-tossing-test")


def _signed_in_user(db_session) -> tuple[User, str]:
    user = User(email="ada@example.test", name="Ada", role=Role.REQUESTER)
    db_session.add(user)
    db_session.commit()
    return user, platform_session.issue_session(user.id)


def test_a_bare_forge_session_cookie_sent_to_a_prod_instance_is_ignored(
    client, db_session, prod_session_mode
):
    """Cookie tossing: generated code at `{id}.{APPS_BASE_DOMAIN}` can set a cookie the
    platform host receives. A perfectly valid signed value under the dev name must not
    authenticate anyone in prod; the same value under the `__Host-` name does."""
    _user, cookie = _signed_in_user(db_session)

    tossed = client.get("/api/users/me", headers={"Cookie": f"forge_session={cookie}"})
    assert tossed.status_code == 401

    genuine = client.get("/api/users/me", headers={"Cookie": f"__Host-forge_session={cookie}"})
    assert genuine.status_code == 200
    assert genuine.json()["email"] == "ada@example.test"


def test_a_bare_forge_login_cookie_is_ignored_by_the_prod_callback(
    client, prod_session_mode, monkeypatch
):
    """The login-state cookie is read through the same predicate: a tossed `forge_login`
    never reaches `verify_login_state`, so the callback stops before the code exchange. The
    `__Host-` name with the same value gets as far as the exchange (the control: a 401
    from the exchange itself would look identical from outside, hence the call record)."""
    from app.services import oidc

    exchanges: list[str] = []

    def _exchange(**kwargs):
        exchanges.append(kwargs["code"])
        raise oidc.OidcError("stubbed")

    monkeypatch.setattr(oidc, "exchange_code_for_id_token", _exchange)
    monkeypatch.setattr(settings, "platform_public_url", "https://forge.example.test")
    login = platform_session.issue_login_state(state="s", code_verifier="v", next_path="/")
    url = "/api/auth/callback?code=c&state=s"

    tossed = client.get(url, headers={"Cookie": f"forge_login={login}"})
    assert tossed.status_code == 401
    assert exchanges == []

    genuine = client.get(url, headers={"Cookie": f"__Host-forge_login={login}"})
    assert genuine.status_code == 401
    assert exchanges == ["c"]
