"""Unit tests for the generated-service template data contract
(ADR 0023 §Data services / §Schema migrations):
DATABASE_URL/REDIS_URL handling, the shipped initial
migration's shape, and where its own alembic config has to live under the
sandbox's staging rules. No Docker here -- the full container lifecycle
(migration-before-first-request ordering, a real Redis round-trip) is
exercised by `tests/test_deploy_integration.py` against a real Docker
Engine; this module covers what does not need one.
"""

from __future__ import annotations

import importlib.util
import itertools
import os
import re
import shutil
import sys

import pytest

from app.services.policy_checks import (
    PolicyCheckName,
    PolicyCheckStatus,
    check_destructive_ddl,
    check_resource_limits,
    run_policy_checks,
)

TEMPLATE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "templates", "generic-webtool")
)
TEMPLATE_APP_DIR = os.path.join(TEMPLATE_DIR, "app")

_module_name_counter = itertools.count()


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _load_module_fresh(path: str):
    """Loads the .py file at `path` as a brand-new module object, re-
    executing its top-level code every call (no sys.modules caching) --
    needed so each test gets its own fresh `DATABASE_URL = resolve_
    database_url()` module-level evaluation against that test's own
    monkeypatched environment."""
    name = f"template_module_{next(_module_name_counter)}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # may raise -- that is what some tests assert on
    return module


# ---------------------------------------------------------------------------
# (c1) everything the agent needs to see (alembic config + migrations)
# lives under app/ -- sandbox_build_executor.py stages ONLY app/ into the
# agent's workspace.
# ---------------------------------------------------------------------------


def test_alembic_config_and_migrations_live_under_app():
    assert os.path.isfile(os.path.join(TEMPLATE_APP_DIR, "alembic.ini"))
    assert os.path.isdir(os.path.join(TEMPLATE_APP_DIR, "migrations"))
    assert os.path.isfile(os.path.join(TEMPLATE_APP_DIR, "migrations", "env.py"))
    assert os.path.isfile(os.path.join(TEMPLATE_APP_DIR, "migrations", "script.py.mako"))

    versions_dir = os.path.join(TEMPLATE_APP_DIR, "migrations", "versions")
    versions = [f for f in os.listdir(versions_dir) if f.endswith(".py")]
    assert len(versions) >= 1

    # Not duplicated at the template root -- only Dockerfile/compose.yaml
    # are protected paths there.
    assert not os.path.isfile(os.path.join(TEMPLATE_DIR, "alembic.ini"))


# ---------------------------------------------------------------------------
# (a1) DATABASE_URL / REDIS_URL read by exact literal names -- the
# platform composes them as literals with no shared constant
# (deploy.py:188-202), so this pins the actual source text, not an
# imported constant.
# ---------------------------------------------------------------------------


def test_db_module_reads_database_url_by_exact_literal_name():
    source = _read(os.path.join(TEMPLATE_APP_DIR, "db.py"))
    assert 'os.environ.get("DATABASE_URL")' in source


def test_cache_module_reads_redis_url_by_exact_literal_name():
    source = _read(os.path.join(TEMPLATE_APP_DIR, "cache.py"))
    assert 'os.environ.get("REDIS_URL")' in source


# ---------------------------------------------------------------------------
# (a2) fails loudly, no fallback, when DATABASE_URL is absent/unparseable
# ---------------------------------------------------------------------------


def test_db_module_import_fails_when_database_url_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(Exception) as exc_info:
        _load_module_fresh(os.path.join(TEMPLATE_APP_DIR, "db.py"))
    assert exc_info.type.__name__ == "DatabaseConfigurationError"
    assert "DATABASE_URL is not set" in str(exc_info.value)


def test_db_module_import_fails_when_database_url_unparseable(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "not a valid url at all")
    with pytest.raises(Exception) as exc_info:
        _load_module_fresh(os.path.join(TEMPLATE_APP_DIR, "db.py"))
    assert exc_info.type.__name__ == "DatabaseConfigurationError"
    assert "could not be parsed" in str(exc_info.value)


