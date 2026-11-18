"""the suite never reaches the developer's dev service Postgres.

`dev.ps1` starts `forge-service-postgres` on a loopback port and it holds the demo service's live
database. The defect these tests close had two halves:

- a fake admin DSN spelled at that very port in ten test files, eight of them with the real admin
  role name (one test's orphan sweep connected to the live instance and was stopped only by a wrong
  password);
- the ambient `settings.service_postgres_admin_dsn`, which is the developer's REAL credential from
  `backend/.env` (observed: the singleton in a test process resolved to the dev instance's port and the
  admin role). Any test that reached provisioning code without setting the DSN was handed that
  credential.

What holds the line, and what proves it:

- `tests/fake_admin_dsn.py`: one fake DSN that cannot connect (pinned below, and connected to once);
- `tests/conftest.py::_session_service_postgres_guard`: the ambient DSN starts as None, and assigning or
  constructing a DSN that reaches the dev instance raises, naming the rule. The wiring tests below only
  pass while that autouse fixture is active;
- `test_config.py::test_service_postgres_admin_dsn_port_is_not_duplicated_as_a_second_setting`: the
  static scans over `backend/tests/` (`find_dev_instance_references`, `find_literal_admin_dsns`),
  whose logic is exercised here against planted trees.

The dev instance's port and role are read from `dev.ps1`, so this file spells neither.
"""

import pytest

from app.config import Settings, settings
from tests.dev_instance import (
    DEV_PS1,
    REQUIRE_ENV,
    DevInstance,
    DevInstanceMissing,
    DevInstanceReached,
    DevInstanceUnreadable,
    dsn_targets_dev_instance,
    find_dev_instance_references,
    find_literal_admin_dsns,
    guarded_ports,
    load_dev_instance_or_none,
    read_dev_instance,
    refuse_dev_instance,
)
from tests.fake_admin_dsn import FAKE_ADMIN_DSN

BOM = chr(0xFEFF)  # built from the code point, never typed
DEV = load_dev_instance_or_none()
if DEV is None:  # a backend-only container: no dev.ps1 to read (CI sets REQUIRE_ENV, so it fails there)
    pytest.skip("no dev.ps1 beside backend/: there is no dev instance to guard", allow_module_level=True)
PORTS = guarded_ports(DEV, None)


def _dsn(host: str = "127.0.0.1", *, user: str = "someone", password: str = "hunter2-caseak", port: int | None = None):
    return f"postgresql://{user}:{password}@{host}:{DEV.port if port is None else port}/postgres"


# -- reading the dev instance from dev.ps1 ---------------------------------


def test_read_dev_instance_takes_the_port_and_role_from_the_script(tmp_path):
    script = tmp_path / "dev.ps1"
    # CRLF and a BOM, as the Windows checkout has; the second `$pgPort` line is the override from
    # backend/.env, which is not a number and must not be mistaken for the default.
    script.write_bytes(
        (
            BOM + "    $pgPort = 4242\r\n"
            "    if ($m.Success) { $pgPort = [int]$m.Groups[1].Value }\r\n"
            "    docker exec c psql -c \"CREATE ROLE some_admin WITH LOGIN PASSWORD 'x' CREATEDB\"\r\n"
        ).encode("utf-8")
    )
    assert read_dev_instance(script) == DevInstance(port=4242, admin_role="some_admin")


def test_read_dev_instance_refuses_a_script_it_cannot_read(tmp_path):
    with pytest.raises(DevInstanceMissing, match="does not exist"):
        read_dev_instance(tmp_path / "missing.ps1")
    shapeless = tmp_path / "dev.ps1"
    shapeless.write_text("Write-Host nothing about a database here\n", encoding="utf-8")
    with pytest.raises(DevInstanceUnreadable, match="no longer has"):
        read_dev_instance(shapeless)


