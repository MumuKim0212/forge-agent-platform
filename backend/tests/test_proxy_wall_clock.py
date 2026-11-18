"""The preview proxy's exec script gets a REAL wall-clock deadline, and the
smoke check's caller can ask for "do not follow redirects".

`urlopen(timeout=N)` bounds each socket operation, not the request, and `docker exec` has no limit
of its own, so a hostile preview app that drips a byte every few seconds, accepts and never answers,
or chains redirects could hold one call (and with ADR 0038's single worker, every build) for hours.

These tests run the generated script as a plain host subprocess (no Docker, as
`test_proxy_isolation.py` does) against a local hostile server, with a REAL clock. Every hostile
server is FINITE (it stops after a fixed number of bytes / seconds), so a mutation that removes the
deadline ends by itself and goes red on the exit code and the elapsed time instead of hanging.
Run under `timeout`.
"""

from __future__ import annotations

import http.server
import socketserver
import subprocess
import sys
import threading
import time

import pytest

from app.services import deploy_manager as deploy_manager_module
from app.services import preview_manager as preview_manager_module
from app.services.preview_manager import (
    WALL_CLOCK_EXIT_CODE,
    PreviewManager,
    PreviewStartError,
    _build_proxy_exec_script,
)

DEADLINE = 1.0  # seconds, in the script
# The script has to be done well inside this, on a loaded machine too, yet the hostile servers below
# would keep it for 4+ seconds without the deadline.
MAX_ELAPSED = 3.0


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def _serve(handler):
    server = _Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _run(script: str, *, patience: float = 30.0) -> tuple[subprocess.CompletedProcess, float]:
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=patience)
    return result, time.monotonic() - started


class _Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        pass


class _Dripper(_Quiet):
    """Sends the headers, then ONE byte every 0.2 s, 100 times (20 s at most), then stops."""

    drips = 0

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "100")
        self.end_headers()
        self.wfile.flush()
        try:
            for _ in range(100):
                self.wfile.write(b"x")
                self.wfile.flush()
                type(self).drips += 1
                time.sleep(0.2)
        except OSError:
            pass


class _Silent(_Quiet):
    """Accepts, reads the request, and says nothing for 8 s (at most), then closes."""

    def do_GET(self):  # noqa: N802
        time.sleep(8)


class _RedirectLoop(_Quiet):
    """Redirects to itself, 0.4 s per hop; urllib gives up after 10 hops (4 s) at the latest."""

    hops = 0

    def do_GET(self):  # noqa: N802
        type(self).hops += 1
        time.sleep(0.4)
        self.send_response(302)
        self.send_header("Location", self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _Redirector(_Quiet):
    """`/old` redirects to `/target`; `/target` answers 200 and counts how often it is asked."""

    target_hits = 0

    def do_GET(self):  # noqa: N802
        if self.path == "/old":
            self.send_response(302)
            self.send_header("Location", "/target")
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        type(self).target_hits += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "6")
        self.end_headers()
        self.wfile.write(b"target")


class _Instant(_Quiet):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


# --- the script text ----------------------------------------------------------------------------


@pytest.mark.parametrize("module", [preview_manager_module, deploy_manager_module], ids=["preview", "deploy"])
def test_with_the_defaults_the_script_is_byte_for_byte_the_old_one(module):
    expected = (
        "import urllib.request, sys\n"
        "req = urllib.request.Request('http://127.0.0.1:1/x', method='GET')\n"
        "try:\n"
        "    with urllib.request.urlopen(req, timeout=5.0) as resp:\n"
        "        sys.stdout.buffer.write(str(resp.status).encode() + b'\\n')\n"
        "        sys.stdout.buffer.write((resp.headers.get('Content-Type') or '').encode() + b'\\n')\n"
        "        sys.stdout.buffer.write(resp.read())\n"
        "except urllib.error.HTTPError as e:\n"
        "    sys.stdout.buffer.write(str(e.code).encode() + b'\\n')\n"
        "    sys.stdout.buffer.write((e.headers.get('Content-Type') or '').encode() + b'\\n')\n"
        "    sys.stdout.buffer.write(e.read())\n"
    )
    assert module._build_proxy_exec_script("http://127.0.0.1:1/x", "GET", 5.0) == expected


