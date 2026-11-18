"""the anchored preview shape, and what layer 3 does with it.

Two pure-function tables, no ASGI and no database:

- `matches_preview_shape(path, raw_path)` -- every row has its OWN observable (one expected boolean,
  and each "never exempt" reason is isolated: the row that tests an encoded slash carries a decoded path
  with no extra slash, the row that tests a dot segment carries a clean raw path, and so on, so no row is
  satisfied by another reason also being present).
- `should_refuse(..., on_preview_shape=...)` -- the exemption itself: exactly `null`, exactly on the
  shape, unsafe methods only; `is_exempt_origin` / `NEVER_EXEMPT_ORIGINS` keep refusing `null`.

Path literals are spelled out on purpose (contract strings): a rename of `/preview/` must turn a row red.
"""

from __future__ import annotations

import pytest

from app.services.fetch_metadata import NEVER_EXEMPT_ORIGINS, is_exempt_origin, should_refuse
from app.services.preview_shape import matches_preview_shape

BASE = "/api/projects/p1/preview/b1"
TOKEN = "0123456789abcdef.1767225600.fedcba9876543210"  # nonce.expires.signature: two dots


def _both(path: str) -> tuple[str, bytes]:
    """A clean request: the decoded path and the raw bytes are the same string."""
    return path, path.encode()


EXEMPT_ROWS = [
    ("entry", *_both(BASE)),
    ("entry-trailing-slash", *_both(BASE + "/")),
    ("token-segment-trailing-slash", *_both(f"{BASE}/~{TOKEN}/")),
    ("token-segment-no-trailing-slash", *_both(f"{BASE}/~{TOKEN}")),
    ("token-segment-then-item", *_both(f"{BASE}/~{TOKEN}/items")),
    ("plain-item", *_both(BASE + "/items")),
    ("deep-path", *_both(BASE + "/a/b/c.js")),
    ("dots-inside-a-segment-are-fine", *_both(BASE + "/app.v2/a..b/c.d")),
    ("uuid-style-ids", *_both("/api/projects/3f2a-77/preview/9b-c1/items")),
    ("percent-encoded-space-in-a-subpath", *_both(BASE + "/a%20b")),
]


@pytest.mark.parametrize(("case", "path", "raw"), EXEMPT_ROWS, ids=[r[0] for r in EXEMPT_ROWS])
def test_on_the_shape(case, path, raw):
    assert matches_preview_shape(path, raw) is True


NEVER_ROWS = [
    # -- a dot segment, in the decoded path only (the raw bytes are clean) ----------------------------
    ("dot-segment-decoded-only", BASE + "/./x", (BASE + "/a/x").encode()),
    ("dotdot-segment-decoded-only", BASE + "/../x", (BASE + "/a/x").encode()),
    # -- a dot segment, in the raw bytes only (the decoded path is clean) -----------------------------
    ("dot-segment-raw-only", BASE + "/a/x", (BASE + "/./x").encode()),
    ("dotdot-segment-raw-only", BASE + "/a/x", (BASE + "/../x").encode()),
    ("trailing-dotdot", BASE + "/..", (BASE + "/..").encode()),
    ("dotdot-before-the-build", "/api/projects/p1/preview/../b1", "/api/projects/p1/preview/../b1".encode()),
    # -- an empty segment ------------------------------------------------------------------------------
    ("empty-segment-in-the-middle", BASE + "/a//b", (BASE + "/a//b").encode()),
    ("empty-segment-right-after-the-build", BASE + "//items", (BASE + "//items").encode()),
    ("two-trailing-slashes", BASE + "//", (BASE + "//").encode()),
    ("empty-segment-in-raw-only", BASE + "/a/b", (BASE + "/a//b").encode()),
    # -- an encoded slash, dot or backslash in the RAW path; the decoded path is clean ----------------
    ("encoded-slash-lower", BASE + "/a/b", (BASE + "/a%2fb").encode()),
    ("encoded-slash-upper", BASE + "/a/b", (BASE + "/a%2Fb").encode()),
    ("encoded-dot-lower", BASE + "/a.b", (BASE + "/a%2eb").encode()),
    ("encoded-dot-upper", BASE + "/a.b", (BASE + "/a%2Eb").encode()),
    ("encoded-backslash", BASE + "/a\\b", (BASE + "/a%5cb").encode()),
    ("encoded-backslash-upper", BASE + "/a\\b", (BASE + "/a%5Cb").encode()),
    ("encoded-dot-hiding-a-dotdot-segment", BASE + "/../x", (BASE + "/%2e%2e/x").encode()),
    # -- the request cannot be shown clean --------------------------------------------------------------
    ("no-raw-path-at-all", BASE + "/items", None),
    # -- a neighbouring family, with otherwise clean paths ---------------------------------------------
    ("preview-x-family", "/api/projects/p1/preview-x/b1", "/api/projects/p1/preview-x/b1".encode()),
    ("previews-family", "/api/projects/p1/previews/b1", "/api/projects/p1/previews/b1".encode()),
    ("preview-without-a-build", "/api/projects/p1/preview", "/api/projects/p1/preview".encode()),
    ("preview-with-an-empty-build", "/api/projects/p1/preview/", "/api/projects/p1/preview/".encode()),
    ("service-route", "/api/projects/p1/service/items", "/api/projects/p1/service/items".encode()),
    ("health", "/api/health", "/api/health".encode()),
    ("another-api-route", "/api/projects/p1/deployments/stop", "/api/projects/p1/deployments/stop".encode()),
    ("preview-in-another-position", "/api/preview/p1/preview/b1", "/api/preview/p1/preview/b1".encode()),
    ("no-api-prefix", "/projects/p1/preview/b1", "/projects/p1/preview/b1".encode()),
    ("a-prefix-before-api", "/x/api/projects/p1/preview/b1", "/x/api/projects/p1/preview/b1".encode()),
    ("extra-segment-between-project-and-preview", "/api/projects/p1/x/preview/b1", "/api/projects/p1/x/preview/b1".encode()),
    ("api-prefix-longer", "/apix/projects/p1/preview/b1", "/apix/projects/p1/preview/b1".encode()),
    # -- an id outside [A-Za-z0-9-] ---------------------------------------------------------------------
    ("project-id-with-underscore", "/api/projects/p_1/preview/b1", "/api/projects/p_1/preview/b1".encode()),
    ("project-id-with-dot", "/api/projects/p.1/preview/b1", "/api/projects/p.1/preview/b1".encode()),
    ("build-id-with-dot", "/api/projects/p1/preview/b.1", "/api/projects/p1/preview/b.1".encode()),
    ("build-id-with-tilde", "/api/projects/p1/preview/~b1", "/api/projects/p1/preview/~b1".encode()),
    ("build-id-with-space", "/api/projects/p1/preview/b 1", "/api/projects/p1/preview/b 1".encode()),
    ("build-id-with-semicolon", "/api/projects/p1/preview/b1;x/items", "/api/projects/p1/preview/b1;x/items".encode()),
    ("project-id-empty", "/api/projects//preview/b1", "/api/projects//preview/b1".encode()),
    # -- the ends are anchored --------------------------------------------------------------------------
    ("trailing-newline-after-the-build", BASE + "\n", (BASE + "\n").encode()),
    ("no-leading-slash", BASE[1:], BASE[1:].encode()),
    ("empty", "", b""),
]


