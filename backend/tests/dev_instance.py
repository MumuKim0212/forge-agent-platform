"""which database is the developer's dev service Postgres, and the checks that keep tests off it.

`dev.ps1` starts `forge-service-postgres`, publishes it on 127.0.0.1 and creates the admin role the
platform logs in with. It holds the demo service's live database. Tests run on the same machine, so
a test whose `service_postgres_admin_dsn` resolves to that port reaches the developer's data (the ambient
`backend/.env` value does exactly that: it is the developer's real credential, loaded at import before
anything can stop it, and `tests/conftest.py` neutralises it for the session).

The port and the role name are READ FROM `dev.ps1`, never spelled here or in any other test file: a
guard that carried its own copy would go stale the day `dev.ps1` changed, and a test file that
carried the literal is the defect the static scan below exists to find. If `dev.ps1` cannot be read
the way this module expects, that is an error, not a skip: a guard that silently stops guarding is
worse than no guard.

No pytest import: `conftest.py`, `test_config.py` and `test_dev_instance_guard.py` all use it.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_PS1 = REPO_ROOT / "dev.ps1"

# The line that sets the default port: `$pgPort = <digits>`. A line such as
# `$pgPort = [int]$m.Groups[1].Value` (the override from backend/.env) is not a number and does not
# match, which is the point. (This file spells no port: the scan over `backend/tests/` would flag it.)
_PORT_LINE = re.compile(r"(?m)^\s*\$pgPort\s*=\s*(\d+)\s*$")
_ROLE_CREATE = re.compile(r"CREATE ROLE (\w+) WITH LOGIN")

_DEFAULT_POSTGRES_PORT = 5432


class DevInstanceUnreadable(RuntimeError):
    """`dev.ps1` is unreadable or no longer has the shape this module reads."""


class DevInstanceMissing(DevInstanceUnreadable):
    """There is no `dev.ps1` at all (a backend-only container): no dev instance is defined here."""


class DevInstanceReached(AssertionError):
    """A test pointed the admin DSN at the dev service Postgres."""


@dataclass(frozen=True)
class DevInstance:
    port: int
    admin_role: str


def read_dev_instance(dev_ps1: Path = DEV_PS1) -> DevInstance:
    if not dev_ps1.is_file():
        raise DevInstanceMissing(f"{dev_ps1} does not exist")
    try:
        text = dev_ps1.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise DevInstanceUnreadable(
            f"cannot read {dev_ps1}: the dev service Postgres's port and admin role are defined there, "
            f"and the guard that keeps tests off it cannot work without them ({exc})"
        ) from exc
    port, role = _PORT_LINE.search(text), _ROLE_CREATE.search(text)
    if port is None or role is None:
        raise DevInstanceUnreadable(
            f"{dev_ps1.name} no longer has a `$pgPort = <number>` line and a `CREATE ROLE <name> WITH LOGIN` "
            "statement; update tests/dev_instance.py to the new shape rather than letting the guard go quiet"
        )
    return DevInstance(port=int(port.group(1)), admin_role=role.group(1))


# The switch `.github/workflows/ci.yml` sets (and `tests/test_no_raw_bidi_controls.py` reads) to say
# "this run must have a checkout": without it a backend-only container, which has no `dev.ps1`, skips
# these guards, and with it that absence is a failure.
REQUIRE_ENV = "FORGE_REQUIRE_REPO_GUARD"


def load_dev_instance_or_none(
    dev_ps1: Path = DEV_PS1, environ: Mapping[str, str] | None = None
) -> DevInstance | None:
    """`read_dev_instance()`, except that a `dev.ps1` that is not there at all gives None (nothing
    defines a dev instance in a backend-only container) unless `REQUIRE_ENV` is set, in which case
    it raises. A `dev.ps1` that is there but has changed shape always raises."""
    env = os.environ if environ is None else environ
    try:
        return read_dev_instance(dev_ps1)
    except DevInstanceMissing:
        if env.get(REQUIRE_ENV, "") not in ("", "0"):
            raise
        return None


# -- what a DSN points at --------------------------------------------------


def _is_loopback(host: str) -> bool:
    host = host.strip().strip("[]").lower()
    if host in ("", "localhost"):  # no host: libpq's default is the local machine
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _split_host_port(entry: str) -> tuple[str, str]:
    entry = entry.strip()
    if entry.startswith("["):  # [::1]:5432
        host, _, rest = entry[1:].partition("]")
        return host, rest.lstrip(":")
    host, _, port = entry.rpartition(":") if entry.count(":") == 1 else (entry, "", "")
    return host, port


_CONNINFO_PAIR = re.compile(r"""(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|\S+)""")


def _hosts_and_ports(dsn: str) -> tuple[list[str], list[int]]:
    """Every host and port the DSN names, URL or key=value form, multi-host included. Deliberately an
    over-approximation (hosts x ports): for a guard, a false positive costs a message and a false
    negative costs the developer's data."""
    hosts: list[str] = []
    ports: list[str] = []
    dsn = dsn.strip()
    if "://" in dsn:
        parts = urlsplit(dsn)
        for entry in parts.netloc.rpartition("@")[2].split(","):
            if entry.strip():
                host, port = _split_host_port(entry)
                hosts.append(host)
                if port:
                    ports.append(port)
        for key, value in parse_qsl(parts.query):
            if key in ("host", "hostaddr"):
                hosts.extend(value.split(","))
            elif key == "port":
                ports.extend(value.split(","))
    else:
        for key, value in _CONNINFO_PAIR.findall(dsn):
            value = value.strip("'")
            if key in ("host", "hostaddr"):
                hosts.extend(value.split(","))
            elif key == "port":
                ports.extend(value.split(","))
    numeric = []
    for port in ports:
        try:
            numeric.append(int(port.strip()))
        except ValueError:
            continue
    return hosts, numeric


