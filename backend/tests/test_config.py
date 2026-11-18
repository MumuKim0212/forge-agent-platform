"""runtime-mode config. Settings is a pydantic-settings model, so
each test constructs its own instance (with `_env_file=None` to ignore any
local `.env`) instead of relying on the process-wide `app.config.settings`
singleton — that singleton is already fixed at import time to dev mode for
the rest of the suite."""

import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from cryptography.fernet import Fernet

from app.config import (
    BootstrapAdminPasswordTooLongError,
    LocalBootstrapAdminEmailCountError,
    LocalBootstrapAdminPasswordMissingError,
    OidcConfigMissingError,
    PlatformTimezoneError,
    ProdDatabaseUrlMissingError,
    ProdDevAuthModeError,
    ProdServiceEnvEncryptionKeyError,
    ProdWeakBootstrapPasswordError,
    Settings,
)
from tests.dev_instance import (
    find_dev_instance_references,
    find_literal_admin_dsns,
    load_dev_instance_or_none,
)
from tests.fake_admin_dsn import FAKE_ADMIN_DSN

# ADR 0022: `local` mode's own minimal-but-legal construction kwargs,
# the `local`-mode equivalent of OIDC_CONFIG above.
LOCAL_CONFIG = {
    "bootstrap_admin_emails": "admin@example.test",
    "bootstrap_admin_password": "admin",  # legal in dev; prod refuses it (see below)
}

# prod defaults `AUTH_MODE` to `oidc`, which requires all three OIDC
# settings — so every prod-mode construction now has to carry them.
OIDC_CONFIG = {
    "oidc_issuer": "https://idp.example.test",
    "oidc_client_id": "forge-test",
    "oidc_client_secret": "not-a-real-secret",
}

# prod also requires SERVICE_ENV_ENCRYPTION_KEY
# explicitly (`ProdServiceEnvEncryptionKeyError` otherwise, see below) — a
# real, valid Fernet key literal, not derived from any production code path
# (a test must not share a source of truth with the
# code it tests). Used only by constructions that are expected to SUCCEED —
# tests that intentionally leave a DIFFERENT prod requirement unmet (e.g.
# `auth_mode="dev"`, a missing OIDC setting, a weak bootstrap password) must
# keep failing on THEIR OWN condition, so they deliberately do not carry
# this.
PROD_SERVICE_ENV_ENCRYPTION_KEY = "-6Q6OIXC_6ezyiOPJFDC0Qsv6tXNJNPatQb77E0Y2xY="

# prod also requires BOTH signing secrets, each at
# least 32 bytes (UTF-8). Literals, not generated, and DIFFERENT from each other. Carried only
# by constructions expected to SUCCEED, for the same reason as the encryption key above.
PROD_SIGNING_SECRETS = {
    "session_signing_secret": "prod-session-secret-0123456789-abcdefghijklmnop",
    "preview_signing_secret": "prod-preview-secret-9876543210-ponmlkjihgfedcba",
}

# prod also requires an https PLATFORM_PUBLIC_URL, a literal origin.
PROD_PLATFORM_ORIGIN = {"platform_public_url": "https://forge.example.test"}


def test_dev_mode_defaults_to_sqlite_when_database_url_unset():
    s = Settings(_env_file=None, forge_env="dev")
    assert s.database_url == "sqlite:///./forge_dev.db"
    assert s.build_concurrency == 1
    assert s.build_max_duration_minutes == 20
    assert s.is_prod is False
    assert s.sandbox_memory_limit_mb == 2048
    assert s.sandbox_cpu_limit_cores == 1.0
    # the disk row, which was configured nowhere.
    assert s.sandbox_disk_limit_mb == 2048
    assert s.sandbox_disk_poll_interval_seconds == 1.0


def test_prod_mode_without_database_url_raises_at_startup():
    """the message is also asserted, not just the exception
    type -- `ProdDatabaseUrlMissingError` now covers three conditions
    (unset, blank, sqlite-set) behind one type, and this pins the specific
    diagnosis this branch produces, in the same test that exercises it
    (previously this was pinned only incidentally, by the blank test from
    (e) sharing the same branch -- a real pin, but in the wrong place: it
    would have silently stopped pinning this branch's message if the blank
    test were ever re-scoped to something that no longer reaches here)."""
    with pytest.raises(ProdDatabaseUrlMissingError) as exc_info:
        Settings(_env_file=None, forge_env="prod")
    message = str(exc_info.value)
    assert "requires DATABASE_URL to be set explicitly" in message
    assert "refuses a SQLite DATABASE_URL" not in message


def test_prod_mode_with_sqlite_database_url_raises_at_startup():
    """closes the gap the "unset" branch above cannot see. The
    documented workflow (`copy .env.example .env` then set `FORGE_ENV=prod`)
    used to leave DATABASE_URL uncommented at the dev SQLite file, which the
    "unset" check cannot catch since a value IS present.

    asserts on the phrase only THIS branch produces, and
    negatively on the phrase only the unset/blank sibling produces --
    `"SQLite" in message` / `"missing" not in message.lower()` (the
    original assertions here) are BOTH also satisfied by the sibling
    message ("...falls back to the dev SQLite file", no "missing" either),
    so a mutation swapping this branch's raise for the sibling's message
    with routing left untouched would have left both green. Same shape the
    blank test from (e) already gets right."""
    with pytest.raises(ProdDatabaseUrlMissingError) as exc_info:
        Settings(
            _env_file=None,
            forge_env="prod",
            database_url="sqlite:///./forge_dev.db",
            **OIDC_CONFIG,
        )
    message = str(exc_info.value)
    assert "refuses a SQLite DATABASE_URL" in message
    assert "requires DATABASE_URL to be set explicitly" not in message


@pytest.mark.parametrize("blank_value", ["", "   "], ids=["empty", "whitespace-only"])
def test_prod_mode_with_blank_database_url_raises_at_startup(blank_value):
    """a blank/whitespace-only value is treated as the SAME
    condition as unset, not a third diagnosis -- an operator who cleared the
    value (the natural half-step before pasting a real DSN) did not "set" it.
    `ProdDatabaseUrlMissingError` now covers three conditions (unset, blank,
    sqlite-set) behind one type, so the type alone cannot distinguish this
    case from `test_prod_mode_with_sqlite_database_url_raises_at_startup`
    above -- assert on the message matching the "unset" diagnosis and NOT
    the sqlite one."""
    with pytest.raises(ProdDatabaseUrlMissingError) as exc_info:
        Settings(
            _env_file=None,
            forge_env="prod",
            database_url=blank_value,
            **OIDC_CONFIG,
        )
    message = str(exc_info.value)
    assert "requires DATABASE_URL to be set explicitly" in message
    assert "refuses a SQLite DATABASE_URL" not in message


