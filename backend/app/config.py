import re
from datetime import tzinfo
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services.passwords import PASSWORD_MAX_BYTES

# the two values a
# prod bootstrap password is checked against. Named module-level constants
# (not inlined into `model_post_init`) so the rule reads as data, not magic
# numbers buried in a conditional.
_DEFAULT_DEV_BOOTSTRAP_PASSWORD = "admin"
_PROD_BOOTSTRAP_PASSWORD_MIN_LENGTH = 12

# ADR 0023 §Amendment 10 "Sizing, and where the number lives": fixed
# headroom added on top of `service_data_expected_services *
# service_data_connections_per_service` for the platform's own admin
# connections and live previews. A named constant, not a literal inside the
# `service_postgres_max_connections` expression below, so a test can pin the
# computed default and a change to either sizing INPUT stays visible instead
# of being absorbed into an unlabelled number.
_SERVICE_POSTGRES_CONNECTION_HEADROOM = 20


class ProdDatabaseUrlMissingError(RuntimeError):
    """Raised at startup when FORGE_ENV=prod and DATABASE_URL is unset,
    blank/whitespace-only (an operator who cleared the value did
    not set it — treated as the SAME condition as unset, same message, not
    a third diagnosis), or set explicitly to a sqlite: URL (the
    documented `copy .env.example .env` workflow used to ship DATABASE_URL
    uncommented, pointed at the dev SQLite file — the guard has to catch
    that value being *present*, not only *absent*). Prod must never run on
    SQLite or on nothing, however that "nothing" is spelled."""


class ProdDevAuthModeError(RuntimeError):
    """Raised at startup when FORGE_ENV=prod but AUTH_MODE=dev — the dev
    identity stand-in (X-Forge-User-Id header) must never be reachable in
    prod (ADR 0018 §6). Deliberately no override
    flag: a "temporary" auth bypass in prod outlives its reason."""


class OidcConfigMissingError(RuntimeError):
    """Raised at startup when AUTH_MODE=oidc but one of OIDC_ISSUER /
    OIDC_CLIENT_ID / OIDC_CLIENT_SECRET is unset. A missing value fails at
    startup, not at
    first login."""


class LocalBootstrapAdminEmailCountError(RuntimeError):
    """Raised at startup when AUTH_MODE=local but BOOTSTRAP_ADMIN_EMAILS does
    not hold exactly one entry. Reuses the existing setting rather than a
    second, near-identically-named one (one declaration site for who is the
    first admin); zero
    or several entries fails here, at startup, never at first login."""


class LocalBootstrapAdminPasswordMissingError(RuntimeError):
    """Raised at startup when AUTH_MODE=local but BOOTSTRAP_ADMIN_PASSWORD is
    unset. In `local` mode there is no login event for JIT creation to hang
    a password off of -- the bootstrap admin row (and its password) is
    created at startup, so a missing value has to fail here (ADR 0022 §4)."""


class ProdWeakBootstrapPasswordError(RuntimeError):
    """Raised at startup when FORGE_ENV=prod and AUTH_MODE=local but
    BOOTSTRAP_ADMIN_PASSWORD is unset, equals the documented dev default
    ("admin"), or is shorter than 12 characters. No override flag, matching the
    ADR 0018 §6
    precedent -- an intent to "change it before live" is exactly the kind
    that outlives the person who held it."""


class BootstrapAdminPasswordTooLongError(RuntimeError):
    """Raised at startup when BOOTSTRAP_ADMIN_PASSWORD is set to more than 72
    bytes of UTF-8. The hash
    function reads only the first 72 bytes and the pinned bcrypt raises past them,
    so without this check the value would pass startup and fail on the first
    request that tried to create the bootstrap row -- "at startup, not at first
    use". Checked whenever the value is set, in every mode: a password the
    platform cannot hash is a misconfiguration whichever mode reads it."""


class PlatformTimezoneError(RuntimeError):
    """Raised at startup when PLATFORM_TIMEZONE is not a resolvable IANA zone
    name. Month boundaries are
    evaluated in one configured timezone, so an unresolvable name has to fail
    here rather than silently shift every rollup window.

    the case that actually occurs in the field is not a typo'd zone
    name — it is a stale venv missing the `tzdata` package (`zoneinfo` ships
    no tz database of its own, and Windows, the documented dev platform,
    supplies none either). The message names that cause and the exact
    command that fixes it, not just "your config is wrong"."""


class ProdServiceEnvEncryptionKeyError(RuntimeError):
    """ONE exception type covers two distinct
    "prod, encryption of `Project.service_env_json` is broken" conditions,
    deliberately — same idiom as `ProdDatabaseUrlMissingError` above. What
    must NOT be shared between them is the message (a
    message written to also satisfy a sibling condition lets both tests
    pass whether or not the second condition's own check exists):

    (a) `SERVICE_ENV_ENCRYPTION_KEY` is unset at startup — or blank, which
        is the same condition (`Settings` reads a blank value as
        unset, see `_BLANK_IS_UNSET_SETTINGS`) — raised from
        `model_post_init` below. A key that changes on restart makes every
        already-stored value PERMANENTLY unreadable, so this fails here,
        at startup, never at the next deploy.
    (b) a stored value fails to decrypt with the current key or
        `SERVICE_ENV_ENCRYPTION_KEY_OLD` — raised from
        `services.deploy.ensure_service_env` at the point of use. Prod
        never treats this as "absent, re-issue": that would rotate a live
        service's credentials (its DB password) out from
        under it and break the service. The operation fails instead, and
        the stored row is left untouched.

    There is no override flag for either condition."""


class ForgeEnvNotSetError(RuntimeError):
    """Raised wherever a `Settings` is built while `FORGE_ENV` is unset or
    blank. There is no default: every prod
    guard keys off `FORGE_ENV=prod`, so a missing variable used to mean "dev"
    -- dev users seeded, an unauthenticated identity list, the
    `X-Forge-User-Id` header trusted. Blank is the same condition as unset
    — not a third diagnosis. The message says what to set."""


class ProdSigningSecretError(RuntimeError):
    """Raised at startup when FORGE_ENV=prod and `SESSION_SIGNING_SECRET` or
    `PREVIEW_SIGNING_SECRET` is unset, blank (the same condition: a blank
    secret is an unset one) or shorter than
    32 bytes in UTF-8. One type for both secrets and both conditions, with a
    DIFFERENT message per condition and per secret name (the
    `ProdDatabaseUrlMissingError` idiom): a per-process random key is a dev
    convenience that in prod logs everyone out on every restart and
    disagrees between the API and the worker, and a short key is guessable
    offline against any cookie a user already holds. No override flag."""


class CorsOriginsError(RuntimeError):
    """Raised at startup when `CORS_ORIGINS` holds a value the platform will
    not trust (ADR 0028 §Amendment 2026-10-17): `*`, `null`, anything that is
    not a bare `scheme://host[:port]` origin, or an origin whose host is
    `APPS_BASE_DOMAIN` or one of its subdomains -- i.e. a generated service.
    The list is both the browser CORS allowlist and the Fetch-Metadata write
    exemption, and Starlette answers `*` with credentials by echoing whatever
    origin asks. One type, a distinct message per refused shape. No override
    flag."""


class PlatformPublicUrlError(RuntimeError):
    """Raised at startup when `PLATFORM_PUBLIC_URL` is unusable:
    in prod, unset/blank or not `https`; in any mode, not a bare `scheme://host[:port]`
    origin (a path, query, fragment, credentials, a bad port or no host). Prod session
    and login cookies are `Secure` and `__Host-` prefixed, so a prod platform that is
    not served over TLS is a login that never works -- refused at startup instead. A
    distinct message per refused shape, naming the setting. No override flag."""


# a blank or whitespace-only value in
# any of these is read as UNSET (the same rule as for DATABASE_URL, which keeps its
# own handling below). pydantic-settings reads `SESSION_SIGNING_SECRET=` as the
# empty string, not as absent, and a signer given `""` happily HMACs with an
# empty key -- anyone could then mint a session for any user id. Every
# credential-valued setting is listed, not only the signing secrets, so that
# "blank means unset" is one rule rather than one fix per secret; the guard
# `tests/test_config_fail_safe.py::test_every_secret_looking_setting_reads_blank_as_unset`
# fails when a new `*_secret`/`*_key`/`*_token`/`*_password`/`*_dsn` field is
# added without joining this tuple (or the exemption list in that test).
_BLANK_IS_UNSET_SETTINGS = (
    "session_signing_secret",
    "preview_signing_secret",
    "service_env_encryption_key",
    "service_env_encryption_key_old",
    "anthropic_api_key",
    "openai_api_key",
    "github_token",
    "oidc_client_secret",
    "bootstrap_admin_password",
    "service_postgres_admin_dsn",
    "notify_webhook_url",
    "notify_smtp_password",
)

# in prod each signing secret is
# at least this many bytes (UTF-8).
_PROD_SIGNING_SECRET_MIN_BYTES = 32


def _require_prod_signing_secret(env_name: str, value: str | None) -> None:
    """Prod only: `value` (already None when blank) must be set and at least
    `_PROD_SIGNING_SECRET_MIN_BYTES` UTF-8 bytes, else `ProdSigningSecretError`
    naming `env_name`, with a different message for each of the two conditions.
    Never echoes the value."""
    if value is None:
        raise ProdSigningSecretError(
            f"FORGE_ENV=prod requires {env_name} to be set (blank counts as "
            "not set). A per-process random key logs everyone out on every "
            "restart and differs between the API server and the worker. "
            'Generate one with: python -c "import secrets; '
            'print(secrets.token_urlsafe(48))". There is no override flag.'
        )
    size = len(value.encode("utf-8"))
    if size < _PROD_SIGNING_SECRET_MIN_BYTES:
        raise ProdSigningSecretError(
            f"FORGE_ENV=prod requires {env_name} to be at least "
            f"{_PROD_SIGNING_SECRET_MIN_BYTES} bytes long (UTF-8); this one is "
            f"{size}. A shorter key is guessable offline against any cookie or "
            "link a user already holds. Generate one with: "
            'python -c "import secrets; print(secrets.token_urlsafe(48))". '
            "There is no override flag."
        )


