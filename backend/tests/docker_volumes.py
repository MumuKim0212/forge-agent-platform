"""shared helpers for tests that prove a container's anonymous
volumes are gone after its removal (`remove(v=True)`)."""

from __future__ import annotations

import re

# Docker names an anonymous volume with 64 hex characters (a generated id); a
# named volume (e.g. `forge-service-postgres-data`) carries a human-chosen name.
_ANONYMOUS_NAME = re.compile(r"^[0-9a-f]{64}$")


def anonymous_volume_names(container) -> list[str]:
    """Names of the anonymous volumes mounted on `container`, read from its
    inspect data. Call `container.reload()` first if the container was just
    started."""
    mounts = container.attrs.get("Mounts") or []
    return [
        m["Name"]
        for m in mounts
        if m.get("Type") == "volume" and _ANONYMOUS_NAME.match(m.get("Name") or "")
    ]


def volume_exists(docker_client, name: str) -> bool:
    from docker.errors import NotFound

    try:
        docker_client.volumes.get(name)
        return True
    except NotFound:
        return False
