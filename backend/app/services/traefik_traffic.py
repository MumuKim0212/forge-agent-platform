"""The traffic signal under Traefik.

Where Traefik routes, no platform code sees a request to a deployed service, so before this
module nothing recorded traffic and the idle sweep exempted every live service
(`project_lifecycle.live_services_exempt_from_idle_sweep`). Here the worker reads Traefik's own
Prometheus counter `traefik_service_requests_total` through `docker exec` into the operator's
Traefik container, from a loopback-only metrics entrypoint, every `POLL_INTERVAL_SECONDS`, and
records each deployed service's increase as traffic activity.

Three parts:

* `parse_metrics` -- the exposition format, parsed for exactly two families. A line of either
  family that does not parse fails the whole poll; lines of every other family are never looked at.
* `TrafficSignalPoller.poll` -- one poll: read, verify the endpoint is loopback-only, apply the
  continuity rules and commit the activity writes, the per-deployment snapshot and
  the signal state together or not at all. A failed poll writes nothing and does not move
  "last successful poll".
* `signal_is_trusted` -- the runtime half of the sweep's live-service exemption.

Activity is written with `project_activity.touch_last_active_for_traffic` (the same 15-minute
coarsening `record_traffic_activity` wraps) and NOT with `record_traffic_activity` itself: that
function commits on its own, and the poll's activity writes must commit with the snapshot.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import docker
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.models.deployment import Deployment
from app.models.project import Project
from app.models.traffic_signal import SINGLETON_ID, TrafficSignalState
from app.services.project_activity import touch_last_active_for_traffic

logger = logging.getLogger("app.traefik_traffic")

# Fixed cadence, a property of the mechanism like the 15-minute coarsening: not a setting.
POLL_INTERVAL_SECONDS = 60
# The sweep trusts the signal only while its last successful poll is at most three cadences old.
SIGNAL_MAX_AGE = timedelta(seconds=3 * POLL_INTERVAL_SECONDS)
# A Traefik restart keeps continuity when it happened within this long of the last successful poll.
RESTART_CONTINUITY_TOLERANCE = timedelta(minutes=15)
# `process_start_time_seconds` is a float that round-trips through a DateTime column.
_START_TIME_TOLERANCE = timedelta(seconds=1)
# The bound on one read of Traefik's output. A deployment contributes a handful of lines (one per
# status code, method and protocol seen), ~250 bytes each, so a few KiB: 16 MiB leaves headroom
# for thousands of services. Over it the poll fails and the exemption holds.
METRICS_READ_CAP_BYTES = 16 * 1024 * 1024
# /proc/net/tcp* of one container is a few lines per socket.
_PROC_READ_CAP_BYTES = 1024 * 1024
_EXEC_WGET_TIMEOUT_SECONDS = 10

_SERVICE_FAMILY = "traefik_service_requests_total"
_START_FAMILY = "process_start_time_seconds"
_DEPLOYMENT_SERVICE = re.compile(r"^forge-deploy-([0-9A-Za-z-]+)@docker$")
_ID_CHUNK = 500


class TrafficSignalError(Exception):
    """A poll that must not be used: unreadable, over the cap, or not parseable."""


# -- the exposition format ---------------------------------------------------------------


@dataclass(frozen=True)
class ParsedMetrics:
    started_at: datetime
    # service label -> sum over every other label
    service_totals: dict[str, float]


def _parse_labels(text: str, pos: int) -> tuple[dict[str, str], int]:
    """`text[pos]` is just after `{`. Returns the labels and the index after `}`."""
    labels: dict[str, str] = {}
    n = len(text)
    while True:
        while pos < n and text[pos] in " \t":
            pos += 1
        if pos >= n:
            raise TrafficSignalError("unterminated label set")
        if text[pos] == "}":
            return labels, pos + 1
        eq = text.find("=", pos)
        if eq < 0:
            raise TrafficSignalError("label without '='")
        name = text[pos:eq].strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise TrafficSignalError("bad label name")
        pos = eq + 1
        if pos >= n or text[pos] != '"':
            raise TrafficSignalError("label value not quoted")
        pos += 1
        value: list[str] = []
        while True:
            if pos >= n:
                raise TrafficSignalError("unterminated label value")
            ch = text[pos]
            if ch == "\\":
                if pos + 1 >= n:
                    raise TrafficSignalError("dangling escape")
                esc = text[pos + 1]
                if esc == "n":
                    value.append("\n")
                elif esc in ('"', "\\"):
                    value.append(esc)
                else:
                    raise TrafficSignalError("bad escape in label value")
                pos += 2
            elif ch == '"':
                pos += 1
                break
            else:
                value.append(ch)
                pos += 1
        labels[name] = "".join(value)
        while pos < n and text[pos] in " \t":
            pos += 1
        if pos < n and text[pos] == ",":
            pos += 1
        elif pos < n and text[pos] == "}":
            continue
        else:
            raise TrafficSignalError("junk after a label value")


def _parse_sample(line: str, family: str) -> tuple[dict[str, str], float]:
    pos = len(family)
    labels: dict[str, str] = {}
    if pos < len(line) and line[pos] == "{":
        labels, pos = _parse_labels(line, pos + 1)
    parts = line[pos:].split()
    if len(parts) not in (1, 2):  # value, optional timestamp
        raise TrafficSignalError(f"malformed sample line for {family}")
    try:
        value = float(parts[0])
        if len(parts) == 2:
            int(parts[1])
    except ValueError:
        raise TrafficSignalError(f"malformed value for {family}") from None
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        raise TrafficSignalError(f"unusable value for {family}")
    return labels, value


def _family_of(line: str, family: str) -> bool:
    return line.startswith(family) and (len(line) == len(family) or line[len(family)] in "{ \t")


def parse_metrics(text: str) -> ParsedMetrics:
    """Sums `traefik_service_requests_total` per `service` over every other label and reads the
    one `process_start_time_seconds`. Any line of those two families that does not parse, a
    series with no `service` label, or a missing/duplicate start time raises
    `TrafficSignalError`. Every other line is ignored unread."""
    totals: dict[str, float] = {}
    started: float | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _family_of(line, _SERVICE_FAMILY):
            labels, value = _parse_sample(line, _SERVICE_FAMILY)
            service = labels.get("service")
            if not service:
                raise TrafficSignalError("traefik_service_requests_total series without a service label")
            totals[service] = totals.get(service, 0.0) + value
        elif _family_of(line, _START_FAMILY):
            _, value = _parse_sample(line, _START_FAMILY)
            if started is not None:
                raise TrafficSignalError("more than one process_start_time_seconds")
            started = value
    if started is None:
        raise TrafficSignalError("process_start_time_seconds missing")
    try:
        started_at = datetime.fromtimestamp(started, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise TrafficSignalError("process_start_time_seconds out of range") from None
    return ParsedMetrics(started_at=started_at, service_totals=totals)


# -- docker exec -------------------------------------------------------------------------


def docker_exec_read(container_name: str, cmd: list[str], cap: int, *, client=None) -> str:
    """Runs `cmd` in the named container and returns its stdout. Raises `TrafficSignalError` on a
    missing container, a non-zero exit, undecodable output, or more than `cap` bytes (stdout and
    stderr together; the stream is abandoned at the cap, never buffered past it)."""
    try:
        client = client or docker.from_env(timeout=30)
        container = client.containers.get(container_name)
        exec_id = client.api.exec_create(container.id, cmd, stdout=True, stderr=True)["Id"]
        out = bytearray()
        total = 0
        stream = client.api.exec_start(exec_id, stream=True, demux=True)
        for stdout, stderr in stream:
            total += len(stdout or b"") + len(stderr or b"")
            if total > cap:
                close = getattr(stream, "close", None)
                if close:
                    close()
                raise TrafficSignalError(f"output of {cmd[0]} exceeded the {cap}-byte cap")
            if stdout:
                out += stdout
        info = client.api.exec_inspect(exec_id)
        for _ in range(50):
            if not info.get("Running"):
                break
            time.sleep(0.1)
            info = client.api.exec_inspect(exec_id)
        if info.get("Running"):
            raise TrafficSignalError(f"{cmd[0]} did not finish")
        if info.get("ExitCode") != 0:
            raise TrafficSignalError(f"{cmd[0]} exited with {info.get('ExitCode')}")
        return out.decode("utf-8")
    except TrafficSignalError:
        raise
    except Exception as exc:  # noqa: BLE001 - docker errors, decode errors: all a failed read
        raise TrafficSignalError(f"could not read from {container_name}: {type(exc).__name__}: {exc}") from exc


def _default_exec(cmd: list[str], cap: int) -> str:
    return docker_exec_read(settings.traefik_container_name, cmd, cap)


# -- the loopback-only check -------------------------------------------------------------


def _listening_addresses(proc_text: str, port: int, *, v6: bool) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    found = []
    for line in proc_text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] != "0A":  # 0A: TCP_LISTEN
            continue
        host_hex, _, port_hex = fields[1].partition(":")
        if int(port_hex, 16) != port:
            continue
        raw = bytes.fromhex(host_hex)
        if v6:  # four 32-bit words, each in host (little-endian) byte order
            raw = b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
            found.append(ipaddress.IPv6Address(raw))
        else:
            found.append(ipaddress.IPv4Address(raw[::-1]))
    return found


def _is_loopback(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped.is_loopback
    return addr.is_loopback


def listens_beyond_loopback(proc_tcp: str, proc_tcp6: str, port: int) -> bool:
    """True when some listening socket on `port` is bound to a non-loopback address, wildcards
    included. Input is the text of the container's `/proc/net/tcp` and `/proc/net/tcp6`."""
    addresses = _listening_addresses(proc_tcp, port, v6=False) + _listening_addresses(proc_tcp6, port, v6=True)
    return any(not _is_loopback(a) for a in addresses)