def test_prod_mode_with_database_url_uses_prod_defaults():
    s = Settings(
        _env_file=None,
        forge_env="prod",
        database_url="postgresql://x/y",
        service_env_encryption_key=PROD_SERVICE_ENV_ENCRYPTION_KEY,
        **PROD_SIGNING_SECRETS,
        **PROD_PLATFORM_ORIGIN,
        **OIDC_CONFIG,
    )
    assert s.database_url == "postgresql://x/y"
    assert s.build_concurrency == 2
    assert s.build_max_duration_minutes == 30
    assert s.is_prod is True
    assert s.sandbox_memory_limit_mb == 4096
    assert s.sandbox_cpu_limit_cores == 2.0
    assert s.sandbox_disk_limit_mb == 5120  # prod 5 GiB, dev 2 GiB (the spec's table)


def test_explicit_overrides_win_over_mode_defaults():
    s = Settings(
        _env_file=None,
        forge_env="prod",
        database_url="postgresql://x/y",
        build_concurrency=7,
        build_max_duration_minutes=99,
        sandbox_disk_limit_mb=123,
        service_env_encryption_key=PROD_SERVICE_ENV_ENCRYPTION_KEY,
        **PROD_SIGNING_SECRETS,
        **PROD_PLATFORM_ORIGIN,
        **OIDC_CONFIG,
    )
    assert s.build_concurrency == 7
    assert s.build_max_duration_minutes == 99
    assert s.sandbox_disk_limit_mb == 123


def test_invalid_forge_env_rejected():
    with pytest.raises(ValueError):
        Settings(_env_file=None, forge_env="staging", database_url="postgresql://x/y")


def test_traefik_enabled_mode_defaults_and_override():
    """Traefik routing defaults off in dev, on in prod (the only new
    mode-conditional branch, in model_post_init); explicit
    values win in both directions."""
    assert Settings(_env_file=None, forge_env="dev").traefik_enabled is False
    assert (
        Settings(
            _env_file=None,
            forge_env="prod",
            database_url="postgresql://x/y",
            service_env_encryption_key=PROD_SERVICE_ENV_ENCRYPTION_KEY,
            **PROD_SIGNING_SECRETS,
            **PROD_PLATFORM_ORIGIN,
            **OIDC_CONFIG,
        ).traefik_enabled
        is True
    )
    assert Settings(_env_file=None, forge_env="dev", traefik_enabled=True).traefik_enabled is True
    assert (
        Settings(
            _env_file=None,
            forge_env="prod",
            database_url="postgresql://x/y",
            traefik_enabled=False,
            service_env_encryption_key=PROD_SERVICE_ENV_ENCRYPTION_KEY,
            **PROD_SIGNING_SECRETS,
            **PROD_PLATFORM_ORIGIN,
            **OIDC_CONFIG,
        ).traefik_enabled
        is False
    )


# --- auth mode (ADR 0018 §6)


def test_auth_mode_defaults_from_forge_env():
    assert Settings(_env_file=None, forge_env="dev").auth_mode == "dev"
    assert Settings(_env_file=None, forge_env="dev").oidc_enabled is False
    prod = Settings(
        _env_file=None,
        forge_env="prod",
        database_url="postgresql://x/y",
        service_env_encryption_key=PROD_SERVICE_ENV_ENCRYPTION_KEY,
        **PROD_SIGNING_SECRETS,
        **PROD_PLATFORM_ORIGIN,
        **OIDC_CONFIG,
    )
    assert prod.auth_mode == "oidc"
    assert prod.oidc_enabled is True


def test_prod_mode_with_dev_auth_mode_raises_at_startup():
    """ADR 0018 §6: fail-closed with a named error, and no override flag
    exists to get around it."""
    with pytest.raises(ProdDevAuthModeError):
        Settings(
            _env_file=None,
            forge_env="prod",
            database_url="postgresql://x/y",
            auth_mode="dev",
            **OIDC_CONFIG,
        )


def test_invalid_auth_mode_rejected():
    with pytest.raises(ValueError):
        Settings(_env_file=None, forge_env="dev", auth_mode="saml")


@pytest.mark.parametrize("missing", ["oidc_issuer", "oidc_client_id", "oidc_client_secret"])
def test_oidc_mode_without_required_setting_raises_at_startup(missing):
    """A missing value fails at startup,
    not at first login."""
    config = dict(OIDC_CONFIG)
    config[missing] = None
    with pytest.raises(OidcConfigMissingError) as exc_info:
        Settings(_env_file=None, forge_env="dev", auth_mode="oidc", **config)
    assert missing.upper() in str(exc_info.value)


def test_dev_mode_oidc_settings_are_optional():
    """Dev mode needs no IdP at all — that is what keeps the suite runnable."""
    s = Settings(_env_file=None, forge_env="dev")
    assert s.oidc_issuer is None
    assert s.session_idle_timeout_hours == 8.0
    assert s.session_absolute_max_days == 7.0
    assert s.bootstrap_admin_email_list == []
    # Same generate-if-unset precedent as `preview_signing_secret`.
    assert s.session_signing_secret


def test_bootstrap_admin_emails_parsed_as_lowercased_list():
    s = Settings(
        _env_file=None, forge_env="dev", bootstrap_admin_emails=" Ada@example.com , bob@x.io "
    )
    assert s.bootstrap_admin_email_list == ["ada@example.com", "bob@x.io"]


def test_oidc_scope_list_defaults():
    assert Settings(_env_file=None, forge_env="dev").oidc_scope_list == [
        "openid",
        "email",
        "profile",
    ]


# --- quota default + platform timezone --------------------


def test_quota_and_timezone_defaults():
    """the global default is 100.0 USD/month and
    is deliberately NOT mode-conditional — it is a placeholder chosen before
    any usage data existed, identical in dev and prod
    until real spend says otherwise."""
    dev = Settings(_env_file=None, forge_env="dev")
    prod = Settings(
        _env_file=None,
        forge_env="prod",
        database_url="postgresql://x/y",
        service_env_encryption_key=PROD_SERVICE_ENV_ENCRYPTION_KEY,
        **PROD_SIGNING_SECRETS,
        **PROD_PLATFORM_ORIGIN,
        **OIDC_CONFIG,
    )
    assert dev.quota_monthly_usd_default == 100.0
    assert prod.quota_monthly_usd_default == 100.0
    assert dev.platform_timezone == "UTC"
    assert dev.platform_tzinfo is not None


def test_quota_default_and_timezone_are_operator_overridable():
    s = Settings(
        _env_file=None, forge_env="dev", quota_monthly_usd_default=250.5,
        platform_timezone="Asia/Seoul",
    )
    assert s.quota_monthly_usd_default == 250.5
    assert s.platform_timezone == "Asia/Seoul"
    # An offset, not UTC — the whole point of the setting.
    assert s.platform_tzinfo.utcoffset(datetime(2026, 7, 1)) == timedelta(hours=9)


def test_an_unresolvable_platform_timezone_fails_at_startup():
    """A typo'd zone name would otherwise shift every rollup window silently,
    and the failure would surface as a wrong number rather than an error.

    pinned on message text, not just the exception type — the message
    must name the case that actually occurs (a stale venv missing `tzdata`)
    and the exact command that fixes it, so it cannot silently regress to
    the earlier "your config is wrong" wording."""
    with pytest.raises(PlatformTimezoneError) as exc_info:
        Settings(_env_file=None, forge_env="dev", platform_timezone="Mars/Olympus_Mons")
    message = str(exc_info.value)
    assert "tzdata" in message
    assert 'pip install -e ".[dev]"' in message