@pytest.mark.parametrize(("case", "path", "raw"), NEVER_ROWS, ids=[r[0] for r in NEVER_ROWS])
def test_never_on_the_shape(case, path, raw):
    assert matches_preview_shape(path, raw) is False


def test_a_query_string_in_the_raw_path_is_ignored_not_a_reason():
    # Some servers hand the target with its query; the path rules apply to the path part only.
    assert matches_preview_shape(BASE + "/items", (BASE + "/items?x=%2f").encode()) is True
    assert matches_preview_shape(BASE + "/items", (BASE + "/a%2fb?x=1").encode()) is False


def test_the_token_segment_itself_is_not_a_dot_rule():
    # The token has two dots; only a WHOLE segment equal to `.` or `..` is refused.
    assert matches_preview_shape(f"{BASE}/~{TOKEN}/x", f"{BASE}/~{TOKEN}/x".encode()) is True


# ---------------------------------------------------------------------------
# layer 3: the exemption, as a pure rule
# ---------------------------------------------------------------------------

ALLOWED = ["https://spa.example.com"]
UNSAFE = ["POST", "PUT", "PATCH", "DELETE"]


@pytest.mark.parametrize("method", UNSAFE)
@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_null_on_the_shape_is_exempt(method, site):
    assert should_refuse(method, site, "null", ALLOWED, on_preview_shape=True) is False


@pytest.mark.parametrize("method", UNSAFE)
def test_null_off_the_shape_is_still_refused(method):
    assert should_refuse(method, "cross-site", "null", ALLOWED, on_preview_shape=False) is True
    assert should_refuse(method, "cross-site", "null", ALLOWED) is True  # the default is "off the shape"


@pytest.mark.parametrize("origin", ["https://evil.example.com", "http://127.0.0.1:5391", "http://evil.test", "NULL", "Null", " null", "null ", "", "*"])
@pytest.mark.parametrize("method", UNSAFE)
def test_any_other_origin_on_the_shape_is_still_refused(method, origin):
    assert should_refuse(method, "cross-site", origin, ALLOWED, on_preview_shape=True) is True


@pytest.mark.parametrize("method", UNSAFE)
def test_no_origin_at_all_on_the_shape_is_still_refused(method):
    assert should_refuse(method, "cross-site", None, ALLOWED, on_preview_shape=True) is True


def test_the_existing_origin_rules_are_unchanged():
    assert NEVER_EXEMPT_ORIGINS == frozenset({"null", "*"})
    assert is_exempt_origin("null", ["null"]) is False
    assert is_exempt_origin("null", ALLOWED) is False
    assert is_exempt_origin("https://spa.example.com", ALLOWED) is True