# -- one poll ----------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class TrafficSignalPoller:
    """One worker process's poller. `exec_fn(cmd, cap) -> stdout` is the seam tests fake;
    the default is `docker exec` into `settings.traefik_container_name`."""

    def __init__(self, exec_fn=None):
        self._exec = exec_fn or _default_exec
        # The Traefik process start time the loopback check last ran against, and its verdict.
        # None at worker start, so the first poll of a process runs the check.
        self._verified_start: datetime | None = None
        self._refused = False

    # -- read -------------------------------------------------------------------------

    def _read_metrics(self, port: int) -> ParsedMetrics:
        text = self._exec(
            ["wget", "-q", "-T", str(_EXEC_WGET_TIMEOUT_SECONDS), "-O", "-", f"http://127.0.0.1:{port}/metrics"],
            METRICS_READ_CAP_BYTES,
        )
        return parse_metrics(text)

    def _endpoint_is_loopback_only(self, port: int, started_at: datetime) -> bool:
        """Runs at worker start (the first poll of this process) and after each detected restart.
        A read error propagates (a failed poll) and leaves the check due again next time."""
        if (
            self._verified_start is not None
            and abs(started_at - self._verified_start) <= _START_TIME_TOLERANCE
        ):
            return not self._refused
        tcp = self._exec(["cat", "/proc/net/tcp"], _PROC_READ_CAP_BYTES)
        if "local_address" not in tcp:
            raise TrafficSignalError("/proc/net/tcp is not in the expected format")
        # No tcp6 file means no IPv6 sockets; any other failure of `cat` (exit 1) is the same
        # shell result, but the container being gone already failed the read above.
        tcp6 = self._exec(["sh", "-c", "cat /proc/net/tcp6 2>/dev/null || true"], _PROC_READ_CAP_BYTES)
        self._refused = listens_beyond_loopback(tcp, tcp6, port)
        self._verified_start = started_at
        if self._refused:
            logger.error(
                "traffic signal REFUSED: port %d on the Traefik container %r listens on a non-loopback "
                "address, so every service network shared with Traefik could read the metrics. Declare the "
                "metrics entrypoint as 127.0.0.1:%d (--entrypoints.<name>.address=127.0.0.1:%d) and restart "
                "Traefik. Live services stay exempt "
                "from the idle sweep until then.",
                port,
                settings.traefik_container_name,
                port,
                port,
            )
        return not self._refused

    # -- apply ------------------------------------------------------------------------

    def _apply(self, db: Session, parsed: ParsedMetrics, now: datetime) -> None:
        by_id: dict[str, tuple[Deployment, float]] = {}
        wanted: dict[str, float] = {}
        for service, total in parsed.service_totals.items():
            match = _DEPLOYMENT_SERVICE.match(service)
            if match:
                wanted[match.group(1)] = wanted.get(match.group(1), 0.0) + total
        ids = list(wanted)
        for i in range(0, len(ids), _ID_CHUNK):
            for deployment in db.scalars(select(Deployment).where(Deployment.id.in_(ids[i : i + _ID_CHUNK]))):
                by_id[deployment.id] = (deployment, wanted[deployment.id])

        state = db.get(TrafficSignalState, SINGLETON_ID)
        if state is None:
            # First poll ever: a baseline, no activity. The counters cover the
            # whole life of the Traefik process and are not all fresh traffic.
            for deployment, total in by_id.values():
                deployment.traffic_counter_seen = total
            db.add(
                TrafficSignalState(
                    id=SINGLETON_ID, traefik_started_at=parsed.started_at, last_poll_at=now, observed_since=now
                )
            )
            return

        previous_start = _aware(state.traefik_started_at)
        restarted = abs(parsed.started_at - previous_start) > _START_TIME_TOLERANCE
        if restarted:
            # Every counter started from zero. A snapshot of a series that has not reappeared is
            # stale, so it goes too.
            for deployment in db.scalars(
                select(Deployment).where(Deployment.traffic_counter_seen.is_not(None))
            ):
                if deployment.id not in by_id:
                    deployment.traffic_counter_seen = None
            if parsed.started_at - _aware(state.last_poll_at) > RESTART_CONTINUITY_TOLERANCE:
                state.observed_since = parsed.started_at

        for deployment, total in by_id.values():
            seen = deployment.traffic_counter_seen or 0.0
            if restarted or total < seen:
                # Reset by a restart, or one series dropped and re-created: the whole current
                # value is traffic since then, not a gap.
                increase = total
            else:
                increase = total - seen
            if increase > 0:
                project = db.get(Project, deployment.project_id)
                if project is not None:
                    touch_last_active_for_traffic(project, now=now)
            deployment.traffic_counter_seen = total
        state.traefik_started_at = parsed.started_at
        state.last_poll_at = now

    def poll(self, db: Session, *, now: datetime | None = None) -> bool:
        """One poll. True when it was used (and committed); False when it failed or was refused,
        in which case nothing was written. Never raises an `Exception`."""
        port = settings.traefik_metrics_port
        if not settings.traefik_enabled or port is None:
            return False
        moment = now or _utcnow()
        try:
            parsed = self._read_metrics(port)
            if not self._endpoint_is_loopback_only(port, parsed.started_at):
                return False
            self._apply(db, parsed, moment)
            db.commit()
            return True
        except TrafficSignalError as exc:
            logger.warning("traffic signal: poll failed, nothing recorded: %s", exc)
        except Exception:  # noqa: BLE001 - a poll must never end the worker
            logger.exception("traffic signal: poll failed at commit or apply, nothing recorded")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.exception("traffic signal: rollback after a failed poll also failed")
        return False


# -- the sweep's view of the signal ------------------------------------------------------


def signal_state(db: Session) -> TrafficSignalState | None:
    return db.get(TrafficSignalState, SINGLETON_ID)


def signal_is_trusted(db: Session, *, now: datetime, cutoff: datetime) -> bool:
    """the last successful poll is at most `SIGNAL_MAX_AGE` old, and the signal has
    watched the whole idle window (`observed_since <= cutoff`, where `cutoff = now - threshold`)."""
    state = signal_state(db)
    if state is None:
        return False
    return (
        now - _aware(state.last_poll_at) <= SIGNAL_MAX_AGE and _aware(state.observed_since) <= cutoff
    )


def signal_is_configured(cfg: Settings) -> bool:
    return bool(cfg.traefik_enabled) and cfg.traefik_metrics_port is not None
