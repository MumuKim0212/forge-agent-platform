"""what the platform-origin proxies take from a request and hand to the
generated container -- and, just as much, what they refuse to.

`routers/preview_proxy.py` and `routers/service_proxy.py` forward a bounded query string, a bounded
body and one allowlisted header (`Content-Type`). Everything else about the request (`Cookie`,
`Authorization`, `Host`, `Origin`, `Referer`, `X-Forwarded-*`, any custom header) is never read here,
so there is no code path that could forward it: the managers receive these four values and nothing
else. The numbers are decisions of the ADR, kept as module constants rather than `Settings` fields.

Request data reaches the exec script only through `repr` / base64 (`preview_manager` and
`deploy_manager` build the script); the path and the query are percent-encoded here so that a decoded
`?` or `#` inside a path segment cannot turn into a query or a fragment once they are joined."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import quote, quote_from_bytes, unquote_to_bytes

import anyio.from_thread
from starlette.requests import ClientDisconnect, Request

# Literal numbers on purpose: the tests pin each one with its own literal.
MAX_QUERY_BYTES = 8 * 1024
MAX_BODY_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# Methods whose body is forwarded. GET never is (the routes do not mount HEAD at all, and answer
# OPTIONS with 405, but the rule is stated here in full so the helper is correct on its own).
BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# The one request header that is forwarded, and only for these media types (parameters ignored).
FORWARDED_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/x-www-form-urlencoded",
        "text/plain",
        "multipart/form-data",
    }
)

# Not an ADR number: a guard on the exec script's size. A value this long is not a media type with a
# boundary (a multipart boundary is at most 70 characters), and the whole script has to stay under
# the 128 KiB single-argument limit with a 64 KiB body (about 88 KiB encoded) and an 8 KiB query.
MAX_CONTENT_TYPE_CHARS = 1024

# Characters a query string may carry through unchanged. Everything else (a space, `#`, a control
# character, any byte over 0x7f) is percent-encoded, so the query cannot end the URL early.
_QUERY_SAFE = "%&=+;:@/?!$'()*,~[]"


class ProxyRequestError(Exception):
    """The request is refused before it reaches the container. The route turns it into an
    `HTTPException` carrying the isolation headers."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ForwardedRequest:
    """Everything the proxy hands the container besides the method and the path."""

    query: str
    body: bytes | None
    content_type: str | None


def quote_path(path: str) -> str:
    """The route's `path` arrives percent-decoded; re-encode it before it joins the URL. Only the
    unreserved characters and `/` stay as they are, so a decoded `?`, `#`, `%`, quote, newline or
    NUL is encoded and cannot become a query, a fragment or a second request line."""
    return quote(path, safe="/")


def _is_token_pair(pair: bytes) -> bool:
    name = pair.split(b"=", 1)[0]
    return unquote_to_bytes(name.replace(b"+", b" ")) == b"token"


def prepare_query(raw_query: bytes, *, strip_token: bool) -> str:
    """The query string as it goes to the container: verbatim, bar the percent-encoding of anything
    that could not sit in a URL, and (preview route) without any `token` parameter. A parameter is
    `token` when its percent-decoded name is `token`, wherever it stands and however often it is
    repeated, so the credential cannot ride along under an encoded spelling of its name.

    Over `MAX_QUERY_BYTES` raises `ProxyRequestError(414)` -- measured on the raw string, before the
    stripping, so the bound does not depend on what the stripping removes."""
    if len(raw_query) > MAX_QUERY_BYTES:
        raise ProxyRequestError(414, "query string too long")
    if strip_token:
        raw_query = b"&".join(pair for pair in raw_query.split(b"&") if not _is_token_pair(pair))
    return quote_from_bytes(raw_query, safe=_QUERY_SAFE)


def forwardable_content_type(value: str | None) -> str | None:
    """The `Content-Type` value to forward, or `None` to drop the header. The media type (parameters
    ignored, case-insensitive) must be on the allowlist; the full original value is then forwarded,
    `boundary` and `charset` included. A value with a control character (it could not be a header
    line) or an unreasonable length is dropped."""
    if value is None or len(value) > MAX_CONTENT_TYPE_CHARS:
        return None
    if any((ord(ch) < 0x20 and ch != "\t") or ord(ch) == 0x7F for ch in value):
        return None
    media_type = value.split(";", 1)[0].strip().lower()
    return value if media_type in FORWARDED_MEDIA_TYPES else None


async def read_bounded_body(chunks: AsyncIterator[bytes], *, declared_length: int | None) -> bytes:
    """Read a request body of at most `MAX_BODY_BYTES`, never holding more than that.

    Refuses (`ProxyRequestError(413)`) when the declared length is over the cap -- without reading a
    byte -- and when the running count would pass it, which is the only guard for a chunked or
    otherwise undeclared stream (and for a declared length that lies). The chunk that would pass the
    cap is not kept, so the route holds at most the cap, and has pulled at most one chunk beyond it
    from the stream when it stops."""
    if declared_length is not None and declared_length > MAX_BODY_BYTES:
        raise ProxyRequestError(413, "request body too large")
    received = bytearray()
    try:
        async for chunk in chunks:
            if len(received) + len(chunk) > MAX_BODY_BYTES:
                raise ProxyRequestError(413, "request body too large")
            received += chunk
    except ClientDisconnect as exc:
        raise ProxyRequestError(400, "client disconnected while sending the body") from exc
    return bytes(received)


def _declared_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None  # not a length: the running count is the guard


def collect_forwarded_request(request: Request, *, strip_token: bool) -> ForwardedRequest:
    """Called from the routes, which are plain `def` (they run in the threadpool): the body
    stream is async, so it is read through `anyio.from_thread.run` on the event loop. Raises
    `ProxyRequestError` (414 / 413 / 400); a `GET` is never read at all, so a body sent with one is
    simply not looked at."""
    query = prepare_query(request.scope.get("query_string", b""), strip_token=strip_token)
    method = request.method.upper()
    if method not in BODY_METHODS:
        return ForwardedRequest(query=query, body=None, content_type=None)

    declared = _declared_length(request)

    async def _read() -> bytes:
        return await read_bounded_body(request.stream(), declared_length=declared)

    body = anyio.from_thread.run(_read)
    return ForwardedRequest(
        query=query,
        body=body or None,
        content_type=forwardable_content_type(request.headers.get("content-type")),
    )
