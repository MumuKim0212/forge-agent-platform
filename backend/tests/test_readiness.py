"""`GET /api/ready`.

Every state, the exact body, no authentication, and the bound: a hanging database answers 503 at
the timeout instead of hanging the request. The hanging factory gives up by itself after
`HANG_CEILING_SECONDS` so a regression (no bound) FAILS the elapsed-time assertion rather than
hanging the suite.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.main import app
from app.models.worker_lease import WorkerLease

HANG_CEILING_SECONDS = 6.0


def _wait_idle() -> None:
    """The abandoned check of a hang test ends a moment after `release.set()`; the next test must
    not meet its in-flight slot."""
    from app.routers import readiness

    deadline = time.monotonic() + 5
    while readiness._in_flight.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not readiness._in_flight.locked()


def _factory(db_session):
    return sessionmaker(autocommit=False, autoflush=False, bind=db_session.get_bind())


@pytest.fixture()
def ready_client(client, db_session):
    from app.routers.readiness import get_session_factory

    app.dependency_overrides[get_session_factory] = lambda: _factory(db_session)
    yield client
    app.dependency_overrides.pop(get_session_factory, None)


def _lease(db_session, *, heartbeat_age: float, holder: str | None = "abc") -> None:
    now = datetime.now(timezone.utc)
    db_session.add(
        WorkerLease(
            id="platform",
            holder_id=holder,
            holder_host="secret-host",
            holder_pid=4242,
            acquired_at=now - timedelta(minutes=5),
            heartbeat_at=now - timedelta(seconds=heartbeat_age),
        )
    )
    db_session.commit()


def test_ready_with_a_fresh_lease(ready_client, db_session):
    _lease(db_session, heartbeat_age=3)
    response = ready_client.get("/api/ready")  # no identity header: unauthenticated
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_not_ready_when_no_lease_row_exists_yet(ready_client):
    response = ready_client.get("/api/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "failing": ["worker"]}


def test_not_ready_when_the_lease_is_stale(ready_client, db_session):
    _lease(db_session, heartbeat_age=61)
    response = ready_client.get("/api/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "failing": ["worker"]}


def test_not_ready_when_the_lease_was_released(ready_client, db_session):
    _lease(db_session, heartbeat_age=1, holder=None)
    assert ready_client.get("/api/ready").json() == {"status": "not_ready", "failing": ["worker"]}


def test_not_ready_names_only_database_when_the_database_raises(client):
    from app.routers.readiness import get_session_factory

    def broken():
        raise RuntimeError("password=hunter2 host=db.internal")

    app.dependency_overrides[get_session_factory] = lambda: broken
    try:
        response = client.get("/api/ready")
    finally:
        app.dependency_overrides.pop(get_session_factory, None)
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "failing": ["database"]}
    assert "hunter2" not in response.text and "db.internal" not in response.text


def test_a_hanging_database_answers_503_within_the_timeout(client, monkeypatch):
    from app.routers.readiness import get_session_factory

    monkeypatch.setattr(settings, "readiness_timeout_seconds", 0.3)
    release = threading.Event()

    def hanging():
        release.wait(HANG_CEILING_SECONDS)
        raise RuntimeError("gave up waiting")

    app.dependency_overrides[get_session_factory] = lambda: hanging
    started = time.monotonic()
    try:
        response = client.get("/api/ready")
        elapsed = time.monotonic() - started
    finally:
        release.set()
        app.dependency_overrides.pop(get_session_factory, None)
        _wait_idle()
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "failing": ["database"]}
    assert elapsed < 2.0


def test_a_poll_during_a_hung_check_answers_503_at_once_and_starts_no_second_thread(
    client, monkeypatch
):
    from app.routers.readiness import get_session_factory

    monkeypatch.setattr(settings, "readiness_timeout_seconds", 0.5)
    release = threading.Event()
    calls = []

    def hanging():
        calls.append(1)
        release.wait(HANG_CEILING_SECONDS)
        raise RuntimeError("gave up waiting")

    app.dependency_overrides[get_session_factory] = lambda: hanging
    try:
        first = client.get("/api/ready")  # times out at 0.5 s, its thread still hung
        started = time.monotonic()
        second = client.get("/api/ready")
        second_elapsed = time.monotonic() - started
    finally:
        release.set()
        app.dependency_overrides.pop(get_session_factory, None)
        _wait_idle()
    assert first.json() == {"status": "not_ready", "failing": ["database"]}
    assert second.status_code == 503
    assert second.json() == {"status": "not_ready", "failing": ["database"]}
    assert second_elapsed < 0.3  # immediate, not another wait of the timeout
    assert len(calls) == 1  # no second check was started


def test_the_body_never_carries_lease_detail(ready_client, db_session):
    _lease(db_session, heartbeat_age=61)
    text = ready_client.get("/api/ready").text
    for leak in ("secret-host", "4242", "abc", "heartbeat"):
        assert leak not in text


def test_health_stays_the_static_liveness_check(client):
    assert client.get("/api/health").json() == {"status": "ok"}
