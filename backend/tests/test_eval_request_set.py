"""the versioned request set loads, and the loader
refuses a set that could not be judged.

The first test pins the SHIPPED file with literals (version, the case ids, how many checks
each has): a change to the set is a reviewed change that bumps `version`, so a silent edit of the
file fails here and is a visible diff in this one.
"""

import copy

import pytest

from app.services.eval_request_set import (
    REQUEST_SET_PATH,
    RequestSetError,
    load_request_set,
    parse_request_set,
)


def test_the_shipped_v3_request_set_loads_and_is_pinned():
    request_set = load_request_set()

    assert REQUEST_SET_PATH.name == "request_set.json"
    assert request_set.version == 3
    assert [c.id for c in request_set.cases] == [
        "static-page",
        "database-page",
        "write-then-read-form",
        "unprompted-screen",
        "room-bookings",
    ]
    assert [len(c.checks) for c in request_set.cases] == [1, 1, 2, 2, 2]
    # Every shipped check names a method and an absolute path and expects a substring.
    for case in request_set.cases:
        assert 0 < len(case.request) <= 8000
        for check in case.checks:
            assert check.method in ("GET", "POST")
            assert check.path.startswith("/")
            assert check.expect_status == 200
            assert check.expect_substring
    # The write-then-read case posts, then reads back what it posted.
    write, read = request_set.cases[2].checks
    assert (write.method, write.path) == ("POST", "/notes?text=forge-eval-note-7431")
    assert (read.method, read.path) == ("GET", "/notes")
    assert write.expect_substring == read.expect_substring == "forge-eval-note-7431"


def test_the_unprompted_screen_case_never_mentions_a_screen_and_is_judged_on_the_root():
    """this case measures the system prompt, not the request, so the request must not
    say what shape the answer takes. The word list is the shapes a requester could name."""
    case = load_request_set().cases[3]

    assert case.id == "unprompted-screen"
    request = case.request.lower()
    for word in ("page", "screen", "html", "browser", "website", "web ", "site", "ui ", "interface", "form"):
        assert word not in request
    # Judged by the existing check kinds: the name the request gave shows up on `GET /`, and the
    # body is a document rather than JSON. The starter page has the second but not the first.
    assert [(c.method, c.path, c.expect_status, c.expect_substring) for c in case.checks] == [
        ("GET", "/", 200, "Friday Lunch Tally"),
        ("GET", "/", 200, "<html"),
    ]
    assert "Friday Lunch Tally" in case.request


def test_the_room_bookings_case_names_its_routes_and_never_the_defect_or_the_repair():
    """The request is what a non-developer would write, so it must not hand the agent
    the defect it is meant to tempt (an unquoted `start`/`end` column) nor mention the repair."""
    case = load_request_set().cases[4]

    assert case.id == "room-bookings"
    request = case.request.lower()
    for word in ("sql", "column", "quot", "reserved", "error", "repair", "table", "database", "migration"):
        assert word not in request
    assert "post /bookings" in request and "get /bookings" in request and "get /healthz" in request
    for name in ("room", "start", "end"):
        assert f"`{name}`" in request
    assert [(c.method, c.path, c.expect_status, c.expect_substring) for c in case.checks] == [
        ("POST", "/bookings?room=forge-eval-room-7431&start=09:00&end=10:00", 200, "forge-eval-room-7431"),
        ("GET", "/bookings", 200, "forge-eval-room-7431"),
    ]
    assert len(case.request) <= 8000


def _bookings_probe(answers):
    from tests.test_eval_judge import HEALTHY, StubProbe
    from app.services.eval_judge import ProbeResponse

    base = {("GET", "/healthz"): HEALTHY}
    return StubProbe({**base, **{k: ProbeResponse(*v) for k, v in answers.items()}})


def _judge_bookings(probe, *, status="SUCCEEDED", reason=None):
    from app.services.eval_judge import judge_case

    return judge_case(
        build_status=status,
        build_failure_reason=reason,
        checks=load_request_set().cases[4].checks,
        probe=probe,
    )


POSTED = ("POST", "/bookings?room=forge-eval-room-7431&start=09:00&end=10:00")


