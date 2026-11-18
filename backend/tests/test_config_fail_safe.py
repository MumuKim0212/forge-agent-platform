"""configuration fails safe (ADR 0028
§Amendment 2026-10-17).

Every `Settings` here is built explicitly -- `_env_file=None`, or an env file the
test writes -- never from the ambient `backend/.env` (a test
that reads it answers differently on CI than locally). The real entry points
(`uvicorn app.main:app`, `python -m app.worker`) are driven in
`test_startup_refuses_without_forge_env.py`; the end-to-end forged cookie is in
`test_session_secret_blank.py`; the re-encrypt command in
`test_reencrypt_service_env.py`.

Messages are asserted as well as exception types: `ProdSigningSecretError` and
`CorsOriginsError` each cover several conditions behind one type, so only a
phrase that one branch alone produces proves THAT branch fired.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.config import (
    CorsOriginsError,
    ForgeEnvNotSetError,
    ProdServiceEnvEncryptionKeyError,
    ProdSigningSecretError,
    Settings,
)
from tests.credential_fields import credential_looking_fields

# A real Fernet key literal (never derived from production code), and the other
# prod requirements, so a construction fails only on the condition under test.
FERNET_KEY = "-6Q6OIXC_6ezyiOPJFDC0Qsv6tXNJNPatQb77E0Y2xY="
SESSION_32 = "s" * 32
PREVIEW_32 = "p" * 32
PROD_BASE = {
    "_env_file": None,
    "forge_env": "prod",
    "database_url": "postgresql://x/y",
    "oidc_issuer": "https://idp.example.test",
    "oidc_client_id": "forge-test",
    "oidc_client_secret": "not-a-real-secret",
    "service_env_encryption_key": FERNET_KEY,
    "session_signing_secret": SESSION_32,
    "preview_signing_secret": PREVIEW_32,
    "platform_public_url": "https://forge.example.test",  # prod requires an https origin
}

SIGNING_SECRETS = [
    pytest.param("session_signing_secret", "SESSION_SIGNING_SECRET", id="session"),
    pytest.param("preview_signing_secret", "PREVIEW_SIGNING_SECRET", id="preview"),
]
BLANKS = [
    pytest.param("", id="empty"),
    pytest.param("   ", id="spaces"),
    pytest.param(" \t\r\n", id="tabs-and-newlines"),
]


def _prod(**overrides):
    """`PROD_BASE` with `overrides` applied; a value of `...` drops the key (unset)."""
    kwargs = {**PROD_BASE, **overrides}
    return Settings(**{k: v for k, v in kwargs.items() if v is not ...})


# --- 1. FORGE_ENV has no default -------------------------------------------------


def test_forge_env_has_no_default(monkeypatch):
    """The refuse path itself. Giving the field its old `"dev"` default back turns
    this red: an unset variable would then construct a dev `Settings`."""
    monkeypatch.delenv("FORGE_ENV", raising=False)
    with pytest.raises(ForgeEnvNotSetError) as exc_info:
        Settings(_env_file=None)
    message = str(exc_info.value)
    # Says what to set, in both spellings, and where.
    assert "FORGE_ENV=dev" in message
    assert "FORGE_ENV=prod" in message
    assert ".env" in message


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "spaces"])
def test_a_blank_forge_env_is_the_same_condition_as_unset(monkeypatch, blank):
    monkeypatch.setenv("FORGE_ENV", blank)
    with pytest.raises(ForgeEnvNotSetError):
        Settings(_env_file=None)


def test_forge_env_unset_in_an_env_file_is_refused(monkeypatch, tmp_path):
    """The operator's way of getting it wrong: an env file that simply lacks the line."""
    monkeypatch.delenv("FORGE_ENV", raising=False)
    env_file = tmp_path / "operator.env"
    env_file.write_text("CORS_ORIGINS=http://localhost:5173\n", encoding="utf-8")
    with pytest.raises(ForgeEnvNotSetError):
        Settings(_env_file=env_file)


def test_forge_env_is_read_from_an_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("FORGE_ENV", raising=False)
    env_file = tmp_path / "operator.env"
    env_file.write_text("FORGE_ENV=dev\n", encoding="utf-8")
    assert Settings(_env_file=env_file).forge_env == "dev"


def test_an_unknown_forge_env_is_still_a_value_error():
    with pytest.raises(ValueError, match="must be 'dev' or 'prod'"):
        Settings(_env_file=None, forge_env="staging")


def test_env_example_sets_forge_env_dev_on_a_live_line():
    """`copy .env.example .env` must yield a working dev file now that there is no
    default: the line is live, not commented."""
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    assert "FORGE_ENV=dev" in [line.strip() for line in text.splitlines()]


# --- 2. a blank secret is an unset secret ----------------------------------------


