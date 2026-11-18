"""Test support for the build's entrypoint check.

`SandboxBuildExecutor._publish` fails a build whose workspace lacks the template's service
entrypoint (`MISSING_ENTRYPOINT`). A test that drives the real `_publish` with an
otherwise-empty workspace, to exercise something after that check (the policy gate, the
requester resolution), must hand it a workspace that HOLDS the entrypoint. The path is
derived from the template through `service_entrypoint`, the same function the check uses,
so renaming the skeleton's module moves this and the check together and no test spells
`app/app.py`.
"""

from __future__ import annotations

import os

from app.config import settings
from app.services.service_entrypoint import service_entrypoint_paths


def place_entrypoint(workspace_dir) -> None:
    """Write the template's entrypoint file into `workspace_dir` (a path or `Path`)."""
    relative = service_entrypoint_paths(settings.template_skeleton_dir)[0]
    target = os.path.join(str(workspace_dir), *relative.split("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as f:
        f.write(b"app = 1\n")
