"""Judging one eval case.

A case passes only if, checked in this order, all hold -- and the FIRST failure is the recorded
reason:

  1. the build is `SUCCEEDED` (otherwise the build's own failure code);
  2. the preview answers `GET /healthz` with 200 (otherwise `PREVIEW_UNHEALTHY`);
  3. every platform check, in order: the status matches and, if one is given, the expected
     substring is in the body (otherwise `CHECK_FAILED`, with the check's index).

**Measured, never agent-reported.** `judge_case` takes the build's status and failure code as
plain values, not the build: the agent's summary and the build trace are not reachable from
here, and no import below could reach them (the exact-mention scans over `app/`
in `tests/test_agent_summary.py` and `tests/test_build_trace_capture.py` cover this module like
every other). The checks are made by the PLATFORM, in L2, through a `probe` -- a callable taking
a method and a path and returning a status and a bounded body. The default probe
(`PreviewRouteProbe`) goes through the preview's token-checked route; a test passes a stub.

**The response body is untrusted.** It is read up to `BODY_READ_LIMIT_BYTES`, searched for the
expected substring, and dropped. What is kept is a short platform-authored outcome string and the
status code, never the body.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Sequence
from urllib.parse import parse_qs, urlsplit

from app.models.eval_run import EvalCaseResult, EvalFailureReason
from app.services.eval_request_set import Check
from app.services.preview_manager import (
    PreviewAccessError,
    PreviewManager,
    PreviewStartError,
    verify_preview_token,
)
from app.services.status import BuildFailureReason, BuildStatus

logger = logging.getLogger("app.eval_judge")

# One request's wall-clock allowance, and how much of a response is looked at. Named, not
# inlined: they are the "timeout and bounded read" the spec requires, and a test pins both.
CHECK_TIMEOUT_SECONDS = 10.0
BODY_READ_LIMIT_BYTES = 64 * 1024

HEALTHZ_PATH = "/healthz"


class ProbeError(Exception):
    """The platform could not make the request at all (no live preview, a bad token, the
    container not running, a timeout). The message is platform-authored and safe to store."""


@dataclass(frozen=True)
class ProbeResponse:
    status: int
    body: bytes  # already bounded by the probe; the judge truncates again, never trusts it


# `(method, path) -> ProbeResponse`, raising `ProbeError` when the request cannot be made.
Probe = Callable[[str, str], ProbeResponse]


@dataclass(frozen=True)
class Verdict:
    result: str  # EvalCaseResult.PASSED | FAILED
    failure_reason: str | None
    failed_check_index: int | None
    # One entry per request made, platform-authored: {index (None for /healthz), method, path,
    # expected_status, status (the actual one or None), ok, outcome}. Never a body.
    outcomes: list[dict] = field(default_factory=list)


def build_succeeded(build_status: str) -> bool:
    """The first gate, named so a mutation of it is one place."""
    return build_status == BuildStatus.SUCCEEDED


def check_passes(check: Check, response: ProbeResponse) -> tuple[bool, str]:
    """Whether one check passes, and the short outcome that is stored in place of the body.
    Only the first `BODY_READ_LIMIT_BYTES` of the body are searched."""
    if response.status != check.expect_status:
        return False, f"expected status {check.expect_status}, got {response.status}"
    if check.expect_substring is not None:
        text = response.body[:BODY_READ_LIMIT_BYTES].decode("utf-8", errors="replace")
        if check.expect_substring not in text:
            return False, (
                f"status {response.status} as expected, but the expected text was not found in "
                f"the first {BODY_READ_LIMIT_BYTES} bytes of the response"
            )
        return True, f"status {response.status} as expected and the expected text was found"
    return True, f"status {response.status} as expected"


def _make_request(probe: Probe, method: str, path: str) -> tuple[ProbeResponse | None, str | None]:
    try:
        return probe(method, path), None
    except ProbeError as exc:
        return None, f"the request could not be made: {exc}"
    except Exception as exc:  # noqa: BLE001 - a probe that raises must fail the case, never the worker tick
        logger.warning("eval judge: probe raised %s for %s %s", type(exc).__name__, method, path)
        return None, f"the request could not be made ({type(exc).__name__})"


def judge_case(
    *,
    build_status: str,
    build_failure_reason: str | None,
    checks: Sequence[Check],
    probe: Probe,
) -> Verdict:
    if not build_succeeded(build_status):
        return Verdict(
            result=EvalCaseResult.FAILED,
            # A FAILED build always carries a code (`mark_failed` requires it); the fallback is
            # for a status that should not have reached the judge at all.
            failure_reason=build_failure_reason or BuildFailureReason.INFRA_ERROR,
            failed_check_index=None,
        )

    outcomes: list[dict] = []
    response, error = _make_request(probe, "GET", HEALTHZ_PATH)
    healthy = response is not None and response.status == 200
    outcomes.append(
        {
            "index": None,
            "method": "GET",
            "path": HEALTHZ_PATH,
            "expected_status": 200,
            "status": response.status if response is not None else None,
            "ok": healthy,
            "outcome": error
            or (f"status {response.status} as expected" if healthy else f"expected status 200, got {response.status}"),
        }
    )
    if not healthy:
        return Verdict(
            result=EvalCaseResult.FAILED,
            failure_reason=EvalFailureReason.PREVIEW_UNHEALTHY,
            failed_check_index=None,
            outcomes=outcomes,
        )

    for index, check in enumerate(checks):
        response, error = _make_request(probe, check.method, check.path)
        if response is None:
            ok, outcome = False, error or "the request could not be made"
        else:
            ok, outcome = check_passes(check, response)
        outcomes.append(
            {
                "index": index,
                "method": check.method,
                "path": check.path,
                "expected_status": check.expect_status,
                "status": response.status if response is not None else None,
                "ok": ok,
                "outcome": outcome,
            }
        )
        if not ok:
            return Verdict(
                result=EvalCaseResult.FAILED,
                failure_reason=EvalFailureReason.CHECK_FAILED,
                failed_check_index=index,
                outcomes=outcomes,
            )

    return Verdict(
        result=EvalCaseResult.PASSED, failure_reason=None, failed_check_index=None, outcomes=outcomes
    )


class PreviewRouteProbe:
    """The default probe: the preview's token-checked route, made from L2.

    The route (`routers/preview_proxy.py`) is two steps -- verify the signed token against the
    build's stored hash, then forward one request into the preview container through
    `PreviewManager.proxy` (the container has no published port at all). This does exactly those
    two steps with the token the build's own `preview_url` carries, rather than calling the
    platform over HTTP: the worker is a separate process, and in Traefik mode the same URL is a
    host name the worker may not be able to resolve. A missing, expired or mismatching token is
    a `ProbeError`, exactly as the route would have answered 403.
    """

    def __init__(
        self,
        *,
        build_id: str,
        preview_url: str | None,
        token_hash: str | None,
        preview_manager: PreviewManager,
        timeout_seconds: float = CHECK_TIMEOUT_SECONDS,
        max_body_bytes: int = BODY_READ_LIMIT_BYTES,
    ) -> None:
        self._build_id = build_id
        self._preview_url = preview_url
        self._token_hash = token_hash
        self._manager = preview_manager
        self._timeout = timeout_seconds
        self._max_body = max_body_bytes

    def _token(self) -> str:
        if not self._preview_url:
            raise ProbeError("the build has no live preview")
        values = parse_qs(urlsplit(self._preview_url).query).get("token")
        if not values or not values[0]:
            raise ProbeError("the build's preview address carries no token")
        return values[0]

    def __call__(self, method: str, path: str) -> ProbeResponse:
        try:
            verify_preview_token(self._build_id, self._token(), expected_hash=self._token_hash)
        except PreviewAccessError as exc:
            raise ProbeError(f"the preview token was refused: {exc}") from exc
        try:
            status, body, _content_type = self._manager.proxy(
                self._build_id,
                method=method,
                path=path,
                timeout_seconds=self._timeout,
                max_body_bytes=self._max_body,
            )
        except PreviewStartError as exc:
            # Not `exc`'s text: a failed exec's message can carry what the container printed,
            # which is generated-service output and never stored.
            logger.warning("eval judge: preview of build %s unreachable: %s", self._build_id, exc)
            raise ProbeError("the preview could not be reached (it is not running, or did not answer in time)") from exc
        return ProbeResponse(status=status, body=body[: self._max_body])