@pytest.mark.parametrize("blank", BLANKS)
@pytest.mark.parametrize("name, _env_name", SIGNING_SECRETS)
def test_blank_signing_secret_in_dev_is_generated_not_kept_blank(name, _env_name, blank):
    settings = Settings(_env_file=None, forge_env="dev", **{name: blank})
    value = getattr(settings, name)
    assert value is not None
    assert value.strip(), "a blank secret was kept: the signer would HMAC with an empty key"
    assert value != blank
    assert len(value.encode("utf-8")) >= 32


@pytest.mark.parametrize("name, env_name", SIGNING_SECRETS)
def test_blank_signing_secret_in_an_env_file_is_generated(tmp_path, name, env_name):
    """The config as an operator writes it: `SESSION_SIGNING_SECRET=` in the file.
    pydantic-settings reads that as the empty string, not as absent."""
    env_file = tmp_path / "operator.env"
    env_file.write_text(f"FORGE_ENV=dev\n{env_name}=\n", encoding="utf-8")
    value = getattr(Settings(_env_file=env_file), name)
    assert value and value.strip()


def test_unset_signing_secrets_are_generated_per_process_in_dev():
    first = Settings(_env_file=None, forge_env="dev")
    second = Settings(_env_file=None, forge_env="dev")
    assert first.session_signing_secret != second.session_signing_secret
    assert first.preview_signing_secret != second.preview_signing_secret
    assert first.session_signing_secret != first.preview_signing_secret


@pytest.mark.parametrize("blank", BLANKS)
def test_blank_encryption_key_in_dev_is_generated_and_not_configured(blank):
    settings = Settings(_env_file=None, forge_env="dev", service_env_encryption_key=blank)
    # A real Fernet key, and "configured" is about what an operator supplied, so a blank
    # does not count -- the migration relies on exactly that distinction.
    Fernet(settings.service_env_encryption_key.encode("ascii"))
    assert settings.service_env_encryption_key_configured is False


@pytest.mark.parametrize("blank", BLANKS)
def test_blank_old_encryption_key_is_unset(blank):
    settings = Settings(_env_file=None, forge_env="dev", service_env_encryption_key_old=blank)
    assert settings.service_env_encryption_key_old is None


@pytest.mark.parametrize("blank", BLANKS)
def test_a_blank_secret_value_is_unset_not_just_a_blank_signing_secret(blank):
    """Every credential-valued setting, not only the signing secrets."""
    settings = Settings(
        _env_file=None,
        forge_env="dev",
        anthropic_api_key=blank,
        openai_api_key=blank,
        github_token=blank,
        oidc_client_secret=blank,
        bootstrap_admin_password=blank,
        service_postgres_admin_dsn=blank,
    )
    assert settings.anthropic_api_key is None
    assert settings.openai_api_key is None
    assert settings.github_token is None
    assert settings.oidc_client_secret is None
    assert settings.bootstrap_admin_password is None
    assert settings.service_postgres_admin_dsn is None


def test_a_non_blank_secret_is_kept_byte_for_byte():
    """Only a blank is unset: a real secret is never stripped or altered."""
    settings = Settings(_env_file=None, forge_env="dev", session_signing_secret="  padded secret  ")
    assert settings.session_signing_secret == "  padded secret  "


def test_every_secret_looking_setting_reads_blank_as_unset():
    """The guard that makes "blank means unset" a rule rather than one fix per
    secret: every `Settings` field whose name says it holds a credential and whose
    type is a string must read a blank as unset. A new `*_secret`, `*_key`,
    `*_token`, `*_password` or `*_dsn` field that forgets it fails here by name.
    Behavioural on purpose -- it does not import the list production keeps. The discovery
    rule lives in `tests/credential_fields.py`, shared with the redaction guard in
    `test_settings_never_print_credentials.py`; it skips a non-string field such as the integer
    `build_token_budget`."""
    found = []
    for name in credential_looking_fields(Settings):
        found.append(name)
        value = getattr(Settings(_env_file=None, forge_env="dev", **{name: "   "}), name)
        assert value is None or value.strip(), f"{name} kept a blank value as if it were set"
    # Not vacuous: the scan really does reach the settings this task names.
    assert {
        "session_signing_secret",
        "preview_signing_secret",
        "service_env_encryption_key",
        "service_env_encryption_key_old",
    } <= set(found)


# --- 3. prod requires both signing secrets, at least 32 bytes ----------------------


@pytest.mark.parametrize("name, env_name", SIGNING_SECRETS)
def test_prod_with_an_unset_signing_secret_refuses_to_start(name, env_name):
    with pytest.raises(ProdSigningSecretError) as exc_info:
        _prod(**{name: ...})
    message = str(exc_info.value)
    assert f"requires {env_name} to be set" in message
    assert "at least 32 bytes" not in message  # the sibling (short) diagnosis, not this one


