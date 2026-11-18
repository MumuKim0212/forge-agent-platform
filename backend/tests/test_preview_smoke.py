"""the platform smoke check of a live preview -- selection, bounds, classification.

No Docker. The runner takes a prober, so every case here is a recording fake; the default prober
(`PreviewSmokeProber`) is exercised against a fake `PreviewManager` with real preview tokens, the
way `test_eval_judge.py` does for `PreviewRouteProbe`.
"""

from __future__ import annotations

import json

import pytest

from app.services import preview_smoke
from app.services.eval_judge import ProbeError
from app.services.preview_manager import PreviewStartError, hash_token, issue_preview_token
from app.services.preview_smoke import (
    CHUNK_BYTES,
    MAX_ROUTES,
    OPENAPI_READ_LIMIT_BYTES,
    ROUTE_READ_LIMIT_BYTES,
    PreviewSmokeProber,
    SmokeResponse,
    classify,
    read_bounded,
    run_smoke_check,
    select_routes,
)


def _doc(paths: dict) -> dict:
    return {"openapi": "3.1.0", "paths": paths}


def _get(**extra) -> dict:
    return {"get": {"responses": {"200": {"description": "ok"}}, **extra}}


class RecordingProber:
    """Answers `/openapi.json` with `document` and every other path from `answers` (default 200).
    Records every call as (method, path, read_limit, timeout)."""

    def __init__(self, document=None, *, answers=None, openapi_status=200, openapi_body=None):
        self.calls: list[tuple[str, str, int, float]] = []
        self.answers = answers or {}
        self.document = document
        self.openapi_status = openapi_status
        self.openapi_body = openapi_body

    def __call__(self, method, path, read_limit, timeout):
        self.calls.append((method, path, read_limit, timeout))
        if path == "/openapi.json":
            body = self.openapi_body if self.openapi_body is not None else json.dumps(self.document).encode()
            return SmokeResponse(self.openapi_status, [body])
        answer = self.answers.get(path, SmokeResponse(200, [b"ok"]))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def route_calls(self):
        return [c for c in self.calls if c[1] != "/openapi.json"]


# --- selection --------------------------------------------------------------------------------


def test_a_parameterless_get_is_called_and_nothing_is_skipped():
    selection = select_routes(_doc({"/ping": _get()}))

    assert selection.eligible == ("/ping",)
    assert selection.skipped_by_reason == {}


def test_a_path_with_a_path_parameter_is_skipped_for_that_reason():
    selection = select_routes(_doc({"/items/{id}": _get(), "/ok": _get()}))

    assert selection.eligible == ("/ok",)
    assert selection.skipped_by_reason == {preview_smoke.SKIP_PATH_PARAMETER: 1}


def test_a_required_query_parameter_on_the_operation_is_skipped_for_that_reason():
    op = _get(parameters=[{"name": "q", "in": "query", "required": True}])

    selection = select_routes(_doc({"/search": op}))

    assert selection.eligible == ()
    assert selection.skipped_by_reason == {preview_smoke.SKIP_REQUIRED_PARAMETER: 1}


def test_a_required_query_parameter_on_the_path_item_is_skipped_for_that_reason():
    item = {**_get(), "parameters": [{"name": "q", "in": "query", "required": True}]}

    selection = select_routes(_doc({"/search": item}))

    assert selection.eligible == ()
    assert selection.skipped_by_reason == {preview_smoke.SKIP_REQUIRED_PARAMETER: 1}


def test_an_optional_query_parameter_is_called():
    op = _get(parameters=[{"name": "q", "in": "query", "required": False}, {"name": "r", "in": "query"}])

    selection = select_routes(_doc({"/search": op}))

    assert selection.eligible == ("/search",)
    assert selection.skipped_by_reason == {}


@pytest.mark.parametrize("level", ["operation", "path_item"])
def test_an_unresolved_ref_parameter_is_skipped_as_its_own_reason(level):
    ref = [{"$ref": "#/components/parameters/Page"}]
    doc = _doc({"/list": _get(parameters=ref)} if level == "operation" else {"/list": {**_get(), "parameters": ref}})

    selection = select_routes(doc)

    assert selection.eligible == ()
    assert selection.skipped_by_reason == {preview_smoke.SKIP_UNRESOLVED_REF: 1}


