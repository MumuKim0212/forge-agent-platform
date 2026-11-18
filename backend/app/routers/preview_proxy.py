"""ADR 0028 layer 2: the platform-origin preview proxy, on its own router.

Moved verbatim out of `routers/projects.py`, unchanged in routing,
methods, and HTTP behaviour beyond ADR 0028 layer 1's three isolation
headers (`services/proxy_headers.py`). `main.create_app` mounts this
router only when `settings.traefik_enabled` is false (dev, or prod with
Traefik off) — in Traefik mode `preview_manager.start_preview` hands out a
direct Traefik-host URL instead (`traefik_preview_url`), and nothing in
`app/` or the frontend references this path
(ADR 0028 §Decision layer 2).

Forward-auth (`routers/preview_auth.py`) is a separate router and is
unaffected by this move or by the conditional mounting below.

`proxy_preview` is a plain `def`, so FastAPI runs it in the threadpool. Everything it does
blocks -- sync SQLAlchemy, a `docker exec` of up to 5 seconds, a `time.sleep(0.3)` retry loop --
and as an `async def` it ran all of that ON the event loop, so one slow upstream stalled every
request the worker was serving. Nothing in it awaits anything, so nothing needed to stay async.

the route now forwards a bounded query, a bounded body and one allowlisted
`Content-Type` (`services/proxy_request.py`), and reads at most 2 MiB back. The body stream is async;
the route stays a plain `def` and reads it through `anyio.from_thread.run` (see that module).

the token may also arrive as a first path segment `~{token}`, so that the
relative URLs of a page served under `.../preview/{build}/~{token}/` resolve under a path that carries
the credential. A `GET` of the entry URL `.../preview/{build}?token=T` answers `307` to that path form
(after the token verified). The route reads no cookie and forwards none."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.db import get_db, release_connection
from app.models.build import Build
from app.services.preview_manager import (
    PreviewAccessError,
    PreviewManager,
    PreviewStartError,
    verify_preview_token,
)
from app.services.proxy_headers import build_isolation_headers
from app.services.proxy_request import (
    MAX_RESPONSE_BYTES,
    ProxyRequestError,
    collect_forwarded_request,
    quote_path,
)
from app.services.shared_docker_client import shared_docker_client

router = APIRouter(prefix="/projects/{project_id}", tags=["preview-proxy"])

# The first path segment of a sub-request carries the token as `~{token}`. A token
# is `nonce.expires.signature` and contains two dots: nothing here looks at dots.
TOKEN_SEGMENT_PREFIX = "~"


def _preview_manager() -> PreviewManager:
    """The manager this route forwards through: built per request, because a manager holds
    no per-request state, but on the process-wide Docker client instead of one `docker.from_env()`
    -- a new connection pool and a round trip to the daemon -- per request. The seam tests replace."""
    return PreviewManager(client=shared_docker_client())


@router.api_route(
    "/preview/{build_id}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
@router.api_route(
    "/preview/{build_id}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
def proxy_preview(
    project_id: str,
    build_id: str,
    request: Request,
    path: str = "",
    token: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """signed, expiring
    access to a build's live preview. No `X-Forge-User-Id` identity is
    required — the signed `token` query param is the access control here
    (ADR 0016: pre-SSO, intranet-holder-of-the-link posture, accepted
    residual risk). Forwards the request into the preview container via
    `PreviewManager.proxy` (docker exec — the container has no published
    host port at all, see preview_manager.py docstring).

    both decorators carry `include_in_schema=False` — a catch-all
    proxy's "operations" are whatever the generated service behind it does,
    so its OpenAPI entry documented nothing real and its two stacked routes
    (path with and without the trailing `{path:path}`) were the app's only
    source of duplicate operation ids. Route registration, methods, and
    HTTP behaviour are otherwise byte-identical.

    ADR 0028 layer 1: every response this handler returns —
    including its own 403/404 — carries the same three isolation headers
    (`build_isolation_headers`), so a browser never runs proxied content
    with the platform's own origin authority. The successful path forwards
    the upstream `Content-Type`; the error paths pass `None`, which
    resolves to a non-executable default.

    ADR 0028 layer 2: this whole route exists only where Traefik does not
    route — see `main.create_app`'s conditional registration and this
    module's own docstring.

    the database connection is given back BEFORE the upstream call. The session this
    handler holds has begun a transaction by its first read, which keeps one pooled connection
    checked out until the request ends -- and the upstream call can take seconds. A burst of slow
    previews would otherwise hold the pool (5 + 10 on Postgres) against every other request of the
    API. Nothing below reads the database again."""
    build = db.get(Build, build_id)
    if build is None or build.project_id != project_id:
        raise HTTPException(
            status_code=404, detail="Build not found", headers=build_isolation_headers(None)
        )

    # the `~{token}` segment, when present, IS the credential and wins over `?token=`
    # (which is still removed from what the container is sent). It is verified like any token, and a
    # `~` segment that does not verify is a 403 that never reaches the container.
    first_segment, _slash, remainder = path.partition("/")
    token_in_path = first_segment.startswith(TOKEN_SEGMENT_PREFIX)
    presented_token = first_segment[len(TOKEN_SEGMENT_PREFIX) :] if token_in_path else (token or "")
    container_path = remainder if token_in_path else path

    try:
        verify_preview_token(build_id, presented_token, expected_hash=build.preview_token_hash)
    except PreviewAccessError as exc:
        raise HTTPException(
            status_code=403, detail=str(exc), headers=build_isolation_headers(None)
        ) from exc

    release_connection(db)  # a read-only transaction: nothing to keep, the connection goes back to the pool

    # after the connection is given back, because reading the body waits on
    # the client. The `token` parameter is removed from the query here, so the credential never
    # reaches generated code.
    try:
        forwarded = collect_forwarded_request(request, strip_token=True)
    except ProxyRequestError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail, headers=build_isolation_headers(None)
        ) from exc

    # the entry URL (`GET`, no sub-path, token in the query) moves to the path form.
    # Only after the token verified, so an unverified value never lands in a header; built from the
    # route's own ids and the server's `root_path`, never from `request.url` or the `Host` header.
    if request.method == "GET" and not path and not token_in_path:
        root_path = request.scope.get("root_path", "").strip("/")
        location = (
            (f"/{quote(root_path, safe='/')}" if root_path else "")
            + f"/api/projects/{quote(project_id, safe='')}/preview/{quote(build_id, safe='')}"
            + f"/{TOKEN_SEGMENT_PREFIX}{quote(presented_token, safe='')}/"
            + (f"?{forwarded.query}" if forwarded.query else "")
        )
        return Response(
            status_code=307,
            headers={
                **build_isolation_headers(None),
                "Location": location,
                "Cache-Control": "no-store",
            },
        )

    manager = _preview_manager()
    try:
        status_code, body, content_type = manager.proxy(
            build_id,
            method=request.method,
            path=quote_path(f"/{container_path}") if container_path else "/",
            query=forwarded.query,
            body=forwarded.body,
            content_type=forwarded.content_type,
            max_body_bytes=MAX_RESPONSE_BYTES + 1,
        )
    except PreviewStartError as exc:
        raise HTTPException(
            status_code=502, detail=str(exc), headers=build_isolation_headers(None)
        ) from exc

    if len(body) > MAX_RESPONSE_BYTES:
        raise HTTPException(
            status_code=502,
            detail="preview response too large",
            headers=build_isolation_headers(None),
        )

    return Response(
        content=body,
        status_code=status_code,
        headers=build_isolation_headers(content_type),
    )
