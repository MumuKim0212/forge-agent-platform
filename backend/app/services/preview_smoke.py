"""A platform smoke check of the live preview (measured facts and agent
claims).

After a build's preview is up, the platform reads the generated service's `/openapi.json` through
the preview, calls every `GET` route that needs no path parameter and no required parameter, and
records one measured fact on the build: per route, the HTTP status or "did not answer"
(`Build.smoke_checks`). **It is informational.** It never fails a build, never changes
`ExecutionResult.ok`, never blocks or auto-decides an approval; the executor wraps the whole thing
so that any exception leaves the fact NULL ("not measured") and the build untouched.

**What is untrusted.** The OpenAPI document and every response are L3 output (ADR 0003/0032).
  * The document is read up to `OPENAPI_READ_LIMIT_BYTES`; over it, non-200, not JSON, not an
    object, or no object `paths` all mean the platform could not look -> NULL, not "zero routes".
  * A path is an agent-chosen string. Only paths matching `SAFE_PATH` are ever sent or stored;
    every other `GET` route is counted in `skipped` and its text goes nowhere.
  * A response body is read (bounded) only to learn whether it is over the read limit, and dropped.
    What is stored is a path that passed the filter, an integer the platform read, and an outcome
    word the platform chose. The text of a `PreviewStartError` (which can carry what the container
    printed) is never stored: it becomes `no_answer`.

**Preview data is disposable, and only `GET` is ever issued.** The runner passes the
constant `METHOD` to its prober; the default prober refuses anything else.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator

from app.services.eval_judge import ProbeError, PreviewRouteProbe
from app.services.preview_manager import PreviewManager

logger = logging.getLogger("app.preview_smoke")

METHOD = "GET"
OPENAPI_PATH = "/openapi.json"
# The framework's own routes: not the generated service's, so neither called nor counted.
FRAMEWORK_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})

OPENAPI_READ_LIMIT_BYTES = 256 * 1024
ROUTE_READ_LIMIT_BYTES = 64 * 1024
MAX_ROUTES = 20
REQUEST_TIMEOUT_SECONDS = 10.0
TOTAL_BUDGET_SECONDS = 60.0
# How big a piece the default prober hands over at a time; the runner stops after the piece that
# crosses a read limit, so at most one piece beyond it is ever pulled.
CHUNK_BYTES = 8 * 1024

# `fullmatch`, never `match`/`$`: `$` also matches before a trailing newline.
SAFE_PATH = re.compile(r"/[A-Za-z0-9._~/-]{0,199}")

OUTCOME_OK = "ok"
OUTCOME_CLIENT_ERROR = "client_error"
OUTCOME_SERVER_ERROR = "server_error"
OUTCOME_NO_ANSWER = "no_answer"
OUTCOME_TOO_LARGE = "too_large"

# Why a GET route was not called. Counted per reason so that each is its own observable.
SKIP_PATH_PARAMETER = "path_parameter"
SKIP_UNSAFE_PATH = "unsafe_path"
SKIP_REQUIRED_PARAMETER = "required_parameter"
SKIP_UNRESOLVED_REF = "unresolved_ref"


@dataclass(frozen=True)
class SmokeResponse:
    status: int
    body: Iterable[bytes]  # pulled by the runner only as far as a read limit needs


# `(method, path, read_limit_bytes, timeout_seconds) -> SmokeResponse`; raises `ProbeError` when the
# request cannot be made. The prober is expected to deliver at most `read_limit_bytes + 1` bytes
# (the extra byte is how "over the limit" is told from "exactly at it"); the runner does not rely
# on that and stops reading by itself.
Prober = Callable[[str, str, int, float], SmokeResponse]


@dataclass(frozen=True)
class RouteSelection:
    eligible: tuple[str, ...]  # safe, parameterless GET paths, sorted; NOT yet capped at MAX_ROUTES
    skipped_by_reason: dict[str, int]

    @property
    def skipped(self) -> int:
        return sum(self.skipped_by_reason.values())


def read_bounded(chunks: Iterable[bytes], cap: int) -> bytes | None:
    """The bytes of `chunks`, or None as soon as more than `cap` have been seen. Stops pulling from
    the source at that point (so at most one chunk past the cap is taken)."""
    collected = bytearray()
    for chunk in chunks:
        collected += chunk
        if len(collected) > cap:
            return None
    return bytes(collected)


def classify(status: int) -> str:
    """2xx and 3xx are `ok`, 4xx `client_error`, 5xx `server_error`; anything else (1xx, or not a
    status at all) is `no_answer`."""
    if 200 <= status < 400:
        return OUTCOME_OK
    if 400 <= status < 500:
        return OUTCOME_CLIENT_ERROR
    if 500 <= status < 600:
        return OUTCOME_SERVER_ERROR
    return OUTCOME_NO_ANSWER


def route_failed(route: object) -> bool:
    """The ONE definition of a failing smoke route: an integer status of 500 or
    above, or the outcome `server_error` or `no_answer`. A 5xx counts whatever the outcome says: the
    runner gives `too_large` precedence over the status class, so a 500 with an oversized body is
    stored as `too_large` with status 500. A 4xx is the route's own answer (a missing parameter, a
    login wall), a 3xx is a redirect that is not followed: neither is a failure. The repair loop
    (`sandbox_build_executor`) calls this; the approval console's frontend states the same rule in
    `isSmokeRouteFailure` (`measuredFactsLines.ts`), which this must keep agreeing with."""
    if not isinstance(route, dict):
        return False
    if route.get("outcome") in (OUTCOME_SERVER_ERROR, OUTCOME_NO_ANSWER):
        return True
    status = route.get("status")
    return isinstance(status, int) and not isinstance(status, bool) and status >= 500


def failing_routes(fact: object) -> list[dict]:
    """The routes of a stored smoke fact (`Build.smoke_checks`) that `route_failed` calls failing,
    in stored order, at most `MAX_ROUTES`. A fact that is None ("not measured") or not the stored
    shape has none: nothing unmeasured ever counts as a failure. Each route is returned as
    `{"path", "status", "outcome"}` and only if its path passes `SAFE_PATH` again (defence in depth:
    the fact came out of the database, and this list is the only thing a repair prompt is built
    from)."""
    if not isinstance(fact, dict) or not isinstance(fact.get("routes"), list):
        return []
    found: list[dict] = []
    for route in fact["routes"]:
        if not route_failed(route):
            continue
        path = route.get("path")
        if not isinstance(path, str) or not SAFE_PATH.fullmatch(path):
            continue
        status = route.get("status")
        found.append(
            {
                "path": path,
                "status": status if isinstance(status, int) and not isinstance(status, bool) else None,
                "outcome": route.get("outcome"),
            }
        )
        if len(found) >= MAX_ROUTES:
            break
    return found


def has_failing_route(fact: object) -> bool:
    """True when the stored smoke fact holds at least one failing route (`route_failed`). The repair
    loop's trigger."""
    if not isinstance(fact, dict) or not isinstance(fact.get("routes"), list):
        return False
    return any(route_failed(route) for route in fact["routes"])


