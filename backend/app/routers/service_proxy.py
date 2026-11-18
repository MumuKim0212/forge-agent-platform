"""ADR 0028 layer 2: the platform-origin deployed-service proxy, on its own
router.

Moved verbatim out of `routers/deployments.py`, unchanged in
routing, methods, and HTTP behaviour beyond ADR 0028 layer 1's three
isolation headers (`services/proxy_headers.py`). `main.create_app`
mounts this router only when `settings.traefik_enabled` is false (dev, or
prod with Traefik off) — in Traefik mode a deployed service is reachable at
its own stable hostname instead, and nothing in `app/` or the frontend
references this path
(ADR 0028 §Decision layer 2).

`proxy_service` is a plain `def`, so FastAPI runs it in the threadpool. Everything it does
blocks -- sync SQLAlchemy, a `docker exec` of up to 5 seconds -- and as an `async def` it ran all
of that ON the event loop, so one slow upstream stalled every request the worker was serving.
Nothing in it awaits anything, so nothing needed to stay async.

the route now forwards a bounded query, a bounded body and one allowlisted
`Content-Type` (`services/proxy_request.py`), and reads at most 2 MiB back. The body stream is async;
the route stays a plain `def` and reads it through `anyio.from_thread.run` (see that module).

The manager comes from this module's own seam, `_proxy_manager()`, built on the process-wide
Docker client (`services/shared_docker_client.py`). It is NOT `routers/deployments.py`'s
`_deploy_manager()` any more: that one builds a fresh `docker.from_env()` for each call and stays
that way for the confirm/rollback/stop actions (`tests/test_deploy_controls.py` pins it), which is
the very cost this route stopped paying per request."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.db import get_db, release_connection
from app.models.project import Project
from app.services.deploy import get_active_deployment
from app.services.deploy_manager import DeployManager, DeployProxyError
from app.services.project_activity import record_traffic_activity
from app.services.proxy_headers import build_isolation_headers
from app.services.proxy_request import (
    MAX_RESPONSE_BYTES,
    ProxyRequestError,
    collect_forwarded_request,
    quote_path,
)
from app.services.shared_docker_client import shared_docker_client

router = APIRouter(prefix="/projects/{project_id}", tags=["service-proxy"])


def _proxy_manager() -> DeployManager:
    """The manager this route forwards through: per request, because a manager holds no
    per-request state, but on the process-wide Docker client rather than a `docker.from_env()` per
    request. The seam tests replace. Raises whatever `docker.from_env()` raises when the daemon is
    not reachable; the handler answers that with a 502."""
    return DeployManager(client=shared_docker_client())


@router.api_route(
    "/service", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False
)
@router.api_route(
    "/service/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
def proxy_service(
    project_id: str,
    request: Request,
    path: str = "",
    db: Session = Depends(get_db),
) -> Response:
    """Dev-mode routing: the
    deployed service is reachable through this platform-proxy route — the
    same mechanism previews use; no host ports are
    published. The route always resolves the project's ACTIVE deployment at
    request time, so the health-gated switchover's DB flip IS the route
    flip. No identity header: a deployed service is an intranet-facing app
    (prod shape is an open Traefik hostname until SSO forward-auth lands).

    a request that actually reaches the container is the
    strongest evidence the service is in use, so a successful proxy call
    marks the project active — coarsened (`touch_last_active_for_traffic`)
    so a busy service pays at most one write per
    `project_activity.TRAFFIC_TOUCH_COARSEN_INTERVAL`, not one per request.
    Nothing is written on a failed/unreachable proxy attempt: no traffic
    actually reached the service. Unlike the build/deploy write sites, this
    is the one call that does not ride an existing transaction, so the
    commit itself is issued by `record_traffic_activity`, not here directly
    (review round 2, task (b)/(b2)): an ordinary commit failure must not
    turn an already-successfully-proxied response into a 500.

    both decorators carry `include_in_schema=False`, same reasoning
    as `routers/preview_proxy.py::proxy_preview` — this route's OpenAPI
    entry documented nothing real, and the two stacked routes here were the
    other half of the app's only duplicate-operation-id source. Route
    registration, methods, and HTTP behaviour are otherwise unchanged.

    ADR 0028 layer 1: every response this handler returns —
    including its own 404/502 — carries the same three isolation headers
    (`build_isolation_headers`). This route does NOT reuse
    `deployments._get_project` — that helper is shared with
    confirm/rollback/stop/list, which must NOT gain these headers, so a
    project lookup local to this handler keeps the header attachment
    scoped to exactly this proxy.

    ADR 0028 layer 2: this whole route exists only where Traefik does not
    route — see `main.create_app`'s conditional registration and this
    module's own docstring.

    the database connection is given back BEFORE the upstream call (see
    `routers/preview_proxy.py::proxy_preview`): the transaction the first read began would keep a
    pooled connection checked out for the seconds the upstream takes. The one write this handler
    makes, the traffic touch, comes after it and opens its own transaction."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=404, detail="Project not found", headers=build_isolation_headers(None)
        )

    active = get_active_deployment(db, project_id)
    if active is None:
        raise HTTPException(
            status_code=404,
            detail="No active deployment for this project",
            headers=build_isolation_headers(None),
        )

    active_id = active.id  # read before the transaction ends: the row is not touched again
    release_connection(db)  # a read-only transaction: nothing to keep, the connection goes back to the pool

    # after the connection is given back, because reading the body waits on
    # the client.
    try:
        forwarded = collect_forwarded_request(request, strip_token=False)
    except ProxyRequestError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail, headers=build_isolation_headers(None)
        ) from exc

    try:
        manager = _proxy_manager()
        status_code, body, content_type = manager.proxy(
            active_id,
            method=request.method,
            path=quote_path(f"/{path}") if path else "/",
            query=forwarded.query,
            body=forwarded.body,
            content_type=forwarded.content_type,
            max_body_bytes=MAX_RESPONSE_BYTES + 1,
        )
    except DeployProxyError as exc:
        raise HTTPException(
            status_code=502, detail=str(exc), headers=build_isolation_headers(None)
        ) from exc
    except Exception as exc:  # noqa: BLE001 - Docker unreachable
        raise HTTPException(
            status_code=502,
            detail=f"service unreachable: {exc}",
            headers=build_isolation_headers(None),
        ) from exc

    if len(body) > MAX_RESPONSE_BYTES:
        raise HTTPException(
            status_code=502,
            detail="service response too large",
            headers=build_isolation_headers(None),
        )

    record_traffic_activity(db, project)

    return Response(
        content=body,
        status_code=status_code,
        headers=build_isolation_headers(content_type),
    )