@pytest.mark.parametrize("parameters", ["not-a-list", ["not-a-dict"], [{"name": "x", "required": "yes"}]])
def test_a_malformed_or_truthy_required_parameter_is_treated_as_required(parameters):
    selection = select_routes(_doc({"/odd": _get(parameters=parameters)}))

    assert selection.eligible == ()
    assert selection.skipped_by_reason == {preview_smoke.SKIP_REQUIRED_PARAMETER: 1}


def test_a_path_item_with_only_a_post_is_not_called_and_is_not_counted():
    selection = select_routes(_doc({"/make": {"post": {"responses": {}}}, "/also": {"put": {}, "delete": {}}}))

    assert selection.eligible == ()
    assert selection.skipped_by_reason == {}


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"])
def test_the_frameworks_own_paths_are_not_called_and_not_counted(path):
    selection = select_routes(_doc({path: _get(), "/mine": _get()}))

    assert selection.eligible == ("/mine",)
    assert selection.skipped_by_reason == {}


def _unsafe_paths() -> list[str]:
    return [
        "/a b",
        "/a%20b",
        "/a?x=1",
        "/a#frag",
        "/caf" + chr(0xE9),
        "/a" + chr(92) + "b",
        "/" + "a" * 200,  # 200 characters after the slash: one over the filter
        "no-leading-slash",
        "/trailing-newline" + chr(10),
        "",
    ]


@pytest.mark.parametrize("path", _unsafe_paths())
def test_a_path_outside_the_filter_is_counted_as_skipped_and_never_selected(path):
    selection = select_routes(_doc({path: _get(), "/fine": _get()}))

    assert selection.eligible == ("/fine",)
    assert selection.skipped_by_reason == {preview_smoke.SKIP_UNSAFE_PATH: 1}


def test_the_longest_path_the_filter_allows_is_called():
    longest = "/" + "a" * 199

    selection = select_routes(_doc({longest: _get()}))

    assert selection.eligible == (longest,)


def test_each_skip_reason_is_counted_under_its_own_name():
    doc = _doc(
        {
            "/items/{id}": _get(),
            "/a b": _get(),
            "/req": _get(parameters=[{"name": "q", "in": "query", "required": True}]),
            "/ref": _get(parameters=[{"$ref": "#/x"}]),
            "/ok": _get(),
        }
    )

    selection = select_routes(doc)

    assert selection.eligible == ("/ok",)
    assert selection.skipped_by_reason == {
        preview_smoke.SKIP_PATH_PARAMETER: 1,
        preview_smoke.SKIP_UNSAFE_PATH: 1,
        preview_smoke.SKIP_REQUIRED_PARAMETER: 1,
        preview_smoke.SKIP_UNRESOLVED_REF: 1,
    }
    assert selection.skipped == 4


def test_eligible_paths_come_back_sorted():
    selection = select_routes(_doc({"/z": _get(), "/a": _get(), "/m/n": _get()}))

    assert selection.eligible == ("/a", "/m/n", "/z")


@pytest.mark.parametrize("document", [{}, {"paths": None}, {"paths": []}, {"paths": "x"}])
def test_a_document_without_an_object_paths_cannot_be_selected_from(document):
    assert select_routes(document) is None


def test_a_valid_document_with_no_paths_is_a_selection_of_nothing_not_none():
    selection = select_routes(_doc({}))

    assert selection is not None and selection.eligible == () and selection.skipped == 0


# --- the runner: what is sent, what is stored ---------------------------------------------------


def test_the_runner_sends_only_get_and_stores_path_status_and_outcome():
    prober = RecordingProber(_doc({"/ok": _get(), "/boom": _get()}), answers={"/boom": SmokeResponse(500, [b"x"])})

    fact = run_smoke_check(prober)

    assert fact == {
        "routes": [
            {"path": "/boom", "status": 500, "outcome": "server_error"},
            {"path": "/ok", "status": 200, "outcome": "ok"},
        ],
        "skipped": 0,
        "truncated": False,
    }
    assert {method for method, *_ in prober.calls} == {"GET"}
    assert [c[1] for c in prober.calls] == ["/openapi.json", "/boom", "/ok"]