# --- auth mode: `local` (ADR 0022)


def test_auth_mode_accepts_local_explicitly_and_it_is_never_a_default():
    """Mode defaults are unchanged (`dev` -> dev,
    `prod` -> oidc) -- `local` is only ever set explicitly."""
    dev_default = Settings(_env_file=None, forge_env="dev")
    assert dev_default.auth_mode == "dev"
    prod_default = Settings(
        _env_file=None,
        forge_env="prod",
        database_url="postgresql://x/y",
        service_env_encryption_key=PROD_SERVICE_ENV_ENCRYPTION_KEY,
        **PROD_SIGNING_SECRETS,
        **PROD_PLATFORM_ORIGIN,
        **OIDC_CONFIG,
    )
    assert prod_default.auth_mode == "oidc"

    explicit_local = Settings(_env_file=None, forge_env="dev", auth_mode="local", **LOCAL_CONFIG)
    assert explicit_local.auth_mode == "local"
    assert explicit_local.local_accounts_enabled is True
    assert explicit_local.session_auth_enabled is True
    assert explicit_local.oidc_enabled is False


def test_capability_properties_across_all_three_modes():
    dev = Settings(_env_file=None, forge_env="dev")
    local = Settings(_env_file=None, forge_env="dev", auth_mode="local", **LOCAL_CONFIG)
    oidc = Settings(_env_file=None, forge_env="dev", auth_mode="oidc", **OIDC_CONFIG)

    assert (dev.local_accounts_enabled, dev.oidc_enabled, dev.session_auth_enabled) == (
        False,
        False,
        False,
    )
    assert (local.local_accounts_enabled, local.oidc_enabled, local.session_auth_enabled) == (
        True,
        False,
        True,
    )
    assert (oidc.local_accounts_enabled, oidc.oidc_enabled, oidc.session_auth_enabled) == (
        False,
        True,
        True,
    )


@pytest.mark.parametrize("email_count", [0, 2])
def test_local_mode_requires_exactly_one_bootstrap_admin_email(email_count):
    """Zero or several entries fails at
    startup, never at first login -- one named error covers both cases."""
    emails = ",".join(f"admin{i}@example.test" for i in range(email_count))
    with pytest.raises(LocalBootstrapAdminEmailCountError):
        Settings(
            _env_file=None,
            forge_env="dev",
            auth_mode="local",
            bootstrap_admin_emails=emails,
            bootstrap_admin_password="admin",
        )


def test_local_mode_with_exactly_one_bootstrap_admin_email_starts():
    s = Settings(_env_file=None, forge_env="dev", auth_mode="local", **LOCAL_CONFIG)
    assert s.bootstrap_admin_email_list == ["admin@example.test"]


def test_local_mode_requires_a_bootstrap_admin_password_even_in_dev():
    """Not scoped to prod: there is no login event in `local` mode for JIT
    creation to hang a password off of, so a missing value fails at startup
    regardless of FORGE_ENV."""
    with pytest.raises(LocalBootstrapAdminPasswordMissingError):
        Settings(
            _env_file=None,
            forge_env="dev",
            auth_mode="local",
            bootstrap_admin_emails="admin@example.test",
        )


@pytest.mark.parametrize(
    "bad_password",
    [None, "short-11c"],  # unset, and <12 chars -- deliberately not "admin";
    ids=["unset", "too-short"],  # that case is its own test below.
)
def test_prod_refuses_an_unset_or_short_bootstrap_password(bad_password):
    """ADR 0022 §5: no override flag.

    Each of these two inputs is only rejected by ITS OWN branch -- `None`
    fails the `not password` check (a length check on `None` would raise
    `TypeError`, not `ProdWeakBootstrapPasswordError`), and "short-11c" is
    not the documented default, so only the length check catches it. That
    is what makes asserting the exception type alone still a real pin for
    these two. The "equals the documented default" case does NOT have this
    property -- see the dedicated test below for why."""
    kwargs = {
        "_env_file": None,
        "forge_env": "prod",
        "database_url": "postgresql://x/y",
        "auth_mode": "local",
        "bootstrap_admin_emails": "admin@example.test",
        **OIDC_CONFIG,  # irrelevant to `local` mode, harmless if present
    }
    if bad_password is not None:
        kwargs["bootstrap_admin_password"] = bad_password
    with pytest.raises(ProdWeakBootstrapPasswordError):
        Settings(**kwargs)


def test_prod_refuses_the_documented_dev_default_bootstrap_password_by_name():
    """The case that needs its own test rather
    than a shared parameter: "admin" is 5 characters, so the length check
    (<12) already rejects it regardless of whether the "equals the
    documented default" branch exists at all -- asserting only
    `ProdWeakBootstrapPasswordError` here would pass either way, pinning
    nothing about this specific branch. Asserts on the message actually
    naming the default value instead, which only this branch produces (the
    length branch's message never mentions "admin" or "documented
    development default")."""
    with pytest.raises(ProdWeakBootstrapPasswordError) as exc_info:
        Settings(
            _env_file=None,
            forge_env="prod",
            database_url="postgresql://x/y",
            auth_mode="local",
            bootstrap_admin_emails="admin@example.test",
            bootstrap_admin_password="admin",
            **OIDC_CONFIG,
        )
    message = str(exc_info.value)
    assert "documented development default" in message
    assert "admin" in message


def test_a_compliant_prod_local_mode_config_starts():
    s = Settings(
        _env_file=None,
        forge_env="prod",
        database_url="postgresql://x/y",
        auth_mode="local",
        bootstrap_admin_emails="admin@example.test",
        bootstrap_admin_password="a-genuinely-strong-passphrase",
        service_env_encryption_key=PROD_SERVICE_ENV_ENCRYPTION_KEY,
        **PROD_SIGNING_SECRETS,
        **PROD_PLATFORM_ORIGIN,
    )
    assert s.auth_mode == "local"
    assert s.local_accounts_enabled is True


def test_local_login_attempt_limiting_defaults():
    s = Settings(_env_file=None, forge_env="dev", auth_mode="local", **LOCAL_CONFIG)
    assert s.local_login_max_attempts == 5
    assert s.local_login_lockout_minutes == 15.0


# --- the 72-byte limit -------


# Literals, not derived from `PASSWORD_MAX_BYTES`: the limit is 72 bytes of UTF-8 because that
# is what bcrypt reads, and a rename or a drift of the constant must not move both sides.
_KOREAN_24 = "가" * 24  # 72 bytes: the longest all-Korean password that fits
_KOREAN_25 = "가" * 25  # 75 bytes: the case the spec names, a 25-character Korean passphrase


