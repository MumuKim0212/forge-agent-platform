"""No access-log line carries a query-string value.

uvicorn's access log writes the request target with its query string, and the preview bearer `token`
(`/api/projects/<p>/preview/<b>?token=...`) and the OIDC callback's `code`/`state` travel there. The
platform filters the `uvicorn.access` logger itself (`app/access_log.py`, installed by `create_app`), so
the rule holds however uvicorn is started.

The main test runs a REAL uvicorn server (a thread, lifespan off so nothing touches a database) with
access logging on, serving the real `app.main.app`, and reads the line uvicorn really formats. Every
value is a MARKER, never a real credential.
"""

from __future__ import annotations

import io
import logging
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.access_log import ACCESS_LOGGER_NAME, QueryValueRedactingFilter, redact_query_values
from app.main import app, create_app

TOKEN_MARKER = "TOKMARKER7Q2X"
CODE_MARKER = "CODEMARKER4K8Z"
STATE_MARKER = "STATEMARKER1V6W"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def access_lines():
    """The access logger, configured the way uvicorn's own config would leave it (INFO, a stream
    handler) but without `dictConfig`, which would rewire process-wide logging for every other test."""
    logger = logging.getLogger(ACCESS_LOGGER_NAME)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    old_level, old_propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


@pytest.fixture()
def served_port():
    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_config=None, access_log=True, lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_a_request_with_secret_query_values_is_logged_without_them(access_lines, served_port):
    base = f"http://127.0.0.1:{served_port}"
    with httpx.Client() as client:
        client.get(f"{base}/api/health?token={TOKEN_MARKER}&page=2")
        client.get(f"{base}/api/auth/callback?code={CODE_MARKER}&state={STATE_MARKER}")
    # uvicorn logs after the response is sent; give the server thread a moment to flush the last line.
    deadline = time.monotonic() + 5
    while access_lines.getvalue().count("\n") < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    logged = access_lines.getvalue()

    assert TOKEN_MARKER not in logged
    assert CODE_MARKER not in logged
    assert STATE_MARKER not in logged
    # What stays: the path and the parameter names (the line is still useful).
    assert "/api/health?token=[redacted]&page=[redacted]" in logged
    assert "/api/auth/callback?code=[redacted]&state=[redacted]" in logged
    assert logged.count("\n") == 2  # a line per request: the filter edits records, it never drops one


# the preview token also travels as a `~{token}` PATH segment, which uvicorn
# logs decoded and which no query redaction touches.


@pytest.fixture()
def served_preview_port(db_session):
    """A real uvicorn thread serving a fresh app whose database is the test's own, so a request to the
    preview route reaches the real handler (an unknown build: a 404 that needs no preview container)."""
    from app.db import get_db

    fresh = create_app()

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    fresh.dependency_overrides[get_db] = _override_get_db
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(fresh, host="127.0.0.1", port=port, log_config=None, access_log=True, lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_a_path_token_is_logged_without_it_with_and_without_a_query(access_lines, served_preview_port):
    base = f"http://127.0.0.1:{served_preview_port}/api/projects/p1/preview/b1"
    requests = [
        f"{base}/~{TOKEN_MARKER}/",  # trailing segment, no query
        f"{base}/~{TOKEN_MARKER}",  # last segment, no trailing slash
        f"{base}/~{TOKEN_MARKER}/items",  # middle segment, no query
        f"{base}/~{TOKEN_MARKER}/items?page={STATE_MARKER}",  # with a query
        f"{base}?token={CODE_MARKER}",  # the old carrier, still redacted
    ]
    with httpx.Client(follow_redirects=False) as client:
        for url in requests:
            client.get(url)
    deadline = time.monotonic() + 5
    while access_lines.getvalue().count("\n") < len(requests) and time.monotonic() < deadline:
        time.sleep(0.02)
    logged = access_lines.getvalue()

    for marker in (TOKEN_MARKER, STATE_MARKER, CODE_MARKER):
        assert marker not in logged
    assert "/api/projects/p1/preview/b1/~[redacted]/ " in logged
    assert "/api/projects/p1/preview/b1/~[redacted] " in logged
    assert "/api/projects/p1/preview/b1/~[redacted]/items " in logged
    assert "/api/projects/p1/preview/b1/~[redacted]/items?page=[redacted] " in logged
    assert "/api/projects/p1/preview/b1?token=[redacted] " in logged
    assert logged.count("\n") == len(requests)


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/api/projects/p1/preview/b1/~M/", "/api/projects/p1/preview/b1/~[redacted]/"),  # middle, trailing slash
        ("/api/projects/p1/preview/b1/~M", "/api/projects/p1/preview/b1/~[redacted]"),  # trailing segment
        ("/api/projects/p1/preview/b1/~M/items/x", "/api/projects/p1/preview/b1/~[redacted]/items/x"),
        ("/api/projects/p1/preview/b1/~M/items?a=1", "/api/projects/p1/preview/b1/~[redacted]/items?a=[redacted]"),
        ("/api/projects/p1/preview/b1/~M?a=1", "/api/projects/p1/preview/b1/~[redacted]?a=[redacted]"),
        ("/api/projects/p1/preview/b1/~M.123.sig/x", "/api/projects/p1/preview/b1/~[redacted]/x"),  # dots in it
        ("/a/~one/b/~two", "/a/~[redacted]/b/~[redacted]"),
        ("/api/projects/p1/preview/b1/items~M", "/api/projects/p1/preview/b1/items~M"),  # not a segment start
        ("/api/projects/p1/preview/b1/a?x=~M", "/api/projects/p1/preview/b1/a?x=[redacted]"),
    ],
)
def test_redact_a_tilde_path_segment(target, expected):
    assert redact_query_values(target) == expected


