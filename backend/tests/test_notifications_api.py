"""the settings, the console's channel state, the admin-only "send test message", and the
detector's start-up wiring. Receivers are real local servers (`tests/notify_servers.py`)."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.main as app_main
from app.config import NotifyConfigError, Settings, settings
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.user import User
from app.schema_check import alembic_config
from app.routers import notifications as notifications_router
from app.services.roles import Role
from tests.notify_servers import WebhookReceiver
from tests.test_config import (
    OIDC_CONFIG,
    PROD_PLATFORM_ORIGIN,
    PROD_SERVICE_ENV_ENCRYPTION_KEY,
    PROD_SIGNING_SECRETS,
)

HEALTH = "/api/console/health"
TEST_PATH = "/api/platform/notifications/test"


def _prod(**extra) -> Settings:
    return Settings(
        _env_file=None,
        forge_env="prod",
        database_url="postgresql://x/y",
        service_env_encryption_key=PROD_SERVICE_ENV_ENCRYPTION_KEY,
        **PROD_SIGNING_SECRETS,
        **PROD_PLATFORM_ORIGIN,
        **OIDC_CONFIG,
        **extra,
    )


# --- settings -----------------------------------------------------------------------------


def test_prod_accepts_an_https_webhook():
    assert _prod(notify_webhook_url="https://hooks.example.test/T000/B000/XXXX").notify_webhook_configured


def test_prod_refuses_a_plain_http_webhook_and_never_prints_the_url():
    with pytest.raises(NotifyConfigError) as caught:
        _prod(notify_webhook_url="http://hooks.example.test/T000/B000/URL-SECRET-marker")
    assert "NOTIFY_WEBHOOK_URL" in str(caught.value)
    assert "https" in str(caught.value)
    assert "URL-SECRET-marker" not in str(caught.value)


def test_dev_accepts_an_http_webhook():
    cfg = Settings(_env_file=None, forge_env="dev", notify_webhook_url="http://127.0.0.1:9099/hook")
    assert cfg.notify_webhook_configured


@pytest.mark.parametrize("value", ["ftp://hooks.example.test/x", "not a url", "https://"])
def test_a_webhook_that_is_not_an_http_url_with_a_host_is_refused(value):
    with pytest.raises(NotifyConfigError, match="NOTIFY_WEBHOOK_URL"):
        Settings(_env_file=None, forge_env="dev", notify_webhook_url=value)


SMTP = {
    "notify_smtp_host": "smtp.example.test",
    "notify_smtp_from": "forge@example.test",
    "notify_email_to": "ops@example.test",
}


def test_prod_refuses_plain_smtp_without_tls():
    with pytest.raises(NotifyConfigError, match="NOTIFY_SMTP_TLS=none"):
        _prod(notify_smtp_tls="none", **SMTP)


@pytest.mark.parametrize("mode", ["starttls", "ssl"])
def test_prod_accepts_starttls_and_implicit_tls(mode):
    assert _prod(notify_smtp_tls=mode, **SMTP).notify_email_configured


def test_dev_accepts_plain_smtp():
    assert Settings(_env_file=None, forge_env="dev", notify_smtp_tls="none", **SMTP).notify_email_configured


def test_an_unknown_tls_mode_is_refused():
    with pytest.raises(NotifyConfigError, match="NOTIFY_SMTP_TLS"):
        Settings(_env_file=None, forge_env="dev", notify_smtp_tls="maybe", **SMTP)


@pytest.mark.parametrize(
    "override, setting",
    [
        ({"notify_smtp_from": None}, "NOTIFY_SMTP_FROM"),
        ({"notify_email_to": ""}, "NOTIFY_EMAIL_TO"),
        ({"notify_email_to": "ops@example.test, bad address"}, "NOTIFY_EMAIL_TO"),
        ({"notify_smtp_username": "mailer"}, "NOTIFY_SMTP_PASSWORD"),
        ({"notify_smtp_password": "pw"}, "NOTIFY_SMTP_PASSWORD"),
    ],
)
def test_a_half_configured_smtp_channel_is_refused_by_name(override, setting):
    with pytest.raises(NotifyConfigError, match=setting):
        Settings(_env_file=None, forge_env="dev", **{**SMTP, **override})


def test_a_blank_host_and_blank_url_are_unset_not_configured():
    cfg = Settings(_env_file=None, forge_env="dev", notify_webhook_url="  ", notify_smtp_host="  ")
    assert (cfg.notify_webhook_configured, cfg.notify_email_configured) == (False, False)


# --- the console and the test message ------------------------------------------------------


@pytest.fixture()
def receiver():
    items: list[WebhookReceiver] = []

    def _make(**kwargs):
        item = WebhookReceiver(**kwargs).start()
        items.append(item)
        return item

    yield _make
    for item in items:
        item.stop()


def _login(db_session, role) -> dict[str, str]:
    return {"X-Forge-User-Id": db_session.query(User).filter_by(role=role).first().id}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(settings, "notify_retry_count", 0)
    monkeypatch.setattr(settings, "notify_retry_backoff_seconds", 0.0)
    monkeypatch.setattr(settings, "notify_test_min_interval_seconds", 30.0)


def test_the_console_lists_both_channels_and_never_the_url(client, db_session, receiver, monkeypatch):
    hook = receiver()
    monkeypatch.setattr(settings, "notify_webhook_url", hook.url)

    response = client.get(HEALTH, headers=_login(db_session, Role.ADMIN))

    assert response.status_code == 200
    channels = {c["channel"]: c for c in response.json()["notification_channels"]}
    assert channels["webhook"] == {
        "channel": "webhook",
        "configured": True,
        "last_attempt_at": None,
        "last_result": None,
        "last_error_class": None,
    }
    assert channels["email"]["configured"] is False
    assert "SECRET-PATH-TOKEN" not in response.text


def test_an_unconfigured_install_says_so(client, db_session):
    body = client.get(HEALTH, headers=_login(db_session, Role.ADMIN)).json()
    assert [(c["channel"], c["configured"]) for c in body["notification_channels"]] == [
        ("webhook", False),
        ("email", False),
    ]


def test_an_admin_sends_a_test_message_and_the_console_shows_the_result(client, db_session, receiver, monkeypatch):
    hook = receiver()
    monkeypatch.setattr(settings, "notify_webhook_url", hook.url)
    admin = _login(db_session, Role.ADMIN)

    response = client.post(TEST_PATH, headers=admin)

    assert response.status_code == 200
    assert response.json() == {"results": [{"channel": "webhook", "ok": True, "error_class": None}]}
    [request] = hook.requests
    assert b"Test message" in request["body"]
    shown = {c["channel"]: c for c in client.get(HEALTH, headers=admin).json()["notification_channels"]}
    assert shown["webhook"]["last_result"] == "OK"
    assert shown["webhook"]["last_attempt_at"] is not None
    audit = db_session.scalars(select(AuditLog).where(AuditLog.action == "notification.test_sent")).one()
    assert audit.actor_id == db_session.query(User).filter_by(role=Role.ADMIN).first().id
    assert audit.metadata_json == {"results": {"webhook": "OK"}}


def test_a_failing_test_message_is_shown_by_class_without_the_body(client, db_session, receiver, monkeypatch):
    hook = receiver(status=500)
    monkeypatch.setattr(settings, "notify_webhook_url", hook.url)
    admin = _login(db_session, Role.ADMIN)

    response = client.post(TEST_PATH, headers=admin)

    assert response.status_code == 200
    assert response.json()["results"] == [{"channel": "webhook", "ok": False, "error_class": "HTTP_500"}]
    health = client.get(HEALTH, headers=admin)
    shown = {c["channel"]: c for c in health.json()["notification_channels"]}
    assert (shown["webhook"]["last_result"], shown["webhook"]["last_error_class"]) == ("FAILED", "HTTP_500")
    assert "secret-response-body" not in health.text + response.text


@pytest.mark.parametrize("role", [Role.REQUESTER, Role.APPROVER])
def test_only_an_admin_may_send_one(client, db_session, receiver, monkeypatch, role):
    hook = receiver()
    monkeypatch.setattr(settings, "notify_webhook_url", hook.url)

    assert client.post(TEST_PATH, headers=_login(db_session, role)).status_code == 403
    assert hook.requests == []


def test_an_unauthenticated_request_is_refused(client, receiver, monkeypatch):
    hook = receiver()
    monkeypatch.setattr(settings, "notify_webhook_url", hook.url)

    assert client.post(TEST_PATH).status_code == 401
    assert hook.requests == []


def test_a_cross_site_post_is_refused_before_it_reaches_the_handler(client, db_session, receiver, monkeypatch):
    hook = receiver()
    monkeypatch.setattr(settings, "notify_webhook_url", hook.url)
    headers = {
        **_login(db_session, Role.ADMIN),
        "Sec-Fetch-Site": "cross-site",
        "Origin": "https://evil.example.test",
    }

    assert client.post(TEST_PATH, headers=headers).status_code == 403
    assert hook.requests == []


def test_with_no_channel_the_action_says_so(client, db_session):
    assert client.post(TEST_PATH, headers=_login(db_session, Role.ADMIN)).status_code == 409


def test_a_second_test_inside_the_interval_is_refused(client, db_session, receiver, monkeypatch):
    hook = receiver()
    monkeypatch.setattr(settings, "notify_webhook_url", hook.url)
    admin = _login(db_session, Role.ADMIN)

    assert client.post(TEST_PATH, headers=admin).status_code == 200
    second = client.post(TEST_PATH, headers=admin)

    assert second.status_code == 429
    assert second.headers["Retry-After"] == "30"
    assert len(hook.requests) == 1
    assert len(db_session.scalars(select(AuditLog).where(AuditLog.action == "notification.test_sent")).all()) == 1


def test_a_test_is_allowed_again_once_the_interval_has_passed(client, db_session, receiver, monkeypatch):
    hook = receiver()
    monkeypatch.setattr(settings, "notify_webhook_url", hook.url)
    monkeypatch.setattr(settings, "notify_test_min_interval_seconds", 0.0)
    admin = _login(db_session, Role.ADMIN)

    assert client.post(TEST_PATH, headers=admin).status_code == 200
    assert client.post(TEST_PATH, headers=admin).status_code == 200
    assert len(hook.requests) == 2


def test_simultaneous_clicks_send_once(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'gate.db').as_posix()}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    start = threading.Barrier(6)
    results: list[bool] = []

    def _click():
        with maker() as db:
            start.wait(5)
            results.append(notifications_router._take_gate(db, datetime.now(timezone.utc)))

    threads = [threading.Thread(target=_click) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    engine.dispose()

    assert sorted(results) == [False] * 5 + [True]


def test_the_console_router_is_still_read_only_and_the_action_lives_outside_it():
    paths = app_main.app.openapi()["paths"]
    assert set(paths[TEST_PATH]) == {"post"}
    assert all(set(ops) == {"get"} for path, ops in paths.items() if path.startswith("/api/console"))


# --- the detector is started by the API process only when a channel exists ------------------


def _detector_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "forge-worker-detector"]


@pytest.fixture()
def startup_db(tmp_path, monkeypatch):
    """The lifespan against a real migrated SQLite file (the schema check demands the head)."""
    from alembic import command

    url = f"sqlite:///{(tmp_path / 'startup.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    command.upgrade(alembic_config(url), "head")
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(app_main, "engine", engine)
    monkeypatch.setattr(app_main, "SessionLocal", sessionmaker(bind=engine, autoflush=False))
    yield engine
    engine.dispose()


async def _lifespan_while(check):
    async with app_main.lifespan(app_main.app):
        check()


def test_the_api_process_starts_a_worker_detector_only_when_a_channel_is_configured(startup_db, monkeypatch):
    seen: dict = {}
    asyncio.run(_lifespan_while(lambda: seen.__setitem__("none", len(_detector_threads()))))
    monkeypatch.setattr(settings, "notify_webhook_url", "http://127.0.0.1:9/hook")
    asyncio.run(_lifespan_while(lambda: seen.__setitem__("one", len(_detector_threads()))))

    assert seen == {"none": 0, "one": 1}
    deadline = time.monotonic() + 5
    while _detector_threads() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _detector_threads() == []  # stopped with the lifespan