@pytest.mark.parametrize("blank", BLANKS)
@pytest.mark.parametrize("name, env_name", SIGNING_SECRETS)
def test_prod_with_a_blank_signing_secret_is_refused_like_an_unset_one(name, env_name, blank):
    with pytest.raises(ProdSigningSecretError) as exc_info:
        _prod(**{name: blank})
    message = str(exc_info.value)
    assert f"requires {env_name} to be set" in message
    assert "at least 32 bytes" not in message


@pytest.mark.parametrize("name, env_name", SIGNING_SECRETS)
def test_prod_with_a_31_byte_signing_secret_refuses_to_start(name, env_name):
    with pytest.raises(ProdSigningSecretError) as exc_info:
        _prod(**{name: "x" * 31})
    message = str(exc_info.value)
    assert f"requires {env_name} to be at least 32 bytes" in message
    assert "this one is 31" in message
    assert "to be set" not in message  # the sibling (unset) diagnosis, not this one


@pytest.mark.parametrize("name, _env_name", SIGNING_SECRETS)
def test_prod_with_a_32_byte_signing_secret_starts(name, _env_name):
    settings = _prod(**{name: "x" * 32})
    assert getattr(settings, name) == "x" * 32
    assert settings.is_prod is True


def test_the_length_rule_counts_utf8_bytes_not_characters():
    """11 Hangul syllables are 11 characters and 33 bytes: long enough. 31
    one-byte characters are 31 bytes: not."""
    korean = "비밀키" * 3 + "씨앤"  # 11 characters, 3 bytes each
    assert len(korean) == 11 and len(korean.encode("utf-8")) == 33
    assert _prod(session_signing_secret=korean).session_signing_secret == korean
    with pytest.raises(ProdSigningSecretError):
        _prod(session_signing_secret="비" * 10 + "k")  # 10*3 + 1 = 31 bytes


def test_each_signing_secret_is_checked_on_its_own():
    """A good session secret does not excuse a short preview secret, or the reverse:
    the error names the one that failed."""
    with pytest.raises(ProdSigningSecretError, match="PREVIEW_SIGNING_SECRET"):
        _prod(session_signing_secret=SESSION_32, preview_signing_secret="short")
    with pytest.raises(ProdSigningSecretError, match="SESSION_SIGNING_SECRET"):
        _prod(session_signing_secret="short", preview_signing_secret=PREVIEW_32)


def test_prod_never_generates_a_signing_secret():
    """The generate-if-unset branch is dev only; prod refuses instead of filling in."""
    with pytest.raises(ProdSigningSecretError):
        _prod(session_signing_secret=..., preview_signing_secret=PREVIEW_32)


def test_the_error_never_echoes_the_secret():
    secret = "too-short-secret-value"
    with pytest.raises(ProdSigningSecretError) as exc_info:
        _prod(session_signing_secret=secret)
    assert secret not in str(exc_info.value)


@pytest.mark.parametrize("blank", BLANKS)
def test_prod_with_a_blank_encryption_key_is_refused_like_an_unset_one(blank):
    with pytest.raises(ProdServiceEnvEncryptionKeyError) as exc_info:
        _prod(service_env_encryption_key=blank)
    message = str(exc_info.value)
    assert "requires SERVICE_ENV_ENCRYPTION_KEY to be set explicitly" in message
    assert "could not be decrypted" not in message  # the error's other condition


def test_prod_with_a_blank_encryption_key_in_an_env_file_is_refused(tmp_path):
    env_file = tmp_path / "operator.env"
    env_file.write_text("SERVICE_ENV_ENCRYPTION_KEY=\n", encoding="utf-8")
    with pytest.raises(ProdServiceEnvEncryptionKeyError):
        _prod(_env_file=env_file, service_env_encryption_key=...)


def test_a_fully_specified_prod_config_starts():
    settings = _prod()
    assert settings.is_prod
    assert settings.session_signing_secret == SESSION_32
    assert settings.preview_signing_secret == PREVIEW_32
    assert settings.service_env_encryption_key_configured is True


# --- 4. CORS_ORIGINS is validated at startup ---------------------------------------

_APPS = "apps.forge.local"  # the default `apps_base_domain`; pinned by a literal here

