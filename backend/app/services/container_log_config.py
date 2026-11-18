"""The capped log config of every deploy and preview container.

Without one, the daemon's default `json-file` driver keeps a container's stdout and stderr
without a bound unless the operator configured `daemon.json`, and a chatty generated service
fills the host disk. Both create sites (`deploy_manager`, `preview_manager`) take their
`log_config` from here, so the cap is one definition read from settings at create time.
"""

from __future__ import annotations

from docker.types import LogConfig

from app.config import settings


def capped_log_config() -> LogConfig:
    """`json-file` with `max-size` and `max-file` from `CONTAINER_LOG_MAX_SIZE` /
    `CONTAINER_LOG_MAX_FILES` (Docker takes both option values as strings)."""
    return LogConfig(
        type=LogConfig.types.JSON,
        config={
            "max-size": settings.container_log_max_size,
            "max-file": str(settings.container_log_max_files),
        },
    )