@pytest.mark.parametrize(
    "password",
    ["a" * 73, _KOREAN_25],
    ids=["73-ascii-bytes", "25-korean-characters"],
)
@pytest.mark.parametrize(
    "mode_kwargs",
    [
        {"forge_env": "dev", "auth_mode": "local"},
        {"forge_env": "dev"},
        {
            "forge_env": "prod",
            "database_url": "postgresql://x/y",
            "auth_mode": "local",
            "service_env_encryption_key": PROD_SERVICE_ENV_ENCRYPTION_KEY,
            **PROD_SIGNING_SECRETS,
            **PROD_PLATFORM_ORIGIN,
        },
    ],
    ids=["dev-local", "dev-no-local", "prod-local"],
)
def test_a_bootstrap_password_over_72_bytes_is_refused_at_startup(password, mode_kwargs):
    """The hash function raises past 72 bytes, so an over-long BOOTSTRAP_ADMIN_PASSWORD would
    pass startup and fail on the first request that tried to create the row. Counted in bytes
    (the Korean case is 25 characters, well under any character limit), and refused whichever
    mode reads it -- a password the platform cannot hash is a misconfiguration either way."""
    with pytest.raises(BootstrapAdminPasswordTooLongError) as exc_info:
        Settings(
            _env_file=None,
            bootstrap_admin_emails="admin@example.test",
            bootstrap_admin_password=password,
            **mode_kwargs,
        )
    message = str(exc_info.value)
    assert "BOOTSTRAP_ADMIN_PASSWORD" in message
    assert "72" in message
    assert password not in message, "the refusal must not echo the value"


@pytest.mark.parametrize("password", ["a" * 72, _KOREAN_24], ids=["72-ascii-bytes", "24-korean-characters"])
def test_a_bootstrap_password_of_exactly_72_bytes_starts(password):
    s = Settings(
        _env_file=None,
        forge_env="dev",
        auth_mode="local",
        bootstrap_admin_emails="admin@example.test",
        bootstrap_admin_password=password,
    )
    assert s.bootstrap_admin_password == password


# --- SERVICE_ENV_ENCRYPTION_KEY -------------------


def test_service_env_encryption_key_unset_in_dev_generates_a_valid_fernet_key():
    """Same generate-if-unset precedent as PREVIEW_SIGNING_SECRET /
    SESSION_SIGNING_SECRET -- but the generated value additionally has to be
    a REAL Fernet key (constructing `Fernet()` validates the format), not
    just any random string, since the whole point of this one is that it can
    actually encrypt and decrypt."""
    s = Settings(_env_file=None, forge_env="dev")
    assert s.service_env_encryption_key
    assert s.service_env_encryption_key_configured is False

    fernet = Fernet(s.service_env_encryption_key.encode("ascii"))
    token = fernet.encrypt(b"probe")
    assert fernet.decrypt(token) == b"probe"


def test_service_env_encryption_key_unset_in_prod_raises_at_startup():
    """Unlike PREVIEW_SIGNING_SECRET/SESSION_SIGNING_SECRET, prod refuses to
    START on this one rather than degrading gracefully: a key that changes
    on restart makes every already-stored `service_env_json` value
    PERMANENTLY unreadable."""
    with pytest.raises(ProdServiceEnvEncryptionKeyError) as exc_info:
        Settings(
            _env_file=None,
            forge_env="prod",
            database_url="postgresql://x/y",
            **OIDC_CONFIG,
        )
    message = str(exc_info.value)
    assert "requires SERVICE_ENV_ENCRYPTION_KEY to be set explicitly" in message
    # `ProdServiceEnvEncryptionKeyError` covers a SECOND, unrelated prod
    # condition too (a stored value that fails to decrypt --
    # tests/test_deploy_service.py). Must not share a message phrase with
    # it (a shared phrase lets both conditions' tests pass
    # whether or not the second condition's own check exists).
    assert "could not be decrypted" not in message


def test_service_env_encryption_key_explicit_value_is_marked_configured():
    """Pins `service_env_encryption_key_configured` — the signal the
    Alembic migration's abort check reads
    (`app.services.service_env_crypto.encrypt_rows_for_migration`) — against
    an EXPLICIT value, as opposed to the generated-in-dev case above."""
    s = Settings(
        _env_file=None,
        forge_env="dev",
        service_env_encryption_key=PROD_SERVICE_ENV_ENCRYPTION_KEY,
    )
    assert s.service_env_encryption_key == PROD_SERVICE_ENV_ENCRYPTION_KEY
    assert s.service_env_encryption_key_configured is True


def test_three_generated_dev_secrets_are_pairwise_distinct():
    """The natural-generation case: PREVIEW_SIGNING_SECRET,
    SESSION_SIGNING_SECRET and SERVICE_ENV_ENCRYPTION_KEY are three
    independently generated values in dev, never the same secret wearing
    three names (the reasoning for SESSION_SIGNING_SECRET vs
    PREVIEW_SIGNING_SECRET, extended to the third)."""
    s = Settings(_env_file=None, forge_env="dev")
    values = {s.preview_signing_secret, s.session_signing_secret, s.service_env_encryption_key}
    assert len(values) == 3


def test_three_secrets_read_their_own_setting_not_a_sibling():
    """Criterion: "must not pass by luck" — in dev all three are generated,
    so "no two are equal" would stay green even if the implementation
    derived all three from a shared source, purely by the odds of random
    generation. Pins the MECHANISM instead: all three set EXPLICITLY to
    known, distinct literals. This goes red under a mutation that points
    `service_env_encryption_key` at `session_signing_secret` (or either
    other pairing) — the value read back would equal the WRONG literal, not
    just fail an equality-of-two-random-values check."""
    session_secret = "session-secret-literal-AAAAAAAAAAAAAAAAAAAA"
    preview_secret = "preview-secret-literal-BBBBBBBBBBBBBBBBBBBB"
    encryption_key = PROD_SERVICE_ENV_ENCRYPTION_KEY

    s = Settings(
        _env_file=None,
        forge_env="dev",
        session_signing_secret=session_secret,
        preview_signing_secret=preview_secret,
        service_env_encryption_key=encryption_key,
    )

    assert s.session_signing_secret == session_secret
    assert s.preview_signing_secret == preview_secret
    assert s.service_env_encryption_key == encryption_key
    assert (
        len({s.session_signing_secret, s.preview_signing_secret, s.service_env_encryption_key})
        == 3
    )


# --- `.env.example` documents every `Settings` field, and vice versa.
# "A test must not share a source of truth with the code it tests"
# is satisfied here by construction: neither side is
# hand-written. The forward set comes from `Settings.model_fields` (the code
# under test) and the reverse set comes from parsing `.env.example` (a
# document neither side controls) — a rename has to break one of the two.

ENV_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / ".env.example"

# Real env vars, read directly via `os.environ` by a container entry point
# (`proxy_server.py`: the dialect, the per-dialect key, the admin secret, the
# port and the upstream URL the LLM egress proxy container reads at its own
# startup) or injected
# as a generated service's own env at deploy time
# (`services/deploy.issue_service_env`: `FORGE_SERVICE_BASE_URL`) — never
# modeled as a `Settings` field, so the platform process itself never reads
# them via pydantic-settings and the parity check below cannot see them
# from either direction. Named explicitly, never by a pattern: a `FORGE_*`
# prefix match would also silently absorb a future real `Settings` field
# that happens to share the prefix (e.g. `FORGE_ENV` itself).
ENV_ONLY_ALLOWLIST = frozenset(
    {
        "FORGE_PROXY_ANTHROPIC_API_KEY",
        "FORGE_PROXY_OPENAI_API_KEY",
        "FORGE_PROXY_DIALECT",
        "FORGE_PROXY_ADMIN_SECRET",
        "FORGE_PROXY_PORT",
        "FORGE_PROXY_ADMIN_PORT",
        "FORGE_PROXY_UPSTREAM_BASE_URL",
        "FORGE_SERVICE_BASE_URL",
    }
)


