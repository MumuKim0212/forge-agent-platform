"""the one anchored URL shape of the platform-origin preview route.

Two layers act on this shape and on nothing else: layer 3 (`services/fetch_metadata.py`) lets a
sandboxed page's unsafe request with `Origin: null` through to the route, and the preview CORS layer
(`services/preview_sandbox_cors.py`) answers a preflight and adds `Access-Control-Allow-Origin: null`.
Both ask the same pure question, so it lives here, once, and a test table pins every row of it.

The shape is `/api/projects/{id}/preview/{id}` followed by nothing or by `/` and any sub-path, with
`{id}` made of `[A-Za-z0-9-]` only. It is matched against the path the server decoded AND against the
raw path the client sent (ASGI `raw_path`, bytes): a decoded path hides `%2f` / `%2e` / `%5c`, and a
dot segment is only dangerous once something downstream normalises it. A request is NEVER on the shape
when either path has a `.` or `..` segment, an empty segment (`//`), or an encoded slash, dot or
backslash. A neighbouring family (`/preview-x/`, `/previews/`, `/preview` with no build, the service
route, any other `/api` route) is not on the shape: the pattern is anchored at both ends.

A preview token is `nonce.expires.signature` and contains two dots, so no rule here may be "a path
containing a dot": only a WHOLE segment equal to `.` or `..` is refused.
"""

from __future__ import annotations

import re

# Anchored with `fullmatch` and DOTALL (a decoded newline in a sub-path must not slip past a `$`).
_PREVIEW_SHAPE = re.compile(r"/api/projects/[A-Za-z0-9-]+/preview/[A-Za-z0-9-]+(?:/.*)?", re.DOTALL)

# Encoded slash, dot, backslash in the RAW path, whatever the case of the hex digits.
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|2e|5c)", re.IGNORECASE)


def _has_ambiguous_segment(path: str) -> bool:
    """A `.` or `..` segment, or an empty one. One trailing slash is a normal URL
    (`.../{build}/`), so a single empty final segment is allowed; `//` anywhere else is not."""
    segments = path.split("/")[1:]
    if segments and segments[-1] == "":
        segments = segments[:-1]
    return any(segment in ("", ".", "..") for segment in segments)


def matches_preview_shape(path: str, raw_path: bytes | None) -> bool:
    """True when a request for `path` (ASGI `scope["path"]`) sent as `raw_path` (ASGI
    `scope["raw_path"]`) is on the preview route's shape and carries no ambiguity.

    Fails closed: with no `raw_path` the request cannot be shown clean, so it is not on the shape."""
    if raw_path is None:
        return False
    raw = raw_path.decode("latin-1").partition("?")[0]
    if _ENCODED_SEPARATOR.search(raw):
        return False
    if _has_ambiguous_segment(path) or _has_ambiguous_segment(raw):
        return False
    return _PREVIEW_SHAPE.fullmatch(path) is not None and _PREVIEW_SHAPE.fullmatch(raw) is not None