def test_the_real_dev_ps1_is_readable():
    """The guard fails closed when `dev.ps1` changes shape; this is the test that says so first. The
    two values are checked against the script's own text a second way, so a reader that returned the
    wrong number (every other test here derives its expectation from the reader) is caught."""
    assert 1024 <= DEV.port <= 65535
    assert DEV.admin_role.isidentifier()
    text = DEV_PS1.read_text(encoding="utf-8-sig")
    assert f"$pgPort = {DEV.port}" in text
    assert f"CREATE ROLE {DEV.admin_role} WITH LOGIN" in text


def test_a_missing_dev_ps1_skips_the_guard_only_where_no_checkout_is_required(tmp_path):
    """A backend-only container has no `dev.ps1`; the guards then run without a port from it. CI sets
    the same switch the bidi-control guard reads, and there the absence is a failure. A script that is
    there but has the wrong shape is never excused."""
    missing = tmp_path / "dev.ps1"
    assert load_dev_instance_or_none(missing, environ={}) is None
    assert load_dev_instance_or_none(missing, environ={REQUIRE_ENV: "0"}) is None
    with pytest.raises(DevInstanceMissing):
        load_dev_instance_or_none(missing, environ={REQUIRE_ENV: "1"})
    missing.write_text("nothing about a database\n", encoding="utf-8")
    with pytest.raises(DevInstanceUnreadable, match="no longer has"):
        load_dev_instance_or_none(missing, environ={})
    assert guarded_ports(None, None) == frozenset()
    assert guarded_ports(None, "postgresql://u:p@127.0.0.1:55999/postgres") == frozenset({55999})


# -- which DSNs reach the dev instance --------------------------------------


@pytest.mark.parametrize(
    "dsn",
    [
        pytest.param(_dsn(), id="loopback-ip"),
        pytest.param(_dsn("localhost"), id="localhost"),
        pytest.param(_dsn("[::1]"), id="ipv6-loopback"),
        pytest.param(_dsn("0.0.0.0"), id="unspecified-address"),
        pytest.param(_dsn(user="a", password="b").replace("postgresql://", "postgresql+psycopg://"), id="sqlalchemy-driver"),
        pytest.param(f"postgresql://u:p@db.example:1,127.0.0.1:{DEV.port}/postgres", id="multi-host"),
        pytest.param(f"postgresql:///postgres?host=127.0.0.1&port={DEV.port}", id="host-and-port-in-the-query"),
        pytest.param(f"host=127.0.0.1 port={DEV.port} user=u password='a b' dbname=postgres", id="key-value-form"),
    ],
)
def test_dsn_shapes_that_reach_the_dev_instance(dsn):
    assert dsn_targets_dev_instance(dsn, PORTS) is True


@pytest.mark.parametrize(
    "dsn",
    [
        pytest.param(None, id="unset"),
        pytest.param("   ", id="blank"),
        pytest.param(FAKE_ADMIN_DSN, id="the-shared-fake"),
        pytest.param(_dsn(port=1), id="another-loopback-port"),
        pytest.param(_dsn("db.example"), id="the-dev-port-on-another-host"),
        pytest.param("postgresql://u:p@127.0.0.1:55999/postgres", id="a-throwaway-container-port"),
    ],
)
def test_dsn_shapes_that_do_not_reach_the_dev_instance(dsn):
    assert dsn_targets_dev_instance(dsn, PORTS) is False


def test_the_guarded_ports_follow_a_port_the_owners_env_file_chose():
    """`dev.ps1` publishes on the `.env` DSN's port when one is set, so that port is the dev instance
    too. A DSN on another machine adds nothing."""
    assert guarded_ports(DEV, "postgresql://u:p@127.0.0.1:55999/postgres") == frozenset({DEV.port, 55999})
    assert guarded_ports(DEV, "postgresql://u:p@db.example:55999/postgres") == frozenset({DEV.port})
    assert guarded_ports(DEV, None) == frozenset({DEV.port})


# -- the autouse guard in conftest.py is in effect --------------------------


