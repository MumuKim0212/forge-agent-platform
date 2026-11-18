"""the API process serves the built SPA on one origin.

Traversal cases go through a raw ASGI call, not `TestClient`: httpx normalises `/../` out of a URL
before it is sent, so a TestClient "traversal" would test the client, not the server. Each case
plants a secret file OUTSIDE the dist directory and asserts its text never appears -- the
distinguishing observable is `index.html`'s marker versus the secret's."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.spa_static import FrontendDistError, install_spa

INDEX_MARK = "<!--INDEX-MARKER-->"
SECRET = "TOP-SECRET-OUTSIDE-DIST"


@pytest.fixture()
def tree(tmp_path):
    """tmp/secret.txt (outside) and tmp/dist/{index.html, assets/app-abc123.js, favicon.svg}."""
    (tmp_path / "secret.txt").write_text(SECRET)
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(f"<html>{INDEX_MARK}</html>")
    (dist / "assets" / "app-abc123.js").write_text("console.log('app');")
    (dist / "assets" / "style.css").write_text("body{}")
    (dist / "favicon.svg").write_text("<svg/>")
    return tmp_path


def _app(dist_dir) -> FastAPI:
    return create_app(
        Settings(_env_file=None, forge_env="dev", frontend_dist_dir=None if dist_dir is None else str(dist_dir))
    )


@pytest.fixture()
def spa(tree):
    return TestClient(_app(tree / "dist"))


def _raw_get(app, path: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
    """One request with `path` handed to the app exactly as given (already-decoded ASGI path)."""

    async def run():
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
        }
        await app(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
        return start["status"], headers, body

    return asyncio.run(run())


# --- unset means nothing is served -------------------------------------------------------------


def test_unset_serves_no_files(tree):
    client = TestClient(_app(None))
    assert Settings(_env_file=None, forge_env="dev").frontend_dist_dir is None
    resp = client.get("/")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}
    assert client.get("/projects/abc").status_code == 404
    assert client.get("/api/health").json() == {"status": "ok"}


# --- serving ------------------------------------------------------------------------------------


def test_real_file_is_served_with_its_content_type(spa):
    js = spa.get("/assets/app-abc123.js")
    assert js.status_code == 200
    assert js.text == "console.log('app');"
    assert js.headers["content-type"].startswith(("text/javascript", "application/javascript"))
    css = spa.get("/assets/style.css")
    assert css.headers["content-type"].startswith("text/css")
    assert spa.get("/favicon.svg").headers["content-type"].startswith("image/svg+xml")


@pytest.mark.parametrize("path", ["/", "/projects/anything", "/admin/console/x/y", "/assets/missing.js", "/assets"])
def test_other_get_paths_get_index_html(spa, path):
    resp = spa.get(path)
    assert resp.status_code == 200
    assert INDEX_MARK in resp.text
    assert resp.headers["content-type"].startswith("text/html")


def test_head_on_an_spa_path_is_answered(spa):
    resp = spa.head("/projects/x")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


# --- traversal: each case is its own test -------------------------------------------------------


def _assert_not_outside(status, body):
    assert SECRET.encode() not in body
    assert status == 200 and INDEX_MARK.encode() in body  # fell back to index.html


def test_traversal_dotdot_segments(tree):
    status, _, body = _raw_get(_app(tree / "dist"), "/../secret.txt")
    _assert_not_outside(status, body)


def test_traversal_dotdot_nested(tree):
    status, _, body = _raw_get(_app(tree / "dist"), "/assets/../../secret.txt")
    _assert_not_outside(status, body)


def test_traversal_encoded_dots_left_literal(tree):
    # uvicorn hands the app a DECODED path, so an encoded `..` arrives as `..`; a path that still
    # carries the literal text `%2e%2e` is just an unknown name.
    status, _, body = _raw_get(_app(tree / "dist"), "/%2e%2e/secret.txt")
    _assert_not_outside(status, body)
    status, _, body = _raw_get(_app(tree / "dist"), "/assets/..%2f..%2fsecret.txt")
    _assert_not_outside(status, body)


def test_traversal_absolute_path(tree):
    secret = (tree / "secret.txt").resolve().as_posix()  # C:/.../secret.txt or /tmp/.../secret.txt
    status, _, body = _raw_get(_app(tree / "dist"), "//" + secret.lstrip("/"))
    _assert_not_outside(status, body)
    status, _, body = _raw_get(_app(tree / "dist"), "/" + secret)
    _assert_not_outside(status, body)


def test_traversal_nul_byte(tree):
    status, _, body = _raw_get(_app(tree / "dist"), "/assets/app\x00.js")
    assert status == 200 and INDEX_MARK.encode() in body


def test_symlink_pointing_out_of_the_directory(tree):
    link = tree / "dist" / "escape.txt"
    try:
        link.symlink_to(tree / "secret.txt")
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create symlinks without privileges")
    status, _, body = _raw_get(_app(tree / "dist"), "/escape.txt")
    _assert_not_outside(status, body)


def test_symlinked_directory_pointing_out(tree):
    link = tree / "dist" / "linkdir"
    try:
        link.symlink_to(tree, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Windows without symlink privilege: a directory junction resolves outside the same way.
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tree)], capture_output=True) if os.name == "nt" else None
        if made is None or made.returncode != 0:
            pytest.skip("this host cannot create symlinks or junctions")
    status, _, body = _raw_get(_app(tree / "dist"), "/linkdir/secret.txt")
    _assert_not_outside(status, body)


# --- API and schema keep their behaviour --------------------------------------------------------


def test_unknown_api_path_is_the_json_404_not_index(spa):
    resp = spa.get("/api/no-such-thing")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}
    assert INDEX_MARK not in resp.text
    assert spa.get("/api").status_code == 404


def test_wrong_method_on_a_real_api_route_is_still_405(spa):
    assert spa.post("/api/health").status_code == 405


def test_api_health_and_openapi_unchanged(spa):
    assert spa.get("/api/health").json() == {"status": "ok"}
    served = spa.get("/openapi.json")
    assert served.status_code == 200
    assert "/api/health" in served.json()["paths"]
    assert served.json() == TestClient(_app(None)).get("/openapi.json").json()


# --- methods ------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_method_on_an_spa_path_is_refused(spa, method):
    resp = spa.request(method, "/projects/abc")
    assert resp.status_code == 405
    assert INDEX_MARK not in resp.text
    assert resp.headers["allow"] == "GET, HEAD"


# --- headers ------------------------------------------------------------------------------------


def test_index_is_no_cache_and_hashed_assets_cache_long(spa):
    assert spa.get("/").headers["cache-control"] == "no-cache"
    assert spa.get("/projects/x").headers["cache-control"] == "no-cache"
    asset = spa.get("/assets/app-abc123.js").headers["cache-control"]
    assert asset == "public, max-age=31536000, immutable"
    assert spa.get("/favicon.svg").headers["cache-control"] == "no-cache"


def test_spa_responses_carry_nosniff_and_no_sandbox_csp(spa):
    for path in ("/", "/assets/app-abc123.js"):
        headers = spa.get(path).headers
        assert headers["x-content-type-options"] == "nosniff"
        assert "content-security-policy" not in headers


def test_fetch_metadata_still_refuses_a_cross_site_write(spa):
    resp = spa.post("/api/projects", headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403
    assert resp.json() == {"detail": "cross-site or same-site write refused"}
    # the same refusal applies to an SPA path: it is the middleware, ahead of routing
    assert spa.post("/projects/x", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


# --- startup checks -----------------------------------------------------------------------------


def test_prod_missing_directory_refuses_to_start(tmp_path):
    with pytest.raises(FrontendDistError, match="not an existing directory"):
        install_spa(FastAPI(), str(tmp_path / "nope"), is_prod=True)


def test_prod_directory_without_index_refuses_to_start(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(FrontendDistError, match="no index.html"):
        install_spa(FastAPI(), str(tmp_path / "empty"), is_prod=True)


def test_create_app_passes_the_prod_flag_through(tmp_path):
    cfg = Settings(_env_file=None, forge_env="dev", frontend_dist_dir=str(tmp_path / "nope"))
    cfg.forge_env = "prod"  # a full prod Settings needs a dozen unrelated values; only the flag matters here
    assert cfg.is_prod
    with pytest.raises(FrontendDistError):
        create_app(cfg)


def test_dev_missing_directory_only_warns_and_serves_nothing(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.spa_static"):
        client = TestClient(_app(tmp_path / "nope"))
    assert any("not an existing directory" in r.getMessage() for r in caplog.records)
    assert client.get("/").status_code == 404


def test_dev_directory_without_index_only_warns(tmp_path, caplog):
    (tmp_path / "empty").mkdir()
    with caplog.at_level(logging.WARNING, logger="app.services.spa_static"):
        client = TestClient(_app(tmp_path / "empty"))
    assert any("no index.html" in r.getMessage() for r in caplog.records)
    assert client.get("/").status_code == 404