def test_the_deadline_and_the_redirect_option_only_add_to_the_script_when_asked_for():
    plain = _build_proxy_exec_script("http://127.0.0.1:1/x", "GET", 5.0)

    with_deadline = _build_proxy_exec_script("http://127.0.0.1:1/x", "GET", 5.0, wall_clock_seconds=2.5)
    no_redirect = _build_proxy_exec_script("http://127.0.0.1:1/x", "GET", 5.0, follow_redirects=False)
    explicit_defaults = _build_proxy_exec_script(
        "http://127.0.0.1:1/x", "GET", 5.0, wall_clock_seconds=None, follow_redirects=True
    )

    assert explicit_defaults == plain
    assert "threading.Timer(2.5, os._exit, (124,))" in with_deadline and "os._exit" not in plain
    assert "HTTPRedirectHandler" in no_redirect and "HTTPRedirectHandler" not in plain
    assert "HTTPRedirectHandler" not in with_deadline and "os._exit" not in no_redirect
    for script in (plain, with_deadline, no_redirect):
        compile(script, "<script>", "exec")


# --- a hostile server, a real clock -------------------------------------------------------------


def test_a_server_that_drips_a_byte_at_a_time_is_cut_off_at_the_deadline():
    _Dripper.drips = 0
    server, thread = _serve(_Dripper)
    try:
        script = _build_proxy_exec_script(
            f"http://127.0.0.1:{server.server_port}/", "GET", 30.0, wall_clock_seconds=DEADLINE
        )
        result, elapsed = _run(script)
    finally:
        _stop(server, thread)

    assert result.returncode == WALL_CLOCK_EXIT_CODE == 124
    assert result.stdout == b""  # `_exit`: nothing half-written is mistaken for an answer
    assert elapsed < MAX_ELAPSED
    assert 0 < _Dripper.drips < 100  # it was dripping when it was cut, and was not left to finish


def test_a_server_that_accepts_and_never_answers_is_cut_off_at_the_deadline():
    server, thread = _serve(_Silent)
    try:
        # The socket timeout (30 s) is far beyond the deadline: only the deadline can end this.
        script = _build_proxy_exec_script(
            f"http://127.0.0.1:{server.server_port}/", "GET", 30.0, wall_clock_seconds=DEADLINE
        )
        result, elapsed = _run(script)
    finally:
        _stop(server, thread)

    assert result.returncode == 124
    assert elapsed < MAX_ELAPSED


def test_a_redirect_loop_is_cut_off_at_the_deadline_when_redirects_are_followed():
    _RedirectLoop.hops = 0
    server, thread = _serve(_RedirectLoop)
    try:
        script = _build_proxy_exec_script(
            f"http://127.0.0.1:{server.server_port}/loop", "GET", 30.0, wall_clock_seconds=DEADLINE
        )
        result, elapsed = _run(script)
    finally:
        _stop(server, thread)

    assert result.returncode == 124
    assert elapsed < MAX_ELAPSED
    assert 1 <= _RedirectLoop.hops < 10  # it was still hopping when it was cut


def test_without_following_redirects_a_redirect_loop_is_one_answer_with_the_3xx_status():
    _RedirectLoop.hops = 0
    server, thread = _serve(_RedirectLoop)
    try:
        script = _build_proxy_exec_script(
            f"http://127.0.0.1:{server.server_port}/loop",
            "GET",
            30.0,
            wall_clock_seconds=DEADLINE,
            follow_redirects=False,
        )
        result, elapsed = _run(script)
    finally:
        _stop(server, thread)

    assert result.returncode == 0, result.stderr
    status, _body, _content_type = preview_manager_module._parse_proxy_output(result.stdout)
    assert status == 302
    assert _RedirectLoop.hops == 1


def test_a_redirect_is_followed_by_default_and_its_own_answer_without_following():
    _Redirector.target_hits = 0
    server, thread = _serve(_Redirector)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        followed, _ = _run(_build_proxy_exec_script(f"{base}/old", "GET", 5.0))
        hits_when_followed = _Redirector.target_hits
        not_followed, _ = _run(_build_proxy_exec_script(f"{base}/old", "GET", 5.0, follow_redirects=False))
        hits_after = _Redirector.target_hits
    finally:
        _stop(server, thread)

    assert preview_manager_module._parse_proxy_output(followed.stdout) == (200, b"target", "text/plain")
    assert hits_when_followed == 1
    assert preview_manager_module._parse_proxy_output(not_followed.stdout)[0] == 302
    assert hits_after == hits_when_followed  # not followed: the target was not asked again


