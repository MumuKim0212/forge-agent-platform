"""The eval request set.

The set is code: a versioned JSON file in the repository (`backend/evals/request_set.json`),
an ordered list of cases, each with a request text and one or more platform checks. Changing a
case or a check is a reviewed change that bumps `version`; a run records the version it used, so
results are compared only within one version.

`load_request_set` validates the file and raises `RequestSetError` (a message safe to show)
rather than ever returning a set that cannot be judged: unique case ids, a request within the
usual 8,000-character cap, at least one check per case, and a check that names an HTTP method,
a path and a status that make sense. A check carries no request body: the platform makes it
against the preview with a method and a path (query string included), nothing else.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.schemas.change_request import NL_REQUEST_MAX_CHARS

REQUEST_SET_PATH = Path(__file__).resolve().parents[2] / "evals" / "request_set.json"

# The methods a check may use. Deliberately small: a check observes a preview, it does not
# drive it beyond the one write a "write then read" case needs.
CHECK_METHODS = ("GET", "POST")
CHECK_PATH_MAX_CHARS = 200
EXPECT_SUBSTRING_MAX_CHARS = 200
_CASE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")  # lower-case: it becomes part of a repository name


class RequestSetError(ValueError):
    """The request set file is not a valid set. The message names what is wrong."""


@dataclass(frozen=True)
class Check:
    method: str
    path: str
    expect_status: int
    expect_substring: str | None


@dataclass(frozen=True)
class EvalCaseSpec:
    id: str
    request: str
    checks: tuple[Check, ...]


@dataclass(frozen=True)
class RequestSet:
    version: int
    cases: tuple[EvalCaseSpec, ...]


def _check(case_id: str, index: int, raw: object) -> Check:
    where = f"case {case_id!r} check {index}"
    if not isinstance(raw, dict):
        raise RequestSetError(f"{where}: must be an object")
    method = raw.get("method")
    if method not in CHECK_METHODS:
        raise RequestSetError(f"{where}: method must be one of {', '.join(CHECK_METHODS)}")
    path = raw.get("path")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or len(path) > CHECK_PATH_MAX_CHARS
        or any(ord(c) < 0x21 or ord(c) > 0x7E for c in path)
    ):
        raise RequestSetError(
            f"{where}: path must start with a single '/', be at most {CHECK_PATH_MAX_CHARS} "
            "printable ASCII characters and hold no whitespace"
        )
    status = raw.get("expect_status")
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise RequestSetError(f"{where}: expect_status must be an integer from 100 to 599")
    substring = raw.get("expect_substring")
    if substring is not None and (
        not isinstance(substring, str) or not substring or len(substring) > EXPECT_SUBSTRING_MAX_CHARS
    ):
        raise RequestSetError(
            f"{where}: expect_substring, when given, must be a non-empty string of at most "
            f"{EXPECT_SUBSTRING_MAX_CHARS} characters"
        )
    return Check(method=method, path=path, expect_status=status, expect_substring=substring)


def parse_request_set(data: object) -> RequestSet:
    """Validate an already-parsed document. Split from `load_request_set` so a test can feed it
    a deliberately broken one without writing a file."""
    if not isinstance(data, dict):
        raise RequestSetError("the request set must be a JSON object")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise RequestSetError("version must be a positive integer")
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise RequestSetError("cases must be a non-empty list")

    cases: list[EvalCaseSpec] = []
    seen: set[str] = set()
    for position, raw in enumerate(raw_cases):
        if not isinstance(raw, dict):
            raise RequestSetError(f"case {position}: must be an object")
        case_id = raw.get("id")
        if not isinstance(case_id, str) or not _CASE_ID.match(case_id):
            raise RequestSetError(
                f"case {position}: id must be 1-40 characters of lower-case letters, digits and "
                "hyphens, starting with a letter or digit"
            )
        if case_id in seen:
            raise RequestSetError(f"duplicate case id {case_id!r}")
        seen.add(case_id)
        request = raw.get("request")
        if not isinstance(request, str) or not request.strip():
            raise RequestSetError(f"case {case_id!r}: request must be a non-empty string")
        if len(request) > NL_REQUEST_MAX_CHARS:
            raise RequestSetError(
                f"case {case_id!r}: request is {len(request)} characters, over the "
                f"{NL_REQUEST_MAX_CHARS}-character cap"
            )
        raw_checks = raw.get("checks")
        if not isinstance(raw_checks, list) or not raw_checks:
            raise RequestSetError(f"case {case_id!r}: needs at least one check")
        checks = tuple(_check(case_id, i, c) for i, c in enumerate(raw_checks))
        cases.append(EvalCaseSpec(id=case_id, request=request, checks=checks))
    return RequestSet(version=version, cases=tuple(cases))


def load_request_set(path: Path | None = None) -> RequestSet:
    target = path or REQUEST_SET_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RequestSetError(f"the request set file could not be read ({type(exc).__name__})") from exc
    except ValueError as exc:
        raise RequestSetError("the request set file is not valid JSON") from exc
    return parse_request_set(data)