def test_a_path_outside_the_filter_is_never_sent_and_never_stored():
    prober = RecordingProber(_doc({"/a b": _get(), "/a%20b": _get(), "/ok": _get()}))

    fact = run_smoke_check(prober)

    assert [c[1] for c in prober.route_calls] == ["/ok"]
    assert fact["skipped"] == 2
    assert "a b" not in json.dumps(fact) and "%20" not in json.dumps(fact)


def test_a_post_only_path_and_the_framework_paths_are_never_sent():
    prober = RecordingProber(
        _doc({"/make": {"post": {}}, "/docs": _get(), "/redoc": _get(), "/docs/oauth2-redirect": _get(), "/ok": _get()})
    )

    run_smoke_check(prober)

    assert [c[1] for c in prober.route_calls] == ["/ok"]


def test_a_measured_result_with_no_routes_is_different_from_not_measured():
    fact = run_smoke_check(RecordingProber(_doc({"/items/{id}": _get(), "/make": {"post": {}}})))

    assert fact == {"routes": [], "skipped": 1, "truncated": False}


# --- the openapi document: whenever the platform could not look, the fact is None --------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"openapi_status": 404, "openapi_body": b"{}"},
        {"openapi_status": 500, "openapi_body": b'{"paths": {}}'},
        {"openapi_body": b"<html>not json</html>"},
        {"openapi_body": b"[]"},
        {"openapi_body": b'"paths"'},
        {"openapi_body": b'{"openapi": "3.1.0"}'},
        {"openapi_body": b'{"paths": []}'},
        {"openapi_body": b"\xff\xfe"},
        {"openapi_body": b"[" * 100000},
    ],
)
def test_an_unreadable_openapi_document_leaves_the_fact_not_measured(kwargs):
    prober = RecordingProber(**kwargs)

    assert run_smoke_check(prober) is None
    assert prober.route_calls == []


def test_a_probe_error_reading_the_document_leaves_the_fact_not_measured():
    def prober(method, path, read_limit, timeout):
        raise ProbeError("no live preview")

    assert run_smoke_check(prober) is None


class CountingChunks:
    """A FINITE source of `total` bytes in `chunk` -sized pieces that counts what was pulled."""

    def __init__(self, total, chunk=CHUNK_BYTES):
        self.total, self.chunk, self.pulled, self.bytes_pulled = total, chunk, 0, 0

    def __iter__(self):
        remaining = self.total
        while remaining > 0:
            size = min(self.chunk, remaining)
            remaining -= size
            self.pulled += 1
            self.bytes_pulled += size
            yield b"{" * size


def test_read_bounded_stops_at_the_cap_plus_one_chunk():
    source = CountingChunks(OPENAPI_READ_LIMIT_BYTES * 4)

    assert read_bounded(source, OPENAPI_READ_LIMIT_BYTES) is None

    assert source.bytes_pulled <= OPENAPI_READ_LIMIT_BYTES + CHUNK_BYTES
    assert source.bytes_pulled > OPENAPI_READ_LIMIT_BYTES  # it did read up to the cap, not less


def test_a_document_over_the_read_cap_is_not_measured_and_the_runner_stops_reading_it():
    source = CountingChunks(OPENAPI_READ_LIMIT_BYTES * 4)

    def prober(method, path, read_limit, timeout):
        return SmokeResponse(200, source)

    assert run_smoke_check(prober) is None
    assert source.bytes_pulled <= OPENAPI_READ_LIMIT_BYTES + CHUNK_BYTES


def test_a_document_exactly_at_the_cap_is_read():
    body = json.dumps(_doc({"/ok": _get()})).encode()
    padded = body[:-1] + b" " * (OPENAPI_READ_LIMIT_BYTES - len(body)) + body[-1:]
    assert len(padded) == OPENAPI_READ_LIMIT_BYTES
    prober = RecordingProber(openapi_body=padded)

    fact = run_smoke_check(prober)

    assert fact is not None and [r["path"] for r in fact["routes"]] == ["/ok"]


# --- bounds on the routes ---------------------------------------------------------------------------