def _check_cors_origin(origin: str, apps_base_domain: str) -> None:
    """Refuse one `CORS_ORIGINS` entry that is not a trustworthy bare origin
    (ADR 0028 §Amendment 2026-10-17). Raises `CorsOriginsError`; a distinct
    message per shape, so a test pins the check that fired and not merely that
    something did."""
    if "*" in origin:
        raise CorsOriginsError(
            f"CORS_ORIGINS contains {origin!r}: a wildcard is refused. Starlette answers "
            "`*` (with credentials on) by echoing whatever origin asks, which would let "
            "any page, including a generated service on the same site, read the "
            "platform API as the visitor. List each trusted origin explicitly."
        )
    if origin.lower() == "null":
        raise CorsOriginsError(
            "CORS_ORIGINS contains 'null': it is the origin a sandboxed document sends "
            "(every generated preview is one), so trusting it trusts generated code. "
            "List each trusted origin explicitly."
        )
    scheme, separator, rest = origin.partition("://")
    if not separator or scheme not in ("http", "https"):
        raise CorsOriginsError(
            f"CORS_ORIGINS contains {origin!r}, which is not a bare origin: it must start "
            "with http:// or https:// (lower case), as a browser's Origin header does."
        )
    if any(char in rest for char in "/?#"):
        raise CorsOriginsError(
            f"CORS_ORIGINS contains {origin!r}, which carries a path, query or fragment "
            "(a trailing slash counts). A browser's Origin header is only "
            "scheme://host[:port], so such a value would never match anything."
        )
    if "@" in rest or rest.endswith(":") or any(char.isspace() for char in rest):
        raise CorsOriginsError(
            f"CORS_ORIGINS contains {origin!r}, which is not a bare scheme://host[:port] "
            "origin (credentials, whitespace or an empty port in the authority)."
        )
    try:
        parts = urlsplit(origin)
        host = parts.hostname
        parts.port  # noqa: B018 - reading it validates the port; a bad one raises ValueError
    except ValueError as exc:
        raise CorsOriginsError(
            f"CORS_ORIGINS contains {origin!r}, which has an invalid port ({exc})."
        ) from exc
    if not host:
        raise CorsOriginsError(f"CORS_ORIGINS contains {origin!r}, which names no host.")

    # Generated services live at `{project_id}.{apps_base_domain}` and previews
    # at `preview--{project_id}.{apps_base_domain}`:
    # trusting any of them hands generated code the visitor's cookie against
    # the platform. Compared case-insensitively and without a trailing dot.
    apps_domain = apps_base_domain.strip().lower().rstrip(".")
    normalized_host = host.lower().rstrip(".")
    if apps_domain and (normalized_host == apps_domain or normalized_host.endswith("." + apps_domain)):
        raise CorsOriginsError(
            f"CORS_ORIGINS contains {origin!r}, whose host is APPS_BASE_DOMAIN "
            f"({apps_base_domain!r}) or a subdomain of it: generated services live there, "
            "and listing one would grant generated code the visitor's cookie against the "
            "platform API (ADR 0028). Never list a generated service's host."
        )


def _check_platform_public_url(value: str, *, prod: bool) -> None:
    """Refuse a `PLATFORM_PUBLIC_URL` that is not a bare origin (and, in prod, not
    `https`). Raises `PlatformPublicUrlError`, one message per shape."""
    scheme, separator, rest = value.partition("://")
    if not separator or scheme not in ("http", "https"):
        raise PlatformPublicUrlError(
            f"PLATFORM_PUBLIC_URL={value!r} is not an origin: it must start with http:// or "
            "https:// (lower case) and be scheme://host[:port]."
        )
    if any(char in rest for char in "/?#"):
        raise PlatformPublicUrlError(
            f"PLATFORM_PUBLIC_URL={value!r} carries a path, query or fragment (a trailing "
            "slash counts). It is an origin only: scheme://host[:port]."
        )
    if "@" in rest or rest.endswith(":") or any(char.isspace() for char in rest):
        raise PlatformPublicUrlError(
            f"PLATFORM_PUBLIC_URL={value!r} is not a bare scheme://host[:port] origin "
            "(credentials, whitespace or an empty port in the authority)."
        )
    try:
        parts = urlsplit(value)
        host = parts.hostname
        parts.port  # noqa: B018 - reading it validates the port; a bad one raises ValueError
    except ValueError as exc:
        raise PlatformPublicUrlError(
            f"PLATFORM_PUBLIC_URL={value!r} has an invalid port ({exc})."
        ) from exc
    if not host:
        raise PlatformPublicUrlError(f"PLATFORM_PUBLIC_URL={value!r} names no host.")
    if prod and scheme != "https":
        raise PlatformPublicUrlError(
            f"FORGE_ENV=prod requires PLATFORM_PUBLIC_URL to be an https:// origin; got "
            f"{value!r}. Prod session cookies are Secure and __Host- prefixed, so a "
            "platform served over plain http could never log anyone in. Terminate TLS in "
            "front of it. There is no override flag."
        )


def _blank_to_none(value: str | None) -> str | None:
    """A cleared value is unset; a set one is kept trimmed."""
    return None if value is None or not value.strip() else value.strip()


class NotifyConfigError(RuntimeError):
    """Raised at startup when an operator-notification setting is unusable:
    a webhook URL that is not https in prod (or not http/https at all), a half-configured
    SMTP channel, an unknown TLS mode, plain SMTP without TLS in prod, a recipient that is not an
    address. The message names the SETTING and never echoes its value: the webhook URL is a
    credential (a Slack incoming-webhook URL is its own bearer secret). No override flag."""


NOTIFY_SMTP_TLS_MODES = ("starttls", "ssl", "none")


