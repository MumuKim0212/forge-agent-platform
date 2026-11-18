"""ADR 0028 §Decision layer 3: refuses an unsafe cross-site or same-site
write from a browser — the gap CORS itself leaves open. CORS blocks a
JSON-bodied write (it needs a preflight CORS refuses), but never blocks a
body-less "simple" request from reaching the app at all; that residual
path (`…/deployments/rollback`, `…/deployments/stop`, `/auth/logout`, and
in `local` mode the signup `…/reopen`/`…/deactivate` actions) is what this
layer closes, without a platform-origin setting and without touching any
client call.

Implemented as a pure ASGI middleware — it only ever inspects the request
method and two headers (`Sec-Fetch-Site`, `Origin`), never the body, so
there is nothing to buffer and no reason to touch
`starlette.middleware.base.BaseHTTPMiddleware`."""

from __future__ import annotations

from collections.abc import Sequence

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.services.preview_shape import matches_preview_shape

# Only these four ever carry a meaningful side effect on this platform
# (the GET-side-effect inventory is the full list of
# what safe methods are allowed to do instead) — GET/HEAD/OPTIONS are never
# checked. OPTIONS in particular must still reach `CORSMiddleware` for its
# own preflight handling; see this module's `FetchMetadataMiddleware`
# docstring for how the two middlewares are ordered to guarantee that.
CHECKED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# `Sec-Fetch-Site` values this layer refuses outright unless the caller's
# `Origin` is on the operator's own trust list. `same-origin`, `none`, and
# an ABSENT header (non-browser clients: tests, Traefik's server-to-server
# forward-auth call, curl) all pass through unchecked — Fetch Metadata
# never classifies any of those as unsafe.
UNSAFE_FETCH_SITES = frozenset({"cross-site", "same-site"})

# ADR 0028 §Decision layer 3: neither ever exempts a request, however the
# operator's own CORS_ORIGINS is configured. `null` is exactly what a
# layer-1-sandboxed document's own requests carry (self.origin ===
# "null", measured in ADR 0028 §Measured); `*` would switch this layer off
# entirely if treated as a wildcard the way CORSMiddleware's own
# `allow_origins=["*"]` is.
NEVER_EXEMPT_ORIGINS = frozenset({"null", "*"})

REFUSAL_DETAIL = "cross-site or same-site write refused"

# the ONE value that is exempt on the preview shape and nowhere else.
SANDBOXED_ORIGIN = "null"


def is_exempt_origin(origin: str | None, allowed_origins: Sequence[str]) -> bool:
    """True only when `origin` is an EXACT member of `allowed_origins` —
    the operator's own `CORS_ORIGINS` list (`main.create_app` passes the
    identical parsed list `CORSMiddleware` itself received, so the two
    cannot drift by reading it twice). The frontend's `VITE_API_BASE_URL`
    cross-origin shape is exactly what this exemption exists for (ADR 0028
    §Decision layer 3, "The CORS_ORIGINS exemption")."""
    if origin is None or origin in NEVER_EXEMPT_ORIGINS:
        return False
    return origin in allowed_origins


def should_refuse(
    method: str,
    sec_fetch_site: str | None,
    origin: str | None,
    allowed_origins: Sequence[str],
    *,
    on_preview_shape: bool = False,
) -> bool:
    """The one rule ADR 0028 §Decision layer 3 states, as a pure function
    so `tests/test_fetch_metadata.py` can pin the full method x site x
    origin matrix without spinning up an ASGI app for every row.

    `on_preview_shape` is true only when the caller has shown the request is on the
    anchored preview shape (`services/preview_shape.py`). Then, and only then, an `Origin` of exactly
    `null` is exempt -- a sandboxed page's own request. This removes the Fetch Metadata refusal and
    nothing else: the preview route still demands its token. `is_exempt_origin` and
    `NEVER_EXEMPT_ORIGINS` are untouched and still refuse `null` for every other request."""
    if method not in CHECKED_METHODS:
        return False
    if sec_fetch_site not in UNSAFE_FETCH_SITES:
        return False
    if on_preview_shape and origin == SANDBOXED_ORIGIN:
        return False
    return not is_exempt_origin(origin, allowed_origins)


class FetchMetadataMiddleware:
    """ASGI middleware, ADR 0028 layer 3.

    **Ordering (see `main.create_app`):** registered BEFORE
    `CORSMiddleware` — Starlette's `add_middleware` inserts each new
    middleware at the FRONT of the stack, so whichever call happens LAST
    ends up OUTERMOST (wraps everything registered before it, and runs
    first on the way in). Registering this class first and `CORSMiddleware`
    second therefore puts CORS outside this class, with one exception: the
    `SandboxedPreviewCorsMiddleware`, registered last (third), wraps
    both, so for an `Origin: null` request on the preview shape it answers
    first. CORS is outermost only for every other origin: such a request
    reaches `CORSMiddleware` before this class, so a genuine preflight `OPTIONS`
    (one carrying `Access-Control-Request-Method`) is answered by
    `CORSMiddleware` directly and never reaches this class's `__call__` at
    all — a stronger guarantee than this class merely choosing not to
    check `OPTIONS` itself (decision 3: "OPTIONS must still reach
    CORSMiddleware for preflights"). For every other request, CORS just
    forwards to this class (it only ever short-circuits a real preflight),
    which then applies `should_refuse` before the request reaches routing.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_origins: Sequence[str],
        exempt_sandboxed_preview: bool = False,
    ) -> None:
        self._app = app
        # `main.create_app` turns this on only where the preview route is mounted
        # (Traefik off). Off by default, so a middleware built without it behaves exactly as before.
        self.exempt_sandboxed_preview = exempt_sandboxed_preview
        # Copied once at construction, from the SAME list `main.create_app`
        # hands to `CORSMiddleware` — never re-read from `Settings` per
        # request, so the two cannot observe different values mid-request.
        self.allowed_origins: list[str] = list(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        method = scope["method"].upper()
        sec_fetch_site = headers.get("sec-fetch-site")
        origin = headers.get("origin")

        on_preview_shape = (
            self.exempt_sandboxed_preview
            and origin == SANDBOXED_ORIGIN
            and matches_preview_shape(scope["path"], scope.get("raw_path"))
        )

        if should_refuse(
            method, sec_fetch_site, origin, self.allowed_origins, on_preview_shape=on_preview_shape
        ):
            response = JSONResponse({"detail": REFUSAL_DETAIL}, status_code=403)
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)
