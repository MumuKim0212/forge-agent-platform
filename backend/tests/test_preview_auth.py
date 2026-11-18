"""tests for the preview forward-auth verification endpoint
(`routers/preview_auth.py`) and the Traefik preview label generation — no
Docker, no Traefik. The endpoint is what Traefik's ForwardAuth middleware
calls in prod mode; the signed token travels in the `X-Forwarded-Uri`
header (the original request's path+query), never as the endpoint's own
query string. Enforces exactly the existing signed-token rules
(ADR 0016 posture unchanged):
expired / tampered / wrong-build / superseded / missing all rejected.

The real-Traefik end-to-end accept/reject paths are covered by
tests/test_deploy_traefik_integration.py.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.models.build import Build
from app.models.project import Project
from app.services.preview_manager import (
    hash_token,
    issue_preview_token,
    traefik_preview_host,
    traefik_preview_labels,
    traefik_preview_url,
)
from app.services.status import BuildStatus, ProjectStatus


@pytest.fixture()
def project(db_session, seeded_ids):
    project = Project(
        name="preview-auth-project",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.commit()
    return project


@pytest.fixture()
def build_with_preview(db_session, project):
    """A build with a live preview: token issued, only its hash stored
    (never the raw token) — same shape sandbox_build_executor persists."""
    token, expires_at = issue_preview_token("bld-pa-1")
    build = Build(
        id="bld-pa-1",
        project_id=project.id,
        type="create",
        status=BuildStatus.SUCCEEDED,
        preview_url=f"/api/projects/{project.id}/preview/bld-pa-1?token={token}",
        preview_expires_at=expires_at,
        preview_token_hash=hash_token(token),
    )
    db_session.add(build)
    db_session.commit()
    return build, token


def _auth(client, build_id: str, forwarded_uri: str | None):
    headers = {}
    if forwarded_uri is not None:
        headers["X-Forwarded-Uri"] = forwarded_uri
    return client.get(f"/api/internal/preview-auth/{build_id}", headers=headers)


def test_valid_token_in_forwarded_uri_accepted(client, build_with_preview):
    build, token = build_with_preview
    resp = _auth(client, build.id, f"/some/path?token={token}")
    assert resp.status_code == 200


def test_valid_token_with_other_query_params_accepted(client, build_with_preview):
    build, token = build_with_preview
    resp = _auth(client, build.id, f"/?page=2&token={token}&x=y")
    assert resp.status_code == 200


def test_missing_token_rejected(client, build_with_preview):
    build, _ = build_with_preview
    assert _auth(client, build.id, "/some/path").status_code == 403
    assert _auth(client, build.id, None).status_code == 403


def test_tampered_token_rejected(client, build_with_preview):
    build, token = build_with_preview
    nonce, expires_ts, signature = token.split(".")
    tampered = f"{nonce}.{expires_ts}.{'0' * len(signature)}"
    resp = _auth(client, build.id, f"/?token={tampered}")
    assert resp.status_code == 403


def test_non_ascii_signature_rejected_as_403_not_a_500(client, build_with_preview):
    # `compare_digest` on a non-ASCII str raised TypeError, a 500 on the forward-auth call.
    build, token = build_with_preview
    nonce, expires_ts, _signature = token.split(".")
    resp = _auth(client, build.id, f"/?token={nonce}.{expires_ts}.%C3%A9%E2%82%AC")
    assert resp.status_code == 403


def test_malformed_token_rejected(client, build_with_preview):
    build, _ = build_with_preview
    assert _auth(client, build.id, "/?token=not-a-token").status_code == 403


def test_expired_token_rejected(client, db_session, project):
    token, expires_at = issue_preview_token("bld-pa-exp", ttl_minutes=-1)
    build = Build(
        id="bld-pa-exp",
        project_id=project.id,
        type="create",
        status=BuildStatus.SUCCEEDED,
        preview_expires_at=expires_at,
        preview_token_hash=hash_token(token),
    )
    db_session.add(build)
    db_session.commit()

    resp = _auth(client, build.id, f"/?token={token}")
    assert resp.status_code == 403


def test_wrong_build_token_rejected(client, db_session, build_with_preview, project):
    """A token signed for another build never verifies against this one."""
    build, _ = build_with_preview
    other_token, _ = issue_preview_token("some-other-build")
    resp = _auth(client, build.id, f"/?token={other_token}")
    assert resp.status_code == 403


def test_superseded_token_rejected(client, db_session, build_with_preview):
    """After a new preview replaces the old one (new token hash stored),
    the old — still validly signed, unexpired — token is rejected."""
    build, old_token = build_with_preview
    new_token, _ = issue_preview_token(build.id)
    build.preview_token_hash = hash_token(new_token)
    db_session.commit()

    assert _auth(client, build.id, f"/?token={old_token}").status_code == 403
    assert _auth(client, build.id, f"/?token={new_token}").status_code == 200


def test_unknown_build_rejected_with_no_detail_leak(client):
    resp = _auth(client, "no-such-build", "/?token=whatever")
    assert resp.status_code == 403
    # Rejection leaks nothing beyond a generic reason — never a hash/token.
    assert "hash" not in resp.text.lower()


def test_no_live_preview_rejected(client, db_session, project):
    """A build without a live preview (no stored hash) rejects any token."""
    build = Build(
        id="bld-pa-none", project_id=project.id, type="create", status=BuildStatus.SUCCEEDED
    )
    db_session.add(build)
    db_session.commit()
    token, _ = issue_preview_token("bld-pa-none")
    assert _auth(client, build.id, f"/?token={token}").status_code == 403


def test_endpoint_requires_no_identity_header(client, build_with_preview):
    """Identity-free like the dev-mode preview route: the signed token is
    the credential (ADR 0016), and Traefik's forward-auth call carries no
    platform identity."""
    build, token = build_with_preview
    # `client` sends no X-Forge-User-Id unless told to — this asserts the
    # endpoint doesn't 401 without one.
    resp = _auth(client, build.id, f"/?token={token}")
    assert resp.status_code == 200


# -- Traefik preview label generation (exercised without Docker; the rule:
# "Traefik label generation must still be exercised
# by tests") ---------------------------------------------------------------


def test_traefik_preview_labels_shape():
    labels = traefik_preview_labels(
        project_id="proj-x", build_id="bld-y", network_name="forge-preview-net-abc"
    )
    host = traefik_preview_host("proj-x")
    assert host == f"preview--proj-x.{settings.apps_base_domain}"
    assert labels["traefik.enable"] == "true"
    assert labels["traefik.http.routers.forge-preview-bld-y.rule"] == f"Host(`{host}`)"
    # Routed to the preview's OWN network (per-service networks).
    assert labels["traefik.docker.network"] == "forge-preview-net-abc"
    assert (
        labels["traefik.http.services.forge-preview-bld-y.loadbalancer.server.port"]
        == str(settings.preview_container_port)
    )
    # Forward-auth middleware guards the router, pointed at the platform
    # verification endpoint for exactly this build.
    assert labels["traefik.http.routers.forge-preview-bld-y.middlewares"] == (
        "forge-preview-bld-y-fwdauth"
    )
    auth_address = labels[
        "traefik.http.middlewares.forge-preview-bld-y-fwdauth.forwardauth.address"
    ]
    assert auth_address.endswith("/api/internal/preview-auth/bld-y")
    assert settings.traefik_forward_auth_host in auth_address


# -- entrypoint, TLS, and URL scheme are operator config -


def test_traefik_preview_labels_entrypoint_is_operator_configured(monkeypatch):
    """No literal "web" may remain in the builder — it must read
    settings.traefik_entrypoint."""
    monkeypatch.setattr(settings, "traefik_entrypoint", "websecure")
    labels = traefik_preview_labels(project_id="proj-x", build_id="bld-y", network_name="net")
    assert labels["traefik.http.routers.forge-preview-bld-y.entrypoints"] == "websecure"


def test_traefik_preview_labels_default_entrypoint_is_web():
    assert settings.traefik_entrypoint == "web"
    labels = traefik_preview_labels(project_id="proj-x", build_id="bld-y", network_name="net")
    assert labels["traefik.http.routers.forge-preview-bld-y.entrypoints"] == "web"


def test_traefik_preview_labels_omit_tls_keys_entirely_when_resolver_unset():
    """Criterion: with TRAEFIK_CERT_RESOLVER unset, the preview router's
    label dict must carry NO `tls` key and NO `certresolver` key at all —
    not a falsy `tls=false`. Anchored against vacuity by also asserting a
    label the builder DOES emit (`traefik.docker.network`)."""
    assert settings.traefik_cert_resolver is None
    labels = traefik_preview_labels(
        project_id="proj-x", build_id="bld-y", network_name="net-abc"
    )
    assert labels["traefik.docker.network"] == "net-abc"  # vacuity anchor
    assert "traefik.http.routers.forge-preview-bld-y.tls" not in labels
    assert "traefik.http.routers.forge-preview-bld-y.tls.certresolver" not in labels
    assert not any(key.endswith(".tls") or key.endswith(".certresolver") for key in labels)


def test_traefik_preview_labels_carry_tls_when_resolver_set(monkeypatch):
    monkeypatch.setattr(settings, "traefik_cert_resolver", "myresolver")
    labels = traefik_preview_labels(project_id="proj-x", build_id="bld-y", network_name="net")
    assert labels["traefik.http.routers.forge-preview-bld-y.tls"] == "true"
    assert labels["traefik.http.routers.forge-preview-bld-y.tls.certresolver"] == "myresolver"


def test_traefik_preview_forward_auth_address_defaults_to_http():
    """Byte-for-byte with the earlier behaviour."""
    assert settings.service_url_scheme == "http"
    labels = traefik_preview_labels(project_id="proj-x", build_id="bld-y", network_name="net")
    auth_address = labels[
        "traefik.http.middlewares.forge-preview-bld-y-fwdauth.forwardauth.address"
    ]
    assert auth_address.startswith(f"http://{settings.traefik_forward_auth_host}")


def test_service_url_scheme_https_alone_leaves_the_forward_auth_address_http(monkeypatch):
    """the forward-auth hop is Traefik -> host API on the
    Docker bridge, where the API serves plain http. Turning on TLS for the services
    (SERVICE_URL_SCHEME=https) must not turn this hop into https."""
    monkeypatch.setattr(settings, "service_url_scheme", "https")
    monkeypatch.setattr(settings, "traefik_forward_auth_host", "host.docker.internal:8000")
    labels = traefik_preview_labels(project_id="proj-x", build_id="bld-y", network_name="net")
    auth_address = labels[
        "traefik.http.middlewares.forge-preview-bld-y-fwdauth.forwardauth.address"
    ]
    assert auth_address == "http://host.docker.internal:8000/api/internal/preview-auth/bld-y"


def test_traefik_preview_forward_auth_address_follows_its_own_scheme(monkeypatch):
    monkeypatch.setattr(settings, "traefik_forward_auth_scheme", "https")
    monkeypatch.setattr(settings, "service_url_scheme", "http")
    monkeypatch.setattr(settings, "traefik_forward_auth_host", "host.docker.internal:8000")
    labels = traefik_preview_labels(project_id="proj-x", build_id="bld-y", network_name="net")
    auth_address = labels[
        "traefik.http.middlewares.forge-preview-bld-y-fwdauth.forwardauth.address"
    ]
    assert auth_address == "https://host.docker.internal:8000/api/internal/preview-auth/bld-y"


def test_traefik_preview_url_defaults_to_http():
    assert settings.service_url_scheme == "http"
    url = traefik_preview_url("proj-x", "tok-123")
    assert url == f"http://preview--proj-x.{settings.apps_base_domain}/?token=tok-123"


def test_traefik_preview_url_follows_configured_scheme(monkeypatch):
    monkeypatch.setattr(settings, "service_url_scheme", "https")
    url = traefik_preview_url("proj-x", "tok-123")
    assert url == f"https://preview--proj-x.{settings.apps_base_domain}/?token=tok-123"