def _check_notify_settings(cfg: "Settings") -> None:
    prod = cfg.forge_env == "prod"
    if cfg.notify_webhook_url is not None:
        parts = urlsplit(cfg.notify_webhook_url)  # does not raise on junk; judged below
        scheme = parts.scheme.lower()
        try:
            host = parts.hostname
            parts.port  # noqa: B018 - a bad port raises ValueError
        except ValueError:
            host = None
        if scheme not in ("http", "https") or not host:
            raise NotifyConfigError(
                "NOTIFY_WEBHOOK_URL must be an http:// or https:// URL with a host (the value is "
                "not shown: it is a credential)."
            )
        if prod and scheme != "https":
            raise NotifyConfigError(
                "FORGE_ENV=prod requires NOTIFY_WEBHOOK_URL to be an https:// URL (the value is "
                "not shown: it is a credential). There is no override flag."
            )
    if cfg.notify_smtp_tls not in NOTIFY_SMTP_TLS_MODES:
        raise NotifyConfigError(
            f"NOTIFY_SMTP_TLS must be one of {', '.join(NOTIFY_SMTP_TLS_MODES)}, got {cfg.notify_smtp_tls!r}."
        )
    if cfg.notify_smtp_host is None:
        return
    if prod and cfg.notify_smtp_tls == "none":
        raise NotifyConfigError(
            "FORGE_ENV=prod refuses NOTIFY_SMTP_TLS=none: plain SMTP would send the SMTP password and "
            "every notice in the clear. Use starttls or ssl. There is no override flag."
        )
    if not cfg.notify_smtp_from or "@" not in cfg.notify_smtp_from:
        raise NotifyConfigError("NOTIFY_SMTP_HOST is set, so NOTIFY_SMTP_FROM must be an email address.")
    recipients = cfg.notify_email_recipients
    if not recipients:
        raise NotifyConfigError("NOTIFY_SMTP_HOST is set, so NOTIFY_EMAIL_TO must list at least one address.")
    for address in [cfg.notify_smtp_from, *recipients]:
        if "@" not in address or any(char.isspace() or ord(char) < 32 or char in "<>," for char in address):
            raise NotifyConfigError(
                "NOTIFY_SMTP_FROM and NOTIFY_EMAIL_TO must be plain addresses (name@host), "
                "comma-separated, without spaces or angle brackets."
            )
    if bool(cfg.notify_smtp_username) != bool(cfg.notify_smtp_password):
        raise NotifyConfigError("NOTIFY_SMTP_USERNAME and NOTIFY_SMTP_PASSWORD must be set together or not at all.")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", hide_input_in_errors=True
    )
    # a credential-valued field is excluded from `repr` and `str`
    # (`Field(repr=False)` on each: the ten blank-is-unset settings in
    # `_BLANK_IS_UNSET_SETTINGS`, plus `database_url`, whose Postgres URL carries a
    # password). pytest prints `repr(obj)` in the `where` clause of any failing
    # `assert obj.x ...`, and a traceback or log line that formats the object prints
    # it too; the process-wide `settings` holds the operator's `.env`. `SecretStr`
    # was not used: ~30 call sites in `app/` and ~380 references in the tests read
    # these as plain `str` (signers, Fernet, the proxy launcher, the DSN parsers,
    # `monkeypatch.setattr(settings, ..., "literal")`), and `SecretStr("x") == "x"`
    # is False, so a missed call site would fail silently instead of loudly.
    # `tests/test_settings_never_print_credentials.py` pins the set and fails when a
    # credential-looking field is added without the redaction. This does NOT cover
    # an assert written directly on one of these attributes: a failing
    # `assert settings.<credential> is None` prints that operand (compute
    # a local first).
    # `hide_input_in_errors`: a `ValidationError` prints `input_value=<the value>`,
    # and a misspelled credential key in `.env` is an "extra input" error (this class
    # rejects unknown keys) that would carry the credential in the traceback.

    # single explicit switch for all environment differences.
    # Mode-conditional defaults live here only — never `if env == "dev"` in
    # business logic.
    # NO DEFAULT. This used to default
    # to "dev", and every prod guard keys off `forge_env == "prod"`, so an
    # install that merely forgot the variable started as dev: dev users
    # seeded, the identity list served unauthenticated, `X-Forge-User-Id`
    # accepted from anyone who could reach the port. Unset (or blank) now
    # raises `ForgeEnvNotSetError` from `model_post_init`, i.e. wherever a
    # `Settings` is built -- importing `app.config` builds the process-wide one,
    # so the API server, the worker, alembic and every command refuse alike.
    forge_env: str | None = None

    # Left unset by default; dev falls back to the SQLite file in
    # `database_url` below, prod requires this to be set explicitly.
    database_url: str | None = Field(default=None, repr=False)

    # Comma-separated trusted browser origins: the CORS allowlist AND the
    # Fetch-Metadata write exemption (ADR 0028 §Decision 3). ADR 0028
    # §Amendment 2026-10-17: validated at startup (`_check_cors_origin`,
    # `CorsOriginsError`) -- `*`, `null`, anything but a bare
    # `scheme://host[:port]`, and any origin whose host is `apps_base_domain`
    # or a subdomain of it are refused.
    cors_origins: str = "http://localhost:5173"

    # when set, the API process serves this built SPA directory
    # (`frontend/dist`) on its own origin. Unset (the default) serves no files at all. A relative
    # path resolves from the process working directory (`backend/`). Checked by `create_app`, not
    # here, so the worker and Alembic never need the directory (`services/spa_static.py`).
    frontend_dist_dir: str | None = None

    # global concurrent-build cap for the worker. Runtime-mode
    # default (dev 1, prod 2), operator-tunable via env var.
    build_concurrency: int | None = None

    # max build duration
    # (minutes) before the worker sweep fails a stale RUNNING build with
    # TIMEOUT. Runtime-mode default (dev 20, prod 30), operator-tunable.
    build_max_duration_minutes: int | None = None

    # How often (seconds) the worker polls for QUEUED builds and sweeps for
    # timed-out RUNNING builds.
    worker_poll_interval_seconds: float = 2.0

    # pinned builder image
    # every L3 sandbox container is created from. Digest-or-tag pinned by the
    # operator; never `:latest` implicitly.
    sandbox_image: str = "python:3.11-slim"

    # Runtime-mode defaults
    # (dev / prod), operator-tunable via env var.
    sandbox_memory_limit_mb: int | None = None
    sandbox_cpu_limit_cores: float | None = None
    sandbox_pids_limit: int = 256

    # the capped `json-file` log config every deploy and preview container is
    # created with, so a chatty service cannot fill the host whatever `daemon.json` says. Each
    # container keeps at most `container_log_max_files` files of `container_log_max_size` each.
    # The size is Docker's own spelling: digits and an optional k / m / g.
    container_log_max_size: str = "10m"
    container_log_max_files: int = Field(default=3, ge=1)

    @field_validator("container_log_max_size")
    @classmethod
    def _log_max_size_is_dockers_spelling(cls, value: str) -> str:
        if not re.fullmatch(r"[1-9][0-9]*[kKmMgG]?", value):
            raise ValueError(
                "CONTAINER_LOG_MAX_SIZE must be a positive number with an optional k, m or g "
                "(for example 10m); Docker rejects any other spelling at container start"
            )
        return value

    # the build
    # sandbox's disk cap (MiB; runtime-mode default dev 2048 / prod 5120). It is
    # enforced by `SandboxRunner` in two independent places -- a watchdog that
    # polls the container's writable-layer size every
    # `sandbox_disk_poll_interval_seconds` and kills the build over the cap, and
    # a harvest that stops reading the workspace archive at the same number of
    # bytes. It is NOT a `storage_opt`: the engine accepts that option and
    # ignores it on some hosts (measured on Docker Desktop), so it cannot be
    # what the cap rests on. The interval bounds the burst a hostile build can
    # write between two polls.
    # the SAME limit and the SAME interval also bound every live preview
    # (`services/preview_disk_watch.py`, one watcher thread in the worker): a
    # preview runs the same unvetted image for up to its TTL, and has no harvest
    # to fall back on, so the watcher is all that bounds its disk.
    sandbox_disk_limit_mb: int | None = None
    sandbox_disk_poll_interval_seconds: float = 1.0

    # Size cap (bytes) on harvested execution logs —
    # logs are truncated, never unbounded.
    sandbox_log_max_bytes: int = 1_000_000

    # Prefix for the isolated, default-deny (`internal=True`) Docker network
    # created fresh per sandbox run (ADR 0003 invariant 3) and destroyed
    # with the container. A per-run network (not one shared network) is
    # required so sandboxes cannot reach each other even when two builds
    # run concurrently — see SandboxRunner._create_run_network.
    sandbox_network_name: str = "forge-sandbox-net"

    # ADR 0003 §Residual risk re-accepted 2026-08-11: which container
    # runtime the L3 **build container** is created on (the Docker
    # `--runtime=` argument, e.g. `runsc` for gVisor), so measuring a stronger
    # runtime is a config change rather than a code change.
    #
    # Unset (the default) means the runtime argument is NOT PASSED AT ALL —
    # deliberately not the literal "runc": on a host configured with a
    # different `default-runtime`, passing "runc" would silently change which
    # runtime the build container gets. Unset therefore reproduces today's
    # behaviour byte-for-byte on every host.
    #
    # SCOPE: the L3 build container, and (ADR 0046 §Decision 2) the L3
    # preview container through `effective_preview_runtime` below.
    # `deploy_manager.py` (L4 service containers), the LLM proxy and the
    # service data containers do NOT read this setting and must not start
    # doing so as a side effect of a later change; they share only the
    # `sandbox_*` resource caps. That is also why this is named
    # `build_container_runtime` and not `sandbox_runtime`: the misreading to
    # avoid is "every sandbox_* value applies wherever the other sandbox_*
    # values do", which would promote human-approved L4 code onto a stronger
    # runtime (gVisor's syscall gaps would surface as production outages,
    # ADR 0046 §Alternatives). The scoping is pinned by tests, not just by
    # this comment (see tests/test_sandbox_runner_unit.py).
    build_container_runtime: str | None = None

    # ADR 0046 §Decision 2: the runtime the L3 **preview container**
    # is created on. Unset (the default) means "the build runtime's value"
    # (`effective_preview_runtime`), and when that is unset too, no `runtime`
    # argument is passed at all (the rule above). Set, it applies to
    # previews only, never to a build.
    preview_container_runtime: str | None = None

    # ADR 0014: LLM egress proxy. The real Anthropic API key lives
    # only here (proxy env), never in a sandbox container. Left unset by
    # default; the sandbox-backed executor is only selected when this is
    # configured (otherwise PlaceholderBuildExecutor stays the fallback).
    anthropic_api_key: str | None = Field(default=None, repr=False)

    # Anthropic API base URL the proxy forwards to. Overridable in tests to
    # point at a stub upstream instead of the real Anthropic API.
    anthropic_upstream_base_url: str = "https://api.anthropic.com"

    # ADR 0035: the OpenAI engine's real key and upstream, the counterparts of
    # the two settings above. Same rules: the key lives only here (proxy env),
    # never in a sandbox container and never in any console view.
    # Engine selection and the opt-in stay as they were.
    openai_api_key: str | None = Field(default=None, repr=False)
    openai_upstream_base_url: str = "https://api.openai.com"

    # ADR 0035: the rest of the OpenAI engine's environment values.
    # `openai_model` is the model the OpenAI runner is told to use (sent in the
    # task input and recorded on the build); it must have a row in the proxy's
    # price table or it is priced at the ceiling. `openai_agent_sandbox_image` is
    # the pinned image holding the OpenAI Agents SDK, built from
    # backend/docker/agent-sandbox-openai/Dockerfile.
    openai_model: str = "gpt-5-mini"
    openai_agent_sandbox_image: str = "forge-agent-sandbox-openai:latest"

    # ADR 0035 §Decision 5: which engine a build runs on, as an ENVIRONMENT
    # default (`anthropic` | `openai`). Unset means the ADR's rule: the only engine
    # whose key is configured, or `anthropic` when both are. The choice moves
    # into the database; `services/engines.resolve_engine` is the one place that
    # reads this, so that move changes its source and nothing else.
    agent_engine_default: Literal["anthropic", "openai"] | None = None

    @field_validator("agent_engine_default", mode="before")
    @classmethod
    def _blank_engine_default_is_unset(cls, value):
        # `AGENT_ENGINE_DEFAULT=` in a .env is "not chosen", not an invalid engine.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # Host/port the LLM egress proxy binds to inside L2. The sandbox reaches
    # it via the allowlisted network at this address (ADR 0014).
    llm_proxy_host: str = "127.0.0.1"
    llm_proxy_port: int = 8801

    # per-build token/cost budget
    # enforced by the proxy. Operator-overridable; mechanism is fixed by the
    # spec, this is only the initial default (mode-conditional: dev/prod
    # share one default for now — deliberately simple until slice-2 tuning).
    build_token_budget: int = 200_000
    build_cost_budget_usd: float = 5.0

    # How many times one build may repair its own output after the preview's
    # smoke check measured a failing route. Each is a new single-use sandbox run on the same
    # build, proxy token and budget (so the per-build budget and wall-clock cover all of them).
    # 0 turns the loop off. A value outside 0..3 refuses to start, like the other limits.
    build_repair_max_attempts: int = Field(default=1, ge=0, le=3)

    # The spend ceiling of one eval run
    # and the floor under which a further case is not started. The run budget is recorded on
    # the run when it starts; the floor is read by the worker each time it decides whether to
    # queue the next case. Both are environment values (restart to change).
    eval_run_budget_usd: float = 1.0
    eval_case_min_budget_usd: float = 0.05

    # pinned image for agent-run sandboxes (has the
    # agent runtime preinstalled), separate from the generic sandbox_image
    # used by the runner's own tests. Built from
    # backend/docker/agent-sandbox/Dockerfile.
    agent_sandbox_image: str = "forge-agent-sandbox:latest"

    # selects the sandbox-backed BuildExecutor instead of the
    # placeholder when a real key is configured. Explicit operator opt-in
    # (never implicit) so dev without a key stays on the placeholder.
    use_sandbox_executor: bool = False

    # git-host provider selection. "github" (github.com or
    # GitHub Enterprise via github_base_url) or "local" (bare
    # repositories on the host under local_git_root); the name is read by the
    # provider factory in git_provider.py — no other module branches on it.
    git_provider: str = "github"

    # where the local provider keeps one bare repository per project.
    # A relative path is taken inside the backend directory. This holds every
    # service's source: it is operator data that needs a backup, like the
    # platform DB. Gitignored.
    local_git_root: str = "local-git"

    # GitHub provider settings (app/services/github_provider.py). The token
    # lives only in these L2 settings — never logged, never written to a
    # sandbox container's environment. Left unset by default; publish.py
    # raises HARVEST_ERROR (surfaced, not swallowed) if a build tries to
    # publish without a token configured.
    github_token: str | None = Field(default=None, repr=False)
    github_org: str | None = None
    # https://api.github.com for github.com; https://<host>/api/v3 for
    # GitHub Enterprise Server.
    github_base_url: str = "https://api.github.com"

    # template-owned build/deploy
    # manifests the agent may not change. Restored from the pinned template
    # whatever the agent did, and reported as drift when the agent changed them
    # relative to the build's own input tree; image assembly always uses the pinned
    # copy from `template_skeleton_dir`. Comma separated, relative to the workspace
    # root. An entry ending in `/` is a prefix (every file under it, including new
    # ones); any other entry is one exact file.
    protected_paths: str = "Dockerfile,compose.yaml"

    # ADR 0009 §Amendment 2026-10-05: the schema paths of the one template
    # shape -- its Alembic directory and config. On a MODIFY-type build,
    # any file under them added, edited or deleted relative to the build's input
    # tree REFUSES the build (AGENT_ERROR); create-type builds are unaffected and
    # may add forward-only migrations (ADR 0023). Same entry syntax as
    # `protected_paths`: a trailing `/` makes an entry a prefix.
    protected_schema_paths: str = "app/migrations/,app/alembic.ini"

    # the one generic-webtool template's golden-path skeleton.
    # create-type builds stage their workspace from this directory; it also
    # supplies the pinned Dockerfile image assembly always builds from.
    # Relative paths are resolved against the backend/ package root.
    template_skeleton_dir: str = "templates/generic-webtool"

    # ADR 0016: TTL preview container. Initial default 60 minutes,
    # operator-tunable; not mode-conditional (same posture dev/prod for now
    # — tuning is deferred).
    preview_ttl_minutes: int = 60

    # Port the previewed app listens on inside its container (matches the
    # generic-webtool template's compose.yaml `ports: ["8000:8000"]`).
    # No host port is actually published (no
    # ingress beyond what serving the preview requires) — the platform
    # backend reaches this port via `docker exec` inside the preview
    # container itself (see services/preview_manager.py), the same
    # exec-based pattern proxy_container.py already uses for the LLM proxy's
    # admin API, because the preview container's network is `internal=True`
    # (default-deny egress, ADR 0003 invariants 2-4) and therefore has no
    # host-routable published port at all.
    preview_container_port: int = 8000

    # HMAC key signing preview access tokens (a signed,
    # expiring preview URL). Left unset by
    # default; in dev a random per-process key is generated at startup if
    # unset or blank — acceptable because tokens are short-lived (<=
    # preview_ttl_minutes). Prod
    # REQUIRES it, at least 32 bytes (`ProdSigningSecretError`), so a restart
    # doesn't invalidate live preview links and the API and the worker agree.
    #
    # DEV CAVEAT (measured): the WORKER issues a preview token (the
    # sandbox executor starts the preview) and the API process VERIFIES it
    # (`routers/preview_proxy.py`, `preview_auth.py`). With this unset each of
    # the two processes generates its own key, so a token the worker issued is
    # refused by the API ("invalid preview token signature") and the
    # platform-origin preview link answers 403. Set the same value for both
    # processes (backend/.env) to use previews in dev. The session secret and
    # the encryption key below have no such coupling: only the API signs and
    # verifies sessions, and only the worker reads or writes the stored env.
    preview_signing_secret: str | None = Field(default=None, repr=False)

    # which failing checks push a
    # build into the "medium" vs "high" tier (an unlisted check name that
    # flags is treated as "high" — fail closed on tier mapping too, never
    # silently "low"). Comma-separated check names; operator-tunable without
    # a code change, per the initial classification (operator-
    # tunable config, not code). Defaults match the policy table.
    # The defaults add `destructive_ddl` (the sixth check, ADR 0023 §Schema
    # migrations) to the medium default; `.env.example`'s commented default
    # must be kept equal to this string (tests/test_config.py pins it) — an
    # operator who already overrode this in their own `.env` gets
    # `destructive_ddl` at "high" (fail-closed, absent from both configured
    # lists) until they add it.
    policy_medium_tier_checks: str = "dependency_vulnerabilities,resource_limits,template_compliance,destructive_ddl"
    policy_high_tier_checks: str = "secrets_pii,egress_detection"

    # required human-approval
    # count per tier ("low" always auto-approves regardless of this config
    # — these two only govern "medium"/"high"). Initial default 1 for both;
    # operator-tunable so "raise high to 2-person approval" (ADR 0008) is a
    # config change, not code. `services/approval.py` reads these only at
    # ApprovalRequest *creation* time — the count is snapshotted onto the
    # row (`required_approvals`) so a later config change never reinterprets
    # an already-created request.
    policy_medium_approvals_required: int = 1
    policy_high_approvals_required: int = 1

    # L4 deployment container.
    # Health-gated switchover waits for GET /healthz -> 200 within this
    # window (seconds) before failing the deployment with HEALTH_TIMEOUT.
    # Reuses the same port convention as previews (preview_container_port) —
    # the template contract's health endpoint listens on the app's own port.
    deploy_health_check_timeout_seconds: float = 30.0
    # Prefix for the per-deployment, default-deny (`internal=True`) Docker
    # network an L4 service container attaches to (ADR 0002: mandatory
    # caps; no Docker socket). Dev-mode routing reuses the
    # platform-proxy mechanism (docker exec), so no host port is published
    # here either — Traefik labels/prod routing are handled separately.
    deploy_network_name: str = "forge-deploy-net"

    # ADR 0002, ADR 0016: prod-shape Traefik
    # routing. Mode-conditional default (off in dev, on in prod) — dev keeps
    # the platform-proxy mechanisms byte-for-byte unchanged
    # regardless of this flag's value; operator can still force it either
    # way. When enabled, deployed-service and preview containers additionally
    # carry Traefik Docker-provider labels, and the platform connects the
    # operator's Traefik container into each service's own private network
    # once that service is eligible for traffic (per-service networks —
    # the isolation-preserving flavor; L2 never calls a Traefik API, Docker
    # label + network ops only. See deploy_manager.py docstring for the
    # empirically-verified mechanism).
    traefik_enabled: bool | None = None
    # Base domain for stable per-project hostnames: a project's deployed
    # service is routed at `{project_id}.{apps_base_domain}` (spec's
    # `{project_slug}`; `Project.id` is the stable, DNS-safe identifier
    # already used everywhere else — no separate slug field exists or is
    # needed). Previews use `preview--{project_id}.{apps_base_domain}`.
    apps_base_domain: str = "apps.forge.local"
    # Name of the operator-provisioned Traefik container (ADR 0002: Traefik
    # is the prod router, TLS at Traefik — provisioning it is an operator
    # concern, out of scope here). The platform connects THIS container into
    # a service's private network after the health gate passes — that
    # kernel-level attach is the route flip. Verified empirically (see
    # tests/test_deploy_traefik_integration.py): Traefik's Docker provider
    # resolves backend IPs from labels/socket state at container-create
    # time and does NOT react to later `network connect` events on the
    # service container, so the connect-the-service-to-a-shared-network
    # flavor does not work — connecting Traefik itself needs no provider
    # refresh at all, and per-service networks also preserve
    # isolation (no shared bridge between services).
    traefik_container_name: str = "forge-traefik"
    # the port of the operator's loopback-only Prometheus metrics
    # entrypoint on the Traefik container. Unset (the default) means no traffic signal
    # under Traefik, and the idle sweep keeps exempting live services. No effect while
    # `traefik_enabled` is false. The worker reads it through `docker exec`; nothing is
    # published (`services/traefik_traffic.py`).
    traefik_metrics_port: int | None = Field(default=None, ge=1, le=65535)
    # Address (host:port) the platform's own preview-forward-auth
    # verification endpoint is reachable at FROM THE TRAEFIK CONTAINER —
    # i.e. where the forward-auth middleware sends
    # its verification request (prod: "guarded by
    # a forward-auth middleware that calls a platform verification
    # endpoint"). Single-host model (ADR 0002): defaults to the
    # `host.docker.internal` name for "the host this platform process runs
    # on". Docker Desktop injects that name into every container on its own;
    # a plain Linux Docker Engine does NOT (not even 20.10+ -- the engine only
    # offers the `host-gateway` keyword), so on Linux the operator-provisioned
    # Traefik container must be started with
    # `--add-host=host.docker.internal:host-gateway`, or ForwardAuth cannot
    # resolve this address and every guarded preview request gets a 500
    #. Operator overrides this
    # setting if the platform API runs inside its own container instead
    # (point it at that container's name/network address).
    traefik_forward_auth_host: str = "host.docker.internal:8000"

    # the Traefik entrypoint every
    # router label (deploy_manager.traefik_service_labels,
    # preview_manager.traefik_preview_labels) is written onto. Default "web"
    # reproduces today's (previously hardcoded) behaviour byte-for-byte — an
    # operator only needs to change this once they have configured a second,
    # TLS-terminating entrypoint on their own Traefik (conventionally named
    # "websecure") and want routers pointed at it.
    traefik_entrypoint: str = "web"
    # the Traefik certificate resolver name. Unset (the
    # default) means the router labels omit BOTH `…tls` and
    # `…tls.certresolver` entirely — not `tls=false`, a different and wrong
    # outcome — following the "unset sends no key at all" precedent
    # (`build_container_runtime`). Set this to the resolver name already
    # configured on the operator's own Traefik (e.g. an ACME resolver) to
    # turn TLS on for every router this platform writes, for both the
    # service router and the preview router.
    traefik_cert_resolver: str | None = None
    # scheme used to build the service base URL
    # (`services/deploy.ensure_service_env`), the preview URL, and the
    # preview forward-auth middleware address (`services/preview_manager.py`)
    # — every URL Traefik itself terminates for an external caller. Default
    # "http" reproduces today's plaintext-only behaviour byte-for-byte.
    # Setting this to "https" is meaningful only once TRAEFIK_CERT_RESOLVER
    # (or an operator-managed TLS entrypoint) actually terminates TLS at
    # TRAEFIK_ENTRYPOINT — this setting only says which scheme callers are
    # told to use, it does not configure TLS itself. NOT a call site: the
    # in-container loopback URL `PreviewManager.proxy`/`DeployManager._exec_http`
    # build to reach the app from *inside* its own container (`docker exec`)
    # — that app always serves plain HTTP regardless of what Traefik
    # terminates externally, so pointing it at "https" would break the
    # dev-mode proxy outright.
    service_url_scheme: str = "http"
    # the scheme of Traefik's forward-auth hop to the
    # platform API (`TRAEFIK_FORWARD_AUTH_HOST`). Deliberately NOT `service_url_scheme`:
    # that one says what callers are told about the services Traefik terminates TLS for,
    # while this hop is a Traefik-container-to-host-API call on the Docker bridge, where
    # the API serves plain HTTP. `SERVICE_URL_SCHEME=https` alone must never turn it
    # into https. Set to "https" only if the API itself terminates TLS at that address.
    traefik_forward_auth_scheme: str = "http"

    # the platform's own public origin
    # (`scheme://host[:port]`, no path). Prod: required and `https` (PlatformPublicUrlError
    # otherwise). Dev: optional, any scheme. In prod it is also the one source of the
    # OIDC callback URL when `OIDC_REDIRECT_URL` is unset -- never the request's own URL.
    platform_public_url: str | None = None

    # ADR 0018: which
    # identity mechanism `deps.get_current_user` resolves behind its single
    # seam. Mode-conditional default (dev -> "dev" header stand-in,
    # prod -> "oidc"); `FORGE_ENV=prod` + `AUTH_MODE=dev` is refused at
    # startup (ProdDevAuthModeError) and there is no override flag.
    auth_mode: str | None = None

    # ADR 0018 §1: the IdP is configuration, not code. Endpoints come from
    # `{oidc_issuer}/.well-known/openid-configuration` — nothing here (or in
    # services/oidc.py) is vendor-specific. All three are required when
    # `auth_mode` is "oidc" (OidcConfigMissingError at startup otherwise);
    # the client secret lives only in these L2 settings, is never logged and
    # never reaches the browser (the code exchange happens server-side).
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = Field(default=None, repr=False)
    oidc_scopes: str = "openid email profile"
    # Absolute URL of `routers/auth.oidc_callback`, as registered with the
    # IdP. Left unset by default: derived per-request from the callback
    # route's own URL, which is correct for a directly-served backend. Set
    # explicitly when the platform sits behind a TLS-terminating proxy
    # (ADR 0002: Traefik in prod), where the request's own scheme/host is
    # not the externally reachable one.
    oidc_redirect_url: str | None = None

    # idle expiry slides on
    # activity; the absolute cap is fixed at issue time and forces
    # re-authentication regardless of activity. Both operator-tunable.
    session_idle_timeout_hours: float = 8.0
    session_absolute_max_days: float = 7.0

    # HMAC key signing the stateless session cookie and the short-lived
    # login-state cookie (no session table).
    # Same generate-if-unset precedent as `preview_signing_secret`: unset or
    # BLANK -> a random per-process key, which is fine in dev (a restart just
    # forces re-login). A blank value used to be kept as "" and the
    # signer HMACed with an empty key, so anyone could mint a session for any
    # user id; it is now read as unset. Prod REQUIRES it, at least 32 bytes
    # (`ProdSigningSecretError`), and a DIFFERENT value than
    # `preview_signing_secret`: these two sign unrelated credentials.
    session_signing_secret: str | None = Field(default=None, repr=False)

    # Fernet key encrypting `Project.
    # service_env_json` at rest. Unset in dev -> generated per process,
    # same precedent as `preview_signing_secret`/`session_signing_secret`
    # above. UNLIKE those two, unset in prod does not just degrade gracefully
    # (a preview link or a session dies) — a key that changes on restart
    # makes every already-stored service secret PERMANENTLY unreadable, so
    # prod REFUSES TO START instead (`ProdServiceEnvEncryptionKeyError`,
    # no override flag; a blank value is unset). Deliberately a key of its OWN, shared with neither
    # sibling secret above — the reasoning for refusing to share
    # `preview_signing_secret` with the session key: one leak must not hand
    # over two primitives.
    service_env_encryption_key: str | None = Field(default=None, repr=False)

    # accepted for DECRYPTION
    # ONLY, never written to. A key change is: set this to the PREVIOUS
    # key -> set SERVICE_ENV_ENCRYPTION_KEY to the new one -> RUN
    # `python -m app.reencrypt_service_env` (idempotent,
    # re-encrypts every stored service env under the
    # current key and reports rows rewritten / already current / unreadable) ->
    # drop this, but ONLY after that command reports zero rows still under the
    # old key and zero it could not decrypt. Dropping it earlier makes every
    # deploy of a service whose env is still under the old key fail. No
    # service restart — the plaintext a service sees never changes, so
    # nothing redeploys.
    service_env_encryption_key_old: str | None = Field(default=None, repr=False)

    # ADR 0023 §Data services, §Amendment 6, §Amendment 10: the
    # shared, operator-provisioned Postgres instance every generated
    # service's database and role live on. Carries host, port, admin role,
    # password AND the admin maintenance database in ONE DSN — there is no
    # second setting holding the port (ADR 0023 §Amendment 10). Unset means
    # data-service provisioning is OFF: promotion to L4 is refused with
    # PROVISION_UNCONFIGURED rather than
    # deploying a service with no database — see `service_postgres_configured`
    # below, the one predicate both `services.deploy.confirm_deploy` and
    # `execute_deployment` check. `dev.ps1` starts this instance itself (ADR
    # 0023 §Amendment 8: none of the operator's reasons for standing one up
    # by hand — durability, tuning, an upgrade window — apply to a throwaway
    # dev box) and writes a complete generated DSN here when this key is
    # absent, published on loopback only (ADR 0023 §Amendment 10); it never overwrites
    # an existing value. In prod the operator runs their own instance — a
    # Docker container on this SAME daemon, addressed by container name
    # below (ADR 0023 §Amendment 6: a managed/cloud Postgres or a bare host process
    # cannot work here, since the platform reaches it by `network.connect()`,
    # which needs a container) — with a role that is `CREATEDB CREATEROLE`,
    # never `SUPERUSER` (ADR 0023 §Amendment 5).
    service_postgres_admin_dsn: str | None = Field(default=None, repr=False)

    # ADR 0023 §Amendment 6: the shared Postgres instance's container
    # NAME — not its endpoint, which lives in the DSN above (the same split
    # `TRAEFIK_CONTAINER_NAME`/`TRAEFIK_FORWARD_AUTH_HOST` already has). The
    # platform attaches this container to each deployment's network exactly
    # as it attaches Traefik (`deploy_manager.connect_to_traefik`'s pattern);
    # it never creates or removes this container itself — the operator runs
    # it (or `dev.ps1` does, in dev only, ADR 0023 §Amendment 8).
    service_postgres_container_name: str = "forge-service-postgres"

    # ADR 0023 §Amendment 8: the image `dev.ps1` runs the dev
    # service-Postgres from. Pinned, like every other image setting in this
    # file — never a floating tag chosen implicitly.
    service_postgres_image: str = "postgres:18-alpine"

    # ADR 0023 §Redis: the image the platform runs each project's OWN
    # Redis container from (`services/service_data.py`). Unlike the shared
    # Postgres, the platform owns this container's whole lifecycle in every
    # mode, not only dev — ADR 0023 §Amendment 8's second reason: a cache
    # costs nothing to recreate, so nothing forbids the platform from
    # automating it end-to-end the way it cannot for the database.
    service_redis_image: str = "redis:8-alpine"

    # ADR 0023 §Amendment 10 "Sizing, and where the number lives":
    # the two inputs `service_postgres_max_connections` below is computed
    # from. Config, not a comment, so the number a PROD operator sizes their
    # OWN instance's `max_connections` against is never buried in prose —
    # the admin console's read-only configuration view shows both.
    service_data_expected_services: int = 100
    service_data_connections_per_service: int = 5

    # ADR 0023 §Backup, §Amendment 9: scheduled Postgres dumps of
    # every service database, with retention -- Postgres only (Redis is a
    # cache by default, `--save ""`, so backing it up would imply a
    # durability guarantee the platform does not make; see
    # `app/services/service_data_backup.py`'s module docstring). Working
    # defaults per the ADR's own table; every one is operator-tunable.
    service_data_backup_enabled: bool = True
    service_data_backup_interval_hours: int = 24
    service_data_backup_retention_days: int = 7
    # ADR 0023 §Amendment 4: one subdirectory per project underneath this.
    # Resolved CWD-relative with plain `os.path`, the same way
    # `template_skeleton_dir` above is actually consumed. Not committable:
    # `service_data_backup.ensure_backup_dir` writes a `.gitignore`
    # containing `*` INSIDE this directory the first time it is created
    # (decision (e)), and the repository's own top-level
    # `.gitignore` gains a `backups/` entry -- the default path lands
    # inside the working tree, and a dump is a complete plaintext copy of
    # a service's data.
    service_data_backup_dir: str = "backups/service-data"

    # The zombie path: the automatic flag -> stop -> archive sweep over
    # idle projects. Working defaults, all operator-tunable, same
    # shape/style as the backup block above.
    project_lifecycle_zombie_scan_enabled: bool = True
    # "Default three months" --
    # this is that default, rendered as a literal day count (this codebase
    # has no calendar-month arithmetic anywhere else either; every other
    # duration-shaped setting in this file is a plain int too).
    # this window is judged against
    # `last_active_at`, which carries traffic only where the platform-origin
    # service proxy runs, i.e. when `traefik_enabled` is false. While Traefik
    # routes, a project with an ACTIVE deployment is exempt from the sweep
    # whatever this is set to; the admin console's configuration view says so.
    project_lifecycle_idle_threshold_days: int = 90
    # Unlike `service_data_backup_interval_hours`
    # (which gates a real `pg_dump`), an early re-scan here is free and
    # idempotent -- an already-`ARCHIVED` project is excluded by the
    # candidate query, so nothing double-archives. This interval exists
    # only to stop the sweep from retrying a FAILED stop (Docker
    # unreachable, a container already gone) on every worker tick; it is
    # read as an in-process deadline (`worker.sweep_idle_projects`'s own
    # `scan_state`), never a durable one -- a worker restart re-scanning
    # immediately is acceptable (early, never late).
    project_lifecycle_zombie_scan_interval_hours: int = 1

    # ADR 0023
    # §Amendment 2: how often `worker.sweep_orphaned_preview_stores` re-runs
    # its enumeration -- same reasoning as
    # `project_lifecycle_zombie_scan_interval_hours` immediately above, not
    # `service_data_backup_interval_hours`'s: a `pv_` store, once dropped,
    # simply stops being enumerated, so an early re-sweep after a worker
    # restart is free and idempotent, never a double-reclaim. A plain
    # in-process deadline (`worker.sweep_orphaned_preview_stores`'s own
    # `scan_state`), never a durable due-ness record.
    preview_orphan_sweep_interval_hours: int = 1

    # ADR 0037 §Decision 4: how often (seconds) the worker's label-driven reconciler
    # re-runs after its start-up pass. Each pass is two Docker list calls (filtered by this
    # installation's `forge.instance`) plus one database read per kind, and acts only when
    # something has drifted, so a short interval is cheap; it bounds how long an orphaned
    # sandbox / proxy / deployment container can outlive its row, and how long a stopped or
    # missing service Redis goes unnoticed. Must be greater than zero: 0 would reconcile on
    # every poll tick, and a negative value means nothing.
    reconcile_interval_seconds: float = Field(default=60.0, gt=0)

    # the unauthenticated readiness endpoint (`GET /api/ready`)
    # answers within this many seconds even when the database hangs (503 naming `database`).
    readiness_timeout_seconds: float = Field(default=3.0, gt=0)

    # how often (seconds) the worker measures free disk on the Docker
    # data root and the platform's data directories, and the two thresholds a path is judged
    # against. A path is BELOW_THRESHOLD when free space is under EITHER one; a path that cannot
    # be measured is UNKNOWN, never OK. Percent 0 / GB 0 switch that half of the test off.
    host_disk_check_interval_seconds: float = Field(default=300.0, gt=0)
    host_disk_min_free_percent: float = Field(default=10.0, ge=0, le=100)
    host_disk_min_free_gb: float = Field(default=5.0, ge=0)

    # the worker checks every ACTIVE service this often (seconds) from a
    # thread of its own (so a slow sweep never delays it): container present and running, restart
    # count not increased, and `/healthz` answering through `docker exec`. A service is UNHEALTHY
    # after `service_check_failure_threshold` consecutive failed checks and HEALTHY again after one
    # pass. `exec_timeout` bounds one service's whole check (a hung Docker call counts as a
    # `/healthz` timeout, not a stall), `cycle_timeout` bounds the whole pass (services not reached
    # keep their last result and are checked next cycle).
    service_check_interval_seconds: float = Field(default=60.0, gt=0)
    service_check_failure_threshold: int = Field(default=3, ge=1)
    service_check_exec_timeout_seconds: float = Field(default=10.0, gt=0)
    service_check_cycle_timeout_seconds: float = Field(default=120.0, gt=0)

    # operator notifications, two optional channels. A
    # channel exists when its setting is set. The webhook URL is CREDENTIAL-valued (a Slack
    # incoming-webhook URL is a bearer secret): `repr=False`, blank is unset, never printed or
    # logged. `https` is required in prod. Delivery is best-effort: bounded connect/read timeouts,
    # `notify_retry_count` retries after the first attempt (5xx and network errors only, never a
    # redirect), no response body logged, and a failure never fails the check that triggered it.
    notify_webhook_url: str | None = Field(default=None, repr=False)
    notify_webhook_connect_timeout_seconds: float = Field(default=3.0, gt=0)
    notify_webhook_read_timeout_seconds: float = Field(default=5.0, gt=0)
    notify_retry_count: int = Field(default=2, ge=0, le=5)
    notify_retry_backoff_seconds: float = Field(default=2.0, ge=0)
    # SMTP: configured when the host is set; then FROM and TO are required. TLS is `starttls`
    # (default), `ssl` (implicit TLS) or `none` (refused in prod). Certificates are verified.
    notify_smtp_host: str | None = None
    notify_smtp_port: int = Field(default=587, ge=1, le=65535)
    notify_smtp_tls: str = "starttls"
    notify_smtp_username: str | None = None
    notify_smtp_password: str | None = Field(default=None, repr=False)
    notify_smtp_from: str | None = None
    notify_email_to: str = ""
    notify_smtp_timeout_seconds: float = Field(default=10.0, gt=0)
    # Each (kind, subject) is announced once, again only after it recovered AND this many seconds
    # passed since the last announcement of it (a flapping service does not mail every flap).
    notify_cooldown_seconds: float = Field(default=3600.0, ge=0)
    # How often the API process asks whether the worker's lease went stale (a dead worker cannot
    # say so itself). The staleness rule is the lease's own (`worker_lease.STALE_AFTER_SECONDS`).
    notify_worker_check_interval_seconds: float = Field(default=15.0, gt=0)
    # The admin console's "send test message" is refused again within this many seconds.
    notify_test_min_interval_seconds: float = Field(default=30.0, ge=0)

    # ADR 0018 §5: comma-separated emails granted `admin` **only at User row
    # creation**, never on a later login (re-checking every login would
    # silently re-promote a deliberately demoted admin). Empty by default.
    # ADR 0022 §4: in `local` mode this same setting must hold
    # **exactly one** entry -- the bootstrap admin row is created at startup
    # (seed), not at first login, since nobody can authenticate before it
    # exists.
    bootstrap_admin_emails: str = ""

    # ADR 0022 §4: the
    # bootstrap admin's password in `local` mode. Applied only when the row
    # is first CREATED (rule 5) -- a later restart never resets a password
    # the admin has since changed, the same discipline as the email grant
    # above. Required whenever AUTH_MODE=local
    # (LocalBootstrapAdminPasswordMissingError otherwise); FORGE_ENV=prod
    # additionally refuses a default/weak value outright
    # (ProdWeakBootstrapPasswordError, spec rule 6) -- no override flag.
    bootstrap_admin_password: str | None = Field(default=None, repr=False)

    # ADR 0022 §10:
    # per-account consecutive-failure login lockout for `local` mode -- the
    # first mechanism on this platform with a guessable secret. Counted per
    # account on consecutive failures; a success resets the counter. Both
    # operator-tunable; defaults match the spec.
    local_login_max_attempts: int = 5
    local_login_lockout_minutes: float = 15.0

    # the global monthly
    # cost limit applied to every project and every user without an override.
    # A *default*, not a ceiling — exceeding it warns and never blocks, and
    # per-subject overrides live in the DB (nullable
    # `monthly_quota_usd` on `users`/`projects`) precisely because `.env`
    # cannot express one value per entity. Deliberately NOT mode-conditional:
    # a placeholder chosen before any usage data existed,
    # to be revisited once a month of real spend is visible.
    quota_monthly_usd_default: float = 100.0

    # the one timezone
    # calendar-month boundaries are evaluated in, so "this month" means the
    # same window for every user regardless of their location — never the
    # server's local time and never the requester's. An IANA zone name;
    # unresolvable values raise `PlatformTimezoneError` at startup.
    platform_timezone: str = "UTC"

    @field_validator(*_BLANK_IS_UNSET_SETTINGS, mode="before")
    @classmethod
    def _blank_secret_is_unset(cls, value):
        # `SESSION_SIGNING_SECRET=` in
        # an env file is "not set", not an empty key. Done here, once, at the
        # boundary, so everything below (the generate-if-unset branches, the
        # prod "required" checks, every consumer's `if not key`) sees the same
        # None it would for an absent variable. A non-blank value is kept
        # byte for byte: a secret is never stripped.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def model_post_init(self, __context) -> None:
        # First, before anything reads the mode. Blank is unset.
        if self.forge_env is None or not self.forge_env.strip():
            raise ForgeEnvNotSetError(
                "FORGE_ENV is not set. It has no default: set FORGE_ENV=dev for "
                "a development machine or FORGE_ENV=prod for a real install, in the "
                "process environment or in backend/.env (`.env.example` has FORGE_ENV=dev; "
                "dev.ps1 supplies it for the processes it starts)."
            )
        if self.forge_env not in ("dev", "prod"):
            raise ValueError(f"FORGE_ENV must be 'dev' or 'prod', got {self.forge_env!r}")

        # ADR 0028 §Amendment 2026-10-17: validated here, at startup,
        # for every process that builds a `Settings`; `cors_origin_list` is the
        # one parse both the CORS middleware and the Fetch-Metadata exemption
        # read, so what is checked is exactly what is trusted.
        for origin in self.cors_origin_list:
            _check_cors_origin(origin, self.apps_base_domain)

        # a blank/whitespace-only value is treated as the SAME
        # condition as unset, not a third diagnosis — an operator who
        # cleared the value (the natural half-step before pasting a real
        # DSN) did not "set" it. Same branch, same message, same dev
        # fallback; deliberately not a new elif with its own message.
        if self.database_url is None or not self.database_url.strip():
            if self.forge_env == "prod":
                raise ProdDatabaseUrlMissingError(
                    "FORGE_ENV=prod requires DATABASE_URL to be set explicitly "
                    "(prod never falls back to the dev SQLite file)."
                )
            self.database_url = "sqlite:///./forge_dev.db"
        elif self.forge_env == "prod" and self.database_url.strip().lower().startswith("sqlite"):
            # closes the gap the "unset" branch above cannot see —
            # `.env.example` ships DATABASE_URL uncommented at the dev SQLite
            # file, so `copy .env.example .env` + `FORGE_ENV=prod` used to
            # start cleanly on it. Message names the actual problem (SQLite
            # in prod), not "missing" — this is a *different* diagnosis from
            # the branch above, not a duplicate of it.
            raise ProdDatabaseUrlMissingError(
                f"FORGE_ENV=prod refuses a SQLite DATABASE_URL ({self.database_url!r}) "
                "— prod must run on a Postgres-compatible database, never SQLite, "
                "whether DATABASE_URL is unset or set to a sqlite: URL by hand. "
                "There is no override flag."
            )

        if self.build_concurrency is None:
            self.build_concurrency = 2 if self.forge_env == "prod" else 1

        if self.build_max_duration_minutes is None:
            self.build_max_duration_minutes = 30 if self.forge_env == "prod" else 20

        if self.sandbox_memory_limit_mb is None:
            self.sandbox_memory_limit_mb = 4096 if self.forge_env == "prod" else 2048

        if self.sandbox_cpu_limit_cores is None:
            self.sandbox_cpu_limit_cores = 2.0 if self.forge_env == "prod" else 1.0

        if self.sandbox_disk_limit_mb is None:
            self.sandbox_disk_limit_mb = 5120 if self.forge_env == "prod" else 2048

        if self.traefik_enabled is None:
            self.traefik_enabled = self.forge_env == "prod"

        # the auth-mode branch — resolved once here, at startup, so
        # nothing downstream branches on AUTH_MODE (the one consumer is the
        # `oidc_enabled` capability below).
        if self.auth_mode is None:
            self.auth_mode = "oidc" if self.forge_env == "prod" else "dev"

        if self.auth_mode not in ("dev", "local", "oidc"):
            raise ValueError(
                f"AUTH_MODE must be 'dev', 'local' or 'oidc', got {self.auth_mode!r}"
            )

        if self.forge_env == "prod" and self.auth_mode == "dev":
            raise ProdDevAuthModeError(
                "FORGE_ENV=prod cannot run with AUTH_MODE=dev — the dev identity "
                "header stand-in must never be reachable in prod. There is no "
                "override flag; configure OIDC or local accounts instead (ADR 0018 §6)."
            )

        if self.auth_mode == "oidc":
            missing = [
                name
                for name, value in (
                    ("OIDC_ISSUER", self.oidc_issuer),
                    ("OIDC_CLIENT_ID", self.oidc_client_id),
                    ("OIDC_CLIENT_SECRET", self.oidc_client_secret),
                )
                if not value
            ]
            if missing:
                raise OidcConfigMissingError(
                    f"AUTH_MODE=oidc requires {', '.join(missing)} to be set "
                    "(startup fails here rather than at first login)."
                )

        # counted in bytes (a
        # Korean syllable is three), and reported in bytes. Never echoes the value.
        if self.bootstrap_admin_password is not None:
            password_bytes = len(self.bootstrap_admin_password.encode("utf-8"))
            if password_bytes > PASSWORD_MAX_BYTES:
                raise BootstrapAdminPasswordTooLongError(
                    f"BOOTSTRAP_ADMIN_PASSWORD is {password_bytes} bytes in UTF-8; the most "
                    f"the password hash accepts is {PASSWORD_MAX_BYTES} (a Korean character is "
                    "3 bytes, so 24 of them). Choose a shorter one. There is no override flag."
                )

        # ADR 0022 §4-5.
        # Both checks below run at startup, never at first login -- in
        # `local` mode there is no login event before the row exists.
        if self.auth_mode == "local":
            email_count = len(self.bootstrap_admin_email_list)
            if email_count != 1:
                raise LocalBootstrapAdminEmailCountError(
                    "AUTH_MODE=local requires BOOTSTRAP_ADMIN_EMAILS to hold exactly "
                    f"one entry (one declaration site for 'who is the first admin'); "
                    f"got {email_count}."
                )

            if self.forge_env == "prod":
                # spec rule 6: unset, the documented dev default, or too
                # short are all refused -- so "unset" never falls through to
                # the weaker dev-mode check below on a prod config. Each
                # condition raises its OWN message naming which rule was
                # hit: they used to share one lumped message, which made
                # the "equals the dev default" case pass its test whether or
                # not that specific check existed (the default "admin" is 5
                # characters, so the length check alone already rejects it --
                # the branches overlap in BEHAVIOUR but must not overlap in
                # DIAGNOSIS). The dev-default case in particular is worth a
                # named message on its own: it is the one rejection this
                # slice can realistically produce in the field (this
                # project's own backend/.env carries BOOTSTRAP_ADMIN_
                # PASSWORD=admin today), and "you left the documented
                # development default in place" sends an operator to a
                # different fix than "your password is too short."
                password = self.bootstrap_admin_password
                if not password:
                    raise ProdWeakBootstrapPasswordError(
                        "FORGE_ENV=prod with AUTH_MODE=local requires "
                        "BOOTSTRAP_ADMIN_PASSWORD to be set. There is no override flag "
                        "(ADR 0018 §6 precedent)."
                    )
                if password == _DEFAULT_DEV_BOOTSTRAP_PASSWORD:
                    raise ProdWeakBootstrapPasswordError(
                        "FORGE_ENV=prod with AUTH_MODE=local refuses to start with the "
                        "documented development default BOOTSTRAP_ADMIN_PASSWORD "
                        f"({_DEFAULT_DEV_BOOTSTRAP_PASSWORD!r}) still in place -- change it "
                        "before running in prod. There is no override flag (ADR 0018 §6 "
                        "precedent)."
                    )
                if len(password) < _PROD_BOOTSTRAP_PASSWORD_MIN_LENGTH:
                    raise ProdWeakBootstrapPasswordError(
                        "FORGE_ENV=prod with AUTH_MODE=local requires "
                        "BOOTSTRAP_ADMIN_PASSWORD to be at least "
                        f"{_PROD_BOOTSTRAP_PASSWORD_MIN_LENGTH} characters long. There is no "
                        "override flag (ADR 0018 §6 precedent)."
                    )
            elif not self.bootstrap_admin_password:
                raise LocalBootstrapAdminPasswordMissingError(
                    "AUTH_MODE=local requires BOOTSTRAP_ADMIN_PASSWORD to be set — "
                    "there is no login event in local mode for JIT creation to hang "
                    "a password off of."
                )

        # Resolved once here so no rollup query can fail half-way through a
        # read on a typo'd zone name.
        try:
            ZoneInfo(self.platform_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise PlatformTimezoneError(
                f"PLATFORM_TIMEZONE={self.platform_timezone!r} is not a resolvable "
                "IANA time zone name. If this value looks correct (e.g. 'UTC', "
                "'Asia/Seoul'), the venv is most likely stale and missing the "
                "`tzdata` package — run `pip install -e \".[dev]\"` from backend/ "
                "to install it, then retry."
            ) from exc

        # Deliberately placed LAST: every check
        # above raises its own distinct exception for its own distinct
        # condition, and this one is independent of all of them (auth mode,
        # OIDC config, local bootstrap admin, timezone) — ordering among
        # independent invariants has no functional meaning (prod refuses to
        # start on ANY of them), so this sits after every check whose OWN
        # test constructs a prod `Settings()` that intentionally leaves
        # this key unset while probing a different condition.
        #
        # Captured BEFORE the generate-if-unset branch below mutates the
        # field: "configured" means an operator actually supplied a value
        # (env var, `.env` file, or an explicit constructor kwarg), as opposed
        # to this process generating one for itself. This is what lets the
        # Alembic migration (`migrations/env.py` constructs its OWN
        # `Settings()`) tell the two apart (see
        # `service_env_encryption_key_configured` below) — a distinction that
        # matters ONLY for this key, because unlike
        # `preview_signing_secret`/`session_signing_secret` its value is
        # durably baked into stored ciphertext a DIFFERENT process (the
        # app, at its own next start) has to be able to read back.
        # read from the VALUE, not from `model_fields_set` -- a blank
        # `SERVICE_ENV_ENCRYPTION_KEY=` is in the fields set but is not a key
        # (`_blank_secret_is_unset` has already made it None).
        self._service_env_encryption_key_configured = self.service_env_encryption_key is not None
        if self.service_env_encryption_key is None:
            if self.forge_env == "prod":
                raise ProdServiceEnvEncryptionKeyError(
                    "FORGE_ENV=prod requires SERVICE_ENV_ENCRYPTION_KEY to be "
                    "set explicitly (blank counts as not set) — a key that "
                    "changes on restart makes every already-stored "
                    "Project.service_env_json value PERMANENTLY unreadable, so "
                    "this must fail here, at startup, rather than at the next "
                    "deploy. There is no override flag."
                )
            from cryptography.fernet import Fernet

            self.service_env_encryption_key = Fernet.generate_key().decode("ascii")

        # the two signing
        # secrets. Dev: unset or blank (already None by here) means a random
        # per-process key, as before. Prod: required and at least 32 bytes.
        # Placed after the encryption-key check for the same reason that one
        # sits last: tests probing another prod condition build a prod
        # `Settings()` without these, and must keep failing on THEIR condition.
        # Not shared between the two: one leak must not hand over two primitives.
        if self.forge_env == "prod":
            _require_prod_signing_secret("SESSION_SIGNING_SECRET", self.session_signing_secret)
            _require_prod_signing_secret("PREVIEW_SIGNING_SECRET", self.preview_signing_secret)
        else:
            import secrets

            if self.session_signing_secret is None:
                self.session_signing_secret = secrets.token_urlsafe(32)
            if self.preview_signing_secret is None:
                self.preview_signing_secret = secrets.token_urlsafe(32)

        # Last, for the same reason as the checks above.
        # Blank is unset, not a third diagnosis.
        if self.platform_public_url is not None and not self.platform_public_url.strip():
            self.platform_public_url = None
        if self.platform_public_url is None:
            if self.forge_env == "prod":
                raise PlatformPublicUrlError(
                    "FORGE_ENV=prod requires PLATFORM_PUBLIC_URL to be set to the "
                    "platform's public https:// origin (blank counts as not set), e.g. "
                    "https://forge.example.com. Prod cookies are Secure and __Host- "
                    "prefixed and the OIDC callback is derived from it. There is no override flag."
                )
        else:
            self.platform_public_url = self.platform_public_url.strip()
            _check_platform_public_url(self.platform_public_url, prod=self.forge_env == "prod")

        self.notify_smtp_host = _blank_to_none(self.notify_smtp_host)
        self.notify_smtp_username = _blank_to_none(self.notify_smtp_username)
        self.notify_smtp_from = _blank_to_none(self.notify_smtp_from)
        if self.notify_webhook_url is not None:
            self.notify_webhook_url = self.notify_webhook_url.strip()
        _check_notify_settings(self)

        if self.traefik_forward_auth_scheme not in ("http", "https"):
            raise ValueError(
                "TRAEFIK_FORWARD_AUTH_SCHEME must be 'http' or 'https', got "
                f"{self.traefik_forward_auth_scheme!r}"
            )

    @property
    def is_prod(self) -> bool:
        return self.forge_env == "prod"

    @property
    def effective_preview_runtime(self) -> str | None:
        """the runtime a preview container (and the preview disk-cap probe) is created
        on: `PREVIEW_CONTAINER_RUNTIME`, else the build runtime's value, else None -- and None
        means the `runtime` argument is not passed at all. Blank counts as unset."""
        for value in (self.preview_container_runtime, self.build_container_runtime):
            if value is not None and value.strip():
                return value.strip()
        return None

    @property
    def configured_container_runtimes(self) -> list[str]:
        """every distinct runtime name the platform is configured to ask the engine for
        (build, preview), for the worker's startup check. Empty when none is configured."""
        names: list[str] = []
        for value in (self.build_container_runtime, self.effective_preview_runtime):
            if value is not None and value.strip() and value.strip() not in names:
                names.append(value.strip())
        return names

    @property
    def oidc_enabled(self) -> bool:
        """The capability `deps.get_current_user` branches on — derived once
        from the mode, so no business logic reads `auth_mode` itself."""
        return self.auth_mode == "oidc"

    @property
    def local_accounts_enabled(self) -> bool:
        """the capability the signup / local-login endpoints and the
        admin signup-approval surface are conditionally registered behind
        (`main.create_app`) — never
        `auth_mode == "local"` outside this module."""
        return self.auth_mode == "local"

    @property
    def session_auth_enabled(self) -> bool:
        """True whenever `deps.get_current_user` resolves identity from the
        signed session cookie rather than the dev header stand-in — `oidc`
        and `local` both do (`local` reuses the `oidc` session
        representation verbatim).
        This is also what the dev switcher bootstrap's conditional
        registration reads, widened from "not oidc" to "not session-backed"
        """
        return self.oidc_enabled or self.local_accounts_enabled

    @property
    def platform_tzinfo(self) -> tzinfo:
        """The timezone `services/usage.resolve_month_window` computes month
        boundaries in. Validated in `model_post_init`, so this never raises."""
        return ZoneInfo(self.platform_timezone)

    @property
    def service_env_encryption_key_configured(self) -> bool:
        """True when `SERVICE_ENV_ENCRYPTION_KEY` was actually set
        (env var, `.env` file, or an explicit constructor kwarg) BEFORE
        `model_post_init` ran — as opposed to left unset and generated for
        THIS process only. The Alembic migration reads this: it
        constructs its own `Settings()` (`migrations/env.py`), a different
        process than the app that will read the rows back, so it must
        refuse to encrypt under a key it only just generated for itself —
        see `migrations/versions/*_encrypt_service_env_json.py`."""
        return self._service_env_encryption_key_configured

    @property
    def service_postgres_configured(self) -> bool:
        """ADR 0023 §Provisioning lifecycle, §Amendment 3: whether a
        USABLE admin DSN is configured — mirrors
        `service_env_encryption_key_configured`'s idiom. A blank or
        whitespace-only string is NOT configured (`DATABASE_URL` had this exact
        hole once already: an operator who cleared the
        value did not set it).

        Checked from TWO call sites on purpose — `services.deploy.
        confirm_deploy` (the operator-actionable 400 a user sees) AND
        `execute_deployment` (the worker runs in a SEPARATE process with its
        own `Settings`, so a config change after confirm but before the
        worker claims the row would otherwise be invisible to it; settings
        are restart-only, which makes that skew silent). One predicate, two
        callers — never re-derived at either call site."""
        return bool(self.service_postgres_admin_dsn and self.service_postgres_admin_dsn.strip())

    @property
    def service_postgres_max_connections(self) -> int:
        """ADR 0023 §Amendment 10 "Sizing": `max_connections` the DEV
        service-Postgres instance is started with (`dev.ps1`), and the
        number a PROD operator is told to size their OWN instance's
        `max_connections` against (the same
        two settings are what tells them which number to set). Computed
        once here, never a literal duplicated in a script — `dev.ps1` obtains
        it by calling this process's own venv Python (no arithmetic
        repeated in PowerShell)."""
        return (
            self.service_data_expected_services * self.service_data_connections_per_service
            + _SERVICE_POSTGRES_CONNECTION_HEADROOM
        )

    @property
    def notify_email_recipients(self) -> list[str]:
        return [a.strip() for a in self.notify_email_to.split(",") if a.strip()]

    @property
    def notify_webhook_configured(self) -> bool:
        return bool(self.notify_webhook_url)

    @property
    def notify_email_configured(self) -> bool:
        return bool(self.notify_smtp_host)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def protected_paths_list(self) -> list[str]:
        return [p.strip() for p in self.protected_paths.split(",") if p.strip()]

    @property
    def protected_schema_paths_list(self) -> list[str]:
        return [p.strip() for p in self.protected_schema_paths.split(",") if p.strip()]

    @property
    def policy_medium_tier_checks_list(self) -> list[str]:
        return [c.strip() for c in self.policy_medium_tier_checks.split(",") if c.strip()]

    @property
    def policy_high_tier_checks_list(self) -> list[str]:
        return [c.strip() for c in self.policy_high_tier_checks.split(",") if c.strip()]

    @property
    def oidc_scope_list(self) -> list[str]:
        return [s.strip() for s in self.oidc_scopes.replace(",", " ").split() if s.strip()]

    @property
    def bootstrap_admin_email_list(self) -> list[str]:
        """Lower-cased: email claims are compared case-insensitively."""
        return [
            e.strip().lower() for e in self.bootstrap_admin_emails.split(",") if e.strip()
        ]


settings = Settings()
