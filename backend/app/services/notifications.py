"""Operator notifications over a webhook and email.

Two optional channels, each configured by its own settings (`config.py`, `notify_*`):

* **webhook**: an HTTPS POST of a JSON body whose `text` field carries the message (Slack incoming
  webhooks accept it unchanged). Connect and read timeouts are separate and bounded, a small fixed
  number of retries follows a network error or a 5xx/429 (never a redirect, never a 4xx), redirects
  are not followed and the response body is never read into a log. The URL is credential-valued:
  nothing here logs it, and an error is recorded as its CLASS name only.
* **email**: SMTP, STARTTLS or implicit TLS (plain only in dev), certificates verified.

**Events** are `(family, subject)` states: `worker`, `service`, `disk`. `report` is told whether the
subject is healthy NOW; the persisted `NotificationState` decides whether that is news. A transition
is won by a conditional UPDATE on the row's `state` (rows matched = 1), so with several API
processes, or a worker and an API both reporting, exactly one announces it, and a restart that finds
the same state sends nothing. Down fires once; recovery fires once (only for an episode that WAS
announced); a new down after recovery is announced only after `notify_cooldown_seconds` since the
last announcement. The state is committed before delivery, so a crash in between loses a notice
rather than repeating one (best-effort).

**Delivery never fails anything else.** `report` and `send_test_message` catch everything; real
delivery happens on one daemon thread of its own (`_Sender`), so a slow or dead receiver delays only
later notices, never the check, build or deploy that reported.

**The payload is an allowlist**: event kind, subject name, timestamp, a link to the console built
from the public origin. `Message` has no other field, and the subject name is the only free text
(sanitised: control characters and the characters Slack reads as markup are removed).
"""

from __future__ import annotations

import logging
import queue
import smtplib
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Callable

import httpx
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.notification import NotificationChannelStatus, NotificationState

logger = logging.getLogger("app.notifications")

# httpx logs every request line (with the full URL) at INFO. The webhook URL is a credential.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class Family:
    WORKER = "worker"
    SERVICE = "service"
    DISK = "disk"


class EventKind:
    WORKER_DOWN = "worker_down"
    WORKER_RECOVERED = "worker_recovered"
    SERVICE_UNHEALTHY = "service_unhealthy"
    SERVICE_RECOVERED = "service_recovered"
    DISK_LOW = "disk_low"
    DISK_RECOVERED = "disk_recovered"
    TEST = "test"


_KINDS = {
    Family.WORKER: (EventKind.WORKER_DOWN, EventKind.WORKER_RECOVERED),
    Family.SERVICE: (EventKind.SERVICE_UNHEALTHY, EventKind.SERVICE_RECOVERED),
    Family.DISK: (EventKind.DISK_LOW, EventKind.DISK_RECOVERED),
}

_PHRASES = {
    EventKind.WORKER_DOWN: "Worker is down",
    EventKind.WORKER_RECOVERED: "Worker recovered",
    EventKind.SERVICE_UNHEALTHY: "Service unhealthy",
    EventKind.SERVICE_RECOVERED: "Service recovered",
    EventKind.DISK_LOW: "Disk space low",
    EventKind.DISK_RECOVERED: "Disk space recovered",
    EventKind.TEST: "Test message",
}

CHANNEL_WEBHOOK = "webhook"
CHANNEL_EMAIL = "email"
RESULT_OK = "OK"
RESULT_FAILED = "FAILED"

DOWN = "DOWN"
UP = "UP"

SUBJECT_NAME_MAX = 80
_STRIPPED = set("<>&`")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


# Characters that render as nothing or reorder text: the bidirectional controls, the zero-width and
# other default-ignorable characters, the tag block, the Hangul fillers. The same set the frontend marks
# (`visibleControls.ts`) and the repo guard scans for; `test_notifications.py` compares them. They are
# dropped from a name outright. (The C0/C1 controls and DEL become a space, handled in `clean_name`.)
_INVISIBLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x061C, 0x061C), (0x200E, 0x200F), (0x202A, 0x202E), (0x2066, 0x2069),
    (0x200B, 0x200D), (0x2060, 0x2064), (0xFEFF, 0xFEFF), (0x00AD, 0x00AD), (0x034F, 0x034F),
    (0x180E, 0x180E), (0xE0000, 0xE007F),
    (0x2065, 0x2065), (0x206A, 0x206F),
    (0x115F, 0x1160), (0x3164, 0x3164), (0xFFA0, 0xFFA0),
)


