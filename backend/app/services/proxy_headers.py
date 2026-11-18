"""ADR 0028 §Decision layer 1: the isolation header set every
platform-origin proxy response carries.

Both `routers/preview_proxy.py::proxy_preview` and
`routers/service_proxy.py::proxy_service` re-publish unvetted, LLM-generated
output on the platform's own origin. Every response either route returns —
the successfully proxied body AND their own 403/404/502 errors — attaches
this exact header set, built in this one place so no call site can drift
from another (decision 1: "One rule is simpler to pin than two").

Nothing outside this module decides what the headers are; a caller only
supplies the upstream `Content-Type`, if it has one."""

from __future__ import annotations

# ADR 0028 §Decision 1: the INITIAL grant, and the token list that must
# NEVER appear here — allow-same-origin, allow-top-navigation (plus its two
# variants) and allow-popups-to-escape-sandbox — because any one of them
# hands the sandboxed document (or a window it opens) some slice of the
# platform's own origin authority, which is exactly what this ADR withholds.
_SANDBOX_TOKENS = "allow-scripts allow-forms allow-popups allow-modals allow-downloads"

CONTENT_SECURITY_POLICY = f"sandbox {_SANDBOX_TOKENS}"
X_CONTENT_TYPE_OPTIONS = "nosniff"
# defence in depth for a credential that now also travels in a path
# (`.../preview/{build}/~{token}/`). Measured: a sandboxed document sends no Referer at all,
# so this covers browsers not measured; it is not what keeps the token in.
REFERRER_POLICY = "no-referrer"

# What a browser gets when upstream sent no Content-Type, or its value is
# empty/unparseable (decision 1). Never executable.
DEFAULT_CONTENT_TYPE = "text/plain; charset=utf-8"


def build_isolation_headers(content_type: str | None) -> dict[str, str]:
    """The headers ADR 0028 layer 1 requires (CSP, nosniff, Content-Type) plus
    `Referrer-Policy`, and nothing else — no
    other upstream header (`Set-Cookie` included) is ever carried through
    this function; it never reads a request/response object itself, only
    the one value a caller already extracted.

    `content_type` is the upstream `Content-Type` when there is one. `None`,
    blank, or a value that fails to decode cleanly (a caller's problem, not
    this function's — pass `None` in that case) all resolve to
    `DEFAULT_CONTENT_TYPE`, so a browser never treats an unlabelled body as
    HTML."""
    resolved = content_type.strip() if content_type else ""
    return {
        "Content-Security-Policy": CONTENT_SECURITY_POLICY,
        "X-Content-Type-Options": X_CONTENT_TYPE_OPTIONS,
        "Referrer-Policy": REFERRER_POLICY,
        "Content-Type": resolved or DEFAULT_CONTENT_TYPE,
    }
