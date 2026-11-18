"""`python -m app.preflight`: check a host before the first start.

One line per check -- PASS, WARN or FAIL, and under a non-PASS line the fix -- and a non-zero exit
when any check FAILs. Run it from `backend/`, where `.env` lives, as the same user the units run as.

**Read-only.** It creates no container, network, volume, database or row. Everything it does to the
outside world is a read: `docker ping`/`info`/`inspect`, `SELECT` on the two Postgres/SQLite
databases, a read of the `alembic_version` table, and (for the selected engine) one `SELECT` on
`engine_selection`. A SQLite platform DB whose file is missing is reported, not opened, because
opening it would create it.

**It prints no credential.** Every line goes through `scrub` before it is written: the password in
any `scheme://user:password@host` is replaced, and so is every secret value the loaded settings
hold (any `Field(repr=False)` string). The checks also never put a DSN into a line unredacted
(`redact_dsn`), and print a driver's exception by its class name only; the messages they do print
are the platform's own (settings validation, the schema-at-head check).

Settings are loaded by `check_settings` itself, inside a `try`, never by an import: importing
`app.config` builds the process-wide `Settings` and would end this command with a traceback on
exactly the misconfiguration it exists to name. Everything that imports `app.config` (directly or
through `app.services`) is imported only after settings have loaded."""

from __future__ import annotations

import ipaddress
import os
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import unquote

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db_url import normalise_database_url
from app.schema_check import SchemaNotAtHeadError, assert_schema_at_head

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

# Docker's built-in default when `default-address-pools` is not configured: 172.17.0.0/16 up to
# 172.31.0.0/16 (15 networks of /16) and 192.168.0.0/16 split into /20s (16 networks). The PLAN's
# "about 30 networks" is this number.
DOCKER_BUILTIN_NETWORK_CAPACITY = 31

# The Docker host-gateway name Traefik must resolve to reach the platform's forward-auth endpoint.
HOST_GATEWAY_NAME = "host.docker.internal"

_CONNECT_TIMEOUT_SECONDS = 5
_MIN_SECRET_LENGTH = 4  # a shorter "secret" would scrub ordinary words out of every line
_URL_PASSWORD = re.compile(r"(?P<head>[A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@]*:)(?P<pw>[^/\s@]*)(?P<tail>@)")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str
    hint: str = ""


# -- redaction ----------------------------------------------------------------------------


def redact_dsn(dsn: str | None) -> str:
    """`dsn` with its password replaced by `***`. A string SQLAlchemy cannot parse is not echoed at
    all: a malformed DSN is the case where a password is most likely to be sitting in plain text."""
    if not dsn:
        return "<unset>"
    try:
        return make_url(dsn).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - see docstring; nothing about the input may leak
        return "<unparseable DSN>"


def secret_values(settings) -> list[str]:
    """Every credential value `settings` holds: each `str` field declared with `repr=False`
    (`Settings` marks all of its credentials that way), for a URL-shaped one, its password. Longest first, so a secret that contains another is scrubbed whole."""
    found: set[str] = set()
    for name, field in type(settings).model_fields.items():
        if field.repr is not False:
            continue
        value = getattr(settings, name, None)
        if not isinstance(value, str) or not value:
            continue
        if "://" in value:
            # A URL is not itself the secret (scrubbing it whole would blank the host and the path
            # the output exists to show); its password is, in both spellings.
            for match in _URL_PASSWORD.finditer(value):
                for candidate in (match.group("pw"), unquote(match.group("pw"))):
                    if len(candidate) >= _MIN_SECRET_LENGTH:
                        found.add(candidate)
        elif len(value) >= _MIN_SECRET_LENGTH:
            found.add(value)
    return sorted(found, key=len, reverse=True)


def scrub(text_: str, secrets: Iterable[str] = ()) -> str:
    """`text_` with every known secret value and every `scheme://user:password@` password replaced."""
    for secret in secrets:
        text_ = text_.replace(secret, "[redacted]")
    return _URL_PASSWORD.sub(lambda m: f"{m.group('head')}***{m.group('tail')}", text_)


def format_result(result: CheckResult) -> str:
    line = f"{result.status:<4}  {result.name}: {result.detail}"
    if result.hint:
        line += f"\n      fix: {result.hint}"
    return line