def _admin_dsn_is_unset() -> bool:
    """The singleton's admin DSN is None, as a bare boolean.

    Every assertion about the singleton's DSN goes through this (or a local computed from it) and never
    names `settings` inside an `assert`: the singleton holds `backend/.env`, and when an assertion such as
    `assert settings.service_postgres_admin_dsn is None` fails, pytest's rewriting prints the failing value
    AND `repr(settings)` -- the DSN with its password, and every other credential in the file
    (`+ where 'postgresql://...' = Settings(...).service_postgres_admin_dsn`; measured, on a stand-in
    settings class). A property such as `service_postgres_configured` leaks the same way: the
    value is a bool but the `where` clause is not. A test that regresses must say that it did, naming no value."""
    return settings.service_postgres_admin_dsn is None


def test_the_ambient_admin_dsn_is_not_the_owners():
    """Observed on the developer's machine: without the session guard, this singleton holds the DSN from
    `backend/.env` (the dev instance's port, the real admin role and its real password), because it is
    built from that file at import. On a machine with no such file the value was None anyway, so this
    test proves the neutralisation only where the file exists."""
    unset = _admin_dsn_is_unset()
    configured = settings.service_postgres_configured
    assert unset, "service_postgres_admin_dsn is set in the test process: the session guard in tests/conftest.py no longer neutralises the ambient value"
    assert configured is False, "service_postgres_configured is True although the admin DSN is unset"


def test_assigning_a_dsn_that_reaches_the_dev_instance_is_refused(monkeypatch):
    with pytest.raises(DevInstanceReached, match=r"Rule: a test") as refused:
        monkeypatch.setattr(settings, "service_postgres_admin_dsn", _dsn())
    unset = _admin_dsn_is_unset()
    leaked = "hunter2-caseak" in str(refused.value)
    assert unset, "the refused DSN was stored anyway"  # refused before it was stored
    assert not leaked, "the refusal message carries the credential"  # it never does


def test_a_direct_assignment_is_refused_too(monkeypatch):
    # Registers the undo: whatever the next line does (it only stores the value if the guard is gone),
    # the setting is None again when this test ends.
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)
    with pytest.raises(DevInstanceReached):
        settings.service_postgres_admin_dsn = _dsn("localhost")
    unset = _admin_dsn_is_unset()
    assert unset, "the refused DSN was stored anyway"


def test_constructing_settings_with_a_dsn_that_reaches_the_dev_instance_is_refused():
    with pytest.raises(DevInstanceReached, match="Settings construction"):
        Settings(_env_file=None, service_postgres_admin_dsn=_dsn())


def test_the_shared_fake_dsn_passes_the_guard_both_ways(monkeypatch):
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    # Locals first, for the reason `_admin_dsn_is_unset` gives: an `assert settings.<property>` that fails
    # prints the whole singleton.
    assigned_is_configured = settings.service_postgres_configured
    constructed_is_configured = Settings(_env_file=None, service_postgres_admin_dsn=FAKE_ADMIN_DSN).service_postgres_configured
    assert assigned_is_configured is True, "the shared fake DSN was refused or not stored on assignment"
    assert constructed_is_configured is True, "the shared fake DSN was refused or not read on construction"


# -- the shared fake DSN can never connect -----------------------------------


def test_the_shared_fake_dsn_has_nothing_that_can_answer_it():
    from urllib.parse import urlsplit

    parts = urlsplit(FAKE_ADMIN_DSN)
    assert parts.hostname is not None and parts.hostname.endswith(".invalid")  # RFC 6761: never resolves
    assert parts.username != DEV.admin_role  # no such login even if something did answer
    assert "connect_timeout=" in (parts.query or "")  # bounded if a resolver hijacks NXDOMAIN
    assert dsn_targets_dev_instance(FAKE_ADMIN_DSN, PORTS) is False