def test_a_quick_answer_under_a_long_deadline_is_returned_and_the_timer_does_not_hold_the_process():
    server, thread = _serve(_Instant)
    try:
        script = _build_proxy_exec_script(
            f"http://127.0.0.1:{server.server_port}/", "GET", 5.0, wall_clock_seconds=60.0, follow_redirects=False
        )
        result, elapsed = _run(script)
    finally:
        _stop(server, thread)

    assert result.returncode == 0, result.stderr
    assert preview_manager_module._parse_proxy_output(result.stdout) == (200, b"ok", "text/plain")
    assert elapsed < MAX_ELAPSED  # a 60 s timer did not keep the process alive


# --- PreviewManager.proxy: the deadline bounds the whole call, retry loop included --------------


class _Container:
    status = "running"

    def __init__(self, replies):
        self.replies = list(replies)
        self.scripts = []

    def exec_run(self, argv):
        self.scripts.append(argv[2])
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class _Client:
    def __init__(self, container):
        self.containers = self
        self._container = container

    def get(self, name):
        return self._container


def _manager(container):
    return PreviewManager(client=_Client(container))


def test_an_exec_that_hit_its_deadline_is_a_preview_start_error_with_fixed_text_and_no_output():
    secret = "MARKER-from-the-container-9a7"
    container = _Container([(WALL_CLOCK_EXIT_CODE, secret.encode())])

    with pytest.raises(PreviewStartError) as excinfo:
        _manager(container).proxy("b-1", path="/slow", wall_clock_seconds=2.0)

    assert secret not in str(excinfo.value)
    assert len(container.scripts) == 1  # a deadline is an answer, not a reason to retry


def test_without_a_deadline_exit_124_is_the_old_generic_failure():
    container = _Container([(124, b"whatever")])

    with pytest.raises(PreviewStartError, match="exit 124"):
        _manager(container).proxy("b-1", path="/x")


def test_the_retry_loop_is_bounded_by_the_deadline_too():
    """The container refuses the connection for 3+ seconds' worth of retries (11 refusals at 0.3 s)
    and would then answer. The retry window alone (`timeout_seconds`, 30 s here) would wait that
    out; the 1 s wall-clock deadline must end the CALL first. Finite on purpose: the 12th exec
    answers, so removing the bound returns after ~3.3 s instead of looping on."""
    refused = (1, b"urllib.error.URLError: <urlopen error [Errno 111] Connection refused>")
    container = _Container([refused] * 11 + [(0, b"200\ntext/plain\nok")])

    started = time.monotonic()
    with pytest.raises(PreviewStartError, match="wall-clock"):
        _manager(container).proxy("b-1", path="/x", timeout_seconds=30.0, wall_clock_seconds=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 2.5
    assert 2 <= len(container.scripts) < 12  # it did retry, and was stopped while still refused


def test_each_retry_carries_only_what_is_left_of_the_deadline():
    refused = (1, b"Connection refused")
    container = _Container([refused] * 3 + [(0, b"200\ntext/plain\nok")])

    status, body, _ct = _manager(container).proxy("b-1", path="/x", timeout_seconds=30.0, wall_clock_seconds=5.0)

    assert (status, body) == (200, b"ok")
    assert len(container.scripts) == 4

    def deadline_of(script):
        line = next(l for l in script.splitlines() if l.startswith("_deadline = threading.Timer("))
        return float(line.split("(")[1].split(",")[0])

    left = [deadline_of(s) for s in container.scripts]
    assert left[0] == 5.0
    assert left == sorted(left, reverse=True) and left[-1] < left[0]


def test_without_a_deadline_the_manager_runs_the_one_script_it_always_ran():
    container = _Container([(1, b"Connection refused"), (0, b"200\ntext/plain\nok")])

    _manager(container).proxy("b-1", path="/x", timeout_seconds=5.0)

    assert len(container.scripts) == 2 and container.scripts[0] == container.scripts[1]
    assert "threading" not in container.scripts[0] and "HTTPRedirectHandler" not in container.scripts[0]


def test_the_follow_redirects_choice_reaches_the_script():
    container = _Container([(0, b"302\n\n")])

    _manager(container).proxy("b-1", path="/x", follow_redirects=False)

    assert "HTTPRedirectHandler" in container.scripts[0]