def _exc(exc: BaseException) -> str:
    """An exception as a platform-authored phrase: its class, never its message (a driver's message
    can carry the DSN it was given)."""
    return type(exc).__name__


# -- the checks ---------------------------------------------------------------------------


def check_settings(factory: Callable | None = None) -> tuple[CheckResult, object | None]:
    """Settings load under the current `FORGE_ENV`: the startup validation itself, not a copy of its
    rules. Returns the loaded `Settings` (or None) beside the result. A failure is reported by the
    exception's class and its message: those are platform-authored ("FORGE_ENV=prod requires ..."), and
    pydantic's `hide_input_in_errors` keeps the offending value out of a `ValidationError`."""
    name = "settings"
    try:
        if factory is None:
            from app.config import Settings

            factory = Settings
        loaded = factory()
    except Exception as exc:  # noqa: BLE001 - every startup refusal is the same finding here
        return (
            CheckResult(
                name,
                FAIL,
                f"settings did not load ({_exc(exc)}): {str(exc).strip()[:600]}",
                "fix the named setting in backend/.env or the process environment; the API server and the worker refuse to start on the same error",
            ),
            None,
        )
    return CheckResult(name, PASS, f"loaded under FORGE_ENV={loaded.forge_env}"), loaded


def _sqlite_file_missing(url) -> bool:
    database = url.database
    return bool(url.get_backend_name() == "sqlite" and database and database != ":memory:" and not os.path.exists(database))


def check_platform_db(settings, engine_factory: Callable = create_engine) -> tuple[CheckResult, object | None]:
    """The platform DB is reachable and exactly at the Alembic head (`schema_check.assert_schema_at_head`,
    the check the server runs at startup). Returns the engine it opened (caller disposes) for the
    engine-selection read, or None."""
    name = "platform database"
    raw = settings.database_url
    shown = redact_dsn(raw)
    if not raw or not raw.strip():
        return CheckResult(name, FAIL, "DATABASE_URL is not set", "set DATABASE_URL in backend/.env"), None
    try:
        url = make_url(normalise_database_url(raw))
    except Exception:  # noqa: BLE001
        return CheckResult(name, FAIL, "DATABASE_URL is not a valid URL", "correct DATABASE_URL in backend/.env"), None
    if _sqlite_file_missing(url):
        return (
            CheckResult(
                name,
                FAIL,
                f"{shown}: the SQLite file does not exist (preflight does not create it)",
                "run `alembic upgrade head` from backend/ to create the schema",
            ),
            None,
        )
    connect_args = {} if url.get_backend_name() == "sqlite" else {"connect_timeout": _CONNECT_TIMEOUT_SECONDS}
    engine = engine_factory(url, connect_args=connect_args)
    try:
        assert_schema_at_head(engine)
    except SchemaNotAtHeadError as exc:
        engine.dispose()
        return CheckResult(name, FAIL, f"{shown}: {str(exc)[:600]}", "see the remedy named in the message (alembic upgrade head, after a backup)"), None
    except Exception as exc:  # noqa: BLE001 - unreachable host, bad credentials, ...
        engine.dispose()
        return (
            CheckResult(name, FAIL, f"{shown}: could not connect or read ({_exc(exc)})", "check DATABASE_URL, that the database is up, and that the role may connect"),
            None,
        )
    return CheckResult(name, PASS, f"{shown} is reachable and at the migration head"), engine


def check_docker(client) -> CheckResult:
    name = "docker"
    try:
        client.ping()
        info = client.info()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name,
            FAIL,
            f"the Docker daemon is not reachable ({_exc(exc)})",
            "start Docker and make sure this user may use its socket (DOCKER_HOST, or membership of the docker group)",
        )
    return CheckResult(name, PASS, f"reachable (server {info.get('ServerVersion', 'unknown')})")


