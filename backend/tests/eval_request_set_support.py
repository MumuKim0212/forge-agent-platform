"""A frozen three-case request set for the tests that exercise the eval RUNNER and the eval API.

Those tests count cases, name them, and feed a stub probe a body that satisfies "every check of the
set". They are about run lifecycle and budget arithmetic, not about what the shipped set contains,
so they read this copy of set v1 instead of `backend/evals/request_set.json` (adding a case to
the shipped set, as every later version will, would otherwise rewrite a dozen unrelated tests).
What the shipped file contains is pinned in one place, `tests/test_eval_request_set.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import eval_request_set

# The body a stub probe returns so that every check of this set passes.
EVERYTHING = b"Forge Eval Static Page / Stored items: 0 / forge-eval-note-7431"

V1_REQUEST_SET = {
    "version": 1,
    "cases": [
        {
            "id": "static-page",
            "request": 'Change the home page. The route GET / must return an HTML page whose body contains "Forge Eval Static Page".',
            "checks": [
                {"method": "GET", "path": "/", "expect_status": 200, "expect_substring": "Forge Eval Static Page"}
            ],
        },
        {
            "id": "database-page",
            "request": 'Change the home page to show "Stored items: <n>" from the items table.',
            "checks": [{"method": "GET", "path": "/", "expect_status": 200, "expect_substring": "Stored items: "}],
        },
        {
            "id": "write-then-read-form",
            "request": "Add POST /notes and GET /notes over the items table.",
            "checks": [
                {
                    "method": "POST",
                    "path": "/notes?text=forge-eval-note-7431",
                    "expect_status": 200,
                    "expect_substring": "forge-eval-note-7431",
                },
                {"method": "GET", "path": "/notes", "expect_status": 200, "expect_substring": "forge-eval-note-7431"},
            ],
        },
    ],
}


def pin_v1_request_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the loader's default path at a file holding `V1_REQUEST_SET`."""
    target = tmp_path / "request_set_v1.json"
    target.write_text(json.dumps(V1_REQUEST_SET), encoding="utf-8")
    monkeypatch.setattr(eval_request_set, "REQUEST_SET_PATH", target)