def test_cache_module_import_fails_when_redis_url_unset(monkeypatch):
    # `redis` is a dependency this TEMPLATE installs (its own Dockerfile),
    # not a platform backend one -- the platform never talks to a service's
    # Redis directly, only manages the container via the Docker SDK. It was
    # added to the backend's own `dev` extra anyway (pyproject.toml), so
    # this `importorskip` is no longer expected to skip on a normal venv or
    # on CI -- it stays only as a guard against a venv that predates that
    # change (`test.ps1` re-syncs deps first for
    # exactly this reason).
    pytest.importorskip("redis")
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(Exception) as exc_info:
        _load_module_fresh(os.path.join(TEMPLATE_APP_DIR, "cache.py"))
    assert exc_info.type.__name__ == "RedisConfigurationError"


# ---------------------------------------------------------------------------
# (b) the template picks psycopg (v3) and rewrites the scheme itself --
# and preserves the password doing it. This is a regression test for a
# real bug: `str(url)` (SQLAlchemy's default __str__)
# redacts the password as "***"; only `render_as_string(hide_password=
# False)` returns a connectable DSN. Confirmed by hand against a real
# Postgres container that `str(url)` produces a URL Postgres rejects with
# "password authentication failed" using the CORRECT password -- see
# db.py's own comment and this task's `## Result`.
# ---------------------------------------------------------------------------


def test_resolve_database_url_rewrites_driver_and_preserves_password(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://svc_role:p-w_with-specials9@dbhost:5432/svc_db"
    )
    db = _load_module_fresh(os.path.join(TEMPLATE_APP_DIR, "db.py"))
    assert db.DATABASE_URL.startswith("postgresql+psycopg://")
    assert "svc_role:p-w_with-specials9@dbhost:5432/svc_db" in db.DATABASE_URL


# ---------------------------------------------------------------------------
# (a4) the shipped initial migration is additive-only
# ---------------------------------------------------------------------------


def test_destructive_ddl_check_passes_on_real_template():
    result = check_destructive_ddl(TEMPLATE_DIR)
    assert result.result == PolicyCheckStatus.PASS
    assert result.findings == []


# ---------------------------------------------------------------------------
# (d2) the compose.yaml resource-limits block
# ---------------------------------------------------------------------------


def test_resource_limits_check_passes_on_real_template():
    result = check_resource_limits(TEMPLATE_DIR)
    assert result.result == PolicyCheckStatus.PASS
    assert result.findings == []


# ---------------------------------------------------------------------------
# (c2) the generated service's alembic reads its OWN injected DATABASE_URL,
# never settings.database_url -- pinned with a scan, not a habit.
# ---------------------------------------------------------------------------

_PLATFORM_ONLY_REFERENCES = (
    "from app.config import",
    "app.config.settings",
    "settings.database_url",
    "import app.config",
)


def test_template_never_references_the_platforms_own_app_package():
    hits: list[str] = []
    for root, _dirs, files in os.walk(TEMPLATE_DIR):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            text = _read(path)
            for needle in _PLATFORM_ONLY_REFERENCES:
                if needle in text:
                    hits.append(f"{path}: {needle!r}")
    assert hits == []


# ---------------------------------------------------------------------------
# (c3) the alembic revision scaffold does not encourage downgrades
# ---------------------------------------------------------------------------


def test_migration_scaffold_downgrade_stub_raises_not_implemented():
    """script.py.mako's default `downgrade()` fallback (used whenever
    autogenerate has nothing to reverse -- always true here, since this
    template wires no ORM models into target_metadata) is a NotImplemented
    raise, not the stock "pass". Content-level check; the actual Mako
    rendering (with alembic's own `comma` filter wired in) was exercised
    end to end via a real `alembic revision` command inside the built
    image."""
    source = _read(os.path.join(TEMPLATE_APP_DIR, "migrations", "script.py.mako"))
    assert "DOWNGRADE_NOT_SUPPORTED" in source
    assert "raise NotImplementedError(" in source
    assert "downgrades are not supported" in source
    assert "${downgrades if downgrades else DOWNGRADE_NOT_SUPPORTED}" in source


def test_shipped_initial_migration_downgrade_raises_not_implemented():
    versions_dir = os.path.join(TEMPLATE_APP_DIR, "migrations", "versions")
    [filename] = [f for f in os.listdir(versions_dir) if f.endswith(".py")]
    module = _load_module_fresh(os.path.join(versions_dir, filename))
    assert module.down_revision is None  # the ONE revision this template ships
    with pytest.raises(NotImplementedError):
        module.downgrade()


