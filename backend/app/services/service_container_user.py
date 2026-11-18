"""The one non-root user every generated-service container runs as.

A preview and the deployment it previews must behave alike (ADR 0016): an app that writes under
the root-owned `/app` must fail the same way in both, and the approver judges the preview. So
`preview_manager` (L3) and `deploy_manager` (L4) both take `user=` from here. nobody:nogroup, with
no `USER` line needed in the template image (ADR 0046 §Decision 5).
"""

from __future__ import annotations

SERVICE_CONTAINER_USER = "65534:65534"
