"""the operator's pointer to the worker log is cut from what a requester reads, and the
cut is a tail match on two phrases (`requesterBuildDetail` in `frontend/src/components/buildFailure.ts`).

Every place the backend writes such a pointer into a build's failure detail must use one of those
two tails; a new wording ("check the worker log", "see the log") would reach a requester's screen
unless the frontend learns it first. A comment or docstring that merely mentions the worker's log
is not a stored string and is not read here.
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
WRITERS = [APP / "worker.py", APP / "services" / "sandbox_build_executor.py"]
FRONTEND_MODULE = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "buildFailure.ts"
)

# The two tails the frontend strips. `worker log\b` leaves "worker logs" (a docstring verb) alone.
ALLOWED_TAILS = ('see the worker log"', 'its output is in the worker log"')
MENTION = re.compile(r"worker log\b", re.IGNORECASE)


def _string_literal_lines() -> list[tuple[str, int, str]]:
    found = []
    for path in WRITERS:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or not MENTION.search(stripped):
                continue
            if '"' not in stripped:
                continue  # a docstring line, not a literal
            found.append((path.name, number, stripped))
    return found


def test_the_scan_finds_the_known_writers() -> None:
    # A scan that found nothing would make the shape test below vacuous.
    assert len(_string_literal_lines()) >= 10


def test_every_stored_worker_log_pointer_ends_in_a_tail_the_frontend_strips() -> None:
    bad = []
    for name, number, line in _string_literal_lines():
        body = re.sub(r"[,)\s]+$", "", line)
        if not any(body.endswith(tail) for tail in ALLOWED_TAILS):
            bad.append(f"{name}:{number}: {line}")

    assert bad == []


def test_the_frontend_module_names_both_tails() -> None:
    source = FRONTEND_MODULE.read_text(encoding="utf-8")

    for tail in ALLOWED_TAILS:
        assert tail.rstrip('"') in source