def test_the_room_bookings_case_is_expressible_and_a_service_that_answers_both_routes_passes():
    probe = _bookings_probe(
        {
            POSTED: (200, b'{"room": "forge-eval-room-7431", "start": "09:00", "end": "10:00"}'),
            ("GET", "/bookings"): (200, b'[{"room": "forge-eval-room-7431", "start": "09:00", "end": "10:00"}]'),
        }
    )

    verdict = _judge_bookings(probe)

    assert (verdict.result, verdict.failure_reason, verdict.failed_check_index) == ("PASSED", None, None)
    assert probe.calls == [("GET", "/healthz"), POSTED, ("GET", "/bookings")]


@pytest.mark.parametrize(
    ("answers", "index"),
    [
        # The shipped defect: the write route answers 500 (the unquoted `end` column).
        ({POSTED: (500, b"Internal Server Error")}, 0),
        # The write works but the list does not hold what was written.
        ({POSTED: (200, b'{"room": "forge-eval-room-7431"}'), ("GET", "/bookings"): (200, b"[]")}, 1),
    ],
    ids=["write-route-500", "list-lacks-the-booking"],
)
def test_the_room_bookings_case_fails_with_the_failing_checks_index(answers, index):
    verdict = _judge_bookings(_bookings_probe(answers))

    assert (verdict.result, verdict.failure_reason, verdict.failed_check_index) == ("FAILED", "CHECK_FAILED", index)


GOOD = {
    "version": 3,
    "cases": [
        {
            "id": "one",
            "request": "Build a thing.",
            "checks": [{"method": "GET", "path": "/", "expect_status": 200}],
        },
        {
            "id": "two",
            "request": "Build another.",
            "checks": [{"method": "POST", "path": "/x?y=1", "expect_status": 201, "expect_substring": "ok"}],
        },
    ],
}


def _broken(mutate):
    data = copy.deepcopy(GOOD)
    mutate(data)
    return data


def test_a_valid_set_parses_and_substring_is_optional():
    request_set = parse_request_set(GOOD)

    assert request_set.version == 3
    assert request_set.cases[0].checks[0].expect_substring is None
    assert request_set.cases[1].checks[0].expect_substring == "ok"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(version=0), "version"),
        (lambda d: d.update(version="1"), "version"),
        (lambda d: d.update(version=True), "version"),
        (lambda d: d.update(cases=[]), "cases"),
        (lambda d: d["cases"][1].update(id="one"), "duplicate case id 'one'"),
        (lambda d: d["cases"][0].update(id="Upper"), "id must be"),
        (lambda d: d["cases"][0].update(id="has space"), "id must be"),
        (lambda d: d["cases"][0].update(request="   "), "request must be"),
        (lambda d: d["cases"][0].update(request="x" * 8001), "8000-character cap"),
        (lambda d: d["cases"][0].update(checks=[]), "needs at least one check"),
        (lambda d: d["cases"][0]["checks"][0].update(method="DELETE"), "method must be"),
        (lambda d: d["cases"][0]["checks"][0].update(method="get"), "method must be"),
        (lambda d: d["cases"][0]["checks"][0].update(path="no-slash"), "path must start"),
        (lambda d: d["cases"][0]["checks"][0].update(path="//evil.example/x"), "path must start"),
        (lambda d: d["cases"][0]["checks"][0].update(path="/a b"), "path must start"),
        (lambda d: d["cases"][0]["checks"][0].update(path="/" + "a" * 200), "path must start"),
        (lambda d: d["cases"][0]["checks"][0].update(expect_status=99), "expect_status"),
        (lambda d: d["cases"][0]["checks"][0].update(expect_status=600), "expect_status"),
        (lambda d: d["cases"][0]["checks"][0].update(expect_status="200"), "expect_status"),
        (lambda d: d["cases"][0]["checks"][0].update(expect_substring=""), "expect_substring"),
        (lambda d: d["cases"][0]["checks"][0].update(expect_substring="y" * 201), "expect_substring"),
    ],
)
def test_the_loader_refuses_a_set_that_could_not_be_judged(mutate, message):
    with pytest.raises(RequestSetError) as excinfo:
        parse_request_set(_broken(mutate))

    assert message in str(excinfo.value)


def test_a_request_of_exactly_8000_characters_is_accepted():
    data = _broken(lambda d: d["cases"][0].update(request="x" * 8000))

    assert len(parse_request_set(data).cases[0].request) == 8000


def test_a_file_that_is_not_json_or_not_there_raises_the_same_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")

    with pytest.raises(RequestSetError, match="not valid JSON"):
        load_request_set(bad)
    with pytest.raises(RequestSetError, match="could not be read"):
        load_request_set(tmp_path / "missing.json")