# ---------------------------------------------------------------------------
# the skeleton is LF on every host. A Windows checkout with
# core.autocrlf=true used to hand the build a CRLF skeleton while the agent
# writes LF, so a 3-line edit showed up as a whole-file rewrite in the change
# set and diff. `.gitattributes` (`backend/templates/** text eol=lf`) is the
# fix; this pins it on the working tree.
# ---------------------------------------------------------------------------

TEMPLATES_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "templates"))


def _template_files() -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(TEMPLATES_ROOT):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        found.extend(os.path.join(dirpath, name) for name in filenames)
    return sorted(found)


def test_template_files_are_found():
    # A walk that finds nothing would make the CRLF test below vacuous.
    names = {os.path.relpath(p, TEMPLATES_ROOT).replace(os.sep, "/") for p in _template_files()}
    assert "generic-webtool/app/app.py" in names
    assert "generic-webtool/Dockerfile" in names


def test_template_files_contain_no_crlf():
    offenders = []
    for path in _template_files():
        with open(path, "rb") as f:
            if b"\r\n" in f.read():
                offenders.append(os.path.relpath(path, TEMPLATES_ROOT).replace(os.sep, "/"))
    assert offenders == [], f"CRLF in template files (check .gitattributes eol=lf): {offenders}"


# ---------------------------------------------------------------------------
# GET / is the service's screen. The starter page is the example every
# generated service starts from, so it must not teach the habits the dev proxy and the
# image cannot serve: root-relative URLs, form posts, external resources.
# ---------------------------------------------------------------------------

PAGE_PATH = os.path.join(TEMPLATE_APP_DIR, "static", "index.html")

_ABSOLUTE_URL = re.compile(r"https?://")
_ROOT_RELATIVE_ATTRIBUTE = re.compile(r"""(?i)\b(?:src|href|action)\s*=\s*['"]?\s*/""")
_ROOT_RELATIVE_FETCH = re.compile(r"""fetch\s*\(\s*['"`]\s*/""")
_FORM_PARAMETER = re.compile(r"\bForm\b|python[-_]multipart")
# a preview page is served under `.../preview/{build}/~{token}/`, and only a
# PLAIN relative URL stays under that path. A leading `/` leaves the platform's origin path, and a
# leading `./` or `../` is not what the agent contract (`SCREEN_CONTRACT_GUIDANCE`) allows either: `./`
# works but is a second spelling nothing needs, `../` climbs out of the token path.
_NON_PLAIN_RELATIVE_URL = re.compile(
    r"""(?i)(?:\b(?:src|href|action)\s*=\s*|fetch\s*\(\s*)['"`]?\s*(?:/|\./|\.\./)"""
)


def test_the_page_pins_catch_what_they_name():
    # Non-vacuity: each pin below fires on a sample of the habit it forbids.
    assert _ABSOLUTE_URL.search('<script src="https://cdn.example.com/x.js"></script>')
    assert _ROOT_RELATIVE_ATTRIBUTE.search('<a href="/items">')
    assert _ROOT_RELATIVE_ATTRIBUTE.search("<form action = '/items'>")
    assert _ROOT_RELATIVE_ATTRIBUTE.search('<img src=//cdn.example.com/a.png>')
    assert _ROOT_RELATIVE_FETCH.search('fetch("/items")')
    assert _ROOT_RELATIVE_FETCH.search("fetch( `/items` )")
    assert _FORM_PARAMETER.search("def add(value: str = Form(...)):")
    assert _FORM_PARAMETER.search("python-multipart>=0.0.9")
    # ... and stay quiet on the relative forms the starter uses.
    assert not _ROOT_RELATIVE_ATTRIBUTE.search('<a href="items">')
    assert not _ROOT_RELATIVE_FETCH.search('fetch("items")')


def test_the_plain_relative_pin_catches_what_it_names():
    # Non-vacuity, one sample per habit the pin forbids, and the plain forms it must stay quiet on.
    for forbidden in (
        'fetch("/items")',
        'fetch("./items")',
        'fetch("../items")',
        "fetch( `../items` )",
        'fetch("../items?value=" + x)',
        '<a href="./items">',
        '<a href="../items">',
        "<form action='/items'>",
        "<form action = '../items'>",
        "<a href=../items>",
    ):
        assert _NON_PLAIN_RELATIVE_URL.search(forbidden), forbidden
    for plain in ('fetch("items")', 'fetch("items?value=" + x)', '<a href="items">', "<form action='items'>"):
        assert not _NON_PLAIN_RELATIVE_URL.search(plain), plain


