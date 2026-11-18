"""operator notifications over a webhook and email.

Nothing here is a fake of the thing under test. The webhook goes to a real local HTTP server and
the email to a real local SMTP server (STARTTLS and implicit TLS with a real handshake against a
generated certificate); the worker lease is the real `WorkerLease` over a real SQLite file; the
service check runs `run_service_checks` and the disk check `run_disk_check_if_due`. A receiver that
must hang or refuse is finite, so code without the bound under test fails an elapsed-time assertion
instead of hanging the run.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models.base import Base
from app.models.notification import NotificationChannelStatus, NotificationState
from app.models.project import Project
from app.models.worker_lease import SINGLETON_ID as LEASE_ROW_ID, WorkerLease as WorkerLeaseRow
from app.seed import seed
from app.services import host_disk, notifications, service_health
from app.services.service_health import run_service_checks
from app.services.worker_lease import STALE_AFTER_SECONDS, WorkerLease
from tests.notify_servers import SmtpSink, WebhookReceiver, make_certificate
from tests.test_service_health import FakeClient, FakeContainer, _activate

PUBLIC_ORIGIN = "https://forge.example.test"


@pytest.fixture()
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'notify.db').as_posix()}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        seed(db)
    yield engine
    engine.dispose()


@pytest.fixture()
def factory(engine):
    return sessionmaker(bind=engine, autoflush=False)


@pytest.fixture(autouse=True)
def _notify_settings(monkeypatch):
    monkeypatch.setattr(settings, "notify_retry_count", 2)
    monkeypatch.setattr(settings, "notify_retry_backoff_seconds", 0.0)
    monkeypatch.setattr(settings, "notify_webhook_connect_timeout_seconds", 2.0)
    monkeypatch.setattr(settings, "notify_webhook_read_timeout_seconds", 2.0)
    monkeypatch.setattr(settings, "notify_smtp_timeout_seconds", 2.0)
    monkeypatch.setattr(settings, "notify_cooldown_seconds", 3600.0)
    monkeypatch.setattr(settings, "platform_public_url", PUBLIC_ORIGIN)
    monkeypatch.setattr(settings, "service_check_failure_threshold", 1)


@pytest.fixture()
def webhook(monkeypatch):
    receivers: list[WebhookReceiver] = []

    def _make(**kwargs) -> WebhookReceiver:
        receiver = WebhookReceiver(**kwargs).start()
        receivers.append(receiver)
        return receiver

    yield _make
    for receiver in receivers:
        receiver.stop()


def _use_webhook(monkeypatch, receiver: WebhookReceiver) -> None:
    monkeypatch.setattr(settings, "notify_webhook_url", receiver.url)


def _use_smtp(monkeypatch, sink: SmtpSink, *, tls: str, username=None, password=None) -> None:
    monkeypatch.setattr(settings, "notify_smtp_host", "127.0.0.1")
    monkeypatch.setattr(settings, "notify_smtp_port", sink.port)
    monkeypatch.setattr(settings, "notify_smtp_tls", tls)
    monkeypatch.setattr(settings, "notify_smtp_username", username)
    monkeypatch.setattr(settings, "notify_smtp_password", password)
    monkeypatch.setattr(settings, "notify_smtp_from", "forge@example.test")
    monkeypatch.setattr(settings, "notify_email_to", "ops@example.test, oncall@example.test")


def _message(kind=notifications.EventKind.SERVICE_UNHEALTHY, name="lunch-ideas"):
    return notifications.make_message(kind, name, datetime(2026, 11, 15, 9, 30, tzinfo=timezone.utc))


def _state(factory, family, subject):
    with factory() as db:
        return db.get(NotificationState, (family, subject))


def _bodies(receiver: WebhookReceiver) -> list[dict]:
    return [json.loads(item["body"]) for item in receiver.requests]


# --------------------------------------------------------------------------------------------
# Webhook channel
# --------------------------------------------------------------------------------------------


def test_the_webhook_posts_a_json_body_with_a_text_field(monkeypatch, webhook):
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)

    assert notifications.send_webhook(_message()) is None

    [request] = receiver.requests
    assert request["path"] == "/hook/SECRET-PATH-TOKEN"
    assert request["headers"]["Content-Type"].startswith("application/json")
    body = json.loads(request["body"])
    assert body["text"] == "[Forge] Service unhealthy: lunch-ideas\n2026-11-15 09:30:00 UTC\nhttps://forge.example.test/admin"
    assert set(body) == {"text", "kind", "subject", "timestamp", "link"}
    assert (body["kind"], body["subject"]) == ("service_unhealthy", "lunch-ideas")


def test_a_failing_webhook_is_retried_a_fixed_number_of_times(monkeypatch, webhook):
    receiver = webhook(status=500)
    _use_webhook(monkeypatch, receiver)

    assert notifications.send_webhook(_message()) == "HTTP_500"
    assert len(receiver.requests) == 3  # the first attempt and notify_retry_count (2) retries


def test_a_webhook_that_recovers_within_the_retries_succeeds(monkeypatch, webhook):
    receiver = webhook(status=[503, 502, 200])
    _use_webhook(monkeypatch, receiver)

    assert notifications.send_webhook(_message()) is None
    assert len(receiver.requests) == 3


def test_retries_follow_the_setting(monkeypatch, webhook):
    receiver = webhook(status=500)
    _use_webhook(monkeypatch, receiver)
    monkeypatch.setattr(settings, "notify_retry_count", 0)

    assert notifications.send_webhook(_message()) == "HTTP_500"
    assert len(receiver.requests) == 1


def test_a_client_error_is_not_retried(monkeypatch, webhook):
    receiver = webhook(status=404)
    _use_webhook(monkeypatch, receiver)

    assert notifications.send_webhook(_message()) == "HTTP_404"
    assert len(receiver.requests) == 1


def test_a_redirect_is_not_followed(monkeypatch, webhook):
    elsewhere = webhook()
    first = webhook(redirect_to=f"http://127.0.0.1:{elsewhere.port}/stolen")
    _use_webhook(monkeypatch, first)

    assert notifications.send_webhook(_message()) == "HTTP_302"
    assert len(first.requests) == 1
    assert elsewhere.requests == []  # the credential-bearing request never went where it was sent on to


def test_a_slow_receiver_is_given_up_on_within_the_read_timeout(monkeypatch, webhook):
    receiver = webhook(delay=4.0)  # finite: without the bound this returns success after 4 s, not never
    _use_webhook(monkeypatch, receiver)
    monkeypatch.setattr(settings, "notify_webhook_read_timeout_seconds", 0.3)
    monkeypatch.setattr(settings, "notify_retry_count", 0)

    started = time.monotonic()
    error = notifications.send_webhook(_message())
    elapsed = time.monotonic() - started

    assert error == "ReadTimeout"
    assert elapsed < 2.0


def test_an_unreachable_receiver_is_a_recorded_class_not_an_exception(monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # closed again: nothing listens there
    monkeypatch.setattr(settings, "notify_webhook_url", f"http://127.0.0.1:{port}/hook/SECRET-PATH-TOKEN")
    monkeypatch.setattr(settings, "notify_retry_count", 1)
    monkeypatch.setattr(settings, "notify_webhook_connect_timeout_seconds", 0.5)

    # A closed port refuses at once on Linux and is silently dropped for a while on Windows: both
    # are "unreachable", and either way the call is bounded and ends as a class name.
    assert notifications.send_webhook(_message()) in ("ConnectError", "ConnectTimeout")


def test_nothing_logged_carries_the_url_or_the_response_body(monkeypatch, webhook, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(settings, "notify_retry_count", 1)
    monkeypatch.setattr(settings, "notify_webhook_connect_timeout_seconds", 0.5)
    failing = webhook(status=500)
    _use_webhook(monkeypatch, failing)
    notifications.send_webhook(_message())
    _use_webhook(monkeypatch, webhook(status=200))
    notifications.send_webhook(_message())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(settings, "notify_webhook_url", f"http://127.0.0.1:{port}/hook/SECRET-PATH-TOKEN")
    notifications.send_webhook(_message())

    assert "SECRET-PATH-TOKEN" not in caplog.text
    assert "secret-response-body-do-not-log" not in caplog.text


# --------------------------------------------------------------------------------------------
# Email channel
# --------------------------------------------------------------------------------------------


@pytest.fixture()
def smtp(tmp_path):
    sinks: list[SmtpSink] = []
    cert = make_certificate(tmp_path)

    def _make(**kwargs) -> SmtpSink:
        if kwargs.get("tls"):
            kwargs["cert"] = cert
        sink = SmtpSink(**kwargs).start()
        sinks.append(sink)
        return sink

    _make.cert_path = cert[0]
    yield _make
    for sink in sinks:
        sink.stop()


def _trust(monkeypatch, smtp) -> None:
    """The one place a test replaces the verifier: the sink's self-signed certificate is trusted, the
    handshake and the host-name check are still real."""
    import ssl

    monkeypatch.setattr(notifications, "_ssl_context", lambda: ssl.create_default_context(cafile=smtp.cert_path))


def test_an_email_reaches_every_recipient_over_plain_smtp_in_dev(monkeypatch, smtp):
    sink = smtp()
    _use_smtp(monkeypatch, sink, tls="none")

    assert notifications.send_email(_message()) is None

    [mail] = sink.messages
    assert mail["rcpts"] == ["<ops@example.test>", "<oncall@example.test>"]
    assert "Subject: [Forge] Service unhealthy: lunch-ideas" in mail["data"]
    assert "From: forge@example.test" in mail["data"]
    assert "https://forge.example.test/admin" in mail["data"]
    assert mail["secure"] is False


def test_starttls_encrypts_before_the_credentials_are_sent(monkeypatch, smtp):
    sink = smtp(tls="starttls", require_auth=True)  # refuses AUTH with 530 until the session is encrypted
    _trust(monkeypatch, smtp)
    _use_smtp(monkeypatch, sink, tls="starttls", username="mailer", password="smtp-pass-marker")

    assert notifications.send_email(_message()) is None

    [mail] = sink.messages
    assert mail["secure"] is True
    assert mail["auth"] == ("mailer", "smtp-pass-marker")


def test_implicit_tls_delivers(monkeypatch, smtp):
    sink = smtp(tls="ssl")
    _trust(monkeypatch, smtp)
    _use_smtp(monkeypatch, sink, tls="ssl")

    assert notifications.send_email(_message()) is None
    assert [m["secure"] for m in sink.messages] == [True]


def test_a_certificate_that_does_not_verify_sends_nothing(monkeypatch, smtp):
    sink = smtp(tls="starttls", require_auth=True)
    # The default context: a self-signed certificate is not trusted.
    _use_smtp(monkeypatch, sink, tls="starttls", username="mailer", password="smtp-pass-marker")
    monkeypatch.setattr(settings, "notify_retry_count", 0)

    assert notifications.send_email(_message()) == "SSLCertVerificationError"
    assert sink.messages == []


def test_the_default_tls_context_verifies_the_certificate_and_the_host_name():
    import ssl

    context = notifications._ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_a_temporary_smtp_failure_is_retried(monkeypatch, smtp):
    sink = smtp(temp_fail_first=1)
    _use_smtp(monkeypatch, sink, tls="none")

    assert notifications.send_email(_message()) is None
    assert len(sink.messages) == 1
    assert sink.connections == 2


def test_an_unreachable_smtp_server_is_a_recorded_class(monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(settings, "notify_smtp_host", "127.0.0.1")
    monkeypatch.setattr(settings, "notify_smtp_port", port)
    monkeypatch.setattr(settings, "notify_smtp_tls", "none")
    monkeypatch.setattr(settings, "notify_smtp_from", "forge@example.test")
    monkeypatch.setattr(settings, "notify_email_to", "ops@example.test")
    monkeypatch.setattr(settings, "notify_retry_count", 0)
    monkeypatch.setattr(settings, "notify_smtp_timeout_seconds", 0.5)

    assert notifications.send_email(_message()) in ("ConnectionRefusedError", "TimeoutError")


def test_an_smtp_server_that_never_answers_is_given_up_on(monkeypatch):
    held: list[socket.socket] = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    stop = threading.Event()

    def _accept():
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                held.append(listener.accept()[0])  # accepted, never greeted
            except OSError:
                pass

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(settings, "notify_smtp_host", "127.0.0.1")
        monkeypatch.setattr(settings, "notify_smtp_port", listener.getsockname()[1])
        monkeypatch.setattr(settings, "notify_smtp_tls", "none")
        monkeypatch.setattr(settings, "notify_smtp_from", "forge@example.test")
        monkeypatch.setattr(settings, "notify_email_to", "ops@example.test")
        monkeypatch.setattr(settings, "notify_smtp_timeout_seconds", 0.3)
        monkeypatch.setattr(settings, "notify_retry_count", 0)

        started = time.monotonic()
        error = notifications.send_email(_message())
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        thread.join(2)
        for sock in held:
            sock.close()
        listener.close()

    assert error in ("TimeoutError", "SMTPServerDisconnected")
    assert elapsed < 2.0


def test_nothing_logged_carries_the_smtp_password(monkeypatch, smtp, caplog):
    caplog.set_level(logging.DEBUG)
    sink = smtp(tls="starttls", require_auth=True)
    _use_smtp(monkeypatch, sink, tls="starttls", username="mailer", password="smtp-pass-marker")
    monkeypatch.setattr(settings, "notify_retry_count", 0)
    notifications.deliver(_message(), lambda: (_ for _ in ()).throw(RuntimeError("no db")))  # status write fails too

    assert "smtp-pass-marker" not in caplog.text


# --------------------------------------------------------------------------------------------
# De-duplication, recovery, cooldown, restart
# --------------------------------------------------------------------------------------------


def _report(factory, healthy, *, family="worker", subject="worker", name="Forge worker", now=None):
    return notifications.report(factory, family=family, subject=subject, name=name, healthy=healthy, now=now)


def test_down_is_announced_once_however_often_it_is_reported(monkeypatch, webhook, factory):
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)

    assert [_report(factory, False) for _ in range(4)] == ["worker_down", None, None, None]

    assert notifications.flush()
    assert [b["kind"] for b in _bodies(receiver)] == ["worker_down"]


def test_recovery_is_announced_once(monkeypatch, webhook, factory):
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)
    _report(factory, False)

    assert [_report(factory, True) for _ in range(3)] == ["worker_recovered", None, None]

    assert notifications.flush()
    assert [b["kind"] for b in _bodies(receiver)] == ["worker_down", "worker_recovered"]


def test_a_subject_that_was_never_down_says_nothing_when_healthy(monkeypatch, webhook, factory):
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)

    assert _report(factory, True) is None
    assert notifications.flush()
    assert receiver.requests == []
    assert _state(factory, "worker", "worker") is None


def test_a_new_down_inside_the_cooldown_is_not_announced_and_neither_is_its_recovery(monkeypatch, webhook, factory):
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)
    t0 = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)
    _report(factory, False, now=t0)
    _report(factory, True, now=t0 + timedelta(minutes=1))

    assert _report(factory, False, now=t0 + timedelta(minutes=2)) is None  # a flap, inside the hour
    assert _report(factory, True, now=t0 + timedelta(minutes=3)) is None  # nothing was announced to recover from

    assert notifications.flush()
    assert [b["kind"] for b in _bodies(receiver)] == ["worker_down", "worker_recovered"]


def test_a_new_down_after_the_cooldown_is_announced_again(monkeypatch, webhook, factory):
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)
    t0 = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)
    _report(factory, False, now=t0)
    _report(factory, True, now=t0 + timedelta(minutes=1))

    assert _report(factory, False, now=t0 + timedelta(hours=1, minutes=1)) == "worker_down"
    assert _report(factory, True, now=t0 + timedelta(hours=1, minutes=2)) == "worker_recovered"

    assert notifications.flush()
    assert [b["kind"] for b in _bodies(receiver)] == [
        "worker_down", "worker_recovered", "worker_down", "worker_recovered",
    ]


def test_a_restart_that_finds_the_same_state_sends_nothing(monkeypatch, webhook, tmp_path, engine):
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)
    _report(sessionmaker(bind=engine), False)
    assert notifications.flush()
    assert len(receiver.requests) == 1

    # "Restart": a new engine and session factory on the same database, as a fresh process has.
    reborn = create_engine(str(engine.url), connect_args={"check_same_thread": False})
    try:
        again = sessionmaker(bind=reborn)
        assert _report(again, False) is None  # still down: nothing new
        assert notifications.flush()
        assert len(receiver.requests) == 1
        assert _report(again, True) == "worker_recovered"  # the recovery is still owed, once
        assert _report(sessionmaker(bind=reborn), True) is None
        assert notifications.flush()
        assert len(receiver.requests) == 2
    finally:
        reborn.dispose()


def test_nothing_is_recorded_or_sent_when_no_channel_is_configured(factory):
    assert _report(factory, False) is None
    assert _state(factory, "worker", "worker") is None


# --------------------------------------------------------------------------------------------
# One test per transition, through the code that finds it
# --------------------------------------------------------------------------------------------


def _service_pass(factory, deployment_id, container):
    with factory() as db:
        return run_service_checks(db, client=FakeClient([container]))


def test_service_unhealthy_is_announced_with_the_project_name(monkeypatch, webhook, factory):
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)
    deployment_id, _project_id = _activate(factory, "lunch-ideas")

    _service_pass(factory, deployment_id, FakeContainer(deployment_id, status="exited"))

    assert notifications.flush()
    [body] = _bodies(receiver)
    assert (body["kind"], body["subject"]) == ("service_unhealthy", "lunch-ideas")


def test_service_recovered_is_announced(monkeypatch, webhook, factory):
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)
    deployment_id, _project_id = _activate(factory, "lunch-ideas")
    _service_pass(factory, deployment_id, FakeContainer(deployment_id, status="exited"))

    _service_pass(factory, deployment_id, FakeContainer(deployment_id))
    _service_pass(factory, deployment_id, FakeContainer(deployment_id))  # a second pass: nothing new

    assert notifications.flush()
    assert [b["kind"] for b in _bodies(receiver)] == ["service_unhealthy", "service_recovered"]


def test_a_service_that_keeps_failing_is_announced_once(monkeypatch, webhook, factory):
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)
    deployment_id, _project_id = _activate(factory, "lunch-ideas")

    for _ in range(4):
        _service_pass(factory, deployment_id, FakeContainer(deployment_id, status="exited"))

    assert notifications.flush()
    assert len(receiver.requests) == 1


def test_an_inconclusive_check_announces_nothing(monkeypatch, webhook, factory):
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)
    deployment_id, _project_id = _activate(factory, "lunch-ideas")
    with factory() as db:
        from docker.errors import DockerException

        run_service_checks(db, client=FakeClient(error=DockerException("daemon away")))

    assert notifications.flush()
    assert receiver.requests == []


@pytest.fixture()
def disk_env(tmp_path, monkeypatch):
    root = tmp_path / "dockerroot"
    root.mkdir()

    class _Docker:
        def info(self):
            return {"DockerRootDir": str(root)}

    gib = 1024**3
    state = {"free": 500 * gib}

    def usage(path):
        import os

        if os.path.abspath(path) == str(root):
            return (1000 * gib, 1000 * gib - state["free"], state["free"])
        return (1000 * gib, 0, 1000 * gib)

    monkeypatch.setattr(settings, "host_disk_min_free_percent", 10.0)
    monkeypatch.setattr(settings, "host_disk_min_free_gb", 5.0)
    monkeypatch.setattr(settings, "local_git_root", str(tmp_path / "git"))
    monkeypatch.setattr(settings, "service_data_backup_dir", str(tmp_path / "backups"))
    return _Docker(), usage, state, gib


def test_disk_low_is_announced_once_and_recovery_once(monkeypatch, webhook, factory, disk_env):
    docker, usage, state, gib = disk_env
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)

    def _run():
        with factory() as db:
            host_disk.run_disk_check_if_due(db, docker_client=docker, usage_fn=usage)

    _run()  # plenty free: nothing to say
    state["free"] = 2 * gib
    _run()
    _run()  # still low: nothing new
    state["free"] = 500 * gib
    _run()
    _run()

    assert notifications.flush()
    bodies = _bodies(receiver)
    assert [(b["kind"], b["subject"]) for b in bodies] == [
        ("disk_low", "Docker data root"),
        ("disk_recovered", "Docker data root"),
    ]


def test_an_unmeasurable_disk_is_not_an_alarm_and_not_a_recovery(monkeypatch, webhook, factory, disk_env):
    docker, usage, state, gib = disk_env
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)
    state["free"] = 2 * gib
    with factory() as db:
        host_disk.run_disk_check_if_due(db, docker_client=docker, usage_fn=usage)

    class _Broken:
        def info(self):
            raise RuntimeError("daemon away")

    with factory() as db:
        host_disk.run_disk_check_if_due(db, docker_client=_Broken(), usage_fn=usage)

    assert notifications.flush()
    assert [b["kind"] for b in _bodies(receiver)] == ["disk_low"]


def _lease(factory) -> WorkerLease:
    lease = WorkerLease(factory)
    lease.acquire()
    return lease


def _age_lease(factory, seconds: float) -> None:
    with factory() as db:
        db.execute(
            update(WorkerLeaseRow).values(heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=seconds))
        )
        db.commit()


def test_worker_down_and_recovered_are_found_from_the_lease(monkeypatch, webhook, factory):
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)
    lease = _lease(factory)
    detector = notifications.WorkerDownDetector(factory)

    assert detector.run_cycle() is None  # a fresh lease
    _age_lease(factory, STALE_AFTER_SECONDS + 30)
    assert detector.run_cycle() == "worker_down"
    assert detector.run_cycle() is None
    assert lease.renew() is True
    assert detector.run_cycle() == "worker_recovered"
    assert detector.run_cycle() is None

    assert notifications.flush()
    assert [b["kind"] for b in _bodies(receiver)] == ["worker_down", "worker_recovered"]


def test_a_graceful_release_and_a_never_held_lease_say_nothing(monkeypatch, webhook, factory):
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)
    detector = notifications.WorkerDownDetector(factory)
    assert detector.run_cycle() is None  # no lease row at all
    lease = _lease(factory)
    assert lease.release() is True

    assert detector.run_cycle() is None  # released: stopped on purpose, not down

    assert notifications.flush()
    assert receiver.requests == []


class _BarrierSession(Session):
    """Holds every reader of a notification row until all the racers have read it."""

    barrier: threading.Barrier | None = None

    def get(self, entity, ident, **kwargs):
        row = super().get(entity, ident, **kwargs)
        if entity is NotificationState and type(self).barrier is not None:
            type(self).barrier.wait(5)
        return row


def _race(engine, monkeypatch, receiver, *, row_present: bool):
    monkeypatch.setattr(_BarrierSession, "barrier", threading.Barrier(2))
    _use_webhook(monkeypatch, receiver)
    now = datetime.now(timezone.utc)
    with sessionmaker(bind=engine)() as db:
        db.add(WorkerLeaseRow(id=LEASE_ROW_ID, holder_id="dead", holder_host="h", holder_pid=1,
                              acquired_at=now - timedelta(hours=1),
                              heartbeat_at=now - timedelta(seconds=STALE_AFTER_SECONDS + 60)))
        if row_present:
            db.add(NotificationState(family="worker", subject="worker", state="UP", changed_at=now,
                                     down_notified=False))
        db.commit()
    detectors = [
        notifications.WorkerDownDetector(lambda: _BarrierSession(bind=engine)) for _ in range(2)
    ]
    results: list = []
    threads = [threading.Thread(target=lambda d=d: results.append(d.run_cycle())) for d in detectors]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert notifications.flush()
    return results, time.monotonic() - started


def test_two_detectors_racing_on_a_stale_lease_send_exactly_one_notice(monkeypatch, webhook, engine):
    receiver = webhook()

    results, elapsed = _race(engine, monkeypatch, receiver, row_present=True)

    assert sorted(r or "" for r in results) == ["", "worker_down"]
    assert len(receiver.requests) == 1
    assert elapsed < 15


def test_two_detectors_racing_to_record_the_first_down_send_exactly_one_notice(monkeypatch, webhook, engine):
    receiver = webhook()

    results, elapsed = _race(engine, monkeypatch, receiver, row_present=False)

    assert sorted(r or "" for r in results) == ["", "worker_down"]
    assert len(receiver.requests) == 1
    assert elapsed < 15


class _StaleReadSession(Session):
    """Answers every read of a notification row with a fixed, possibly out-of-date, copy."""

    stale: NotificationState | None = None

    def get(self, entity, ident, **kwargs):
        if entity is NotificationState and type(self).stale is not None:
            return type(self).stale
        return super().get(entity, ident, **kwargs)


def _stale_claim(engine, monkeypatch, *, stale_state: str, first_healthy: bool, again_healthy: bool, down_notified):
    """Completes one claim, then claims the same transition again from a view that is stale: the
    conditional UPDATE is the only thing that can notice the row has already moved on."""
    now = datetime.now(timezone.utc)
    with sessionmaker(bind=engine)() as db:
        db.add(NotificationState(family="worker", subject="worker", state=stale_state, changed_at=now,
                                 down_notified=down_notified))
        db.commit()
    plain = lambda: Session(bind=engine)  # noqa: E731
    first = notifications._claim_transition(plain, "worker", "worker", first_healthy, now)
    monkeypatch.setattr(
        _StaleReadSession, "stale",
        NotificationState(family="worker", subject="worker", state=stale_state, changed_at=now,
                          down_notified=down_notified),
    )
    again = notifications._claim_transition(
        lambda: _StaleReadSession(bind=engine), "worker", "worker", again_healthy, now
    )
    return first, again


def test_a_claim_made_from_a_stale_view_of_a_down_transition_announces_nothing(monkeypatch, engine):
    first, again = _stale_claim(engine, monkeypatch, stale_state="UP", first_healthy=False,
                                again_healthy=False, down_notified=False)

    assert first == "worker_down"
    assert again is None  # the row is DOWN now: the UPDATE matches nothing


def test_a_claim_made_from_a_stale_view_of_a_recovery_announces_nothing(monkeypatch, engine):
    first, again = _stale_claim(engine, monkeypatch, stale_state="DOWN", first_healthy=True,
                                again_healthy=True, down_notified=True)

    assert first == "worker_recovered"
    assert again is None


def test_the_detector_thread_survives_a_cycle_that_raises(monkeypatch, webhook, factory):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database away")
        return factory()

    detector = notifications.WorkerDownDetector(flaky, interval=0.05)
    detector.start()
    try:
        deadline = time.monotonic() + 5
        while detector.cycles < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert detector.cycles >= 4
        assert detector.alive
    finally:
        detector.stop()
    assert not detector.alive


# --------------------------------------------------------------------------------------------
# Payload allowlist
# --------------------------------------------------------------------------------------------


MARKERS = {
    "service env": "SVCENV-marker-9f31",
    "preview token": "PREVIEWTOKEN-marker-5c22",
    "preview url": "https://preview.example.test/x?token=PREVIEWURL-marker-71aa",
    "dsn": "postgresql://admin:DSN-marker-0b17@db.example.test/forge",
    "signing secret": "SIGNING-marker-44de",
    "smtp password": "SMTPPASS-marker-8e90",
}


def test_no_secret_reaches_either_channel(monkeypatch, webhook, smtp, factory, disk_env):
    receiver = webhook()
    sink = smtp(require_auth=True)
    _use_webhook(monkeypatch, receiver)
    _use_smtp(monkeypatch, sink, tls="none", username="mailer", password=MARKERS["smtp password"])
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", MARKERS["dsn"])
    monkeypatch.setattr(settings, "preview_signing_secret", MARKERS["signing secret"])
    monkeypatch.setattr(settings, "session_signing_secret", MARKERS["signing secret"])
    deployment_id, project_id = _activate(factory, "lunch-ideas")
    with factory() as db:
        project = db.get(Project, project_id)
        project.service_env_json = json.dumps({"SECRET_KEY": MARKERS["service env"]})
        from app.models.build import Build

        build = db.scalars(select(Build).where(Build.project_id == project_id)).one()
        build.preview_token_hash = MARKERS["preview token"]
        build.preview_url = MARKERS["preview url"]
        db.commit()

    # All three families: a service, the disk, and the worker.
    _service_pass(factory, deployment_id, FakeContainer(deployment_id, status="exited"))
    docker, usage, state, gib = disk_env
    state["free"] = 2 * gib
    with factory() as db:
        host_disk.run_disk_check_if_due(db, docker_client=docker, usage_fn=usage)
    _lease(factory)
    _age_lease(factory, STALE_AFTER_SECONDS + 30)
    notifications.WorkerDownDetector(factory).run_cycle()
    notifications.send_test_message(factory)
    assert notifications.flush()

    assert len(receiver.requests) == 4  # service, disk, worker, test
    wire = b"\n".join(item["body"] for item in receiver.requests).decode()
    wire += "\n".join(mail["data"] for mail in sink.messages)
    assert len(sink.messages) == 4
    for what, marker in MARKERS.items():
        assert marker not in wire, f"the {what} reached a channel"
    assert "SECRET-PATH-TOKEN" not in wire  # the webhook URL is not echoed into its own payload
    assert "lunch-ideas" in wire  # not vacuous: the project name is the one free-text field
    for body in _bodies(receiver):
        assert set(body) == {"text", "kind", "subject", "timestamp", "link"}


def test_a_hostile_project_name_cannot_inject_markup_or_headers(monkeypatch, smtp, webhook):
    sink = smtp()
    _use_smtp(monkeypatch, sink, tls="none")
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)
    name = "x\r\nBcc: attacker@example.test <!channel> <https://evil.example|click>" + "y" * 200

    notifications.deliver(_message(name=name), lambda: (_ for _ in ()).throw(RuntimeError("no db")))

    [mail] = sink.messages
    assert "\nBcc:" not in mail["data"]
    [request] = receiver.requests
    text = json.loads(request["body"])["text"]
    assert "<" not in text.split("\n")[0] and ">" not in text.split("\n")[0]
    assert len(json.loads(request["body"])["subject"]) <= notifications.SUBJECT_NAME_MAX


def test_the_stripped_set_is_the_repo_guards_set():
    from tests.test_no_raw_bidi_controls import INVISIBLE

    kept = {cp for cp in INVISIBLE if not notifications._is_control(chr(cp))}
    assert {cp for cp in kept if notifications._is_invisible(chr(cp))} == kept
    assert not any(notifications._is_invisible(chr(cp)) for cp in range(0x110000) if cp not in INVISIBLE)
    # every member of the guard's set is gone from a name, whether it is dropped or turned into a space
    for cp in sorted(INVISIBLE):
        assert notifications.clean_name(f"a{chr(cp)}b") in ("ab", "a b"), hex(cp)


def test_invisible_and_direction_controls_in_a_name_reach_neither_channel(monkeypatch, smtp, webhook):
    import email
    import email.policy

    from tests.test_no_raw_bidi_controls import INVISIBLE

    sink = smtp()
    _use_smtp(monkeypatch, sink, tls="none")
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)
    ordered = sorted(INVISIBLE)
    chunks = [ordered[i : i + 20] for i in range(0, len(ordered), 20)]
    for chunk in chunks:
        name = "".join("n" + chr(cp) for cp in chunk)
        notifications.deliver(_message(name=name), lambda: (_ for _ in ()).throw(RuntimeError("no db")))

    assert len(receiver.requests) == len(chunks)
    assert len(sink.messages) == len(chunks)
    for body in _bodies(receiver):
        for field in ("text", "subject"):
            assert not any(ord(ch) in INVISIBLE and ch not in "\n" for ch in body[field]), field
    for mail in sink.messages:
        parsed = email.message_from_string(mail["data"], policy=email.policy.default)
        assert not any(ord(ch) in INVISIBLE for ch in str(parsed["Subject"]))
        assert str(parsed["Subject"]).startswith("[Forge] ") and "n" in str(parsed["Subject"])


# --------------------------------------------------------------------------------------------
# Best-effort: delivery never fails or delays what triggered it
# --------------------------------------------------------------------------------------------


def test_a_dead_receiver_does_not_fail_the_service_check_that_found_the_news(monkeypatch, webhook, factory):
    receiver = webhook(status=500)
    _use_webhook(monkeypatch, receiver)
    deployment_id, _project_id = _activate(factory, "lunch-ideas")

    checked = _service_pass(factory, deployment_id, FakeContainer(deployment_id, status="exited"))

    assert checked == 1
    assert notifications.flush()
    with factory() as db:
        assert db.scalars(select(service_health.ServiceHealthCheck)).one().state == "UNHEALTHY"
        status = db.get(NotificationChannelStatus, "webhook")
        assert (status.last_result, status.last_error_class) == ("FAILED", "HTTP_500")
    assert len(receiver.requests) == 3  # tried, retried, and recorded


def test_reporting_never_raises_even_when_the_state_store_is_broken(monkeypatch, webhook):
    _use_webhook(monkeypatch, webhook())

    def broken():
        raise RuntimeError("database away")

    assert notifications.report(broken, family="worker", subject="worker", name="w", healthy=False) is None


def test_a_raising_notifier_does_not_fail_the_service_pass(monkeypatch, webhook, factory):
    _use_webhook(monkeypatch, webhook())
    deployment_id, _project_id = _activate(factory, "lunch-ideas")

    def boom(*args, **kwargs):
        raise RuntimeError("the notifier is broken")

    monkeypatch.setattr(notifications, "report", boom)

    assert _service_pass(factory, deployment_id, FakeContainer(deployment_id, status="exited")) == 1
    with factory() as db:
        assert db.scalars(select(service_health.ServiceHealthCheck)).one().state == "UNHEALTHY"


def test_a_slow_receiver_does_not_hold_up_the_caller(monkeypatch, webhook, factory):
    receiver = webhook(delay=1.5)  # finite
    _use_webhook(monkeypatch, receiver)
    monkeypatch.setattr(settings, "notify_retry_count", 0)

    started = time.monotonic()
    kind = _report(factory, False)
    elapsed = time.monotonic() - started

    assert kind == "worker_down"
    assert elapsed < 1.0  # delivery is on the sender's thread
    assert notifications.flush(10)  # and it does happen
    assert len(receiver.requests) == 1


def test_the_sender_survives_a_delivery_that_raises(monkeypatch, webhook, factory):
    receiver = webhook()
    _use_webhook(monkeypatch, receiver)
    real = notifications.deliver
    calls = {"n": 0}

    def flaky(message, session_factory):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("delivery blew up")
        return real(message, session_factory)

    monkeypatch.setattr(notifications, "deliver", flaky)
    _report(factory, False, subject="a", name="a")
    _report(factory, False, subject="b", name="b")

    assert notifications.flush(10)
    assert [b["subject"] for b in _bodies(receiver)] == ["b"]


def test_a_channel_failure_is_recorded_per_channel_by_class_only(monkeypatch, webhook, smtp, factory):
    receiver = webhook(status=500)
    sink = smtp()
    _use_webhook(monkeypatch, receiver)
    _use_smtp(monkeypatch, sink, tls="none")

    results = notifications.deliver(_message(), factory)

    assert {(r.channel, r.ok, r.error_class) for r in results} == {("webhook", False, "HTTP_500"), ("email", True, None)}
    with factory() as db:
        rows = {r.channel: r for r in db.scalars(select(NotificationChannelStatus))}
    assert (rows["webhook"].last_result, rows["webhook"].last_error_class) == ("FAILED", "HTTP_500")
    assert (rows["email"].last_result, rows["email"].last_error_class) == ("OK", None)
    assert rows["webhook"].last_attempt_at is not None