def check_traefik(client, settings) -> CheckResult:
    """When Traefik routing is on: the configured container exists, runs, and (if the forward-auth
    address uses the host-gateway name) can resolve it. Docker Desktop injects that name itself."""
    name = "traefik"
    if not settings.traefik_enabled:
        return CheckResult(name, PASS, "routing through Traefik is off (TRAEFIK_ENABLED=false); not checked")
    container_name = settings.traefik_container_name
    try:
        container = client.containers.get(container_name)
    except Exception as exc:  # noqa: BLE001 - NotFound or a daemon error: either way not usable
        return CheckResult(
            name,
            FAIL,
            f"container {container_name!r} was not found ({_exc(exc)})",
            "start the operator's Traefik container, or set TRAEFIK_CONTAINER_NAME to its name",
        )
    attrs = container.attrs or {}
    state = attrs.get("State") or {}
    if not (state.get("Running") or container.status == "running"):
        return CheckResult(
            name, FAIL, f"container {container_name!r} exists but is {container.status or 'not running'}", f"docker start {container_name}"
        )
    if not settings.traefik_forward_auth_host.startswith(f"{HOST_GATEWAY_NAME}:"):
        return CheckResult(name, PASS, f"container {container_name!r} is running (forward-auth host is not the host-gateway name)")
    extra_hosts = (attrs.get("HostConfig") or {}).get("ExtraHosts") or []
    if any(str(entry).startswith(f"{HOST_GATEWAY_NAME}:") for entry in extra_hosts):
        return CheckResult(name, PASS, f"container {container_name!r} is running with a {HOST_GATEWAY_NAME} host mapping")
    try:
        desktop = "Docker Desktop" in str(client.info().get("OperatingSystem", ""))
    except Exception:  # noqa: BLE001
        desktop = False
    if desktop:
        return CheckResult(name, PASS, f"container {container_name!r} is running; Docker Desktop provides {HOST_GATEWAY_NAME} itself")
    return CheckResult(
        name,
        FAIL,
        f"container {container_name!r} has no {HOST_GATEWAY_NAME} host mapping, so every preview's forward-auth call fails",
        f"recreate Traefik with --add-host={HOST_GATEWAY_NAME}:host-gateway (compose: extra_hosts), or point TRAEFIK_FORWARD_AUTH_HOST at an address it already reaches",
    )


def check_service_postgres(settings, engine_factory: Callable = create_engine) -> CheckResult:
    """The shared service Postgres answers to the admin DSN, and that role has `CREATEDB CREATEROLE`
    without being a superuser (ADR 0023). Not configured is FAIL in prod and WARN in dev: without it
    no service can be promoted."""
    name = "service postgres"
    if not settings.service_postgres_configured:
        status = FAIL if settings.is_prod else WARN
        return CheckResult(
            name,
            status,
            "SERVICE_POSTGRES_ADMIN_DSN is not set, so no service can be deployed",
            "stand up the shared Postgres container and set SERVICE_POSTGRES_ADMIN_DSN",
        )
    try:
        url = make_url(settings.service_postgres_admin_dsn).set(drivername="postgresql+psycopg")
    except Exception:  # noqa: BLE001
        return CheckResult(name, FAIL, "SERVICE_POSTGRES_ADMIN_DSN is not a valid URL", "correct SERVICE_POSTGRES_ADMIN_DSN in backend/.env")
    shown = url.render_as_string(hide_password=True)
    engine = engine_factory(url, connect_args={"connect_timeout": _CONNECT_TIMEOUT_SECONDS})
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT rolcreatedb, rolcreaterole, rolsuper FROM pg_roles WHERE rolname = current_user")
            ).one_or_none()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name,
            FAIL,
            f"{shown}: could not connect or read ({_exc(exc)})",
            "check the DSN, that the container is up, and that the loopback port it publishes matches",
        )
    finally:
        engine.dispose()
    if row is None:
        return CheckResult(name, FAIL, f"{shown}: the connected role has no pg_roles row", "connect as a real role")
    createdb, createrole, superuser = (bool(v) for v in row)
    missing = [attr for attr, ok in (("CREATEDB", createdb), ("CREATEROLE", createrole)) if not ok]
    if missing or superuser:
        problems = [f"lacks {' and '.join(missing)}"] if missing else []
        if superuser:
            problems.append("is a superuser (the platform must not hold one)")
        return CheckResult(
            name,
            FAIL,
            f"{shown}: the role {' and '.join(problems)}",
            "ALTER ROLE <admin> NOSUPERUSER CREATEDB CREATEROLE; keep the superuser password elsewhere",
        )
    return CheckResult(name, PASS, f"{shown} is reachable; the role has CREATEDB and CREATEROLE and is not a superuser")