# the one definition of "an entry", shared by the key parity below and by the Korean
# edition's drift check, so every file of this shape is read the same way. Group 1 is the comment
# marker (empty for a live line), 2 the key, 3 the rest of the line.
_ENV_ENTRY_LINE = re.compile(r"^(#?)[ \t]*([A-Z][A-Z0-9_]*)=(.*)$")


def _scan_env_entries(lines: list[str]) -> list[tuple[int, tuple[str, str, str]]]:
    """`(index into lines, (key, value, state))` for every entry, in order. `state` is `"live"` for
    `KEY=...` and `"commented"` for `# KEY=...`. The value is the text after the `=` up to an inline
    trailing comment (whitespace then `#`, as `.env` files read it), whitespace dropped, so the indented
    example in the `BUILD_CONTAINER_RUNTIME` block (`#   BUILD_CONTAINER_RUNTIME=runsc   # requires
    gVisor ...`) is the value `runsc` in every language. A key that appears twice (a commented example
    inside a comment block, then the real commented default) is two entries.

    One kind of line that matches the shape is not an entry: a sentence of the explanation that
    happens to wrap so that it STARTS with `KEY=value` (the English file has four, for example
    `# USE_SANDBOX_EXECUTOR=true are set (otherwise the placeholder executor`). A wrap point is an
    accident of one language and cannot be carried into a translation, so such a line is recognised by
    its shape: the value is more than one word and the line runs on into the next, non-blank comment
    line. A value of several words that is complete on its line (`OIDC_SCOPES=openid email profile`,
    followed by a blank line) is an entry."""
    found = []
    for index, line in enumerate(lines):
        match = _ENV_ENTRY_LINE.match(line)
        if match is None:
            continue
        marker, key, rest = match.groups()
        value = re.split(r"\s+#", rest, maxsplit=1)[0].strip()
        runs_on = index + 1 < len(lines) and lines[index + 1].strip() not in ("", "#")
        if re.search(r"\s", value) and runs_on:
            continue
        found.append((index, (key, value, "commented" if marker else "live")))
    return found


def _env_example_entries(path: Path) -> list[tuple[str, str, str]]:
    """`_scan_env_entries` over a file's entries, without the line numbers. Read fresh on every call,
    never cached at import time, so an edit to the file is visible to a test run without a process
    restart."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [entry for _index, entry in _scan_env_entries(lines)]


def _env_example_documented_keys() -> set[str]:
    """Every `KEY` `.env.example` documents, live (`KEY=...`) or
    commented-out (`# KEY=...`) — the file's house style is "document
    everything, activate nothing", so most entries are commented."""
    return {key for key, _value, _state in _env_example_entries(ENV_EXAMPLE_PATH)}


def test_env_example_documents_every_settings_field():
    """Forward direction: every `Settings` field has a `.env.example` entry
    (commented-out is fine — that is the file's intended house style, live
    is not required). Enumerated from `Settings.model_fields`, never a
    hand-written list, which would just be a second thing to forget — this
    is precisely how `policy_medium_tier_checks`/`policy_high_tier_checks`
    went undocumented for eleven task slices before this test existed."""
    documented = _env_example_documented_keys()
    missing = sorted(
        name.upper()
        for name in Settings.model_fields
        if name.upper() not in documented and name.upper() not in ENV_ONLY_ALLOWLIST
    )
    assert missing == []


def test_env_example_has_no_ghost_keys():
    """Reverse direction: every key `.env.example` documents maps back to a
    real `Settings` field (or the allowlist above). This is the failure the
    forward direction above cannot see: a renamed or deleted setting leaving
    a ghost key that operators keep setting and nothing reads."""
    field_names = {name.upper() for name in Settings.model_fields}
    ghosts = sorted(
        key
        for key in _env_example_documented_keys()
        if key not in field_names and key not in ENV_ONLY_ALLOWLIST
    )
    assert ghosts == []


# --- the Korean edition `.env.example.ko` carries the same entries as `.env.example`.
# A Korean reader who runs `copy .env.example.ko .env` must get exactly the configuration the
# English file gives, so everything that is not a comment sentence (key, value, order, and
# whether the line is live or commented out) is compared, and only the comment text may differ.
# The English file is authoritative; the check names the key that differs and says which file
# it is the Korean one that disagrees with.

ENV_EXAMPLE_KO_PATH = ENV_EXAMPLE_PATH.with_name(".env.example.ko")


def _env_example_drift(english: Path, korean: Path) -> list[str]:
    """Human-readable differences between two `.env.example`-shaped files, empty when they carry the
    same ordered `(key, value, state)` entries. A key added to or removed from one file is reported
    first and alone (the positions after it all shift, so comparing them would only repeat the one
    cause under other keys' names); once both files have the same keys the comparison is by position,
    which reports a changed value, a live/commented flip, or a reordering."""
    en = _env_example_entries(english)
    ko = _env_example_entries(korean)
    en_count = Counter(key for key, _value, _state in en)
    ko_count = Counter(key for key, _value, _state in ko)
    problems = [
        f"{key}: {en_count[key]} entries in {english.name}, {ko_count[key]} in {korean.name}"
        for key in sorted(en_count.keys() | ko_count.keys())
        if en_count[key] != ko_count[key]
    ]
    if problems:
        return problems
    for position, (e, k) in enumerate(zip(en, ko), start=1):
        if e[0] != k[0]:
            problems.append(f"entry {position}: {english.name} has {e[0]}, {korean.name} has {k[0]} (order differs)")
        elif e != k:
            problems.append(
                f"{e[0]} (entry {position}): {english.name} has {e[2]} {e[1]!r}, {korean.name} has {k[2]} {k[1]!r}"
            )
    return problems


def test_env_example_ko_has_the_same_entries_as_env_example():
    """The real pair. Not vacuous: the entries are read, there are at least as many as `Settings`
    has fields (the forward test makes each field one), and the live line every install
    depends on (FORGE_ENV has no default) is among them in both files."""
    english = _env_example_entries(ENV_EXAMPLE_PATH)
    korean = _env_example_entries(ENV_EXAMPLE_KO_PATH)
    assert len(english) >= len(Settings.model_fields)
    assert len(korean) >= len(Settings.model_fields)
    assert ("FORGE_ENV", "dev", "live") in english
    assert ("FORGE_ENV", "dev", "live") in korean
    assert _env_example_drift(ENV_EXAMPLE_PATH, ENV_EXAMPLE_KO_PATH) == []


