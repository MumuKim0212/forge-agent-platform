"""Generic OIDC protocol layer (ADR 0018 §1-2).

Everything IdP-specific is *configuration*: endpoints are read from
`{oidc_issuer}/.well-known/openid-configuration` and signing keys from the
`jwks_uri` that document advertises. There is no vendor branch and no
hard-coded endpoint anywhere in this module — Google Workspace, Entra ID,
Okta and Keycloak all work by setting three env vars.

Signature verification is delegated to PyJWT (+ `cryptography`), never
hand-rolled. The token header's `alg` is deliberately NOT trusted: the
algorithm passed to `jwt.decode` is the one the JWKS key itself declares (or
that its key type implies), which is what makes algorithm-confusion attacks
(`alg: none`, RS256 key replayed as an HS256 secret) inexpressible here.

This module holds no DB or session state: it turns a browser redirect into
verified claims, and the tokens it exchanges are discarded immediately
afterwards (no access/refresh token is
stored).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
from dataclasses import dataclass

import httpx
import jwt
from jwt import PyJWK, PyJWKSet

from app.config import settings

logger = logging.getLogger("app.oidc")

DISCOVERY_PATH = "/.well-known/openid-configuration"

# Asymmetric signature algorithms only. An IdP JWKS should never contain a
# symmetric key, and accepting one would reintroduce the very confusion the
# key-declared-algorithm rule above exists to prevent.
ALLOWED_ID_TOKEN_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}
)

_cache_lock = threading.Lock()
_discovery_document: dict | None = None
_jwks: PyJWKSet | None = None


class OidcError(RuntimeError):
    """Any failure in discovery, code exchange, or ID-token validation.

    Messages are safe to log: callers construct them from failure *reasons*
    only. A raw code, token, or client secret is never interpolated into one
    (log the reason without echoing the token)."""


@dataclass(frozen=True)
class AuthorizationRequest:
    """What login-start needs: where to send the browser, plus the two values
    the callback must be able to check (carried in a signed cookie, not a
    server-side store)."""

    authorization_url: str
    state: str
    code_verifier: str


def reset_caches() -> None:
    """Drop the cached discovery document and JWKS. Only for tests — the
    process-lifetime cache is the documented production behaviour."""
    global _discovery_document, _jwks
    with _cache_lock:
        _discovery_document = None
        _jwks = None


def _http_client() -> httpx.Client:
    """The module's one HTTP seam (same shape as `routers/deployments.py`'s
    `_deploy_manager()`): monkeypatched in tests to serve a stub IdP."""
    return httpx.Client(timeout=10.0)


def _get_json(url: str, *, client: httpx.Client | None = None) -> dict:
    owned = client is None
    client = client or _http_client()
    try:
        response = client.get(url)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcError(f"could not fetch {url}: {type(exc).__name__}") from exc
    finally:
        if owned:
            client.close()


def _issuer() -> str:
    if not settings.oidc_issuer:
        # Unreachable in a correctly configured process: Settings refuses to
        # construct with AUTH_MODE=oidc and no issuer (OidcConfigMissingError).
        raise OidcError("OIDC_ISSUER is not configured")
    return settings.oidc_issuer.rstrip("/")


def discovery_document(*, client: httpx.Client | None = None) -> dict:
    """The IdP's OIDC discovery document, fetched once and cached for the
    process lifetime."""
    global _discovery_document
    if _discovery_document is not None:
        return _discovery_document

    document = _get_json(_issuer() + DISCOVERY_PATH, client=client)
    advertised = str(document.get("issuer", "")).rstrip("/")
    if advertised != _issuer():
        raise OidcError(
            f"discovery issuer mismatch: document says {advertised!r}, "
            f"configured issuer is {_issuer()!r}"
        )
    for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not document.get(required):
            raise OidcError(f"discovery document has no {required}")

    with _cache_lock:
        _discovery_document = document
    return document


def _jwk_for_kid(kid: str | None, *, client: httpx.Client | None = None) -> PyJWK:
    """Resolve a signing key from the cached JWKS, refetching once if the
    token's `kid` is unknown — IdPs rotate keys, and a process-lifetime JWKS
    cache would otherwise reject every login until a restart."""
    global _jwks

    for attempt in (0, 1):
        if _jwks is None or attempt == 1:
            keys = _get_json(discovery_document(client=client)["jwks_uri"], client=client)
            try:
                fetched = PyJWKSet.from_dict(keys)
            except jwt.PyJWKSetError as exc:
                raise OidcError(f"unusable JWKS: {exc}") from exc
            with _cache_lock:
                _jwks = fetched

        candidates = [k for k in _jwks.keys if kid is None or k.key_id == kid]
        if candidates:
            return candidates[0]

    raise OidcError(f"no signing key in the IdP's JWKS matches kid={kid!r}")


def build_authorization_request(redirect_uri: str) -> AuthorizationRequest:
    """Authorization-code + PKCE start. `state` and
    the PKCE `code_verifier` are returned to the caller, which carries them
    through the redirect in a short-lived signed cookie."""
    document = discovery_document()
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    query = httpx.QueryParams(
        {
            "response_type": "code",
            "client_id": settings.oidc_client_id or "",
            "redirect_uri": redirect_uri,
            "scope": " ".join(settings.oidc_scope_list),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    separator = "&" if "?" in document["authorization_endpoint"] else "?"
    return AuthorizationRequest(
        authorization_url=f"{document['authorization_endpoint']}{separator}{query}",
        state=state,
        code_verifier=code_verifier,
    )


def exchange_code_for_id_token(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client: httpx.Client | None = None,
) -> str:
    """Server-side code exchange (ADR 0018 §2: the backend holds the client
    secret; the SPA never sees a token). Returns the raw ID token only — the
    access/refresh tokens in the same response are deliberately dropped
    unread."""
    document = discovery_document(client=client)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        "client_id": settings.oidc_client_id or "",
    }
    # OIDC's default token-endpoint auth method is client_secret_basic; an
    # IdP that only advertises client_secret_post gets the secret in the body
    # instead. Never both.
    methods = document.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    auth: tuple[str, str] | None = None
    if "client_secret_basic" in methods:
        auth = (settings.oidc_client_id or "", settings.oidc_client_secret or "")
    else:
        data["client_secret"] = settings.oidc_client_secret or ""

    owned = client is None
    client = client or _http_client()
    try:
        response = client.post(document["token_endpoint"], data=data, auth=auth)
    except httpx.HTTPError as exc:
        raise OidcError(f"token endpoint unreachable: {type(exc).__name__}") from exc
    finally:
        if owned:
            client.close()

    if response.status_code >= 400:
        # The IdP's own error *code* is useful and safe; its body could echo
        # the submitted code, so only the documented error field is read.
        try:
            reason = response.json().get("error", "unspecified")
        except ValueError:
            reason = "unspecified"
        raise OidcError(f"token exchange rejected (HTTP {response.status_code}, {reason})")

    try:
        id_token = response.json().get("id_token")
    except ValueError as exc:
        raise OidcError("token endpoint returned a non-JSON body") from exc
    if not id_token:
        raise OidcError("token endpoint response carried no id_token")
    return id_token


def validate_id_token(id_token: str, *, client: httpx.Client | None = None) -> dict:
    """Verify signature (against the IdP's JWKS), `iss`, `aud` and expiry,
    and return the claims.

    Raises `OidcError` on any failure. The token is never included in the
    error message or logged — only the reason is."""
    document = discovery_document(client=client)
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise OidcError(f"malformed id_token: {type(exc).__name__}") from exc

    key = _jwk_for_kid(header.get("kid"), client=client)
    algorithm = key.algorithm_name
    if algorithm not in ALLOWED_ID_TOKEN_ALGORITHMS:
        raise OidcError(f"id_token signing algorithm {algorithm!r} is not accepted")

    try:
        claims = jwt.decode(
            id_token,
            key=key.key,
            # The key's own algorithm, not the token header's: an attacker
            # cannot downgrade or swap it.
            algorithms=[algorithm],
            audience=settings.oidc_client_id,
            issuer=document["issuer"],
            options={"require": ["iss", "sub", "aud", "exp", "iat"]},
        )
    except jwt.PyJWTError as exc:
        raise OidcError(f"id_token rejected: {type(exc).__name__}") from exc

    if not claims.get("sub"):
        raise OidcError("id_token has no sub claim")
    return claims