# (id, CORS_ORIGINS value, a phrase only THIS branch's message contains)
REFUSED_CORS = [
    pytest.param("*", "wildcard is refused", id="wildcard"),
    pytest.param("http://localhost:5173,*", "wildcard is refused", id="wildcard-among-others"),
    pytest.param("https://*.example.com", "wildcard is refused", id="wildcard-subdomain"),
    pytest.param("null", "sandboxed document", id="null"),
    pytest.param("NULL", "sandboxed document", id="null-upper-case"),
    pytest.param("https://spa.example.com/app", "path, query or fragment", id="path"),
    pytest.param("https://spa.example.com/", "path, query or fragment", id="trailing-slash"),
    pytest.param("https://spa.example.com?x=1", "path, query or fragment", id="query"),
    pytest.param("https://spa.example.com#frag", "path, query or fragment", id="fragment"),
    pytest.param("spa.example.com", "must start with http:// or https://", id="no-scheme"),
    pytest.param("ftp://spa.example.com", "must start with http:// or https://", id="other-scheme"),
    pytest.param("HTTPS://spa.example.com", "must start with http:// or https://", id="upper-case-scheme"),
    pytest.param("https://user:pw@spa.example.com", "credentials, whitespace or an empty port", id="userinfo"),
    pytest.param("https://spa.example.com:", "credentials, whitespace or an empty port", id="empty-port"),
    pytest.param("https://spa example.com", "credentials, whitespace or an empty port", id="whitespace"),
    pytest.param("https://spa.example.com:99999", "has an invalid port", id="port-out-of-range"),
    pytest.param("https://spa.example.com:http", "has an invalid port", id="port-not-a-number"),
    pytest.param("https://:5173", "names no host", id="no-host"),
    pytest.param(f"https://x.{_APPS}", "generated services live there", id="apps-subdomain"),
    pytest.param(f"https://{_APPS}", "generated services live there", id="apps-domain-itself"),
    pytest.param(f"http://{_APPS}:8000", "generated services live there", id="apps-domain-with-port"),
    pytest.param(f"https://a.b.{_APPS}", "generated services live there", id="apps-deep-subdomain"),
    pytest.param(f"https://X.Apps.Forge.Local", "generated services live there", id="apps-domain-mixed-case"),
    pytest.param(f"https://{_APPS}.", "generated services live there", id="apps-domain-trailing-dot"),
    pytest.param(
        f"http://localhost:5173,https://p--abc.{_APPS}", "generated services live there", id="apps-among-others"
    ),
]
_ALL_PHRASES = {
    "wildcard is refused",
    "sandboxed document",
    "path, query or fragment",
    "must start with http:// or https://",
    "credentials, whitespace or an empty port",
    "names no host",
    "has an invalid port",
    "generated services live there",
}


@pytest.mark.parametrize("cors_origins, phrase", REFUSED_CORS)
def test_a_refused_cors_origin_stops_startup_with_a_named_error(cors_origins, phrase):
    with pytest.raises(CorsOriginsError) as exc_info:
        Settings(_env_file=None, forge_env="dev", cors_origins=cors_origins)
    message = str(exc_info.value)
    assert phrase in message
    # No other shape's diagnosis is claimed for this value.
    assert not any(other in message for other in _ALL_PHRASES - {phrase})


@pytest.mark.parametrize("cors_origins", ["*", f"https://x.{_APPS}"])
def test_cors_origins_are_refused_in_prod_too(cors_origins):
    with pytest.raises(CorsOriginsError):
        _prod(cors_origins=cors_origins)


def test_a_cors_origin_in_an_env_file_is_validated(tmp_path):
    env_file = tmp_path / "operator.env"
    env_file.write_text("FORGE_ENV=dev\nCORS_ORIGINS=*\n", encoding="utf-8")
    with pytest.raises(CorsOriginsError):
        Settings(_env_file=env_file)


def test_the_apps_domain_check_follows_the_configured_apps_base_domain():
    with pytest.raises(CorsOriginsError, match="generated services live there"):
        Settings(
            _env_file=None, forge_env="dev", apps_base_domain="apps.example.org",
            cors_origins="https://x.apps.example.org",
        )
    # The default domain is only the default: once changed, its old subdomains are ordinary hosts.
    Settings(
        _env_file=None, forge_env="dev", apps_base_domain="apps.example.org",
        cors_origins=f"https://x.{_APPS}",
    )


@pytest.mark.parametrize(
    "cors_origins",
    [
        "http://localhost:5173",  # the default
        "https://spa.example.com",
        "http://localhost:5173,https://spa.example.com:8443",
        " http://localhost:5173 , https://spa.example.com ",  # whitespace around entries is the list's, not the origin's
        "http://[::1]:5173",
        "http://127.0.0.1:5173",
        "",  # nothing trusted
        f"https://evil{_APPS}",  # shares a suffix with the apps domain, not a subdomain of it
        f"https://{_APPS}.example.com",  # contains the apps domain, is not under it
        f"https://forge.local",  # the apps domain's PARENT is not the apps domain
    ],
)
def test_a_legitimate_cors_origin_is_accepted(cors_origins):
    settings = Settings(_env_file=None, forge_env="dev", cors_origins=cors_origins)
    assert settings.cors_origin_list == [o.strip() for o in cors_origins.split(",") if o.strip()]