def test_the_starter_page_uses_only_plain_relative_urls():
    # no leading `/`, `./` or `../` in any fetch / href / action URL of the page.
    page = _read(PAGE_PATH)
    assert not _NON_PLAIN_RELATIVE_URL.search(page)
    assert 'fetch("items")' in page  # and the page does use fetch: the pin is not vacuously true


def test_the_starter_page_is_self_contained_and_uses_only_relative_urls():
    page = _read(PAGE_PATH)

    assert "forge generic-webtool" in page
    assert not _ABSOLUTE_URL.search(page)
    assert not _ROOT_RELATIVE_ATTRIBUTE.search(page)
    assert not _ROOT_RELATIVE_FETCH.search(page)
    # The page does use fetch, with relative URLs -- the pins above are not vacuously true.
    assert 'fetch("items")' in page
    assert 'fetch("items?value="' in page
    # Self-contained: inline style and script, nothing linked or imported.
    assert "<style>" in page and "<script>" in page
    assert "<link" not in page and "@import" not in page and "<script src" not in page


def test_the_app_module_takes_no_form_parameters():
    source = _read(os.path.join(TEMPLATE_APP_DIR, "app.py"))
    assert not _FORM_PARAMETER.search(source)
    assert not _FORM_PARAMETER.search(_read(os.path.join(TEMPLATE_DIR, "Dockerfile")))


def test_the_starter_page_passes_the_whole_policy_gate():
    results = run_policy_checks(
        workspace_dir=TEMPLATE_DIR,
        template_skeleton_dir=TEMPLATE_DIR,
        protected_paths=["Dockerfile", "compose.yaml"],
        network_available=True,
    )

    assert {r.check_name for r in results} == set(PolicyCheckName.ALL)
    assert {r.check_name: (r.result, r.findings) for r in results} == {
        name: (PolicyCheckStatus.PASS, []) for name in PolicyCheckName.ALL
    }


def test_the_gate_run_above_reaches_the_starter_page(tmp_path):
    # Non-vacuity for the test above: the same gate over a copy of the template whose page loads a
    # script from another host does flag it, at the page's own path.
    copy = tmp_path / "generic-webtool"
    shutil.copytree(TEMPLATE_DIR, copy, ignore=shutil.ignore_patterns("__pycache__"))
    page = copy / "app" / "static" / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "<script>", '<script src="https://cdn.example.com/x.js"></script>\n<script>', 1
        ),
        encoding="utf-8",
    )

    results = run_policy_checks(
        workspace_dir=str(copy),
        template_skeleton_dir=TEMPLATE_DIR,
        protected_paths=["Dockerfile", "compose.yaml"],
        network_available=True,
    )

    egress = next(r for r in results if r.check_name == PolicyCheckName.EGRESS_DETECTION)
    assert egress.result == PolicyCheckStatus.FLAG
    assert [(f["path"], f["host"]) for f in egress.findings] == [("app/static/index.html", "cdn.example.com")]


@pytest.fixture()
def template_client(monkeypatch):
    """The template's own app, driven in-process. Needs no Postgres or Redis: `db.py` and
    `cache.py` only build lazy clients from placeholder URLs, and the routes under test (`/`,
    `/healthz`) never use them. The template's directory goes on `sys.path` for the duration
    (its modules import each other by bare name), and its `db`/`cache` modules are removed again
    so nothing else in the session sees them."""
    pytest.importorskip("redis")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_URL", "postgresql://svc:pw@db.invalid:5432/svc")
    monkeypatch.setenv("REDIS_URL", "redis://cache.invalid:6379/0")
    monkeypatch.syspath_prepend(TEMPLATE_APP_DIR)
    for name in ("db", "cache"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    try:
        module = _load_module_fresh(os.path.join(TEMPLATE_APP_DIR, "app.py"))
    finally:
        sys.modules.pop("db", None)
        sys.modules.pop("cache", None)
    return TestClient(module.app)


def test_get_root_serves_the_starter_page_as_html(template_client):
    response = template_client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "forge generic-webtool" in response.text
    assert response.text == _read(PAGE_PATH)


def test_healthz_still_answers_without_touching_the_stores(template_client):
    response = template_client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