def test_connecting_with_the_shared_fake_dsn_fails_fast_and_never_reaches_a_server(returns_within):
    """Proves the property rather than the DSN's spelling: a real connection attempt, which must fail
    before any server is involved (an authentication failure would mean something answered)."""
    import time

    import psycopg

    # This test opens its own connection, so the session guard in conftest.py (which only sees what is
    # assigned to `Settings`) never looks at it. If `FAKE_ADMIN_DSN` ever resolved to the dev instance
    # (moving it there as a mutation), the connect below would be a real login attempt
    # against a real database -- its log went 876 -> 877. So refuse first, with the reader the
    # guard uses, before anything touches the network. The message names the rule, never the DSN.
    refuse_dev_instance(FAKE_ADMIN_DSN, PORTS, where="the direct psycopg.connect in this test")

    def attempt():
        started = time.monotonic()
        try:
            psycopg.connect(FAKE_ADMIN_DSN)
        except psycopg.OperationalError as exc:
            return time.monotonic() - started, str(exc)
        raise AssertionError("the shared fake DSN connected to something")

    elapsed, message = returns_within(15, attempt)
    assert elapsed < 5, f"took {elapsed:.1f}s; a fake DSN that hangs makes every test that reaches it slow"
    assert "authentication" not in message.lower() and "password" not in message.lower()


def test_the_direct_connect_test_refuses_before_connecting_if_the_fake_reaches_the_dev_instance(monkeypatch):
    """The refusal in the test above, exercised without ever connecting: the fake is moved onto the dev
    instance and `psycopg.connect` is replaced by a tripwire, so a refusal that came too late (or not at
    all) shows as the tripwire, and nothing can reach the developer's database whatever happens here."""
    import sys

    import psycopg

    reached = []
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: reached.append("psycopg.connect"))
    monkeypatch.setattr(sys.modules[__name__], "FAKE_ADMIN_DSN", _dsn())

    with pytest.raises(DevInstanceReached, match=r"Rule: a test") as refused:
        test_connecting_with_the_shared_fake_dsn_fails_fast_and_never_reaches_a_server(lambda seconds, fn: fn())

    assert reached == [], "the direct connect ran although the DSN resolves to the dev instance"
    assert "hunter2-caseak" not in str(refused.value)


def test_the_platforms_own_admin_connection_fails_fast_on_the_shared_fake_dsn(monkeypatch, returns_within):
    """The path an orphan sweep took: `service_data_backup.list_service_databases` opens the
    platform's real admin engine from `settings`. On the old fake it reached the live instance and
    was refused only for its password; on the shared fake it must end in a name-resolution failure,
    quickly, with nothing answering."""
    import time

    from app.services import service_data_backup

    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)

    def attempt():
        started = time.monotonic()
        try:
            service_data_backup.list_service_databases()
        except service_data_backup.ServiceDataBackupError as exc:
            return time.monotonic() - started, str(exc)
        raise AssertionError("the platform's admin connection succeeded against the shared fake DSN")

    elapsed, message = returns_within(15, attempt)
    assert elapsed < 5, f"took {elapsed:.1f}s"
    assert "failed to resolve host" in message
    assert "authentication" not in message.lower()


# -- the static scan over test sources --------------------------------------


