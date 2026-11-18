"""the API process serves the built SPA on its own origin.

Shape: a 404 handler, not a catch-all route. A route that matched `/{path:path}` would out-rank
nothing but would still change what a wrong-method call on a real `/api` route answers (a full
match on the catch-all beats the partial match that yields 405). Handling only what routing could
not match keeps every `/api/*` route, `/openapi.json`, `/docs` and the JSON 404 of an unknown `/api`
path exactly as they were. Registered only when `FRONTEND_DIST_DIR` is set; unset, nothing here runs.

What is served is decided by one rule: a regular file whose RESOLVED path is inside the resolved
dist directory, else `index.html`. Resolving first is what makes `..`, an absolute path and a symlink
pointing out of the directory all fall to `index.html` rather than to the outside file."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, Response

logger = logging.getLogger(__name__)

INDEX_NAME = "index.html"
ASSETS_PREFIX = "assets/"
NO_CACHE = "no-cache"
# Vite content-hashes everything under assets/, so a changed file is a new URL.
LONG_CACHE = "public, max-age=31536000, immutable"
SPA_METHODS = ("GET", "HEAD")


class FrontendDistError(RuntimeError):
    """Raised at API startup when FORGE_ENV=prod and FRONTEND_DIST_DIR is set but names a missing
    directory or one with no index.html. Dev only logs a warning. The worker and Alembic never
    build the app, so they do not need the directory."""


def _is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/") or path == "/openapi.json"


def resolve_spa_file(dist_root: Path, url_path: str) -> Path:
    """The file to serve for `url_path`: the real file when it is a regular file inside
    `dist_root`, otherwise `index.html`. `dist_root` must already be resolved."""
    relative = url_path.lstrip("/")
    if relative:
        try:
            # `dist_root / "/etc/passwd"` is `/etc/passwd` (an absolute right-hand side replaces
            # the left); that is fine only because the containment check below rejects it.
            candidate = (dist_root / relative).resolve()
            if candidate.is_file() and candidate.is_relative_to(dist_root):
                return candidate
        except (OSError, ValueError):
            pass  # a NUL byte or an unreadable name: treat as "not a file here"
    return dist_root / INDEX_NAME


def _cache_control(dist_root: Path, served: Path) -> str:
    rel = served.relative_to(dist_root).as_posix()
    return LONG_CACHE if rel.startswith(ASSETS_PREFIX) else NO_CACHE


def check_dist_dir(dist_dir: str, *, is_prod: bool) -> Path | None:
    """Validate the directory. Returns the resolved path when usable, else None (dev) or raises
    `FrontendDistError` (prod)."""
    root = Path(dist_dir).resolve()
    problem = None
    if not root.is_dir():
        problem = f"FRONTEND_DIST_DIR={dist_dir!r} is not an existing directory ({root})"
    elif not (root / INDEX_NAME).is_file():
        problem = f"FRONTEND_DIST_DIR={dist_dir!r} has no {INDEX_NAME} ({root}); build the SPA first"
    if problem is None:
        return root
    if is_prod:
        raise FrontendDistError(f"FORGE_ENV=prod refuses to start: {problem}.")
    logger.warning("%s. The SPA will not be served.", problem)
    return None


def install_spa(application: FastAPI, dist_dir: str | None, *, is_prod: bool) -> None:
    if dist_dir is None or not dist_dir.strip():
        return
    root = check_dist_dir(dist_dir.strip(), is_prod=is_prod)
    if root is None:
        return

    async def not_found(request: Request, exc: StarletteHTTPException) -> Response:
        path = request.scope["path"]
        if _is_api_path(path):
            return await http_exception_handler(request, exc)
        if request.method not in SPA_METHODS:
            return Response(
                status_code=405, headers={"Allow": ", ".join(SPA_METHODS)}, content="Method Not Allowed"
            )
        served = resolve_spa_file(root, path)
        if not served.is_file():  # index.html removed after startup
            return await http_exception_handler(request, exc)
        return FileResponse(
            served,
            headers={
                "Cache-Control": _cache_control(root, served),
                # The platform's ordinary header for its own pages. Deliberately NOT the sandbox CSP
                # of `proxy_headers.py` (ADR 0028 layer 1): that policy is for proxied generated
                # content and would break the SPA.
                "X-Content-Type-Options": "nosniff",
            },
        )

    application.add_exception_handler(404, not_found)
