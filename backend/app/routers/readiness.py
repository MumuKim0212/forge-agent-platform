"""`GET /api/ready`: unauthenticated readiness for an uptime monitor.

200 `{"status":"ready"}`, or 503 `{"status":"not_ready","failing":[...]}` naming only `database`
and/or `worker`. `/api/health` stays the static liveness check.

Bounded: the check runs in a worker thread and the route waits `settings.readiness_timeout_seconds`
for it; a database that hangs answers 503 `database` at the deadline (the thread is abandoned, it
is not killed). The session comes from `get_session_factory`, not `get_db`, so a hanging pool
checkout cannot stall the dependency itself.

Not user activity: it touches no project and writes nothing. Not rate-limited: the platform has no
rate limiter. Access log: left as is -- a monitor polling every 10-30 s adds a few thousand lines a
day, the same as `/api/health`, so no exclusion rule is added.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app import db as app_db
from app.config import settings
from app.services.readiness import DATABASE, failing_checks

router = APIRouter(tags=["health"])


# At most one check in flight: a hung database would otherwise cost one abandoned
# thread per poll. A poll that finds one running answers 503 `database` at once.
_in_flight = threading.Lock()


def _settle(setter, value) -> None:
    try:
        setter(value)
    except concurrent.futures.InvalidStateError:
        pass  # the waiter timed out and cancelled; nobody is listening any more


def _run_in_daemon_thread(function, *args) -> "concurrent.futures.Future":
    """A thread of its own per call, not the event loop's default executor: a hung database call
    then holds one abandoned daemon thread, never a slot a later request or the loop's shutdown
    waits on (the default executor is joined when a loop closes)."""
    future: concurrent.futures.Future = concurrent.futures.Future()

    def _target() -> None:
        try:
            outcome = function(*args)
        except BaseException as exc:  # noqa: BLE001 - handed to the awaiting side
            _settle(future.set_exception, exc)
        else:
            _settle(future.set_result, outcome)
        finally:
            _in_flight.release()

    threading.Thread(target=_target, name="readiness-check", daemon=True).start()
    return future


def get_session_factory():
    return app_db.SessionLocal  # read at call time so a redirected factory is honoured


@router.get("/ready")
async def ready(session_factory=Depends(get_session_factory)) -> JSONResponse:
    if not _in_flight.acquire(blocking=False):
        failing = [DATABASE]  # a previous check has not returned: no second thread
    else:
        try:
            future = _run_in_daemon_thread(failing_checks, session_factory)
        except BaseException:
            _in_flight.release()
            raise
        try:
            failing = await asyncio.wait_for(
                asyncio.wrap_future(future), timeout=settings.readiness_timeout_seconds
            )
        except asyncio.TimeoutError:
            failing = [DATABASE]
    if failing:
        return JSONResponse(status_code=503, content={"status": "not_ready", "failing": failing})
    return JSONResponse(status_code=200, content={"status": "ready"})