def _parameter_skip(parameters: object) -> str | None:
    """A reason to skip when `parameters` (a path item's or an operation's list) holds anything the
    check cannot satisfy: an unresolved `$ref` (never resolved, so treated as required), or a
    parameter that is, or might be, required. An absent list is fine; a malformed one is not."""
    if parameters is None:
        return None
    if not isinstance(parameters, list):
        return SKIP_REQUIRED_PARAMETER
    reason: str | None = None
    for parameter in parameters:
        if not isinstance(parameter, dict):
            reason = reason or SKIP_REQUIRED_PARAMETER
        elif "$ref" in parameter:
            return SKIP_UNRESOLVED_REF
        else:
            required = parameter.get("required")
            if required is not None and required is not False:
                reason = reason or SKIP_REQUIRED_PARAMETER
    return reason


def select_routes(document: dict) -> RouteSelection | None:
    """Which routes of an OpenAPI document the check calls. None when `paths` is missing or not an
    object (the platform could not look). Only `get` operations count as routes; a route that is
    left out is counted by its reason, except the framework's own paths and paths with no `get`."""
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return None
    eligible: list[str] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for path in sorted(paths):
        item = paths[path]
        if path in FRAMEWORK_PATHS or not isinstance(item, dict):
            continue
        operation = item.get("get")
        if not isinstance(operation, dict):
            continue
        if "{" in path:
            skip(SKIP_PATH_PARAMETER)
        elif not SAFE_PATH.fullmatch(path):
            skip(SKIP_UNSAFE_PATH)
        else:
            reason = _parameter_skip(item.get("parameters")) or _parameter_skip(operation.get("parameters"))
            if reason is not None:
                skip(reason)
            else:
                eligible.append(path)
    return RouteSelection(eligible=tuple(eligible), skipped_by_reason=skipped)


