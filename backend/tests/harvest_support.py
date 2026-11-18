"""Test support for the executor's harvest hand-off (ADR 0032).

The executor publishes the adapter's `output_dir`, so a fake adapter that reports
success must hand it a directory, as the real one does. `with_output` gives it one
without each test file repeating the plumbing. The executor owns and removes the
directory it is handed (that is what `test_sandbox_build_executor_harvest.py`
proves), so a test using this leaves nothing behind.
"""

from __future__ import annotations

import dataclasses
import os
import tempfile
from collections.abc import Mapping

from app.services.agent_adapter import AgentAdapterResult

DEFAULT_FILES: Mapping[str, bytes] = {"app/app.py": b"print('agent output')\n"}


def make_output_dir(files: Mapping[str, bytes] | None = None) -> str:
    """A fresh harvested-style directory holding `files` (relative path -> bytes)."""
    root = tempfile.mkdtemp(prefix="forge-harvest-test-")
    for rel, data in (DEFAULT_FILES if files is None else files).items():
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    return root


def with_output(result: AgentAdapterResult, files: Mapping[str, bytes] | None = None) -> AgentAdapterResult:
    """`result` with an `output_dir` if it is a success that has none (a failure
    needs no output to be published, and one that already has it keeps it)."""
    if not result.ok or result.output_dir is not None:
        return result
    return dataclasses.replace(result, output_dir=make_output_dir(files))