def test_the_filter_redacts_a_path_token_in_the_tuple_the_mapping_and_the_preformatted_message():
    path = f"/api/projects/p1/preview/b1/~{TOKEN_MARKER}/items"
    tupled = logging.LogRecord(
        ACCESS_LOGGER_NAME, logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", path, "1.1", 200), None,
    )
    mapped = logging.LogRecord(ACCESS_LOGGER_NAME, logging.INFO, __file__, 1, "%(target)s", ({"target": path},), None)
    plain = logging.LogRecord(ACCESS_LOGGER_NAME, logging.INFO, __file__, 1, f"GET {path} 200", None, None)

    for record in (tupled, mapped, plain):
        assert QueryValueRedactingFilter().filter(record) is True
        assert TOKEN_MARKER not in record.getMessage()
        assert "/api/projects/p1/preview/b1/~[redacted]/items" in record.getMessage()
    assert tupled.getMessage() == '127.0.0.1:5000 - "GET /api/projects/p1/preview/b1/~[redacted]/items HTTP/1.1" 200'


def test_create_app_installs_the_filter_once_however_often_it_is_called():
    create_app()
    create_app()
    filters = [f for f in logging.getLogger(ACCESS_LOGGER_NAME).filters if isinstance(f, QueryValueRedactingFilter)]
    assert len(filters) == 1


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/api/health", "/api/health"),
        ("/p?token=M", "/p?token=[redacted]"),
        ("/p?a=1&b=2&c=", "/p?a=[redacted]&b=[redacted]&c=[redacted]"),
        ("/p?M", "/p?[redacted]"),  # a bare value is a value
        ("/p?token=M=N", "/p?token=[redacted]"),  # an `=` inside a value does not leak the tail
        ("/p?token=M%26x%3Dy", "/p?token=[redacted]"),  # an encoded `&`/`=` stays inside its value
        ("/p?token=M#frag", "/p?token=[redacted]"),
        ("/p?", "/p?"),
        ("/api/projects/p1/preview/b1/sub?token=M", "/api/projects/p1/preview/b1/sub?token=[redacted]"),
    ],
)
def test_redact_query_values(target, expected):
    assert redact_query_values(target) == expected


def test_the_filter_rewrites_uvicorns_argument_tuple_and_keeps_the_record():
    record = logging.LogRecord(
        ACCESS_LOGGER_NAME, logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", f"/x?token={TOKEN_MARKER}", "1.1", 200), None,
    )

    assert QueryValueRedactingFilter().filter(record) is True
    assert record.getMessage() == '127.0.0.1:5000 - "GET /x?token=[redacted] HTTP/1.1" 200'


def test_the_filter_covers_a_preformatted_message_and_a_mapping_of_arguments():
    plain = logging.LogRecord(ACCESS_LOGGER_NAME, logging.INFO, __file__, 1, f"GET /x?token={TOKEN_MARKER} 200", None, None)
    mapped = logging.LogRecord(ACCESS_LOGGER_NAME, logging.INFO, __file__, 1, "%(target)s", ({"target": f"/x?token={TOKEN_MARKER}"},), None)

    for record in (plain, mapped):
        QueryValueRedactingFilter().filter(record)
        assert TOKEN_MARKER not in record.getMessage()
        assert "/x?token=[redacted]" in record.getMessage()
