"""Graceful stop for the worker (ADR 0037 §Decision 6): on SIGTERM the worker stops
claiming, runs the failure teardown of the build or deployment in flight, and exits.

**How a stop gets out of the loop.** `worker._run_loop_tick` swallows every
`Exception` an iteration raises and goes round again, so a handler that raised a plain
exception would be logged and ignored. `WorkerShutdown` is a `SystemExit`: a `BaseException`
nothing in the loop's `except Exception` catches, which unwinds through the executor's and
`execute_deployment`'s own `finally` blocks and `except BaseException` failure paths and out
of `main()`. The flag (`requested`) is the second half: a signal that arrives where raising
would be unsafe only sets it, and the loop checks it before it claims anything.

**Where the signal may interrupt.** Not everywhere. A claim, a commit or an audit write cut in
half by an exception is the one place a stop could do harm, so the handler raises only inside
`interruptible()` -- the in-flight build or deployment, and the poll sleep -- and anywhere else
it records the request and lets the code reach the next check (`interruptible()` itself raises
at entry if a request is already pending, so work that was claimed but not yet started is
failed rather than started).

**One-shot.** The first signal raises; later ones are ignored. The failure teardown that the
first one triggered must finish, and a second SIGTERM landing in the middle of it would cut it
short. If the process is killed anyway (SIGKILL after a grace period), the next start's
`recover_orphaned_rows` and the reconciler cover it -- that is what they are for.

SIGINT and (on Windows) SIGBREAK are handled the same way: a developer's Ctrl+C is the only
graceful stop a Windows console has, because `os.kill(pid, SIGTERM)` there is a plain
`TerminateProcess` that no handler sees.
"""

from __future__ import annotations

import logging
import signal
from contextlib import contextmanager

logger = logging.getLogger("app.worker_shutdown")


class WorkerShutdown(SystemExit):
    """The worker was asked to stop. A `SystemExit` (exit status 0: a requested stop is not a
    failure), deliberately NOT an `Exception`: see the module docstring."""

    def __init__(self, signum: int | None = None) -> None:
        super().__init__(0)
        self.signum = signum


def stop_signals() -> tuple[int, ...]:
    """The signals that mean "stop": SIGTERM everywhere, SIGINT, and SIGBREAK where it exists
    (Windows)."""
    found = [signal.SIGTERM, signal.SIGINT]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        found.append(sigbreak)
    return tuple(found)


class ShutdownController:
    def __init__(self) -> None:
        self.requested = False
        self.signum: int | None = None
        self._interruptible = 0

    def reset(self) -> None:
        self.requested = False
        self.signum = None
        self._interruptible = 0

    def handle(self, signum, frame=None) -> None:
        """The signal handler. See the module docstring for when it raises."""
        if self.requested:
            return  # one-shot: the teardown the first signal started must finish
        self.requested = True
        self.signum = signum
        logger.warning("worker: received signal %s; stopping", signum)
        if self._interruptible > 0:
            raise WorkerShutdown(signum)

    @contextmanager
    def interruptible(self):
        """Marks the code inside as safe to interrupt. Raises `WorkerShutdown` at entry when a
        stop was already requested, so a caller never starts work after the request."""
        if self.requested:
            raise WorkerShutdown(self.signum)
        self._interruptible += 1
        try:
            yield
        finally:
            self._interruptible -= 1

    def install(self) -> dict[int, object]:
        """Registers `handle` for every stop signal and returns the handlers it replaced, for
        `restore`. Only the main thread may install signal handlers."""
        previous: dict[int, object] = {}
        for signum in stop_signals():
            previous[signum] = signal.signal(signum, self.handle)
        return previous

    @staticmethod
    def restore(previous: dict[int, object]) -> None:
        for signum, handler in previous.items():
            # `signal.signal` returns None for a handler installed from C code, which it will
            # not accept back; the default disposition is the honest restoration of that.
            signal.signal(signum, signal.SIG_DFL if handler is None else handler)


# The worker's one controller. `worker.main` installs it; `run_one_build`, `run_one_deployment`
# and the poll sleep enter `interruptible()` on it; tests reset it.
SHUTDOWN = ShutdownController()