def selected_engine(settings, db_engine) -> str:
    """The engine builds will use: the admin's stored selection when the platform DB can be read, else
    the environment default. One SELECT; nothing is written."""
    from app.models.engine_selection import SINGLETON_ID, EngineSelection
    from app.services.engines import ENGINES, _default_engine

    if db_engine is not None:
        try:
            with Session(db_engine) as db:
                row = db.get(EngineSelection, SINGLETON_ID)
            if row is not None and row.engine in ENGINES:
                return row.engine
        except Exception:  # noqa: BLE001 - fall back to the default; the DB check already reported its state
            pass
    return _default_engine(settings)


def check_images(client, settings, engine: str) -> CheckResult:
    """The images a build needs are on this host: the LLM proxy (and its advertised capabilities, via
    the worker's own `proxy_image_gap`), the agent sandbox image of the selected engine, and the service
    Redis image (created without a pull, so a fresh host must have it), and the generic
    `SANDBOX_IMAGE` the worker's disk-cap probe creates a container from without pulling."""
    from app.services.engines import engine_config, sandbox_executor_active
    from app.services.proxy_container import PROXY_IMAGE, proxy_image_gap

    name = "images"
    if not sandbox_executor_active(settings):
        return CheckResult(
            name,
            WARN,
            "the sandbox executor is not active (USE_SANDBOX_EXECUTOR unset, or no engine API key), so builds will not run; images not checked",
            "set USE_SANDBOX_EXECUTOR=true and an engine API key when you want builds",
        )
    problems: list[str] = []
    gap = proxy_image_gap(client, PROXY_IMAGE)
    if gap:
        problems.append(gap)
    sandbox_image = engine_config(engine, settings).image
    try:
        client.images.get(sandbox_image)
    except Exception:  # noqa: BLE001 - not there, or the daemon failed: either way a build cannot start
        problems.append(
            f"the agent sandbox image {sandbox_image!r} (engine {engine}) is not on this host; build it from backend/docker/ "
            f"(docker build -t {sandbox_image} -f docker/<engine>/Dockerfile .)"
        )
    # The platform creates each project's Redis with `containers.create`, which never pulls: a host
    # without the image fails the first deployment (PROVISION_FAILED) and silently drops the
    # preview. Read-only like the rest (`images.get`); the hint names the pull, preflight never runs it.
    redis_image = settings.service_redis_image
    try:
        client.images.get(redis_image)
    except Exception:  # noqa: BLE001
        problems.append(f"the service Redis image {redis_image!r} is not on this host (the platform never pulls); run `docker pull {redis_image}`")
    # the worker's disk-cap probe (`SandboxRunner.probe_disk_cap`) creates a container from
    # `settings.sandbox_image` without pulling it. It is present on a `build.sh` host only because
    # the agent images are built FROM that base; a host that lost it would see the probe report
    # "could not be verified" instead of a clear cause. Read-only (`images.get`).
    probe_image = settings.sandbox_image
    try:
        client.images.get(probe_image)
    except Exception:  # noqa: BLE001
        problems.append(f"the sandbox image {probe_image!r} (SANDBOX_IMAGE, used by the worker's disk-cap probe) is not on this host (the platform never pulls); run `docker pull {probe_image}`")
    if problems:
        return CheckResult(name, FAIL, "; ".join(problems), "build or pull the missing image(s) with the commands above")
    return CheckResult(name, PASS, f"{PROXY_IMAGE}, {sandbox_image} (engine {engine}), {redis_image} and {probe_image} are present")


def derive_network_capacity(pools) -> int:
    """How many networks the daemon's `default-address-pools` can hold: for each pool, `2 ** (size -
    base prefix length)`. None or an empty list is Docker's built-in default."""
    if not pools:
        return DOCKER_BUILTIN_NETWORK_CAPACITY
    total = 0
    for pool in pools:
        base = ipaddress.ip_network(pool["Base"], strict=False)
        size = int(pool["Size"])
        if size >= base.prefixlen:
            total += 2 ** (size - base.prefixlen)
    return total


