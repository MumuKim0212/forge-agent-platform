"""Routing (prod):
the platform verification endpoint Traefik's ForwardAuth
middleware calls before letting a request reach a preview container
directly (prod shape only; dev mode's docker-exec proxy route in
`routers/projects.py` is untouched and does not use this endpoint).

The signed preview token remains the sole credential (ADR 0016 posture
unchanged — only the routing shape changes). Traefik's ForwardAuth does not
forward the original request's query string to its own request against the
configured `address`; it instead sets the original path+query on the
`X-Forwarded-Uri` request header, so the token is read from there rather
than from this endpoint's own query string.

Identity-free, like the existing preview/service proxy routes — a signed
token is not tied to a platform user account (pre-SSO posture).
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.build import Build
from app.services.preview_manager import PreviewAccessError, verify_preview_token

router = APIRouter(prefix="/internal", tags=["preview-auth"])


def _token_from_forwarded_uri(request: Request) -> str:
    """Traefik's ForwardAuth sets `X-Forwarded-Uri` to the original
    request's path + query string (e.g.
    `/api/projects/<id>/preview/<build_id>?token=...`) — that's where the
    signed token actually travels, never as this endpoint's own query
    param."""
    forwarded_uri = request.headers.get("x-forwarded-uri", "")
    query = urlsplit(forwarded_uri).query
    token_values = parse_qs(query).get("token")
    return token_values[0] if token_values else ""


@router.get("/preview-auth/{build_id}")
def verify_preview_forward_auth(
    build_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Returns 200 (any body Traefik discards) if the request Traefik is
    forwarding carries a valid, unexpired, matching-build signed preview
    token; 403 otherwise. Leaks nothing on rejection — same generic 403 +
    the existing `PreviewAccessError` message shapes used by the dev-mode
    preview route, never the token or hash values themselves)."""
    build = db.get(Build, build_id)
    if build is None:
        raise HTTPException(status_code=403, detail="preview not found")

    token = _token_from_forwarded_uri(request)
    try:
        verify_preview_token(build_id, token, expected_hash=build.preview_token_hash)
    except PreviewAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return {"status": "ok"}