def _plant(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_the_scan_finds_a_dsn_at_the_dev_port_in_any_test_file(tmp_path):
    _plant(tmp_path, "test_a.py", f'DSN = "postgresql://u:p@127.0.0.1:{DEV.port}/postgres"\n')
    _plant(tmp_path, "sub/helper.txt", f"port {DEV.port}\n")
    found = find_dev_instance_references(tmp_path, DEV)
    assert found == [
        "sub/helper.txt:1: the dev service Postgres's port",
        "test_a.py:1: the dev service Postgres's port",
    ]


def test_the_scan_finds_the_dev_admin_role_in_a_loopback_dsn_whatever_the_port(tmp_path):
    _plant(tmp_path, "test_a.py", f'A = "postgresql://{DEV.admin_role}:pw@127.0.0.1:1/postgres"\n')
    _plant(tmp_path, "test_b.py", f'B = "postgresql://{DEV.admin_role}@localhost/postgres"\n')
    found = find_dev_instance_references(tmp_path, DEV)
    assert found == [
        "test_a.py:1: the dev admin role in a loopback DSN",
        "test_b.py:1: the dev admin role in a loopback DSN",
    ]


def test_the_scan_leaves_alone_what_is_not_the_defect(tmp_path):
    # The dev admin role name is also a substring of the LLM proxy's admin path; the port digits can sit
    # inside a longer number; the role on a non-loopback host is not the dev instance.
    _plant(tmp_path, "test_ok.py", '"/_forge_admin/tokens"\n')
    _plant(tmp_path, "test_longer.py", f"N = 1{DEV.port}9\n")
    _plant(tmp_path, "test_remote.py", f'R = "postgresql://{DEV.admin_role}:pw@db.example:1/postgres"\n')
    _plant(tmp_path, "__pycache__/test_cached.pyc", f"{DEV.port}\n")
    assert find_dev_instance_references(tmp_path, DEV) == []


def test_the_scan_exempts_the_role_only_for_the_file_that_builds_the_throwaway_dsn(tmp_path):
    line = f'dsn = f"postgresql://{DEV.admin_role}:{{pw}}@127.0.0.1:{{host_port}}/postgres"\n'
    _plant(tmp_path, "conftest.py", line)
    _plant(tmp_path, "test_elsewhere.py", line)
    assert find_dev_instance_references(tmp_path, DEV, role_allowed_in=["conftest.py"]) == [
        "test_elsewhere.py:1: the dev admin role in a loopback DSN"
    ]
    # ... and the exemption never covers the port
    _plant(tmp_path, "conftest.py", f'PORT = {DEV.port}\n')
    assert find_dev_instance_references(tmp_path, DEV, role_allowed_in=["conftest.py"]) == [
        "conftest.py:1: the dev service Postgres's port",
        "test_elsewhere.py:1: the dev admin role in a loopback DSN",
    ]


def test_the_scan_finds_a_dsn_literal_given_to_the_setting_in_every_form_tests_use(tmp_path):
    _plant(tmp_path, "test_a.py", 'monkeypatch.setattr(settings, "service_postgres_admin_dsn", "postgresql://fake/fake")\n')
    _plant(tmp_path, "test_b.py", 'x = Settings(_env_file=None, service_postgres_admin_dsn="postgresql://u:p@h/db")\n')
    _plant(tmp_path, "test_c.py", 'D = {"service_postgres_admin_dsn": "postgresql+psycopg://u@h/db"}\n')
    _plant(
        tmp_path,
        "test_d.py",
        'monkeypatch.setattr(\n    settings,\n    "service_postgres_admin_dsn",\n    "postgresql://nobody:x@127.0.0.1:1/postgres",\n)\n',
    )
    assert find_literal_admin_dsns(tmp_path) == [
        "test_a.py:1: a DSN literal given to service_postgres_admin_dsn",
        "test_b.py:1: a DSN literal given to service_postgres_admin_dsn",
        "test_c.py:1: a DSN literal given to service_postgres_admin_dsn",
        "test_d.py:3: a DSN literal given to service_postgres_admin_dsn",
    ]


def test_the_scan_leaves_alone_a_setting_given_anything_but_a_dsn_literal(tmp_path):
    _plant(tmp_path, "test_ok.py", 'monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)\n')
    _plant(tmp_path, "test_none.py", 'monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)\n')
    _plant(tmp_path, "test_blank.py", 'monkeypatch.setattr(settings, "service_postgres_admin_dsn", "   ")\n')
    _plant(tmp_path, "test_kw.py", "Settings(_env_file=None, service_postgres_admin_dsn=dsn)\n")
    _plant(tmp_path, "test_prose.py", '"""a `postgresql://` DSN on service_postgres_admin_dsn is configured"""\n')
    _plant(tmp_path, "allowed.py", 'monkeypatch.setattr(settings, "service_postgres_admin_dsn", "postgresql://u:p@h/db")\n')
    assert find_literal_admin_dsns(tmp_path, allowed_in=["allowed.py"]) == []
