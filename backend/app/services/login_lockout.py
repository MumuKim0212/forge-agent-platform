"""Per-account login attempt limiting for `local` mode (ADR 0022
§10).

`oidc` mode never had a password to guess, so nothing on this platform
rate-limits anything today -- this is the first mechanism with a guessable
secret. Counted **per account** on **consecutive** failures; a success resets
the counter.

**Keyed by the account's id, not by the email the caller typed**. The
email is attacker-chosen, so a map keyed by it grew by one entry for every
distinct address anyone tried, existing account or not. An account id can only
come from a row that exists, so an unknown email has no way to create an entry:
the bound is "one entry per account row", by construction rather than by a
caller remembering to skip the call. The spec says the same thing ("only
existing accounts are counted") and the response to a missing account is
unchanged -- it gets the same generic message, and `routers/auth_local.py` pays
the same hash cost for it.

**Counters of existing accounts are not given a separate expiry**.
A lock already expires on its own and drops its entry when next
looked up. A partial counter (fewer failures than the threshold) stays until a
success or a restart, but the map cannot exceed the number of account rows, and
each row is a heavier thing for a signup flood to create than the entry is. A
failure *window* would also change what "consecutive" means in the spec; if one
is ever wanted it is a change inside `record_failure` and nowhere else.

In-process, thread-safe, **not persisted** -- a restart clears every
counter. Acceptable per spec: "the lock is a timed counter guarding against
sustained guessing... not an account status", so losing it across a
restart is a availability/security tradeoff, not a correctness one, and
there is no new column for it (`app/models/user.py` gains only
`password_hash` / `department` / `status`). Same in-process-registry shape
as `services/llm_proxy.TokenRegistry`.

A locked account's check (`is_locked`) is deliberately made **before** any
password verification in the caller (`routers/auth_local.py`), so a locked
account's response never depends on whether the supplied password happens to be
correct -- indistinguishable from a wrong-password response by construction, not
by coincidence.
"""

from __future__ import annotations

import threading
import time

from app.config import settings

_lock = threading.Lock()
# account id -> {"failures": int, "locked_until": float | None}
_counters: dict[str, dict[str, float | int | None]] = {}


def is_locked(account_id: str, *, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    with _lock:
        counter = _counters.get(account_id)
        if not counter or counter.get("locked_until") is None:
            return False
        if now >= counter["locked_until"]:
            # The lock expires on its own, with no admin action, and is not
            # an account status -- just drop it.
            del _counters[account_id]
            return False
        return True


def record_failure(account_id: str, *, now: float | None = None) -> None:
    """Count one failure against an account that exists. `account_id` is a
    `User.id` -- never an email, so there is nothing to call this with for an
    address that has no account."""
    now = now if now is not None else time.time()
    with _lock:
        counter = _counters.setdefault(account_id, {"failures": 0, "locked_until": None})
        counter["failures"] = int(counter["failures"]) + 1
        if counter["failures"] >= settings.local_login_max_attempts:
            counter["locked_until"] = now + settings.local_login_lockout_minutes * 60


def record_success(account_id: str) -> None:
    with _lock:
        _counters.pop(account_id, None)


def tracked_count() -> int:
    """How many accounts currently hold a counter. Read-only; exists so a test
    can assert the map's size without reaching into module state."""
    with _lock:
        return len(_counters)


def reset_all() -> None:
    """Test-only: the module-level counters otherwise persist across tests
    in the same pytest process. Not called from any request path."""
    with _lock:
        _counters.clear()