def check_address_pools(client, settings) -> CheckResult:
    """Every deployed service gets a private network, so the pool must hold at least
    `SERVICE_DATA_EXPECTED_SERVICES` of them (plus the platform's own). WARN, not FAIL: a smaller
    pool works until the service count reaches it."""
    name = "docker address pools"
    try:
        pools = client.info().get("DefaultAddressPools")
        capacity = derive_network_capacity(pools)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, WARN, f"could not read the daemon's address pools ({_exc(exc)})", "check `docker info`")
    needed = settings.service_data_expected_services
    source = "configured default-address-pools" if pools else "Docker's built-in default (no default-address-pools configured)"
    if capacity < needed:
        return CheckResult(
            name,
            WARN,
            f"{source} hold {capacity} networks, below SERVICE_DATA_EXPECTED_SERVICES={needed}; deploys will fail once the pool runs out",
            'set "default-address-pools" in /etc/docker/daemon.json, e.g. [{"base":"10.64.0.0/12","size":24}] (4096 networks), then restart Docker',
        )
    return CheckResult(name, PASS, f"{source} hold {capacity} networks (>= SERVICE_DATA_EXPECTED_SERVICES={needed})")


def check_public_origin(settings) -> CheckResult:
    """`PLATFORM_PUBLIC_URL` is set and, in prod, an `https` origin. Judged by the startup
    rule itself (`config._check_platform_public_url`), not a copy of it."""
    from app.config import PlatformPublicUrlError, _check_platform_public_url

    name = "public origin"
    value = settings.platform_public_url
    if not value:
        if settings.is_prod:
            return CheckResult(name, FAIL, "PLATFORM_PUBLIC_URL is not set", "set PLATFORM_PUBLIC_URL=https://<host> in backend/.env")
        return CheckResult(
            name, WARN, "PLATFORM_PUBLIC_URL is not set (fine for dev; prod requires an https origin)", "set it before FORGE_ENV=prod"
        )
    try:
        _check_platform_public_url(value, prod=settings.is_prod)
    except PlatformPublicUrlError as exc:
        return CheckResult(name, FAIL, str(exc), "set PLATFORM_PUBLIC_URL to the https origin users reach, e.g. https://forge.example.com")
    return CheckResult(name, PASS, f"{value}")


# -- the run ------------------------------------------------------------------------------


def _skipped(name: str, why: str) -> CheckResult:
    return CheckResult(name, WARN, f"skipped: {why}")


def run_checks(settings_factory: Callable | None = None, docker_factory: Callable | None = None) -> tuple[list[CheckResult], list[str]]:
    """All checks, in order. Returns the results and the secret values to scrub from their output.
    A check whose prerequisite failed is reported as skipped, never run on a guess."""
    results: list[CheckResult] = []
    settings_result, settings = check_settings(settings_factory)
    results.append(settings_result)
    if settings is None:
        return results, []
    secrets = secret_values(settings)

    db_result, db_engine = check_platform_db(settings)
    results.append(db_result)

    client = None
    try:
        if docker_factory is None:
            import docker

            docker_factory = lambda: docker.from_env(timeout=10)  # noqa: E731
        client = docker_factory()
    except Exception:  # noqa: BLE001 - reported by check_docker below
        client = None
    docker_result = check_docker(client) if client is not None else CheckResult(
        "docker", FAIL, "the Docker client could not be created", "install Docker and set DOCKER_HOST if it is not on the default socket"
    )
    results.append(docker_result)
    docker_ok = docker_result.status == PASS

    results.append(check_traefik(client, settings) if docker_ok else _skipped("traefik", "Docker is not reachable"))
    results.append(check_service_postgres(settings))
    if docker_ok:
        results.append(check_images(client, settings, selected_engine(settings, db_engine)))
        results.append(check_address_pools(client, settings))
    else:
        results.append(_skipped("images", "Docker is not reachable"))
        results.append(_skipped("docker address pools", "Docker is not reachable"))
    results.append(check_public_origin(settings))
    if db_engine is not None:
        db_engine.dispose()
    return results, secrets


def render_report(results: list[CheckResult], secrets: Iterable[str] = ()) -> str:
    """The whole output, every line scrubbed. The only way a result reaches a terminal."""
    failed = sum(r.status == FAIL for r in results)
    warned = sum(r.status == WARN for r in results)
    lines = [format_result(r) for r in results]
    lines.append(f"preflight: {len(results) - failed - warned} passed, {warned} warning(s), {failed} failed")
    return scrub("\n".join(lines), list(secrets))


def main(argv: list[str] | None = None) -> int:
    results, secrets = run_checks()
    print(render_report(results, secrets))
    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