def _is_invisible(ch: str) -> bool:
    cp = ord(ch)
    return any(first <= cp <= last for first, last in _INVISIBLE_RANGES)


def _is_control(ch: str) -> bool:
    cp = ord(ch)
    return cp < 32 or 0x7F <= cp <= 0x9F


def clean_name(name: str) -> str:
    """The one free-text field of a notice, made safe for a header, a log line and Slack's markup."""
    kept = "".join(
        " " if _is_control(ch) else ch for ch in str(name) if ch not in _STRIPPED and not _is_invisible(ch)
    )
    return " ".join(kept.split())[:SUBJECT_NAME_MAX] or "(unnamed)"


@dataclass(frozen=True)
class Message:
    """Everything a notice may contain. There is no field for a token,
    an env value, a DSN, a build log or generated content, so none can be put in by accident."""

    kind: str
    subject_name: str
    timestamp: datetime
    link: str | None

    @property
    def title(self) -> str:
        return f"[Forge] {_PHRASES[self.kind]}: {self.subject_name}"

    @property
    def text(self) -> str:
        parts = [self.title, self.timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")]
        if self.link:
            parts.append(self.link)
        return "\n".join(parts)


def console_link() -> str | None:
    """The console's address from the platform's public origin; None when none is configured (dev)."""
    origin = settings.platform_public_url
    return f"{origin.rstrip('/')}/admin" if origin else None


def make_message(kind: str, subject_name: str, now: datetime | None = None) -> Message:
    return Message(kind=kind, subject_name=clean_name(subject_name), timestamp=now or _utcnow(), link=console_link())


def configured_channels() -> list[str]:
    channels = []
    if settings.notify_webhook_configured:
        channels.append(CHANNEL_WEBHOOK)
    if settings.notify_email_configured:
        channels.append(CHANNEL_EMAIL)
    return channels


def any_channel_configured() -> bool:
    return bool(configured_channels())


# --------------------------------------------------------------------------------------------
# Channels. Each returns None on success or the CLASS NAME of what ended it. Never a message,
# never a URL, never a response body.
# --------------------------------------------------------------------------------------------


def _retries() -> int:
    return settings.notify_retry_count


def _backoff() -> None:
    delay = settings.notify_retry_backoff_seconds
    if delay > 0:
        time.sleep(delay)


def send_webhook(message: Message) -> str | None:
    url = settings.notify_webhook_url
    if not url:
        return "NotConfigured"
    timeout = httpx.Timeout(
        connect=settings.notify_webhook_connect_timeout_seconds,
        read=settings.notify_webhook_read_timeout_seconds,
        write=settings.notify_webhook_read_timeout_seconds,
        pool=settings.notify_webhook_connect_timeout_seconds,
    )
    body = {
        "text": message.text,
        "kind": message.kind,
        "subject": message.subject_name,
        "timestamp": message.timestamp.astimezone(timezone.utc).isoformat(),
        "link": message.link,
    }
    error: str | None = None
    for attempt in range(1 + _retries()):
        if attempt:
            _backoff()
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                response = client.post(url, json=body)
                status = response.status_code  # the body is never read or logged
        except Exception as exc:  # noqa: BLE001 - recorded by class name; str(exc) may carry the URL
            error = type(exc).__name__
            continue
        if 200 <= status < 300:
            return None
        error = f"HTTP_{status}"
        if not (status >= 500 or status == 429):
            return error  # a redirect or a 4xx will not change by asking again
    return error


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()  # verifies the certificate and the host name


def _email_message(message: Message) -> EmailMessage:
    email = EmailMessage()
    email["Subject"] = message.title
    email["From"] = settings.notify_smtp_from
    email["To"] = ", ".join(settings.notify_email_recipients)
    email.set_content(message.text)
    return email


def _smtp_once(email: EmailMessage) -> None:
    host = settings.notify_smtp_host
    port = settings.notify_smtp_port
    timeout = settings.notify_smtp_timeout_seconds
    mode = settings.notify_smtp_tls
    if mode == "ssl":
        client: smtplib.SMTP = smtplib.SMTP_SSL(
            host, port, timeout=timeout, context=_ssl_context(), local_hostname="localhost"
        )
    else:
        client = smtplib.SMTP(host, port, timeout=timeout, local_hostname="localhost")
    with client:
        client.ehlo()
        if mode == "starttls":
            client.starttls(context=_ssl_context())
            client.ehlo()
        if settings.notify_smtp_username:
            # Credentials only ever go out after TLS (or in dev with TLS off, which prod refuses).
            client.login(settings.notify_smtp_username, settings.notify_smtp_password or "")
        client.send_message(email)


def _smtp_retryable(exc: BaseException) -> bool:
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    return isinstance(exc, (OSError, smtplib.SMTPServerDisconnected))


def send_email(message: Message) -> str | None:
    if not settings.notify_email_configured:
        return "NotConfigured"
    try:
        email = _email_message(message)
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__
    error: str | None = None
    for attempt in range(1 + _retries()):
        if attempt:
            _backoff()
        try:
            _smtp_once(email)
            return None
        except Exception as exc:  # noqa: BLE001 - class name only
            error = type(exc).__name__
            if not _smtp_retryable(exc):
                return error
    return error


_CHANNEL_SENDERS: dict[str, Callable[[Message], str | None]] = {
    CHANNEL_WEBHOOK: send_webhook,
    CHANNEL_EMAIL: send_email,
}


@dataclass(frozen=True)
class ChannelResult:
    channel: str
    ok: bool
    error_class: str | None


def _record_status(factory: Callable[[], Session], channel: str, error: str | None, now: datetime) -> None:
    """Upserts the channel's last result. Two threads can record the first one at once (the sender
    and an admin's test), so the insert that loses retries as an update."""
    values = {
        "last_attempt_at": now,
        "last_result": RESULT_OK if error is None else RESULT_FAILED,
        "last_error_class": error,
    }
    try:
        with factory() as db:
            matched = db.execute(
                update(NotificationChannelStatus).where(NotificationChannelStatus.channel == channel).values(**values)
            ).rowcount
            if matched == 0:
                db.add(NotificationChannelStatus(channel=channel, **values))
                try:
                    db.flush()
                except IntegrityError:  # the other thread inserted it a moment ago
                    db.rollback()
                    db.execute(
                        update(NotificationChannelStatus)
                        .where(NotificationChannelStatus.channel == channel)
                        .values(**values)
                    )
            db.commit()
    except Exception:  # noqa: BLE001 - the status is a courtesy; delivery already happened
        logger.warning("notifications: could not record a channel's last result", exc_info=True)


def deliver(message: Message, factory: Callable[[], Session]) -> list[ChannelResult]:
    """Sends to every configured channel (each isolated from the other), records each outcome.
    Never raises."""
    results: list[ChannelResult] = []
    for channel in configured_channels():
        try:
            error = _CHANNEL_SENDERS[channel](message)
        except Exception as exc:  # noqa: BLE001 - the senders do not raise; this is the boundary
            error = type(exc).__name__
        if error is not None:
            logger.warning("notifications: delivery by %s failed (%s)", channel, error)
        _record_status(factory, channel, error, _utcnow())
        results.append(ChannelResult(channel, error is None, error))
    return results


# --------------------------------------------------------------------------------------------
# The sender thread.
# --------------------------------------------------------------------------------------------

QUEUE_LIMIT = 100


class _Sender:
    """One daemon thread that delivers queued notices in order. A full queue drops the new notice
    (logged): a receiver that is down for hours must not grow memory without bound."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_LIMIT)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def enqueue(self, message: Message, factory: Callable[[], Session]) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="forge-notify-sender", daemon=True)
                self._thread.start()
        try:
            self._queue.put_nowait((message, factory))
        except queue.Full:
            logger.warning("notifications: the delivery queue is full; a notice was dropped")

    def _loop(self) -> None:
        while True:
            message, factory = self._queue.get()
            try:
                deliver(message, factory)
            except BaseException:  # noqa: BLE001 - one bad job must not end the sender
                logger.exception("notifications: the sender survived a failing delivery")
            finally:
                self._queue.task_done()

    def flush(self, timeout: float) -> bool:
        """Waits until everything queued so far has been attempted. True when it did."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True


_sender = _Sender()


def flush(timeout: float = 10.0) -> bool:
    return _sender.flush(timeout)


# --------------------------------------------------------------------------------------------
# Transitions.
# --------------------------------------------------------------------------------------------


def session_factory_for(db: Session) -> Callable[[], Session]:
    """A factory of sessions on the same database as `db`, for the writes that must not share the
    caller's open transaction (SQLite serialises writers)."""
    bind = db.get_bind()
    return lambda: Session(bind=bind)


def _claim_transition(
    factory: Callable[[], Session], family: str, subject: str, healthy: bool, now: datetime
) -> str | None:
    """Decides, in the database, whether this report is news. Returns the event kind to announce or
    None. The winner of a race is whoever's conditional UPDATE matches the row."""
    down_kind, up_kind = _KINDS[family]
    cooldown = timedelta(seconds=settings.notify_cooldown_seconds)
    with factory() as db:
        row = db.get(NotificationState, (family, subject))
        if row is None:
            if healthy:
                return None  # never down: nothing to record, nothing to say
            db.add(
                NotificationState(
                    family=family, subject=subject, state=DOWN, changed_at=now,
                    down_notified=True, last_down_notified_at=now,
                )
            )
            try:
                db.commit()
            except IntegrityError:  # another process recorded it first; it announces
                db.rollback()
                return None
            return down_kind
        key = (NotificationState.family == family, NotificationState.subject == subject)
        if row.state == UP and not healthy:
            last = _aware(row.last_down_notified_at) if row.last_down_notified_at else None
            announce = last is None or now - last >= cooldown
            values = {"state": DOWN, "changed_at": now, "down_notified": announce}
            if announce:
                values["last_down_notified_at"] = now
            won = db.execute(
                update(NotificationState).where(*key, NotificationState.state == UP).values(**values)
            ).rowcount
            db.commit()
            return down_kind if (won == 1 and announce) else None
        if row.state == DOWN and healthy:
            announced = bool(row.down_notified)
            won = db.execute(
                update(NotificationState)
                .where(*key, NotificationState.state == DOWN)
                .values(state=UP, changed_at=now)
            ).rowcount
            db.commit()
            return up_kind if (won == 1 and announced) else None
        return None


def report(
    factory: Callable[[], Session],
    *,
    family: str,
    subject: str,
    name: str,
    healthy: bool,
    now: datetime | None = None,
) -> str | None:
    """Reports the subject's current health; announces it when that is a new transition. Returns
    the kind queued (for tests) or None. **Never raises**: a notice is best-effort and must not
    fail the check, build or deploy that found the news."""
    try:
        if not any_channel_configured():
            return None
        moment = now or _utcnow()
        kind = _claim_transition(factory, family, subject, healthy, moment)
        if kind is None:
            return None
        _sender.enqueue(make_message(kind, name, moment), factory)
        return kind
    except Exception:  # noqa: BLE001
        logger.warning("notifications: reporting %s/%s failed", family, subject, exc_info=True)
        return None


def send_test_message(factory: Callable[[], Session]) -> list[ChannelResult]:
    """The admin's test: sent now, to every configured channel, outside the de-duplication (it is
    not an event). Synchronous, so the answer can say what happened. Never raises."""
    try:
        return deliver(make_message(EventKind.TEST, "Forge"), factory)
    except Exception:  # noqa: BLE001
        logger.warning("notifications: the test message failed", exc_info=True)
        return []


# --------------------------------------------------------------------------------------------
# The API process's detector for a dead worker.
# --------------------------------------------------------------------------------------------

WORKER_SUBJECT = "worker"
WORKER_NAME = "Forge worker"


class WorkerDownDetector:
    """A dead worker cannot report itself, so the API process looks at the lease with
    the lease's own staleness rule (`readiness.read_lease`): STALE is down, HELD is up. No row and a
    released row (a graceful stop) say nothing either way. Runs on a daemon thread of its own and a
    raising cycle is logged and followed by another; several API processes may each run one, and the
    persisted state decides which of them speaks."""

    def __init__(self, factory: Callable[[], Session], *, interval: float | None = None) -> None:
        self._factory = factory
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.cycles = 0

    def run_cycle(self) -> str | None:
        from app.services.readiness import read_lease

        try:
            with self._factory() as db:
                view = read_lease(db)
            if view.state == "STALE":
                healthy = False
            elif view.state == "HELD":
                healthy = True
            else:
                return None
            return report(
                self._factory, family=Family.WORKER, subject=WORKER_SUBJECT, name=WORKER_NAME, healthy=healthy
            )
        except Exception:  # noqa: BLE001
            logger.warning("notifications: the worker check failed", exc_info=True)
            return None
        finally:
            self.cycles += 1

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.run_cycle()
                except Exception:  # noqa: BLE001
                    logger.exception("notifications: the worker detector carries on after an error")
                wait = self._interval if self._interval is not None else settings.notify_worker_check_interval_seconds
                if self._stop.wait(wait):
                    break
        except BaseException:  # noqa: BLE001 - an ended detector is never silent
            logger.exception("notifications: the worker detector ended; a dead worker will NOT be announced")
            raise

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="forge-worker-detector", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