def test_more_than_twenty_eligible_routes_make_exactly_twenty_calls_and_set_truncated():
    paths = {f"/r{i:02d}": _get() for i in range(MAX_ROUTES + 5)}
    prober = RecordingProber(_doc(paths))

    fact = run_smoke_check(prober)

    assert MAX_ROUTES == 20
    assert len(prober.route_calls) == 20
    assert [r["path"] for r in fact["routes"]] == sorted(paths)[:20]
    assert fact["truncated"] is True and fact["skipped"] == 0


def test_exactly_twenty_eligible_routes_are_not_truncated():
    prober = RecordingProber(_doc({f"/r{i:02d}": _get() for i in range(20)}))

    fact = run_smoke_check(prober)

    assert len(prober.route_calls) == 20 and fact["truncated"] is False


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_the_total_budget_stops_the_calls_and_sets_truncated_without_a_real_sleep():
    clock = Clock()
    answers = {}
    paths = {f"/r{i}": _get() for i in range(10)}
    prober = RecordingProber(_doc(paths), answers=answers)
    original = prober.__call__

    def spending(method, path, read_limit, timeout):
        clock.now += 25.0  # each call costs 25 s of a 60 s budget
        return original(method, path, read_limit, timeout)

    fact = run_smoke_check(spending, clock=clock)

    # openapi (25) + r0 (50) + r1 (75): the third route call is started at 50 s, the fourth finds
    # the budget spent.
    assert [r["path"] for r in fact["routes"]] == ["/r0", "/r1"]
    assert fact["truncated"] is True


def test_the_per_request_timeout_is_ten_seconds_and_never_more_than_the_budget_left():
    clock = Clock()
    prober = RecordingProber(_doc({"/a": _get(), "/b": _get()}))
    original = prober.__call__

    def spending(method, path, read_limit, timeout):
        if path == "/a":
            clock.now += 55.0
        return original(method, path, read_limit, timeout)

    run_smoke_check(spending, clock=clock)

    timeouts = {path: timeout for _, path, _, timeout in prober.calls}
    assert timeouts["/openapi.json"] == 10.0 and timeouts["/a"] == 10.0
    assert timeouts["/b"] == pytest.approx(5.0)


def test_the_read_limits_are_the_documented_ones():
    prober = RecordingProber(_doc({"/ok": _get()}))

    run_smoke_check(prober)

    assert OPENAPI_READ_LIMIT_BYTES == 256 * 1024 and ROUTE_READ_LIMIT_BYTES == 64 * 1024
    assert prober.calls[0][2] == 262144 and prober.calls[1][2] == 65536


def test_a_route_body_over_the_read_limit_is_too_large_with_its_real_status_and_is_not_read_to_the_end():
    source = CountingChunks(ROUTE_READ_LIMIT_BYTES * 4)
    prober = RecordingProber(_doc({"/big": _get()}), answers={"/big": SmokeResponse(200, source)})

    fact = run_smoke_check(prober)

    assert fact["routes"] == [{"path": "/big", "status": 200, "outcome": "too_large"}]
    assert source.bytes_pulled <= ROUTE_READ_LIMIT_BYTES + CHUNK_BYTES


def test_a_route_body_exactly_at_the_read_limit_is_not_too_large():
    prober = RecordingProber(
        _doc({"/edge": _get()}), answers={"/edge": SmokeResponse(200, [b"x" * ROUTE_READ_LIMIT_BYTES])}
    )

    assert run_smoke_check(prober)["routes"][0]["outcome"] == "ok"


# --- classification ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status, outcome",
    [(200, "ok"), (204, "ok"), (302, "ok"), (404, "client_error"), (422, "client_error"), (500, "server_error"), (503, "server_error")],
)
def test_statuses_are_classified(status, outcome):
    assert classify(status) == outcome
    prober = RecordingProber(_doc({"/x": _get()}), answers={"/x": SmokeResponse(status, [])})

    assert run_smoke_check(prober)["routes"] == [{"path": "/x", "status": status, "outcome": outcome}]


@pytest.mark.parametrize("status", [0, 99, 100, 199, 600, 999, True, None, "200"])
def test_a_status_that_is_not_an_http_answer_is_no_answer_with_a_null_status(status):
    prober = RecordingProber(_doc({"/x": _get()}), answers={"/x": SmokeResponse(status, [])})

    assert run_smoke_check(prober)["routes"] == [{"path": "/x", "status": None, "outcome": "no_answer"}]


