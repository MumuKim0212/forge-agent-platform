# Not protected -- see db.py's header for the same note.
#
# Redis client for the generated service (ADR 0023 §Data services).
#
# Reads `REDIS_URL` from the environment by that exact name -- issued
# together with `DATABASE_URL` in the platform's service env, so a
# missing value here means this service was never
# issued data services at all (expected for a preview
# when no admin DSN is configured, never expected for a real deploy --
# promotion is refused first in that case). Fails at import time; no
# fallback.
#
# Named `cache.py`, not `redis.py`: this directory is on `sys.path` ahead
# of site-packages when the app runs (uvicorn's cwd is this directory), so
# a module literally named `redis.py` here would shadow the installed
# `redis` package for every other module in this template that needs it.

from __future__ import annotations

import os

import redis as _redis


class RedisConfigurationError(RuntimeError):
    """Raised when REDIS_URL is missing or blank. No silent fallback
    exists for this condition -- see module docstring."""


def resolve_redis_url() -> str:
    raw = os.environ.get("REDIS_URL")
    if raw is None or not raw.strip():
        raise RedisConfigurationError(
            "REDIS_URL is not set. The platform issues this value "
            "alongside DATABASE_URL at first deploy."
        )
    return raw


REDIS_URL = resolve_redis_url()
redis_client = _redis.Redis.from_url(REDIS_URL, decode_responses=True)
