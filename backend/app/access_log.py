"""the access log never carries a query-string VALUE.

uvicorn's access log writes the request target as the client sent it, query string included. Two
credentials travel there by design: the preview bearer `token` (`routers/preview_proxy.py`, ADR 0028
chose the query parameter) and the OIDC callback's `code` and `state` (`routers/auth.py`). So every
preview view and every login left a usable credential in whatever file the access log goes to
(`dev.ps1` redirects it to `backend/uvicorn.log`). The earlier "no platform log line can include one"
covered request bodies only.

The rule is held **in the app**, as a `logging.Filter` on the `uvicorn.access` logger that
`create_app` installs, so it holds however uvicorn is started: `dev.ps1`, a production unit file that
does not exist yet, `uvicorn.run(...)` in a test. A flag on `dev.ps1`'s command line would protect one
launcher and none of the others. The filter rewrites the record's own arguments, so every handler
downstream (stdout, a file, a collector) sees the redacted line.

the preview token also travels as a path segment (`.../preview/{build}/~{token}/`),
and uvicorn logs the decoded path, so a path with no query would carry it. A path segment that starts
with `~` is therefore redacted to `~[redacted]` exactly as a query value is -- in the middle of a path,
at its end, with or without a query.

What is kept: the path and the parameter NAMES (`?token=[redacted]&page=[redacted]`), which is what
makes the line useful for debugging. What is dropped: every value, not only the ones for names known
to be secret -- a list of secret names is a list somebody forgets to extend. A bare `?SOMETHING` with
no `=` is itself a value, so it is dropped too.
"""

from __future__ import annotations

import logging

ACCESS_LOGGER_NAME = "uvicorn.access"
REDACTED = "[redacted]"


def redact_path_tokens(path: str) -> str:
    """`path` with every segment that starts with `~` (after a `/`) replaced by `~[redacted]`."""
    segments = path.split("/")
    return "/".join(
        f"~{REDACTED}" if index > 0 and segment.startswith("~") else segment
        for index, segment in enumerate(segments)
    )


def redact_query_values(target: str) -> str:
    """`target` with every query-string value replaced and every `~` path segment redacted; a string
    with neither comes back unchanged.

    The fragment is dropped with the rest (a client does not send one, but if it did it would be the
    same kind of value)."""
    path, separator, rest = target.partition("?")
    path = redact_path_tokens(path)
    if not separator:
        return path
    query = rest.partition("#")[0]
    if not query:
        return path + separator
    pairs = []
    for pair in query.split("&"):
        name, equals, _value = pair.partition("=")
        pairs.append(f"{name}={REDACTED}" if equals else REDACTED)
    return path + separator + "&".join(pairs)


def _redact(value):
    if isinstance(value, str) and ("?" in value or "/~" in value):
        return redact_query_values(value)
    return value


class QueryValueRedactingFilter(logging.Filter):
    """Rewrites any `?query` in a record's arguments (uvicorn's access record is
    `(client_addr, method, full_path, http_version, status_code)`) and, for a record with no
    arguments, in the message itself. Never drops a record: it only edits it."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(_redact(a) for a in args)
        elif isinstance(args, dict):
            record.args = {key: _redact(a) for key, a in args.items()}
        elif not args and isinstance(record.msg, str):
            # A pre-formatted message: redact each whitespace-separated word that carries a query.
            record.msg = " ".join(_redact(word) for word in record.msg.split(" "))
        return True


def install_access_log_filter() -> None:
    """Idempotent: `create_app` is called more than once per process (tests build several apps)."""
    access_logger = logging.getLogger(ACCESS_LOGGER_NAME)
    if not any(isinstance(f, QueryValueRedactingFilter) for f in access_logger.filters):
        access_logger.addFilter(QueryValueRedactingFilter())