def test_a_timeout_a_connection_failure_and_a_preview_start_error_are_no_answer_with_null_status():
    # What `PreviewSmokeProber` raises for all three (a timeout and a refused connection surface from the
    # proxy as `PreviewStartError`): a `ProbeError`.
    prober = RecordingProber(
        _doc({"/timeout": _get(), "/refused": _get(), "/gone": _get()}),
        answers={
            "/timeout": ProbeError("did not answer in time"),
            "/refused": ProbeError("connection refused"),
            "/gone": ProbeError("the preview could not be reached"),
        },
    )

    fact = run_smoke_check(prober)

    assert fact["routes"] == [
        {"path": "/gone", "status": None, "outcome": "no_answer"},
        {"path": "/refused", "status": None, "outcome": "no_answer"},
        {"path": "/timeout", "status": None, "outcome": "no_answer"},
    ]


def test_other_exceptions_from_the_prober_propagate_to_the_executor_which_turns_them_into_not_measured():
    prober = RecordingProber(_doc({"/x": _get()}), answers={"/x": RuntimeError("bug")})

    with pytest.raises(RuntimeError):
        run_smoke_check(prober)


MARKER = "MARKER-9f3c-from-the-container"


def test_neither_a_response_body_nor_a_preview_start_error_text_reaches_the_stored_fact():
    class Manager:
        def __init__(self):
            self.methods = []

        def proxy(self, build_id, *, method, path, timeout_seconds, max_body_bytes, **kw):
            self.methods.append(method)
            if path == "/openapi.json":
                doc = _doc({"/page": _get(), "/dead": _get()})
                return 200, json.dumps(doc).encode(), "application/json"
            if path == "/dead":
                raise PreviewStartError(f"preview proxy exec failed (exit 1): b'{MARKER}'")
            return 200, MARKER.encode(), "text/plain"

    token, _ = issue_preview_token("b-1")
    manager = Manager()
    prober = PreviewSmokeProber(
        build_id="b-1",
        preview_url=f"/api/projects/p/preview/b-1?token={token}",
        token_hash=hash_token(token),
        preview_manager=manager,
    )

    fact = run_smoke_check(prober)

    assert fact["routes"] == [
        {"path": "/dead", "status": None, "outcome": "no_answer"},
        {"path": "/page", "status": 200, "outcome": "ok"},
    ]
    assert MARKER not in json.dumps(fact)
    assert set(manager.methods) == {"GET"}


# --- the default prober -------------------------------------------------------------------------


class FakeManager:
    def __init__(self, response=(200, b"ok", None)):
        self.calls = []
        self.response = response

    def proxy(self, build_id, **kwargs):
        self.calls.append((build_id, kwargs))
        return self.response


def _prober(manager, *, token_ok=True, url="/api/projects/p/preview/b-1?token={token}"):
    token, _ = issue_preview_token("b-1")
    return PreviewSmokeProber(
        build_id="b-1",
        preview_url=url.format(token=token) if url else url,
        token_hash=hash_token(token if token_ok else token + "x"),
        preview_manager=manager,
    )


def test_the_default_prober_asks_the_proxy_for_the_limit_plus_one_byte_and_a_get():
    manager = FakeManager(response=(200, b"x" * 100, None))

    response = _prober(manager)("GET", "/ping", 50, 7.5)

    assert manager.calls == [
        (
            "b-1",
            {
                "method": "GET",
                "path": "/ping",
                "timeout_seconds": 7.5,
                "max_body_bytes": 51,
                # A wall-clock deadline equal to the request's allowance, and
                # a 3xx is its own answer.
                "wall_clock_seconds": 7.5,
                "follow_redirects": False,
            },
        )
    ]
    assert response.status == 200 and b"".join(response.body) == b"x" * 51


def test_a_redirect_is_not_followed_and_a_wall_clock_deadline_is_always_asked_for():
    """Mutation (h): the smoke prober passing `follow_redirects=True` (or leaving it out) turns this
    red; so does dropping the deadline."""
    manager = FakeManager(response=(302, b"", None))

    response = _prober(manager)("GET", "/old", 10, 4.0)

    (_build_id, kwargs), = manager.calls
    assert kwargs["follow_redirects"] is False
    assert kwargs["wall_clock_seconds"] == 4.0
    assert response.status == 302