def _drift_after(tmp_path: Path, mutate) -> list[str]:
    """Copy the real English file twice, apply `mutate(lines)` to the second copy (a list of its
    lines, edited in place), and report the drift. The mutations are chosen from the parsed entries
    rather than from key names typed here, so renaming a setting never breaks this file."""
    text = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    english = tmp_path / ".env.example"
    korean = tmp_path / ".env.example.ko"
    english.write_text(text, encoding="utf-8")
    lines = text.splitlines()
    mutate(lines)
    korean.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return _env_example_drift(english, korean)


def _single_commented_entry_with_value(lines: list[str]) -> tuple[int, str]:
    """The first commented entry whose key appears once in the file and whose value is not empty."""
    entries = _scan_env_entries(lines)
    counts = Counter(entry[0] for _index, entry in entries)
    for index, (key, value, state) in entries:
        if state == "commented" and value and counts[key] == 1:
            return index, key
    raise AssertionError("no commented entry with a value and a single occurrence to mutate")


def test_drift_check_is_clean_on_two_identical_files(tmp_path):
    assert _drift_after(tmp_path, lambda lines: None) == []


def test_drift_check_names_the_key_whose_default_was_changed(tmp_path):
    chosen = {}

    def mutate(lines):
        index, key = _single_commented_entry_with_value(lines)
        chosen["key"] = key
        lines[index] = lines[index] + "9"

    problems = _drift_after(tmp_path, mutate)
    assert len(problems) == 1
    assert chosen["key"] in problems[0]


def test_drift_check_names_the_key_deleted_from_one_file(tmp_path):
    chosen = {}

    def mutate(lines):
        index, key = _single_commented_entry_with_value(lines)
        chosen["key"] = key
        del lines[index]

    problems = _drift_after(tmp_path, mutate)
    assert len(problems) == 1
    assert chosen["key"] in problems[0]


def test_drift_check_names_the_key_added_to_one_file_only(tmp_path):
    probe = "FORGE_DRIFT_PROBE_ONLY_IN_THE_KOREAN_FILE"
    problems = _drift_after(tmp_path, lambda lines: lines.append(f"# {probe}=1"))
    assert len(problems) == 1
    assert probe in problems[0]


def test_drift_check_names_a_commented_key_that_was_uncommented(tmp_path):
    chosen = {}

    def mutate(lines):
        index, key = _single_commented_entry_with_value(lines)
        chosen["key"] = key
        lines[index] = re.sub(r"^#\s*", "", lines[index])

    problems = _drift_after(tmp_path, mutate)
    assert len(problems) == 1
    assert chosen["key"] in problems[0]
    assert "live" in problems[0] and "commented" in problems[0]


def test_drift_check_names_a_live_key_that_was_commented_out(tmp_path):
    chosen = {}

    def mutate(lines):
        index, (key, _value, _state) = next(
            (i, e) for i, e in _scan_env_entries(lines) if e[2] == "live"
        )
        chosen["key"] = key
        lines[index] = "# " + lines[index]

    problems = _drift_after(tmp_path, mutate)
    assert len(problems) == 1
    assert chosen["key"] in problems[0]


def test_drift_check_names_both_keys_when_two_entries_trade_places(tmp_path):
    chosen = {}

    def mutate(lines):
        (i, first), (j, second) = _scan_env_entries(lines)[:2]
        chosen["keys"] = (first[0], second[0])
        lines[i], lines[j] = lines[j], lines[i]

    problems = _drift_after(tmp_path, mutate)
    first_key, second_key = chosen["keys"]
    assert first_key != second_key
    assert problems and all("order differs" in p for p in problems)
    assert first_key in problems[0] and second_key in problems[0]


def test_drift_check_ignores_the_comment_text(tmp_path):
    """What a translation changes: every sentence of the explanation. It is not a difference the
    check may report, or the Korean edition could never differ from the English one. (A lone `#`
    line is kept: it is a paragraph break, which the entry parser reads.)"""

    def mutate(lines):
        entry_lines = {index for index, _entry in _scan_env_entries(lines)}
        for index, line in enumerate(lines):
            if index not in entry_lines and line.startswith("#") and line.strip() != "#":
                lines[index] = "# prose of the other language"

    assert _drift_after(tmp_path, mutate) == []


def test_drift_check_ignores_a_note_after_a_value(tmp_path):
    """The words after ` #` on an entry line are comment text too (the indented example in the
    `BUILD_CONTAINER_RUNTIME` block carries one). Planted on an entry whose next line is blank, where
    a parser that kept the note as part of the value would see a different value."""

    def mutate(lines):
        entries = _scan_env_entries(lines)
        index = next(
            i for i, _entry in entries if i + 1 < len(lines) and lines[i + 1].strip() == ""
        )
        lines[index] = lines[index] + "   # an inline note, in the other language"

    assert _drift_after(tmp_path, mutate) == []


def test_a_sentence_that_wraps_to_start_with_key_equals_is_not_an_entry():
    """The entry parser on literal text. The first line pair is the shape the English file has four
    times (a wrap that happens to start a line with `KEY=value`); a several-word value that is complete
    on its line, an indented example with a note, and a live line are entries."""
    lines = [
        "# the sandbox executor only activates when this AND",
        "# USE_SANDBOX_EXECUTOR=true are set (otherwise the placeholder executor",
        "# stays in use).",
        "# USE_SANDBOX_EXECUTOR=false",
        "",
        "# OIDC_SCOPES=openid email profile",
        "",
        "#   BUILD_CONTAINER_RUNTIME=runsc   # requires gVisor",
        "#                                   # in the daemon's config",
        "FORGE_ENV=dev",
    ]
    assert [entry for _index, entry in _scan_env_entries(lines)] == [
        ("USE_SANDBOX_EXECUTOR", "false", "commented"),
        ("OIDC_SCOPES", "openid email profile", "commented"),
        ("BUILD_CONTAINER_RUNTIME", "runsc", "commented"),
        ("FORGE_ENV", "dev", "live"),
    ]


# --- `.env.example`'s stated default for POLICY_MEDIUM_TIER_CHECKS
# must equal the code's actual default. The parity test above only
# checks that the KEY is documented (commented-out or not) — it says
# nothing about whether the documented VALUE matches, so a stale default in
# that comment would be silently wrong forever. Pin it directly.


def test_env_example_policy_medium_tier_checks_default_matches_code():
    text = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    match = re.search(r"(?m)^#\s*POLICY_MEDIUM_TIER_CHECKS=(\S+)\s*$", text)
    assert match is not None, "POLICY_MEDIUM_TIER_CHECKS not documented (commented) in .env.example"
    documented_default = match.group(1)
    code_default = Settings.model_fields["policy_medium_tier_checks"].default
    assert documented_default == code_default


# --- ADR 0023 §Data services: shared-Postgres provisioning config ---


def test_service_postgres_configured_is_false_when_unset():
    settings = Settings(_env_file=None)
    # A local first: `Settings(_env_file=None)` still reads the process environment, so a
    # failing assert written on the attribute would print whatever DSN the shell exported.
    dsn_is_unset = settings.service_postgres_admin_dsn is None
    assert dsn_is_unset, "SERVICE_POSTGRES_ADMIN_DSN reached this Settings from the environment"
    assert settings.service_postgres_configured is False