def _read_openapi(prober: Prober, timeout: float) -> dict | None:
    """The parsed document, or None when the platform could not look at it."""
    try:
        response = prober(METHOD, OPENAPI_PATH, OPENAPI_READ_LIMIT_BYTES, timeout)
    except ProbeError:
        return None
    if response.status != 200:
        return None
    raw = read_bounded(response.body, OPENAPI_READ_LIMIT_BYTES)
    if raw is None:
        return None
    try:
        document = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return document if isinstance(document, dict) else None


def _route_result(prober: Prober, path: str, timeout: float) -> dict:
    try:
        response = prober(METHOD, path, ROUTE_READ_LIMIT_BYTES, timeout)
    except ProbeError:
        return {"path": path, "status": None, "outcome": OUTCOME_NO_ANSWER}
    status = response.status
    outcome = classify(status) if isinstance(status, int) and not isinstance(status, bool) else OUTCOME_NO_ANSWER
    if outcome == OUTCOME_NO_ANSWER:
        return {"path": path, "status": None, "outcome": OUTCOME_NO_ANSWER}
    if read_bounded(response.body, ROUTE_READ_LIMIT_BYTES) is None:
        outcome = OUTCOME_TOO_LARGE
    return {"path": path, "status": status, "outcome": outcome}


def run_smoke_check(prober: Prober, *, clock: Callable[[], float] = time.monotonic) -> dict | None:
    """The measured fact (`Build.smoke_checks`), or None when the platform could not look at the
    service's route list. `ProbeError` from a route is `no_answer`; any other exception from the
    prober propagates (the executor turns it into "not measured")."""
    started = clock()
    document = _read_openapi(prober, REQUEST_TIMEOUT_SECONDS)
    if document is None:
        return None
    selection = select_routes(document)
    if selection is None:
        return None

    routes: list[dict] = []
    truncated = len(selection.eligible) > MAX_ROUTES
    for path in selection.eligible[:MAX_ROUTES]:
        remaining = TOTAL_BUDGET_SECONDS - (clock() - started)
        if remaining <= 0:
            truncated = True
            break
        routes.append(_route_result(prober, path, min(REQUEST_TIMEOUT_SECONDS, remaining)))
    return {"routes": routes, "skipped": selection.skipped, "truncated": truncated}


class _ProxyOptions:
    """Hands `PreviewManager.proxy` the two options `eval_judge.PreviewRouteProbe` does not know
    (a wall-clock deadline and "do not follow redirects"), so the probe -- and `eval_judge.py` --
    stay exactly what the eval judge uses."""

    def __init__(self, manager: PreviewManager, **options) -> None:
        self._manager = manager
        self._options = options

    def proxy(self, build_id: str, **kwargs):
        return self._manager.proxy(build_id, **kwargs, **self._options)


class PreviewSmokeProber:
    """The default prober: the preview's token-checked route, made from L2, exactly as the eval
    judge makes it (`eval_judge.PreviewRouteProbe` does the token check and the
    `PreviewStartError` -> fixed-text `ProbeError` conversion; its message never carries what the
    container printed). One probe per call, because the read limit and timeout differ by request.

    **Redirects are not followed** (`follow_redirects=False`): a route that answers 302 is reported
    as that 302, never as the status of wherever it points. **Every request has a wall-clock
    deadline** inside the exec script (`wall_clock_seconds`), so an app that drips bytes or chains
    redirects cannot hold the build worker; it reads as `no_answer` (or, for `/openapi.json`, as
    "not measured")."""

    def __init__(
        self, *, build_id: str, preview_url: str | None, token_hash: str | None, preview_manager: PreviewManager
    ) -> None:
        self._build_id = build_id
        self._preview_url = preview_url
        self._token_hash = token_hash
        self._manager = preview_manager

    def __call__(self, method: str, path: str, read_limit: int, timeout_seconds: float) -> SmokeResponse:
        if method != METHOD:
            raise ValueError("the preview smoke check only issues GET")
        probe = PreviewRouteProbe(
            build_id=self._build_id,
            preview_url=self._preview_url,
            token_hash=self._token_hash,
            # Wall clock, not just a socket timeout: `docker exec` has no limit of its own and a
            # hostile app can drip bytes or chain redirects indefinitely.
            preview_manager=_ProxyOptions(
                self._manager, wall_clock_seconds=timeout_seconds, follow_redirects=False
            ),
            timeout_seconds=timeout_seconds,
            max_body_bytes=read_limit + 1,
        )
        response = probe(method, path)
        return SmokeResponse(status=response.status, body=_chunks(response.body))


def _chunks(body: bytes) -> Iterator[bytes]:
    for start in range(0, len(body), CHUNK_BYTES):
        yield body[start : start + CHUNK_BYTES]
