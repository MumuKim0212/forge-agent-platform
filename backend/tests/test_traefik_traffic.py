"""the traffic signal read from Traefik's request counters.

The exec seam is faked here (`TrafficSignalPoller(exec_fn=...)`, `docker_exec_read(client=...)`);
the measured behaviour against a real Traefik is in `tests/test_deploy_traefik_integration.py`."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.deployment import Deployment
from app.models.traffic_signal import TrafficSignalState
from app.services import traefik_traffic
from app.services.project_activity import _as_aware_utc
from app.services.status import ProjectStatus
from app.services.traefik_traffic import (
    ParsedMetrics,
    TrafficSignalError,
    TrafficSignalPoller,
    docker_exec_read,
    listens_beyond_loopback,
    parse_metrics,
)
from tests.test_worker import _active_deployment, _idle_project

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
STALE = T0 - timedelta(days=30)
START = T0 - timedelta(days=2)  # the Traefik process start time most tests share

LOOPBACK_TCP = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    "   0: 0100007F:1F92 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1 1 0\n"
    "   1: 00000000:0050 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 2 1 0\n"
)
TCP6_HEADER = "  sl  local_address                         remote_address                        st tx_queue\n"


def _service_lines(service: str, total: int) -> str:
    # Two series per service, so the sum over code/method is exercised.
    a, b = total // 2, total - total // 2
    return (
        f'traefik_service_requests_total{{code="200",method="GET",protocol="http",service="{service}"}} {a}\n'
        f'traefik_service_requests_total{{code="404",method="POST",protocol="http",service="{service}"}} {b}\n'
    )


def metrics_text(start: datetime, totals: dict[str, int]) -> str:
    body = "# HELP go_goroutines Number of goroutines\ngo_goroutines 12\n"
    body += f"process_start_time_seconds {start.timestamp()!r}\n"
    for service, total in totals.items():
        body += _service_lines(service, total)
    return body


class FakeTraefik:
    """The exec seam: a mutable metrics body plus what /proc/net/tcp* says."""

    def __init__(self, *, start=START, totals=None, tcp=LOOPBACK_TCP, tcp6=TCP6_HEADER):
        self.start = start
        self.totals: dict[str, int] = totals or {}
        self.tcp = tcp
        self.tcp6 = tcp6
        self.fail: Exception | None = None
        self.raw: str | None = None
        self.calls: list[str] = []

    def __call__(self, cmd: list[str], cap: int) -> str:
        self.calls.append(cmd[0] if cmd[0] != "sh" else "tcp6")
        if self.fail is not None:
            raise self.fail
        if cmd[0] == "wget":
            return self.raw if self.raw is not None else metrics_text(self.start, self.totals)
        if cmd[0] == "cat":
            return self.tcp
        if cmd[0] == "sh":
            return self.tcp6
        raise AssertionError(cmd)


def svc(deployment: Deployment) -> str:
    return f"forge-deploy-{deployment.id}@docker"


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setattr(settings, "traefik_enabled", True)
    monkeypatch.setattr(settings, "traefik_metrics_port", 8082)


@pytest.fixture()
def service(db_session, seeded_ids):
    project = _idle_project(
        db_session, seeded_ids, name="svc-a", status=ProjectStatus.ACTIVE, last_active_at=STALE
    )
    return project, _active_deployment(db_session, project)


def _last_active(db, project):
    db.refresh(project)
    return _as_aware_utc(project.last_active_at)


def _state(db) -> TrafficSignalState:
    db.expire_all()
    return db.get(TrafficSignalState, "platform")


def _aware(value: datetime) -> datetime:
    return _as_aware_utc(value)


# -- parsing --------------------------------------------------------------------------------


def test_parse_sums_over_every_other_label_and_reads_the_start_time():
    text = metrics_text(START, {"forge-deploy-a@docker": 7, "forge-deploy-b@docker": 1})
    parsed = parse_metrics(text)
    assert parsed.service_totals == {"forge-deploy-a@docker": 7.0, "forge-deploy-b@docker": 1.0}
    assert parsed.started_at == START


def test_parse_ignores_every_other_family_even_a_malformed_line_of_one():
    text = (
        metrics_text(START, {"forge-deploy-a@docker": 2})
        + "traefik_entrypoint_requests_total{broken\n"
        + "go_gc_duration_seconds this is not a number\n"
        + "traefik_service_requests_total_other{service=\"x\"} oops\n"  # a different family name
        + "process_start_time_seconds_extra garbage\n"
    )
    assert parse_metrics(text).service_totals == {"forge-deploy-a@docker": 2.0}


def test_parse_handles_escaped_label_values_scientific_values_and_timestamps():
    text = (
        'traefik_service_requests_total{code="200",service="forge-deploy-a@docker",x="a\\"b\\\\c\\nd"} 1.5e+01 1700000000000\n'
        "process_start_time_seconds 1.7e+09\n"
    )
    parsed = parse_metrics(text)
    assert parsed.service_totals == {"forge-deploy-a@docker": 15.0}


@pytest.mark.parametrize(
    "bad_line",
    [
        'traefik_service_requests_total{service="forge-deploy-a@docker"} notanumber',
        'traefik_service_requests_total{service="forge-deploy-a@docker"',
        'traefik_service_requests_total{service=forge-deploy-a@docker} 3',
        'traefik_service_requests_total{code="200"} 3',  # no service label
        'traefik_service_requests_total{service="a"} -1',
        'traefik_service_requests_total{service="a"} NaN',
        'traefik_service_requests_total{service="a"} 1 2 3',
        'traefik_service_requests_total{service="a\\q"} 1',
        "traefik_service_requests_total",
    ],
)
def test_parse_fails_on_a_malformed_line_of_the_service_family(bad_line):
    with pytest.raises(TrafficSignalError):
        parse_metrics(f"process_start_time_seconds 1.7e+09\n{bad_line}\n")


@pytest.mark.parametrize(
    "text",
    [
        "go_goroutines 1\n",  # no start time
        "process_start_time_seconds abc\n",
        "process_start_time_seconds 1.7e+09\nprocess_start_time_seconds 1.7e+09\n",
        "process_start_time_seconds -5\n",
    ],
)
def test_parse_fails_on_a_missing_or_malformed_start_time(text):
    with pytest.raises(TrafficSignalError):
        parse_metrics(text)


# -- the bounded docker exec read ------------------------------------------------------------


class _FakeApi:
    def __init__(self, chunks, exit_code=0):
        self.chunks = chunks
        self.exit_code = exit_code
        self.consumed = 0

    def exec_create(self, container_id, cmd, **kw):
        return {"Id": "x"}

    def exec_start(self, exec_id, stream=True, demux=True):
        def gen():
            for chunk in self.chunks:
                self.consumed += 1
                yield chunk

        return gen()

    def exec_inspect(self, exec_id):
        return {"Running": False, "ExitCode": self.exit_code}


class _FakeClient:
    def __init__(self, api, missing=False):
        self.api = api
        self._missing = missing

        class _Containers:
            def get(inner, name):
                if missing:
                    raise RuntimeError("no such container")

                class _C:
                    id = "cid"

                return _C()

        self.containers = _Containers()


def test_docker_exec_read_returns_stdout():
    api = _FakeApi([(b"hello ", None), (b"world", None), (None, b"warn")])
    assert docker_exec_read("t", ["wget"], 1000, client=_FakeClient(api)) == "hello world"


def test_docker_exec_read_over_the_cap_fails_without_reading_on():
    api = _FakeApi([(b"x" * 600, None)] * 10)
    with pytest.raises(TrafficSignalError, match="cap"):
        docker_exec_read("t", ["wget"], 1000, client=_FakeClient(api))
    assert api.consumed == 2  # stopped at the chunk that crossed the cap, did not drain the stream


def test_docker_exec_read_non_zero_exit_missing_container_and_bad_bytes_fail():
    with pytest.raises(TrafficSignalError, match="exited with 1"):
        docker_exec_read("t", ["wget"], 1000, client=_FakeClient(_FakeApi([(b"ok", None)], exit_code=1)))
    with pytest.raises(TrafficSignalError):
        docker_exec_read("t", ["wget"], 1000, client=_FakeClient(_FakeApi([]), missing=True))
    with pytest.raises(TrafficSignalError):
        docker_exec_read("t", ["wget"], 1000, client=_FakeClient(_FakeApi([(b"\xff\xfe", None)])))


# -- continuity rules -----------------------------------------------------------


def test_a_first_poll_takes_a_baseline_touches_nothing_and_sets_observed_since(db_session, service, configured):
    project, deployment = service
    fake = FakeTraefik(totals={svc(deployment): 5})

    assert TrafficSignalPoller(fake).poll(db_session, now=T0) is True

    assert _last_active(db_session, project) == STALE  # a baseline is not traffic
    db_session.refresh(deployment)
    assert deployment.traffic_counter_seen == 5.0
    state = _state(db_session)
    assert _aware(state.observed_since) == T0
    assert _aware(state.last_poll_at) == T0
    assert _aware(state.traefik_started_at) == START


def test_in_one_process_the_change_since_the_snapshot_is_recorded_even_after_days_away(
    db_session, service, configured
):
    project, deployment = service
    fake = FakeTraefik(totals={svc(deployment): 5})
    poller = TrafficSignalPoller(fake)
    poller.poll(db_session, now=T0)

    fake.totals[svc(deployment)] = 5
    poller.poll(db_session, now=T0 + timedelta(seconds=60))
    assert _last_active(db_session, project) == STALE  # no change, no touch

    fake.totals[svc(deployment)] = 9
    later = T0 + timedelta(days=3)  # the worker was away for three days
    poller.poll(db_session, now=later)

    assert _last_active(db_session, project) == later
    assert _aware(_state(db_session).observed_since) == T0  # same Traefik process: unbroken
    db_session.refresh(deployment)
    assert deployment.traffic_counter_seen == 9.0


def test_a_series_absent_from_the_snapshot_counts_from_zero(db_session, service, configured):
    project, deployment = service
    fake = FakeTraefik(totals={})  # the baseline has no series for this deployment
    poller = TrafficSignalPoller(fake)
    poller.poll(db_session, now=T0)
    db_session.refresh(deployment)
    assert deployment.traffic_counter_seen is None

    fake.totals[svc(deployment)] = 4  # its first request creates the series
    poller.poll(db_session, now=T0 + timedelta(seconds=60))

    assert _last_active(db_session, project) == T0 + timedelta(seconds=60)


def test_a_restart_within_fifteen_minutes_counts_the_current_values_and_keeps_observed_since(
    db_session, service, configured
):
    project, deployment = service
    fake = FakeTraefik(totals={svc(deployment): 5})
    poller = TrafficSignalPoller(fake)
    poller.poll(db_session, now=T0)

    # Traefik came back 30 s after the last poll. Its counter is 5 again, i.e. EQUAL to the snapshot:
    # a same-process reading would see no change, so a touch can only come from the restart rule.
    fake.start = T0 + timedelta(seconds=30)
    poll_time = T0 + timedelta(seconds=60)
    poller.poll(db_session, now=poll_time)

    assert _last_active(db_session, project) == poll_time
    assert _aware(_state(db_session).observed_since) == T0
    assert _aware(_state(db_session).traefik_started_at) == T0 + timedelta(seconds=30)


def test_a_restart_after_more_than_fifteen_minutes_moves_observed_since_to_the_new_start(
    db_session, service, configured
):
    project, deployment = service
    fake = FakeTraefik(totals={svc(deployment): 5})
    poller = TrafficSignalPoller(fake)
    poller.poll(db_session, now=T0)

    new_start = T0 + timedelta(minutes=40)  # 40 minutes after the last successful poll
    fake.start = new_start
    poll_time = T0 + timedelta(minutes=41)
    poller.poll(db_session, now=poll_time)

    assert _aware(_state(db_session).observed_since) == new_start
    assert _last_active(db_session, project) == poll_time  # traffic since the restart is still traffic


def test_the_fifteen_minute_restart_boundary_is_inclusive(db_session, service, configured):
    _, deployment = service
    fake = FakeTraefik(totals={svc(deployment): 5})
    poller = TrafficSignalPoller(fake)
    poller.poll(db_session, now=T0)

    fake.start = T0 + timedelta(minutes=15)
    poller.poll(db_session, now=T0 + timedelta(minutes=16))

    assert _aware(_state(db_session).observed_since) == T0


def test_one_series_going_down_with_an_unchanged_start_counts_its_value_and_is_not_a_gap(
    db_session, service, configured
):
    project, deployment = service
    fake = FakeTraefik(totals={svc(deployment): 10})
    poller = TrafficSignalPoller(fake)
    poller.poll(db_session, now=T0)

    fake.totals[svc(deployment)] = 4  # Traefik dropped and re-created the series; same process
    poll_time = T0 + timedelta(seconds=60)
    poller.poll(db_session, now=poll_time)

    assert _last_active(db_session, project) == poll_time
    assert _aware(_state(db_session).observed_since) == T0
    db_session.refresh(deployment)
    assert deployment.traffic_counter_seen == 4.0


def test_a_failure_at_commit_leaves_the_old_snapshot_and_the_next_poll_records_the_change(
    db_session, service, configured, monkeypatch
):
    project, deployment = service
    fake = FakeTraefik(totals={svc(deployment): 5})
    poller = TrafficSignalPoller(fake)
    poller.poll(db_session, now=T0)

    fake.totals[svc(deployment)] = 9
    real_commit = db_session.commit

    def failing_commit():
        raise RuntimeError("connection dropped")

    monkeypatch.setattr(db_session, "commit", failing_commit)
    assert poller.poll(db_session, now=T0 + timedelta(seconds=60)) is False
    monkeypatch.setattr(db_session, "commit", real_commit)

    assert _last_active(db_session, project) == STALE
    db_session.refresh(deployment)
    assert deployment.traffic_counter_seen == 5.0  # the old snapshot
    assert _aware(_state(db_session).last_poll_at) == T0  # "last successful poll" did not advance

    retry_time = T0 + timedelta(seconds=120)
    assert poller.poll(db_session, now=retry_time) is True

    assert _last_active(db_session, project) == retry_time
    db_session.refresh(deployment)
    assert deployment.traffic_counter_seen == 9.0


# -- attribution -----------------------------------------------------------------------------


def test_only_forge_deploy_series_that_resolve_to_a_deployment_count(db_session, service, seeded_ids, configured):
    project, deployment = service
    preview_project = _idle_project(
        db_session, seeded_ids, name="svc-preview", status=ProjectStatus.ACTIVE, last_active_at=STALE
    )
    preview_deployment = _active_deployment(db_session, preview_project)
    fake = FakeTraefik(totals={})
    poller = TrafficSignalPoller(fake)
    poller.poll(db_session, now=T0)

    fake.totals = {
        "forge-preview-bld-123@docker": 50,
        # a preview-named series whose suffix IS a deployment id: still not a deployed service
        f"forge-preview-{preview_deployment.id}@docker": 50,
        "forge-deploy-not-a-row@docker": 50,
        f"forge-deploy-{deployment.id}@file": 50,  # not the docker provider
        "api@internal": 50,
        svc(deployment): 1,
    }
    poll_time = T0 + timedelta(seconds=60)
    assert poller.poll(db_session, now=poll_time) is True

    assert _last_active(db_session, project) == poll_time
    assert _last_active(db_session, preview_project) == STALE


def test_a_preview_series_alone_does_not_advance_last_active_at(db_session, service, configured):
    project, deployment = service
    fake = FakeTraefik(totals={})
    poller = TrafficSignalPoller(fake)
    poller.poll(db_session, now=T0)
    fake.totals = {"forge-preview-bld-1@docker": 9}

    poller.poll(db_session, now=T0 + timedelta(seconds=60))

    assert _last_active(db_session, project) == STALE


def test_the_fifteen_minute_coarsening_still_applies(db_session, service, configured):
    project, deployment = service
    fake = FakeTraefik(totals={svc(deployment): 1})
    poller = TrafficSignalPoller(fake)
    poller.poll(db_session, now=T0)

    fake.totals[svc(deployment)] = 2
    poller.poll(db_session, now=T0 + timedelta(seconds=60))
    assert _last_active(db_session, project) == T0 + timedelta(seconds=60)

    fake.totals[svc(deployment)] = 3
    poller.poll(db_session, now=T0 + timedelta(minutes=5))  # within 15 min of the last touch
    assert _last_active(db_session, project) == T0 + timedelta(seconds=60)

    fake.totals[svc(deployment)] = 4
    poller.poll(db_session, now=T0 + timedelta(minutes=17))
    assert _last_active(db_session, project) == T0 + timedelta(minutes=17)


# -- a failed poll writes nothing ------------------------------------------------------------


@pytest.mark.parametrize(
    "break_it",
    [
        lambda f: setattr(f, "fail", TrafficSignalError("non-zero exit")),
        lambda f: setattr(f, "fail", TrafficSignalError("over the cap")),
        lambda f: setattr(f, "raw", "process_start_time_seconds 1.7e+09\ntraefik_service_requests_total{service=\"a\"} bad\n"),
        lambda f: setattr(f, "raw", "go_goroutines 1\n"),
    ],
    ids=["exec-failure", "cap", "malformed-line", "no-start-time"],
)
def test_a_failed_poll_writes_nothing_and_does_not_advance_the_last_successful_poll(
    db_session, service, configured, break_it
):
    project, deployment = service
    fake = FakeTraefik(totals={svc(deployment): 5})
    poller = TrafficSignalPoller(fake)
    poller.poll(db_session, now=T0)

    fake.totals[svc(deployment)] = 99
    break_it(fake)
    assert poller.poll(db_session, now=T0 + timedelta(seconds=60)) is False

    assert _last_active(db_session, project) == STALE
    db_session.refresh(deployment)
    assert deployment.traffic_counter_seen == 5.0
    assert _aware(_state(db_session).last_poll_at) == T0


def test_poll_does_nothing_unless_traefik_routes_and_the_port_is_set(db_session, service, monkeypatch):
    _, deployment = service
    fake = FakeTraefik(totals={svc(deployment): 5})
    monkeypatch.setattr(settings, "traefik_enabled", True)
    monkeypatch.setattr(settings, "traefik_metrics_port", None)
    assert TrafficSignalPoller(fake).poll(db_session, now=T0) is False
    monkeypatch.setattr(settings, "traefik_metrics_port", 8082)
    monkeypatch.setattr(settings, "traefik_enabled", False)
    assert TrafficSignalPoller(fake).poll(db_session, now=T0) is False
    assert fake.calls == []
    assert _state(db_session) is None


# -- the loopback-only check -----------------------------------------------------------------


def _tcp_line(host_hex, port, state="0A"):
    return f"   0: {host_hex}:{port:04X} 00000000:0000 {state} 00000000:00000000 00:00000000 00000000 0 0 1 1 0\n"


HEADER = "  sl  local_address rem_address   st tx_queue rx_queue\n"


@pytest.mark.parametrize(
    ("tcp_lines", "tcp6_lines", "expected"),
    [
        (_tcp_line("0100007F", 8082), "", False),  # 127.0.0.1 only
        (_tcp_line("0100007F", 8082), _tcp_line("00000000000000000000000001000000", 8082), False),  # + ::1
        (_tcp_line("0100007F", 8082), _tcp_line("0000000000000000FFFF00000100007F", 8082), False),  # mapped loopback
        (_tcp_line("00000000", 8082), "", True),  # 0.0.0.0 wildcard
        (_tcp_line("0100007F", 8082) + _tcp_line("020011AC", 8082), "", True),  # + 172.17.0.2
        (_tcp_line("0100007F", 8082), _tcp_line("00000000000000000000000000000000", 8082), True),  # :: wildcard
        (_tcp_line("00000000", 80) + _tcp_line("0100007F", 8082), "", False),  # another port is not ours
        (_tcp_line("020011AC", 8082, state="01"), "", False),  # not LISTEN
    ],
    ids=["v4-loopback", "v4+v6-loopback", "mapped-loopback", "v4-wildcard", "container-address", "v6-wildcard", "other-port", "not-listening"],
)
def test_listens_beyond_loopback(tcp_lines, tcp6_lines, expected):
    assert listens_beyond_loopback(HEADER + tcp_lines, HEADER + tcp6_lines, 8082) is expected


def test_a_non_loopback_listener_refuses_the_signal_logs_an_error_and_uses_no_poll(
    db_session, service, configured, caplog
):
    project, deployment = service
    wildcard = HEADER + _tcp_line("00000000", 8082)
    fake = FakeTraefik(totals={svc(deployment): 5}, tcp=wildcard)
    poller = TrafficSignalPoller(fake)

    with caplog.at_level(logging.ERROR, logger="app.traefik_traffic"):
        assert poller.poll(db_session, now=T0) is False
        assert poller.poll(db_session, now=T0 + timedelta(seconds=60)) is False

    assert _state(db_session) is None  # nothing persisted from a refused signal
    db_session.refresh(deployment)
    assert deployment.traffic_counter_seen is None
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1  # logged when the check ran, not on every poll
    assert "127.0.0.1:8082" in errors[0].getMessage()
    assert "loopback" in errors[0].getMessage()


def test_the_loopback_check_runs_at_start_and_again_only_after_a_detected_restart(db_session, service, configured):
    _, deployment = service
    fake = FakeTraefik(totals={svc(deployment): 5})
    poller = TrafficSignalPoller(fake)

    poller.poll(db_session, now=T0)
    poller.poll(db_session, now=T0 + timedelta(seconds=60))
    assert fake.calls.count("cat") == 1  # once, at the first poll of this process

    fake.start = T0 + timedelta(seconds=90)  # Traefik restarted
    poller.poll(db_session, now=T0 + timedelta(seconds=120))
    assert fake.calls.count("cat") == 2


def test_a_restart_that_exposes_the_port_is_refused_and_a_later_fix_is_picked_up(
    db_session, service, configured
):
    _, deployment = service
    fake = FakeTraefik(totals={svc(deployment): 5})
    poller = TrafficSignalPoller(fake)
    assert poller.poll(db_session, now=T0) is True

    fake.start = T0 + timedelta(seconds=90)
    fake.tcp = HEADER + _tcp_line("00000000", 8082)  # the operator's new Traefik binds the wildcard
    assert poller.poll(db_session, now=T0 + timedelta(seconds=120)) is False
    assert _aware(_state(db_session).last_poll_at) == T0  # the refused poll was not used

    fake.start = T0 + timedelta(seconds=300)  # fixed and restarted again
    fake.tcp = LOOPBACK_TCP
    assert poller.poll(db_session, now=T0 + timedelta(seconds=360)) is True


def test_an_unreadable_proc_file_is_a_failed_poll_not_a_pass(db_session, service, configured):
    _, deployment = service
    fake = FakeTraefik(totals={svc(deployment): 5}, tcp="garbage")

    assert TrafficSignalPoller(fake).poll(db_session, now=T0) is False
    assert _state(db_session) is None


def test_the_parsed_metrics_type_is_what_the_poller_consumes():
    # a guard against renaming the dataclass's fields without the rest following
    assert ParsedMetrics(started_at=START, service_totals={}).service_totals == {}
    assert traefik_traffic.POLL_INTERVAL_SECONDS == 60
    assert traefik_traffic.SIGNAL_MAX_AGE == timedelta(minutes=3)
    assert traefik_traffic.RESTART_CONTINUITY_TOLERANCE == timedelta(minutes=15)