def test_the_default_prober_hands_the_body_over_in_chunks():
    manager = FakeManager(response=(200, b"x" * (CHUNK_BYTES * 2 + 1), None))

    chunks = list(_prober(manager)("GET", "/", CHUNK_BYTES * 3, 1.0).body)

    assert [len(c) for c in chunks] == [CHUNK_BYTES, CHUNK_BYTES, 1]


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD", "get"])
def test_the_default_prober_refuses_any_method_but_get(method):
    manager = FakeManager()

    with pytest.raises(ValueError):
        _prober(manager)(method, "/", 10, 1.0)

    assert manager.calls == []


def test_a_token_that_does_not_match_is_refused_before_anything_is_forwarded():
    manager = FakeManager()

    with pytest.raises(ProbeError):
        _prober(manager, token_ok=False)("GET", "/", 10, 1.0)

    assert manager.calls == []


# --- the one definition of a failing route ---------------------------------------


@pytest.mark.parametrize(
    "route, failing",
    [
        ({"path": "/a", "status": 500, "outcome": "server_error"}, True),
        ({"path": "/a", "status": 503, "outcome": "server_error"}, True),
        ({"path": "/a", "status": None, "outcome": "no_answer"}, True),
        # A 5xx with an oversized body is stored as `too_large`, and is still a failure.
        ({"path": "/a", "status": 500, "outcome": "too_large"}, True),
        ({"path": "/a", "status": 200, "outcome": "ok"}, False),
        ({"path": "/a", "status": 302, "outcome": "ok"}, False),
        ({"path": "/a", "status": 404, "outcome": "client_error"}, False),
        ({"path": "/a", "status": 499, "outcome": "client_error"}, False),
        ({"path": "/a", "status": 200, "outcome": "too_large"}, False),
        ({"path": "/a", "status": True, "outcome": "ok"}, False),  # a bool is not a status
        # Mixed rows: the status and the outcome disagree, and either one alone is a failure.
        ({"path": "/a", "status": 500, "outcome": "client_error"}, True),
        ({"path": "/a", "status": 499, "outcome": "server_error"}, True),
        ({"path": "/a", "status": 503, "outcome": "ok"}, True),
        ({"path": "/a", "status": None, "outcome": "too_large"}, False),
        ("not a route", False),
        (None, False),
    ],
)
def test_route_failed_is_a_5xx_or_a_server_error_or_no_answer(route, failing):
    from app.services.preview_smoke import route_failed

    assert route_failed(route) is failing


def test_has_failing_route_is_false_for_an_unmeasured_or_malformed_fact():
    from app.services.preview_smoke import has_failing_route

    failing = {"path": "/a", "status": 500, "outcome": "server_error"}
    assert has_failing_route({"routes": [failing], "skipped": 0, "truncated": False}) is True
    assert has_failing_route({"routes": [], "skipped": 0, "truncated": False}) is False
    for not_measured in (None, {}, {"routes": "x"}, [failing], "x"):
        assert has_failing_route(not_measured) is False


def test_failing_routes_keeps_stored_order_drops_unsafe_paths_and_caps_at_twenty():
    from app.services.preview_smoke import failing_routes

    routes = [{"path": f"/r{i:02d}", "status": 500, "outcome": "server_error"} for i in range(25)]
    routes.insert(1, {"path": "/bad\nGET /x", "status": 500, "outcome": "server_error"})
    routes.insert(2, {"path": "/fine", "status": 200, "outcome": "ok"})
    routes.insert(3, {"path": "/hung", "status": None, "outcome": "no_answer"})

    found = failing_routes({"routes": routes, "skipped": 0, "truncated": False})

    assert [r["path"] for r in found][:3] == ["/r00", "/hung", "/r01"]
    assert len(found) == 20
    assert {"path": "/hung", "status": None, "outcome": "no_answer"} in found
    assert all("\n" not in r["path"] for r in found)


def test_failing_routes_of_a_fact_that_was_never_measured_is_empty():
    from app.services.preview_smoke import failing_routes

    assert failing_routes(None) == [] and failing_routes({"routes": None}) == []
