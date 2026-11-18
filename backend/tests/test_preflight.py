"""`python -m app.preflight`. Each check is its own function with its
own tests, against a fake Docker client and fake engines; nothing here reaches a real daemon or
database (`.invalid` hosts, tmp SQLite files). Status words and messages are literals."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from docker.errors import ImageNotFound
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import preflight
from app.config import Settings
from app.models.engine_selection import EngineSelection
from app.schema_check import alembic_config

SECRET_PW_DB = "PlatformDbPw-7391xq"
SECRET_PW_SVC = "ServicePgPw-4482zk"
SECRET_KEY = "sk-ant-api-SecretKey-90210"

PROD = {
    "_env_file": None,
    "forge_env": "prod",
    "database_url": f"postgresql://forge:{SECRET_PW_DB}@platform-db.invalid:5432/forge",
    "oidc_issuer": "https://idp.example.test",
    "oidc_client_id": "forge-test",
    "oidc_client_secret": "not-a-real-secret",
    "service_env_encryption_key": "-6Q6OIXC_6ezyiOPJFDC0Qsv6tXNJNPatQb77E0Y2xY=",
    "session_signing_secret": "prod-session-secret-0123456789-abcdefghijklmnop",
    "preview_signing_secret": "prod-preview-secret-9876543210-ponmlkjihgfedcba",
    "platform_public_url": "https://forge.example.test",
}


def _settings(**overrides) -> Settings:
    return Settings(**{**PROD, **overrides})


def _dev_settings(db: Path, **overrides) -> Settings:
    """A dev-mode `Settings` on a SQLite file (prod refuses SQLite)."""
    return Settings(_env_file=None, forge_env="dev", database_url=f"sqlite:///{db.as_posix()}", **overrides)


# -- fakes -------------------------------------------------------------------------------


class FakeContainer:
    def __init__(self, *, running=True, extra_hosts=None):
        self.status = "running" if running else "exited"
        self.attrs = {"State": {"Running": running}, "HostConfig": {"ExtraHosts": extra_hosts}}


class FakeClient:
    """A Docker client that allows reads only: any attribute outside the allow-list raises, so a
    create/start/remove/prune call from the preflight cannot go unnoticed."""

    def __init__(self, *, info=None, containers=None, images=None, ping_error=None):
        self._info = {"ServerVersion": "29.0", "OperatingSystem": "Ubuntu 24.04", **(info or {})}
        self._ping_error = ping_error
        self.calls: list[str] = []
        self.containers = SimpleNamespace(get=self._get_container)
        self.images = SimpleNamespace(get=self._get_image)
        self._containers = containers or {}
        self._images = images or {}

    def ping(self):
        self.calls.append("ping")
        if self._ping_error:
            raise self._ping_error
        return True

    def info(self):
        self.calls.append("info")
        return self._info

    def _get_container(self, name):
        self.calls.append(f"containers.get:{name}")
        if name not in self._containers:
            raise LookupError("no such container")
        return self._containers[name]

    def _get_image(self, name):
        self.calls.append(f"images.get:{name}")
        if name not in self._images:
            raise ImageNotFound("no such image")
        return SimpleNamespace(labels=self._images[name])

    def __getattr__(self, name):  # only reached for what is not defined above
        raise AssertionError(f"preflight called docker client .{name}, which is not a read")


class FakeEngine:
    """Stands in for the service-Postgres engine: records every SQL string and answers the one role query."""

    def __init__(self, row=(True, True, False), error=None):
        self.row, self.error, self.sql, self.disposed = row, error, [], False

    def connect(self):
        if self.error:
            raise self.error
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement):
        self.sql.append(str(statement))
        return SimpleNamespace(one_or_none=lambda: self.row)

    def dispose(self):
        self.disposed = True


# -- settings ------------------------------------------------------------------------------


def test_settings_that_load_pass_and_come_back():
    result, loaded = preflight.check_settings(lambda: _settings())
    assert (result.status, loaded is not None) == ("PASS", True)
    assert "FORGE_ENV=prod" in result.detail


def test_settings_that_the_startup_validation_refuses_fail_with_its_own_message():
    """The rule is the real one (no `https` origin in prod), not a copy: the factory is `Settings`."""
    result, loaded = preflight.check_settings(lambda: _settings(platform_public_url="http://forge.example.test"))
    assert (result.status, loaded) == ("FAIL", None)
    assert "https" in result.detail


# -- platform database ----------------------------------------------------------------------


def _migrated_sqlite(tmp_path: Path) -> Path:
    db = tmp_path / "platform.db"
    command.upgrade(alembic_config(f"sqlite:///{db.as_posix()}"), "head")
    return db


def test_platform_db_at_head_passes(tmp_path):
    db = _migrated_sqlite(tmp_path)
    result, engine = preflight.check_platform_db(_dev_settings(db))
    assert result.status == "PASS"
    engine.dispose()


def test_platform_db_behind_head_fails_with_the_schema_checks_message(tmp_path):
    db = _migrated_sqlite(tmp_path)
    command.downgrade(alembic_config(f"sqlite:///{db.as_posix()}"), "-1")
    result, engine = preflight.check_platform_db(_dev_settings(db))
    assert (result.status, engine) == ("FAIL", None)
    assert "SCHEMA AT ANOTHER REVISION" in result.detail


def test_a_missing_sqlite_file_fails_and_is_not_created(tmp_path):
    missing = tmp_path / "nope.db"
    result, engine = preflight.check_platform_db(_dev_settings(missing))
    assert (result.status, engine) == ("FAIL", None)
    assert missing.exists() is False


def test_an_unreachable_platform_db_fails_without_the_password():
    result, engine = preflight.check_platform_db(
        _settings(database_url=f"postgresql://forge:{SECRET_PW_DB}@platform-db.invalid:5432/forge?connect_timeout=2")
    )
    assert (result.status, engine) == ("FAIL", None)
    out = preflight.format_result(result)
    assert SECRET_PW_DB not in out
    assert "forge:***@platform-db.invalid" in out


def test_reading_the_platform_db_changes_nothing(tmp_path):
    db = _migrated_sqlite(tmp_path)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    result, engine = preflight.check_platform_db(_dev_settings(db))
    engine.dispose()
    assert result.status == "PASS"
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


# -- docker --------------------------------------------------------------------------------


def test_docker_reachable_passes():
    assert preflight.check_docker(FakeClient()).status == "PASS"


def test_docker_unreachable_fails_with_a_fix():
    result = preflight.check_docker(FakeClient(ping_error=ConnectionError("refused")))
    assert (result.status, bool(result.hint)) == ("FAIL", True)


# -- traefik -------------------------------------------------------------------------------


def test_traefik_disabled_is_not_checked():
    client = FakeClient()
    result = preflight.check_traefik(client, _settings(traefik_enabled=False))
    assert (result.status, client.calls) == ("PASS", [])


def test_traefik_container_missing_fails():
    result = preflight.check_traefik(FakeClient(), _settings(traefik_container_name="forge-traefik"))
    assert result.status == "FAIL"
    assert "'forge-traefik'" in result.detail


def test_traefik_container_stopped_fails():
    client = FakeClient(containers={"forge-traefik": FakeContainer(running=False)})
    assert preflight.check_traefik(client, _settings()).status == "FAIL"


def test_traefik_running_with_the_host_gateway_mapping_passes():
    client = FakeClient(containers={"forge-traefik": FakeContainer(extra_hosts=["host.docker.internal:host-gateway"])})
    assert preflight.check_traefik(client, _settings()).status == "PASS"


def test_traefik_running_without_the_mapping_fails_on_linux_and_says_how_to_fix_it():
    client = FakeClient(containers={"forge-traefik": FakeContainer(extra_hosts=None)})
    result = preflight.check_traefik(client, _settings())
    assert result.status == "FAIL"
    assert "--add-host=host.docker.internal:host-gateway" in result.hint


def test_traefik_without_the_mapping_passes_on_docker_desktop():
    client = FakeClient(info={"OperatingSystem": "Docker Desktop"}, containers={"forge-traefik": FakeContainer()})
    assert preflight.check_traefik(client, _settings()).status == "PASS"


def test_traefik_without_the_mapping_passes_when_forward_auth_does_not_use_the_gateway_name():
    client = FakeClient(containers={"forge-traefik": FakeContainer()})
    result = preflight.check_traefik(client, _settings(traefik_forward_auth_host="172.17.0.1:8000"))
    assert result.status == "PASS"


# -- service postgres ----------------------------------------------------------------------

SVC_DSN = f"postgresql://forge_admin:{SECRET_PW_SVC}@fake-admin-postgres.invalid:1/postgres"


def test_service_postgres_with_the_right_role_passes_and_only_selects():
    engine = FakeEngine(row=(True, True, False))
    result = preflight.check_service_postgres(_settings(service_postgres_admin_dsn=SVC_DSN), lambda *a, **k: engine)
    assert result.status == "PASS"
    assert [sql.strip().split()[0].upper() for sql in engine.sql] == ["SELECT"]
    assert engine.disposed is True


@pytest.mark.parametrize(
    ("row", "named"),
    [((False, True, False), "CREATEDB"), ((True, False, False), "CREATEROLE"), ((True, True, True), "superuser")],
)
def test_service_postgres_role_with_the_wrong_attributes_fails(row, named):
    result = preflight.check_service_postgres(
        _settings(service_postgres_admin_dsn=SVC_DSN), lambda *a, **k: FakeEngine(row=row)
    )
    assert result.status == "FAIL"
    assert named in result.detail


def test_service_postgres_unreachable_fails_without_the_password():
    result = preflight.check_service_postgres(
        _settings(service_postgres_admin_dsn=SVC_DSN), lambda *a, **k: FakeEngine(error=ConnectionRefusedError(SVC_DSN))
    )
    out = preflight.format_result(result)
    assert result.status == "FAIL"
    assert SECRET_PW_SVC not in out


def test_service_postgres_unset_is_fail_in_prod_and_warn_in_dev():
    prod = preflight.check_service_postgres(_settings(service_postgres_admin_dsn=None), None)
    dev = preflight.check_service_postgres(Settings(_env_file=None, forge_env="dev", database_url="sqlite://"), None)
    assert (prod.status, dev.status) == ("FAIL", "WARN")


# -- images --------------------------------------------------------------------------------

EXECUTOR_ON = {"use_sandbox_executor": True, "anthropic_api_key": SECRET_KEY}
PROXY_OK = {"forge.proxy.capabilities": "drain"}
REDIS = "redis:8-alpine"
SANDBOX = "python:3.11-slim"  # the settings default; the worker's disk-cap probe creates from it


def test_images_present_pass():
    client = FakeClient(images={"forge-llm-proxy:latest": PROXY_OK, "forge-agent-sandbox:latest": {}, REDIS: {}, SANDBOX: {}})
    assert preflight.check_images(client, _settings(**EXECUTOR_ON), "anthropic").status == "PASS"


def test_a_missing_sandbox_image_for_the_selected_engine_fails_naming_it():
    client = FakeClient(images={"forge-llm-proxy:latest": PROXY_OK, REDIS: {}, SANDBOX: {}})
    result = preflight.check_images(client, _settings(**EXECUTOR_ON, openai_api_key="sk-openai-x1234"), "openai")
    assert result.status == "FAIL"
    assert "forge-agent-sandbox-openai:latest" in result.detail


def test_only_the_selected_engines_image_is_required():
    client = FakeClient(images={"forge-llm-proxy:latest": PROXY_OK, "forge-agent-sandbox:latest": {}, REDIS: {}, SANDBOX: {}})
    result = preflight.check_images(client, _settings(**EXECUTOR_ON, openai_api_key="sk-openai-x1234"), "anthropic")
    assert result.status == "PASS"


def test_a_missing_proxy_image_fails():
    client = FakeClient(images={"forge-agent-sandbox:latest": {}, REDIS: {}, SANDBOX: {}})
    result = preflight.check_images(client, _settings(**EXECUTOR_ON), "anthropic")
    assert result.status == "FAIL"
    assert "forge-llm-proxy:latest" in result.detail


def test_a_missing_service_redis_image_fails_naming_the_pull():
    client = FakeClient(images={"forge-llm-proxy:latest": PROXY_OK, "forge-agent-sandbox:latest": {}, SANDBOX: {}})
    result = preflight.check_images(client, _settings(**EXECUTOR_ON), "anthropic")
    assert result.status == "FAIL"
    assert "docker pull redis:8-alpine" in result.detail
    assert not any(c.startswith("images.pull") for c in client.calls)


def test_the_redis_image_checked_is_the_configured_one():
    client = FakeClient(images={"forge-llm-proxy:latest": PROXY_OK, "forge-agent-sandbox:latest": {}, "redis:7": {}, SANDBOX: {}})
    ok = preflight.check_images(client, _settings(**EXECUTOR_ON, service_redis_image="redis:7"), "anthropic")
    assert ok.status == "PASS"
    assert preflight.check_images(client, _settings(**EXECUTOR_ON), "anthropic").status == "FAIL"


def test_a_missing_generic_sandbox_image_fails_naming_it_and_the_pull():
    """the disk-cap probe creates a container from `SANDBOX_IMAGE` and never pulls it."""
    client = FakeClient(images={"forge-llm-proxy:latest": PROXY_OK, "forge-agent-sandbox:latest": {}, REDIS: {}})
    result = preflight.check_images(client, _settings(**EXECUTOR_ON), "anthropic")
    assert result.status == "FAIL"
    assert "docker pull python:3.11-slim" in result.detail
    assert not any(c.startswith("images.pull") for c in client.calls)


def test_the_generic_sandbox_image_checked_is_the_configured_one():
    client = FakeClient(images={"forge-llm-proxy:latest": PROXY_OK, "forge-agent-sandbox:latest": {}, REDIS: {}, "python:3.12-slim": {}})
    assert preflight.check_images(client, _settings(**EXECUTOR_ON, sandbox_image="python:3.12-slim"), "anthropic").status == "PASS"
    assert preflight.check_images(client, _settings(**EXECUTOR_ON), "anthropic").status == "FAIL"


def test_a_proxy_image_without_the_capability_label_fails():
    client = FakeClient(images={"forge-llm-proxy:latest": {}, "forge-agent-sandbox:latest": {}, REDIS: {}, SANDBOX: {}})
    assert preflight.check_images(client, _settings(**EXECUTOR_ON), "anthropic").status == "FAIL"


def test_images_are_not_checked_when_the_executor_is_off():
    client = FakeClient()
    result = preflight.check_images(client, _settings(), "anthropic")
    assert (result.status, client.calls) == ("WARN", [])


def test_the_selected_engine_is_read_from_the_platform_db_and_defaults_otherwise(tmp_path):
    db = _migrated_sqlite(tmp_path)
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    settings = _settings(anthropic_api_key=SECRET_KEY, openai_api_key="sk-openai-x1234", agent_engine_default="anthropic")
    default = preflight.selected_engine(settings, engine)
    with Session(engine) as session:
        session.add(EngineSelection(engine="openai", set_by_id="u"))
        session.commit()
    chosen = preflight.selected_engine(settings, engine)
    engine.dispose()
    assert (default, chosen) == ("anthropic", "openai")
    assert preflight.selected_engine(settings, None) == "anthropic"


# -- address pools -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pools", "capacity"),
    [
        (None, 31),
        ([], 31),
        ([{"Base": "10.64.0.0/12", "Size": 24}], 4096),
        ([{"Base": "10.0.0.0/16", "Size": 24}, {"Base": "10.1.0.0/16", "Size": 24}], 512),
        ([{"Base": "10.0.0.0/24", "Size": 16}], 0),
    ],
)
def test_network_capacity_is_derived_from_the_pools(pools, capacity):
    assert preflight.derive_network_capacity(pools) == capacity


def test_the_builtin_pool_warns_below_the_expected_service_count_and_hints_daemon_json():
    result = preflight.check_address_pools(FakeClient(), _settings(service_data_expected_services=100))
    assert result.status == "WARN"
    assert "daemon.json" in result.hint
    assert "default-address-pools" in result.hint


def test_a_large_enough_pool_passes():
    client = FakeClient(info={"DefaultAddressPools": [{"Base": "10.64.0.0/12", "Size": 24}]})
    assert preflight.check_address_pools(client, _settings(service_data_expected_services=100)).status == "PASS"


def test_the_builtin_pool_passes_when_few_services_are_expected():
    assert preflight.check_address_pools(FakeClient(), _settings(service_data_expected_services=20)).status == "PASS"


# -- public origin -------------------------------------------------------------------------


def test_public_origin_https_passes():
    assert preflight.check_public_origin(_settings()).status == "PASS"


def test_public_origin_unset_is_fail_in_prod_and_warn_in_dev():
    prod = preflight.check_public_origin(SimpleNamespace(platform_public_url=None, is_prod=True))
    dev = preflight.check_public_origin(SimpleNamespace(platform_public_url=None, is_prod=False))
    assert (prod.status, dev.status) == ("FAIL", "WARN")


def test_public_origin_http_in_prod_fails():
    result = preflight.check_public_origin(SimpleNamespace(platform_public_url="http://forge.example.test", is_prod=True))
    assert result.status == "FAIL"


# -- no credential in the output ---------------------------------------------------------


def test_redact_dsn_replaces_the_password_and_never_echoes_an_unparseable_string():
    assert preflight.redact_dsn("postgresql://u:pw123456@h:5/d") == "postgresql://u:***@h:5/d"
    assert preflight.redact_dsn(None) == "<unset>"
    garbled = "postgresql://u:pw123456@@[bad"
    assert "pw123456" not in preflight.redact_dsn(garbled)


def test_scrub_replaces_url_passwords_and_known_secret_values():
    out = preflight.scrub(f"boom postgresql://u:{SECRET_PW_DB}@h/d and a key {SECRET_KEY}", [SECRET_KEY])
    assert SECRET_PW_DB not in out
    assert SECRET_KEY not in out


def test_secret_values_covers_credential_fields_and_dsn_passwords_but_not_the_whole_url():
    settings = _settings(anthropic_api_key=SECRET_KEY, service_postgres_admin_dsn=SVC_DSN)
    values = preflight.secret_values(settings)
    assert {SECRET_KEY, SECRET_PW_DB, SECRET_PW_SVC} <= set(values)
    assert not any(v.startswith("postgresql://") for v in values)


def test_a_full_run_over_a_fixture_full_of_secrets_prints_none_of_them():
    """Every secret below is in the settings the run loads; the databases are unreachable, so the
    checks that print a DSN or an error all do."""
    settings = _settings(
        service_postgres_admin_dsn=SVC_DSN + "?connect_timeout=2",
        database_url=f"postgresql://forge:{SECRET_PW_DB}@platform-db.invalid:5432/forge?connect_timeout=2",
        **EXECUTOR_ON,
    )
    client = FakeClient(containers={"forge-traefik": FakeContainer()})
    results, secrets = preflight.run_checks(lambda: settings, lambda: client)
    report = preflight.render_report(results, secrets)
    for secret in (SECRET_PW_DB, SECRET_PW_SVC, SECRET_KEY, PROD["oidc_client_secret"], PROD["session_signing_secret"]):
        assert secret not in report
    assert "FAIL  platform database" in report  # the report is not trivially empty
    assert "platform-db.invalid" in report


def test_a_settings_error_that_carries_a_dsn_is_scrubbed_in_the_report():
    def boom():
        raise ValueError(f"cannot parse postgresql://forge:{SECRET_PW_DB}@host/db")

    results, secrets = preflight.run_checks(boom, lambda: FakeClient())
    report = preflight.render_report(results, secrets)
    assert SECRET_PW_DB not in report
    assert "FAIL  settings" in report


# -- the run -------------------------------------------------------------------------------


def test_a_failing_settings_load_stops_the_run_with_one_failure():
    def boom():
        raise ValueError("bad")

    results, _ = preflight.run_checks(boom, lambda: FakeClient())
    assert [(r.name, r.status) for r in results] == [("settings", "FAIL")]


def test_an_unreachable_docker_skips_what_needs_it_and_the_run_fails():
    settings = _settings(service_postgres_admin_dsn=None)
    results, _ = preflight.run_checks(lambda: settings, lambda: FakeClient(ping_error=ConnectionError("x")))
    by_name = {r.name: r.status for r in results}
    assert by_name["docker"] == "FAIL"
    assert (by_name["traefik"], by_name["images"], by_name["docker address pools"]) == ("WARN", "WARN", "WARN")


def test_the_run_asks_docker_for_reads_only(tmp_path):
    """`FakeClient` raises on any attribute outside ping/info/containers.get/images.get, so a whole
    run completing proves the preflight called nothing else."""
    db = _migrated_sqlite(tmp_path)
    settings = _dev_settings(db, **EXECUTOR_ON)
    client = FakeClient(containers={"forge-traefik": FakeContainer(extra_hosts=["host.docker.internal:host-gateway"])})
    results, _ = preflight.run_checks(lambda: settings, lambda: client)
    assert len(results) == 8
    allowed = {"ping", "info", "containers.get:forge-traefik", "images.get:forge-llm-proxy:latest", "images.get:forge-agent-sandbox:latest", "images.get:redis:8-alpine", "images.get:python:3.11-slim"}
    assert set(client.calls) <= allowed


def test_main_exits_non_zero_on_a_failure_and_zero_when_none(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "run_checks", lambda: ([preflight.CheckResult("x", "FAIL", "bad", "do y")], []))
    assert preflight.main([]) == 1
    assert "      fix: do y" in capsys.readouterr().out
    monkeypatch.setattr(preflight, "run_checks", lambda: ([preflight.CheckResult("x", "WARN", "meh")], []))
    assert preflight.main([]) == 0
