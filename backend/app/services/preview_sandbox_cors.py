"""CORS for the preview route's shape, for a sandboxed page only.

A document served under the CSP `sandbox` (ADR 0028 layer 1) has an opaque origin: its requests carry
`Origin: null`. Without `Access-Control-Allow-Origin: null` on the answer the page cannot read it, and a
JSON write needs a preflight that must carry the same header (measured). This layer gives exactly
that, for a request that is on the anchored preview shape (`services/preview_shape.py`) AND whose
`Origin` is exactly `null`. Any other origin, or none, gets nothing from here and the request flows as
before.

**It is the OUTERMOST layer** (`main.create_app` adds it after `CORSMiddleware`): the global CORS
middleware answers a preflight from an origin that is not on `CORS_ORIGINS` with a 400 itself, so it must
never see this one. Being outermost also makes this layer see the FINAL headers of every answer, which
matters for the one thing it removes: the global middleware adds `Access-Control-Allow-Credentials: true`
to every answer for a request that carries an `Origin` (it is configured with credentials for the SPA's
listed origin), and a `null`-origin answer must never carry it. The page uses default credentials; the
platform never grants more.

Never `Access-Control-Allow-Credentials`, never `*`, never an echo of another origin.

Pure ASGI, like `FetchMetadataMiddleware`: it reads two headers and the path, never the body."""

from __future__ import annotations

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.services.preview_shape import matches_preview_shape

SANDBOXED_ORIGIN = "null"

# Literals on purpose (contract strings); tests pin each with its own literal.
PREFLIGHT_HEADERS = {
    "Access-Control-Allow-Origin": "null",
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "600",
    "Vary": "Origin",
}


def _merge_vary_origin(headers: MutableHeaders) -> None:
    """One `Vary` header that names `Origin` exactly once, whatever an inner layer already put there
    (Starlette's CORS layer appends `Origin` for a request it does not allow)."""
    values = [part.strip() for line in headers.getlist("Vary") for part in line.split(",")]
    values = [value for value in values if value]
    if not any(value.lower() == "origin" for value in values):
        values.append("Origin")
    headers["Vary"] = ", ".join(values)


class SandboxedPreviewCorsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if headers.get("origin") != SANDBOXED_ORIGIN or not matches_preview_shape(
            scope["path"], scope.get("raw_path")
        ):
            await self._app(scope, receive, send)
            return

        if scope["method"].upper() == "OPTIONS" and "access-control-request-method" in headers:
            # A preflight: answered here, before routing and before the database.
            await Response(status_code=204, headers=PREFLIGHT_HEADERS)(scope, receive, send)
            return

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                response_headers = MutableHeaders(scope=message)
                response_headers["Access-Control-Allow-Origin"] = SANDBOXED_ORIGIN
                if "access-control-allow-credentials" in response_headers:
                    del response_headers["Access-Control-Allow-Credentials"]
                _merge_vary_origin(response_headers)
            await send(message)

        await self._app(scope, receive, send_with_cors)