def test_service_postgres_configured_is_false_for_blank_string():
    """This exact hole has been paid for once already, with DATABASE_URL: a
    value that is present but blank/whitespace-only must read as NOT
    configured, not as a third state."""
    settings = Settings(_env_file=None, service_postgres_admin_dsn="   ")
    assert settings.service_postgres_configured is False


def test_service_postgres_configured_is_true_for_a_real_value():
    settings = Settings(
        _env_file=None,
        service_postgres_admin_dsn=FAKE_ADMIN_DSN,
    )
    assert settings.service_postgres_configured is True


def test_service_postgres_max_connections_default_is_pinned():
    """The "pin once, then let the rest read against the constant
    as intent" shape (see the POLICY_MEDIUM_TIER_CHECKS test above): a
    literal here, not the expression under test, so a change to either
    sizing INPUT (or the headroom) is visible rather than silently
    self-consistent."""
    settings = Settings(_env_file=None)
    assert settings.service_data_expected_services == 100
    assert settings.service_data_connections_per_service == 5
    assert settings.service_postgres_max_connections == 520  # 100 * 5 + 20 headroom


def test_service_postgres_max_connections_follows_its_inputs():
    settings = Settings(
        _env_file=None,
        service_data_expected_services=10,
        service_data_connections_per_service=3,
    )
    # Proves the headroom constant is ADDED, not itself the only thing
    # that changed -- a test that only varied one input could not tell
    # "follows the formula" apart from "always returns the headroom".
    assert settings.service_postgres_max_connections == 10 * 3 + 20


def test_service_postgres_admin_dsn_port_is_not_duplicated_as_a_second_setting():
    """(a5): the dev service Postgres's port appears in AT MOST ONE place outside `backend/tests/`
    and .env.example's own prose -- the admin DSN carries the loopback port end-to-end; it is
    never a second setting. The port is read from `dev.ps1` (`tests/dev_instance.py`), not
    spelled here, so this file carries no copy to go stale.

    The scope was widened: this check used to skip `backend/tests/` ("outside tests"), which is
    exactly where ten files spelled that port (eight with the dev admin role) as a "fake" DSN. The
    instance is the developer's live one, so a test that reached the network connected to it. Now
    `backend/tests/` may not name the port at all, nor the dev admin role in a loopback DSN
    (`conftest.py` alone builds one, for the throwaway container, with a variable port). A test
    that needs a DSN that must never connect uses `tests.fake_admin_dsn.FAKE_ADMIN_DSN`. The
    scan's own logic is exercised against planted trees in `test_dev_instance_guard.py`."""
    dev = load_dev_instance_or_none()
    if dev is None:
        pytest.skip("no dev.ps1 beside backend/ (a backend-only container): no dev instance to look for")
    port = re.compile(rf"(?<!\d){dev.port}(?!\d)")
    repo_root = Path(__file__).resolve().parents[2]
    backend_root = repo_root / "backend"

    hits = []
    for path in backend_root.rglob("*"):
        if not path.is_file() or path.suffix not in (".py", ".ps1", ".cfg", ".ini", ".toml"):
            continue
        rel_parts = path.relative_to(backend_root).parts
        if ".venv" in rel_parts or "tests" in rel_parts:
            continue
        if path.name == ".env.example":
            continue
        if port.search(path.read_text(encoding="utf-8")):
            hits.append(str(path.relative_to(repo_root)))

    dev_ps1 = repo_root / "dev.ps1"
    if dev_ps1.exists() and port.search(dev_ps1.read_text(encoding="utf-8")):
        hits.append(str(dev_ps1.relative_to(repo_root)))

    assert len(hits) <= 1, f"the dev instance's port appears in more than one place: {hits}"

    tests_dir = Path(__file__).resolve().parent
    in_tests = find_dev_instance_references(tests_dir, dev, role_allowed_in=["conftest.py"])
    assert in_tests == [], (
        "a test file names the dev service Postgres (the developer's live instance); use "
        f"tests.fake_admin_dsn.FAKE_ADMIN_DSN instead: {in_tests}"
    )
    # ... and a second spelling of a "fake" DSN is a finding whatever it points at: the guard test's own
    # planted examples are the one place such literals may sit.
    literals = find_literal_admin_dsns(tests_dir, allowed_in=["test_dev_instance_guard.py"])
    assert literals == [], f"use tests.fake_admin_dsn.FAKE_ADMIN_DSN, not a DSN literal: {literals}"


# --- ADR 0023 §Backup, §Amendment 9: scheduled backup config -----


def test_service_data_backup_settings_defaults():
    """(a1) The four new fields, with the working defaults the ADR's own
    table names."""
    settings = Settings(_env_file=None)
    assert settings.service_data_backup_enabled is True
    assert settings.service_data_backup_interval_hours == 24
    assert settings.service_data_backup_retention_days == 7
    assert settings.service_data_backup_dir == "backups/service-data"


def test_env_example_service_data_backup_defaults_match_code():
    """(a2) Pins the three settings the ADR names NUMBERS for against the
    code's actual default -- the key-presence parity tests above say
    nothing about whether the documented VALUE matches, so a stale default
    in that comment would be silently wrong forever. Shape matches
    `test_env_example_policy_medium_tier_checks_default_matches_code`."""
    text = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    checks = {
        "SERVICE_DATA_BACKUP_ENABLED": "service_data_backup_enabled",
        "SERVICE_DATA_BACKUP_INTERVAL_HOURS": "service_data_backup_interval_hours",
        "SERVICE_DATA_BACKUP_RETENTION_DAYS": "service_data_backup_retention_days",
    }
    for env_key, field_name in checks.items():
        match = re.search(rf"(?m)^#\s*{env_key}=(\S+)\s*$", text)
        assert match is not None, f"{env_key} not documented (commented) in .env.example"
        documented_default = match.group(1)
        code_default = Settings.model_fields[field_name].default
        assert documented_default.lower() == str(code_default).lower()


# --- idle-project sweep config ---


def test_project_lifecycle_zombie_scan_settings_defaults():
    """(e1) The three new fields, with the working defaults: enabled by default,
    90 days ("three months"),
    and an hourly in-process scan interval."""
    settings = Settings(_env_file=None)
    assert settings.project_lifecycle_zombie_scan_enabled is True
    assert settings.project_lifecycle_idle_threshold_days == 90
    assert settings.project_lifecycle_zombie_scan_interval_hours == 1


def test_env_example_project_lifecycle_zombie_scan_defaults_match_code():
    """(e3) Same shape as `test_env_example_service_data_backup_defaults_
    match_code` -- key presence alone (the two parity tests above) says
    nothing about whether the documented VALUE matches the code default."""
    text = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    checks = {
        "PROJECT_LIFECYCLE_ZOMBIE_SCAN_ENABLED": "project_lifecycle_zombie_scan_enabled",
        "PROJECT_LIFECYCLE_IDLE_THRESHOLD_DAYS": "project_lifecycle_idle_threshold_days",
        "PROJECT_LIFECYCLE_ZOMBIE_SCAN_INTERVAL_HOURS": "project_lifecycle_zombie_scan_interval_hours",
    }
    for env_key, field_name in checks.items():
        match = re.search(rf"(?m)^#\s*{env_key}=(\S+)\s*$", text)
        assert match is not None, f"{env_key} not documented (commented) in .env.example"
        documented_default = match.group(1)
        code_default = Settings.model_fields[field_name].default
        assert documented_default.lower() == str(code_default).lower()