def loopback_ports(dsn: str | None) -> frozenset[int]:
    """The ports a DSN reaches on this machine (none if it names no loopback host)."""
    if dsn is None or not dsn.strip():
        return frozenset()
    hosts, ports = _hosts_and_ports(dsn)
    if hosts and not any(_is_loopback(h) for h in hosts):
        return frozenset()
    return frozenset(ports or [_DEFAULT_POSTGRES_PORT])


def dsn_targets_dev_instance(dsn: str | None, guarded_ports: Iterable[int]) -> bool:
    return bool(loopback_ports(dsn) & frozenset(guarded_ports))


def guarded_ports(dev: DevInstance | None, ambient_dsn: str | None) -> frozenset[int]:
    """The dev instance's port from `dev.ps1` (none without a `dev.ps1`), plus the port of whatever
    loopback DSN the developer's `backend/.env` holds: `dev.ps1` publishes on the `.env` DSN's port when one
    is set, so an owner who changed it has a dev instance somewhere `dev.ps1`'s default does not say."""
    return frozenset({dev.port} if dev else ()) | loopback_ports(ambient_dsn)


def refuse_dev_instance(dsn: str | None, ports: Iterable[int], *, where: str) -> None:
    """Raise `DevInstanceReached` if `dsn` reaches a guarded port. The message names the rule and the
    way out, and never prints the DSN (it may carry a password)."""
    hit = loopback_ports(dsn) & frozenset(ports)
    if hit:
        raise DevInstanceReached(
            f"{where}: service_postgres_admin_dsn resolves to the dev service Postgres (loopback port "
            f"{sorted(hit)[0]}, started by dev.ps1), which holds the developer's live data. Rule: a test "
            "never points the admin DSN at it. Use tests.fake_admin_dsn.FAKE_ADMIN_DSN for a DSN that must "
            "never connect, or the throwaway-container fixtures (real_service_postgres_admin_dsn, "
            "scratch_postgres_url) for a real one."
        )


# -- the static scan over the test sources ---------------------------------

_LOOPBACK_HOST_TEXT = r"(?:127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0)"


def find_dev_instance_references(
    root: Path, dev: DevInstance, *, role_allowed_in: Iterable[str] = ()
) -> list[str]:
    """`path:line: why` for every place under `root` that carries the dev instance's port, or the dev
    admin role inside a loopback DSN (`<role>[:password]@127.0.0.1...`). The role rule skips the files
    named in `role_allowed_in`: the one place that builds a DSN for the throwaway container, whose
    port is a variable. The port rule has no exception: a test file has no business naming it.

    The sources are a finite directory tree, read once, `__pycache__` aside."""
    port = re.compile(rf"(?<!\d){dev.port}(?!\d)")
    role = re.compile(rf"(?<!\w){re.escape(dev.admin_role)}(?::[^@\s'\"]*)?@{_LOOPBACK_HOST_TEXT}")
    skip_role = frozenset(role_allowed_in)
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        rel = path.relative_to(root).as_posix()
        text = path.read_bytes().decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if port.search(line):
                found.append(f"{rel}:{number}: the dev service Postgres's port")
            if rel not in skip_role and role.search(line):
                found.append(f"{rel}:{number}: the dev admin role in a loopback DSN")
    return found


# A DSN literal given to the setting. Tests that need one use the shared fake, so a second spelling is
# a finding whatever it points at (the old `postgresql://fake/fake` in test_worker.py resolved through
# the machine's DNS search suffixes and took 2.7 s a lookup; a copy on the dev port was the incident).
_SETTING_GIVEN_A_LITERAL_DSN = re.compile(r"""service_postgres_admin_dsn["']?\s*[,=:]\s*f?["'][A-Za-z][A-Za-z0-9+.-]*://""")


def find_literal_admin_dsns(root: Path, *, allowed_in: Iterable[str] = ()) -> list[str]:
    """`path:line: ...` for every place under `root` that hands `service_postgres_admin_dsn` a DSN
    literal (a `monkeypatch.setattr` argument, a keyword argument or a dict value, wrapped across lines
    or not). `allowed_in` names the files that may, relative to `root`."""
    skip = frozenset(allowed_in)
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or rel in skip:
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        for match in _SETTING_GIVEN_A_LITERAL_DSN.finditer(text):
            number = text.count(chr(10), 0, match.start()) + 1
            found.append(f"{rel}:{number}: a DSN literal given to service_postgres_admin_dsn")
    return found