def test_app_never_drops_a_database_or_a_role():
    """Nothing
    outside the two named exemptions below deletes a database or a role --
    ADR 0023: "never deleted implicitly" protects SERVICE data.

    Exactly two exemptions:
      - `services/preview_data.py` drops a PREVIEW's throwaway
        database+role -- a distinct, deliberately throwaway lifecycle
        (ADR 0023 §Amendment 2), removed automatically on every preview
        teardown trigger.
      - `services/service_data_deprovision.py` drops a
        SERVICE's own database+role -- reachable only through the
        admin-only `POST /projects/{id}/deprovision` action on a project
        that is already `ARCHIVED`. The staged zombie path is real
        (it archives automatically); nothing after archival
        happens without this explicit, audited operator action — the automatic
        path may stop and archive;
        it may not delete.

    The exemption narrows the scan by MODULE PATH, not by relaxing what
    counts as a hit -- and `service_data.py` remaining in the scanned set
    is asserted explicitly below, alongside a SECOND positive assertion
    (`service_data_backup.py`, which only ever deletes FILES) so an
    exemption that silently widens (a typo'd path matching nothing, or the
    wrong file excluded) shows up here rather than in a merged PR nobody
    re-checks. Demonstrated by hand (not a permanent test):
    planting a `DROP ROLE` string into `service_data.py`,
    confirming this test goes red, then reverting; then planting one into
    `service_data_deprovision.py` and confirming this test stays green --
    it is the one file allowed to carry it for SERVICE data.

    Parsed with `ast` rather than grepped, mirroring
    test_console.py's `test_the_configuration_endpoint_neither_serialises_
    nor_iterates_settings`: a docstring or comment EXPLAINING the absence
    of `DROP DATABASE`/`DROP ROLE` (both exempted modules' own module
    docstrings do, deliberately) is not a false positive -- only executable
    code (an f-string or literal actually passed to something SQL-shaped)
    would trip this."""
    import ast

    app_root = Path(__file__).resolve().parents[1] / "app"
    exempt_paths = {
        (app_root / "services" / "preview_data.py").resolve(),
        (app_root / "services" / "service_data_deprovision.py").resolve(),
    }
    for exempt_path in exempt_paths:
        assert exempt_path.is_file(), f"each exemption must name a real file, not a typo: {exempt_path}"

    hits = []
    scanned_paths = set()
    for path in app_root.rglob("*.py"):
        if path.resolve() in exempt_paths:
            continue
        scanned_paths.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # Bare string-literal STATEMENTS (module/class/function docstrings,
        # and any other stray string-as-a-statement) — these are prose, not
        # code a DROP could ever execute from. Collected by identity so the
        # second pass can exclude exactly these nodes and nothing else.
        docstring_node_ids = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in docstring_node_ids:
                continue
            text = node.value
            if "DROP DATABASE" in text or "DROP ROLE" in text:
                hits.append(f"{path.relative_to(app_root)}:{node.lineno}: {text!r}")
    assert hits == []
    service_data_path = app_root / "services" / "service_data.py"
    assert service_data_path in scanned_paths, (
        "service_data.py, which protects real SERVICE data, must stay in "
        "the scanned set -- it is not one of the two named exemptions"
    )
    service_data_backup_path = app_root / "services" / "service_data_backup.py"
    assert service_data_backup_path in scanned_paths, (
        "service_data_backup.py deletes FILES only and must stay in the "
        "scanned set too -- a second positive assertion so the exempt set "
        "cannot silently widen to 'everything under services/'"
    )


def test_app_never_imports_redis():
    """`app/` is the PLATFORM's own backend -- it
    manages a service's Redis container via the Docker SDK
    (`services/service_data.py`, `services/preview_data.py`) and never
    talks to Redis directly as a client. `redis` only joined the `dev`
    extra (pyproject.toml) so `test_cache_module_import_fails_when_redis_
    url_unset` -- which loads the TEMPLATE's own `cache.py` by path -- can
    reach its real assertion locally and on CI instead of skipping
    everywhere. A dev-only dependency is exactly the shape that would let a
    future `import redis` inside `app/` pass silently here and on CI, and
    fail only on a prod install, where that extra is never installed.

    Parsed with `ast` rather than grepped, mirroring
    `test_app_never_drops_a_database_or_a_role`: a docstring or comment
    naming `redis` in prose (this test's own does) is not a false
    positive -- only an actual `import`/`from ... import` statement trips
    this. Covers all three shapes `import redis`, `import redis.asyncio`,
    and `from redis import Redis` would each trip (see this task's Result
    for the by-hand demonstration planting each in turn)."""
    import ast

    app_root = Path(__file__).resolve().parents[1] / "app"

    def _names_redis(module_name: str | None) -> bool:
        return module_name is not None and (
            module_name == "redis" or module_name.startswith("redis.")
        )

    hits = []
    scanned_paths = set()
    for path in app_root.rglob("*.py"):
        scanned_paths.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _names_redis(alias.name):
                        hits.append(f"{path.relative_to(app_root)}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and _names_redis(node.module):
                hits.append(f"{path.relative_to(app_root)}:{node.lineno}: from {node.module} import ...")
    assert hits == []

    # Positive control: the scan actually walked real files rather than
    # silently finding nothing (the flat-scan finding, applied here
    # too) -- two files that are definitely part of `app/` and definitely
    # don't import redis.
    assert (app_root / "main.py") in scanned_paths
    assert (app_root / "services" / "service_data.py") in scanned_paths


def test_preview_manager_never_touches_the_service_env():
    """(d3): a preview never gets the SERVICE's env -- pinned as a scan
    over actual identifiers used in code (`ast.Name`/`ast.Attribute`
    nodes), mirroring the role-literal ban's style
    (test_rbac.py) rather than a grep, so a docstring mentioning
    `ensure_service_env` in prose (this module's own does, to explain the
    invariant) is not a false positive."""
    import ast

    path = Path(__file__).resolve().parents[1] / "app" / "services" / "preview_manager.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    identifiers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
    assert "ensure_service_env" not in identifiers
    assert "service_env_json" not in identifiers


# --- the schema paths a modify-type build may not change ---------------------


def test_protected_schema_paths_default_to_the_template_s_alembic_directory_and_config():
    assert Settings(_env_file=None).protected_schema_paths_list == ["app/migrations/", "app/alembic.ini"]


def test_protected_schema_paths_are_read_from_the_environment_and_trimmed(monkeypatch):
    monkeypatch.setenv("PROTECTED_SCHEMA_PATHS", " db/schema/ , db/alembic.ini ,")

    assert Settings(_env_file=None).protected_schema_paths_list == ["db/schema/", "db/alembic.ini"]
