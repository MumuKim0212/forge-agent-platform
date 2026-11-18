"""The automated policy gate.

Six pure functions, one per check in the catalog, run against a build's
outputs (the sanitised copy of the agent's harvested workspace -- source/diff, dependency manifests, template
config) after the build has `SUCCEEDED`. Each check is a pure function: it
reads the workspace and returns a `CheckResult` — it never mutates the
build, the workspace, or any other state (spec: "Checks never mutate the
build").

Fail-closed (spec + task acceptance criteria): a check that cannot run —
missing tool, unreadable file, or (dependency-vulnerability check) no
network access to a vulnerability feed — returns `flag`, never `pass`, with
a finding that explains why. There is deliberately no "skipped"/"unknown"
result value; from the gate's perspective "couldn't verify" and "verified
unsafe" both mean "flag" (spec: "A check that cannot run ... is treated as
flag, never as pass").

Findings are redacted/truncated before being returned (never copy full secret values
into logs/DB) —
see `_redact_secret`.

Tool choice: pure-stdlib regex/text scanning for all six checks. No new
dependency was added. Rationale per check:
  - Secrets/PII: regex patterns over source text. A dedicated secret-scanner
    (e.g. detect-secrets, trufflehog) would reduce false negatives, but pulls
    in a new dependency for slice-3's first cut; regex patterns are named as
    a starting point and are easy to extend without new tooling.
  - Dependency vulnerabilities: would ideally query PyPI Advisory
    DB/OSV.dev, but this sandboxed test environment has no guaranteed
    network access (per task instructions) and the spec requires graceful
    offline degradation; the pinned-known-vulnerable-version table below is
    the offline-safe placeholder, and any inability to reach a live feed
    (were one wired in later) must fail closed (flag), never silently
    "pass" on incomplete data.
  - Egress: regex over import/call sites for common HTTP client entry
    points, cross-referenced against protected/allowlisted destinations.
    Static, not a real network trace (spec: "Egress detection | Source
    (static)").
  - Resource limits: parses the workspace's compose.yaml (template/deploy
    config) for CPU/memory caps, textually (no new YAML dependency needed
    beyond what's already in the stdlib-adjacent toolchain — a minimal
    hand-rolled scan is sufficient for the flat key: value shape the
    generic-webtool template's compose.yaml uses).
  - Template compliance: compares the workspace's file listing against the
    pinned template skeleton's, outside agent-writable paths.
  - Destructive DDL (ADR 0023 §Schema migrations): regex over
    Alembic revision-script text for `op.drop_*` / a type-changing or
    narrowing `op.alter_column` / raw destructive SQL inside `op.execute`.
    Migration files are identified by content signature (every Alembic-
    generated revision script carries a module-level `down_revision`
    assignment), not by directory name — this check does not scan only
    `migrations/versions/`, deliberately: when the generated-
    service *template* got its first real migration, it placed its alembic
    config and migration scripts under `app/migrations/` rather than a
    workspace-root `migrations/`, since only `app/` is staged into the
    agent's sandbox workspace, and this check was
    already written to make no assumption about that path. **This check
    has no diff access** (see
    `run_policy_checks` below) and therefore scans every migration file
    present in the workspace, not only ones this build touched — see this
    check's task `## Result` for the consequence.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class PolicyCheckStatus:
    PASS = "pass"
    FLAG = "flag"


class PolicyCheckName:
    SECRETS_PII = "secrets_pii"
    DEPENDENCY_VULNERABILITIES = "dependency_vulnerabilities"
    EGRESS_DETECTION = "egress_detection"
    RESOURCE_LIMITS = "resource_limits"
    TEMPLATE_COMPLIANCE = "template_compliance"
    DESTRUCTIVE_DDL = "destructive_ddl"
    # NOT a check, and deliberately not in
    # `ALL`. The name of the one row `policy_gate.record_gate_error_verdict` writes when the gate
    # itself failed, so the timeline and the approval console show a flagged, platform-authored
    # line instead of a build with no verdict.
    GATE_ERROR = "gate_error"

    ALL = (
        SECRETS_PII,
        DEPENDENCY_VULNERABILITIES,
        EGRESS_DETECTION,
        RESOURCE_LIMITS,
        TEMPLATE_COMPLIANCE,
        DESTRUCTIVE_DDL,
    )


class PolicyRiskTier:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Directories never scanned (build/version-control plumbing, not agent-
# authored application source).
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".forge"}

# Scannable source/text extensions for the secrets/PII and egress checks.
_TEXT_EXTENSIONS = (
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".env",
    ".cfg",
    ".ini",
    ".toml",
    ".md",
    # the service's screen. A generated service is used through a page, and a
    # page's `<script src>`, `<link href>` or `@import url()` is as much an outbound
    # request as a Python `requests.get`.
    ".html",
    ".htm",
    ".css",
)

# Extensions the egress scan reads: the code files, plus the screen files. A subset
# of `_TEXT_EXTENSIONS` on purpose -- a URL in a README or a JSON fixture is documentation or
# data, not a call. `.svg` is not here: its XML namespace URL would be a permanent false
# positive, and the answer to that is not an allowlist hole.
_EGRESS_EXTENSIONS = (".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".htm", ".css")
_MARKUP_EXTENSIONS = (".html", ".htm", ".css")


@dataclass(frozen=True)
class CheckResult:
    check_name: str
    result: str  # PolicyCheckStatus
    summary: str
    findings: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class PolicyVerdictResult:
    risk_tier: str
    checks: list[CheckResult]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iter_text_files(workspace_dir: str):
    for root, dirs, files in os.walk(workspace_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if name.lower().endswith(_TEXT_EXTENSIONS):
                path = os.path.join(root, name)
                rel = os.path.relpath(path, workspace_dir).replace(os.sep, "/")
                yield rel, path


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return None


def _redact_secret(value: str, *, keep: int = 4) -> str:
    """Never store a full secret value.
    Keeps only a short prefix, replacing the rest with a fixed-length
    placeholder so the finding is still useful for triage without being a
    usable credential itself."""
    value = value.strip()
    if len(value) <= keep:
        return "***"
    return f"{value[:keep]}***REDACTED***"


def _flag(check_name: str, summary: str, findings: list[dict]) -> CheckResult:
    return CheckResult(check_name=check_name, result=PolicyCheckStatus.FLAG, summary=summary, findings=findings)


def _passed(check_name: str, summary: str) -> CheckResult:
    return CheckResult(check_name=check_name, result=PolicyCheckStatus.PASS, summary=summary, findings=[])


def _run_safely(check_name: str, fn) -> CheckResult:
    """Fail-closed wrapper: any exception from a check function is itself a
    `flag` with an explanatory finding, never a silent pass or a crash that
    would abort the whole gate (spec: "A check that cannot run ... is
    treated as flag, never as pass")."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - fail closed on any check error
        return _flag(
            check_name,
            f"{check_name} check errored and could not verify the build; treated as flag (fail-closed).",
            [{"type": "check_error", "detail": str(exc)[:500]}],
        )


# ---------------------------------------------------------------------------
# Check 1: secrets / PII scan
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("generic_api_key_assignment", re.compile(
        r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|password)\b\s*[:=]\s*['\"]([A-Za-z0-9\-_/+=]{8,})['\"]"
    )),
    ("private_key_block", re.compile(r"-----BEGIN (RSA|EC|OPENSSH|PGP|DSA) PRIVATE KEY-----")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{10,}\b")),
    # ADR 0035 §Decision 7: the OpenAI key shapes. `sk-proj-` (project keys; also the
    # `svcacct` and `admin` prefixes OpenAI issues the same way) and the older `sk-` plus a
    # long unbroken alphanumeric run. The second form needs 32+ characters and no hyphen, so
    # `sk-ant-...` (Anthropic, matched above) and ordinary words ending in "sk" do not hit it.
    ("openai_project_key", re.compile(r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9\-_]{20,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
]

_PII_PATTERNS = [
    ("ssn_like", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("email_collection_hint", re.compile(r"(?i)\b(collect|store|save)\w*\s+(user'?s?\s+)?email\b")),
]


def check_secrets_pii(workspace_dir: str) -> CheckResult:
    findings: list[dict] = []
    for rel_path, path in _iter_text_files(workspace_dir):
        text = _read_text(path)
        if text is None:
            findings.append({
                "type": "unreadable_file",
                "path": rel_path,
                "detail": "file could not be read for scanning",
            })
            continue
        for name, pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(text):
                findings.append({
                    "type": "hardcoded_secret",
                    "pattern": name,
                    "path": rel_path,
                    "match": _redact_secret(match.group(0)),
                })
        for name, pattern in _PII_PATTERNS:
            for match in pattern.finditer(text):
                findings.append({
                    "type": "pii_pattern",
                    "pattern": name,
                    "path": rel_path,
                    "match": _redact_secret(match.group(0)),
                })

    if findings:
        return _flag(
            PolicyCheckName.SECRETS_PII,
            f"Secrets/PII scan found {len(findings)} finding(s) across the workspace.",
            findings,
        )
    return _passed(PolicyCheckName.SECRETS_PII, "No hardcoded secrets or PII-handling patterns found.")


# ---------------------------------------------------------------------------
# Check 2: dependency vulnerabilities
# ---------------------------------------------------------------------------

# Offline-safe placeholder table of known-vulnerable pins. In production
# this would query a live feed (PyPI Advisory DB / OSV.dev); this
# environment has no guaranteed network access, so the check degrades to
# this pinned table plus a network-attempt guard (see below) rather than
# silently passing on data it couldn't verify.
_KNOWN_VULNERABLE_PINS = {
    "requests": {"2.6.0", "2.19.1"},
    "urllib3": {"1.24.1", "1.25.8"},
    "pyyaml": {"5.1", "5.3"},
    "django": {"1.11.0", "2.0.0"},
    "flask": {"0.12"},
    "lodash": {"4.17.4", "4.17.15"},
    "minimist": {"1.2.5"},
    "axios": {"0.21.0"},
}

_MANIFEST_FILES = ("requirements.txt", "pyproject.toml", "package.json")

_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*==\s*([A-Za-z0-9_.\-]+)\s*$")
_PACKAGE_JSON_DEP = re.compile(r'"([A-Za-z0-9_.\-@/]+)"\s*:\s*"[\^~]?([0-9][A-Za-z0-9_.\-]*)"')


def _parse_requirements_txt(text: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        match = _REQ_LINE.match(line)
        if match:
            pins[match.group(1).lower()] = match.group(2)
    return pins


def _parse_package_json(text: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    for match in _PACKAGE_JSON_DEP.finditer(text):
        pins[match.group(1).lower()] = match.group(2)
    return pins


def _find_manifests(workspace_dir: str) -> list[tuple[str, str]]:
    found = []
    for root, dirs, files in os.walk(workspace_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if name in _MANIFEST_FILES:
                path = os.path.join(root, name)
                rel = os.path.relpath(path, workspace_dir).replace(os.sep, "/")
                found.append((rel, path))
    return found


def check_dependency_vulnerabilities(workspace_dir: str, *, network_available: bool = False) -> CheckResult:
    manifests = _find_manifests(workspace_dir)
    if not manifests:
        return _passed(
            PolicyCheckName.DEPENDENCY_VULNERABILITIES,
            "No dependency manifest found in the workspace; nothing to check.",
        )

    findings: list[dict] = []
    for rel_path, path in manifests:
        text = _read_text(path)
        if text is None:
            findings.append({
                "type": "unreadable_manifest",
                "path": rel_path,
                "detail": "manifest could not be read for scanning",
            })
            continue

        if rel_path.endswith("package.json"):
            pins = _parse_package_json(text)
        else:
            pins = _parse_requirements_txt(text)

        for pkg, version in pins.items():
            vulnerable_versions = _KNOWN_VULNERABLE_PINS.get(pkg)
            if vulnerable_versions and version in vulnerable_versions:
                findings.append({
                    "type": "known_vulnerable_pin",
                    "path": rel_path,
                    "package": pkg,
                    "version": version,
                })

    if not network_available:
        # spec: a vuln check needing network data must degrade gracefully
        # offline. Offline == cannot fully verify == flag (fail-closed),
        # even if the static pin table above found nothing on its own —
        # the live-feed portion of the check could not run.
        findings.append({
            "type": "offline_degraded",
            "detail": (
                "No network access to a live vulnerability feed (PyPI Advisory DB / OSV.dev); "
                "only the offline known-vulnerable-pin table was checked. Fail-closed: flag."
            ),
        })
        return _flag(
            PolicyCheckName.DEPENDENCY_VULNERABILITIES,
            f"Dependency check ran offline-only ({len(manifests)} manifest(s) scanned); "
            f"cannot verify against a live feed, so this is flagged per fail-closed policy.",
            findings,
        )

    if findings:
        return _flag(
            PolicyCheckName.DEPENDENCY_VULNERABILITIES,
            f"Dependency vulnerability scan found {len(findings)} finding(s).",
            findings,
        )
    return _passed(
        PolicyCheckName.DEPENDENCY_VULNERABILITIES,
        f"No known-vulnerable pins found across {len(manifests)} manifest(s).",
    )


# ---------------------------------------------------------------------------
# Check 3: egress detection
# ---------------------------------------------------------------------------

# Allowlisted destinations a generated service is permitted to call
# (mirrors the sandbox's own egress allowlist —
# LLM proxy + package registries). Anything else found in a
# static outbound-call site is a finding.
_EGRESS_ALLOWLIST_HOSTS = ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org")

_EGRESS_CALL_PATTERNS = [
    re.compile(r"requests\.(get|post|put|patch|delete|head)\s*\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"urllib\.request\.urlopen\s*\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"httpx\.(get|post|put|patch|delete|head|Client|AsyncClient)\s*\(\s*['\"]?([^'\")\s]*)"),
    re.compile(r"fetch\s*\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"axios\.(get|post|put|patch|delete)\s*\(\s*['\"]([^'\"]+)['\"]"),
]

_URL_PATTERN = re.compile(r"https?://[^\s'\"]+")

# A protocol-relative reference (`//cdn.example.com/x.js`) loads from another host just as an
# absolute one does, and `_URL_PATTERN` does not see it. Read only in the screen files, and
# only where a URL is syntactically expected (an attribute value, `url(`, `@import`): a `//`
# comment in a script is not a URL.
_PROTOCOL_RELATIVE_PATTERN = re.compile(
    r"""(?i)(?:\b(?:src|href|action|poster|srcset)\s*=\s*|url\(\s*|@import\s+)['"]?\s*(//[^\s'"<>)]+)"""
)


def _host_of(url: str) -> str:
    match = re.match(r"https?://([^/:'\"]+)", url)
    return match.group(1) if match else url


def check_egress_detection(workspace_dir: str) -> CheckResult:
    findings: list[dict] = []
    for rel_path, path in _iter_text_files(workspace_dir):
        if not rel_path.lower().endswith(_EGRESS_EXTENSIONS):
            continue
        text = _read_text(path)
        if text is None:
            findings.append({
                "type": "unreadable_file",
                "path": rel_path,
                "detail": "file could not be read for scanning",
            })
            continue

        urls = set(_URL_PATTERN.findall(text))
        if rel_path.lower().endswith(_MARKUP_EXTENSIONS):
            urls.update(f"https:{ref}" for ref in _PROTOCOL_RELATIVE_PATTERN.findall(text))
        for pattern in _EGRESS_CALL_PATTERNS:
            for match in pattern.finditer(text):
                for group in match.groups():
                    if group and group.startswith("http"):
                        urls.add(group)

        for url in urls:
            host = _host_of(url)
            if not any(host == allowed or host.endswith(f".{allowed}") for allowed in _EGRESS_ALLOWLIST_HOSTS):
                findings.append({
                    "type": "non_allowlisted_egress",
                    "path": rel_path,
                    "host": host,
                    "url": url,
                })

    if findings:
        return _flag(
            PolicyCheckName.EGRESS_DETECTION,
            f"Egress detection found {len(findings)} call(s) to non-allowlisted destination(s).",
            findings,
        )
    return _passed(PolicyCheckName.EGRESS_DETECTION, "No outbound calls to non-allowlisted destinations found.")


# ---------------------------------------------------------------------------
# Check 4: resource limits
# ---------------------------------------------------------------------------

_CPU_KEY_PATTERN = re.compile(r"(?im)^\s*cpus?:\s*['\"]?([0-9.]+)['\"]?\s*$")
_MEM_KEY_PATTERN = re.compile(r"(?im)^\s*memory:\s*['\"]?([0-9A-Za-z]+)['\"]?\s*$")
_DEPLOY_BLOCK_PATTERN = re.compile(r"(?im)^\s*deploy:\s*$")


def check_resource_limits(workspace_dir: str) -> CheckResult:
    compose_path = os.path.join(workspace_dir, "compose.yaml")
    if not os.path.isfile(compose_path):
        alt = os.path.join(workspace_dir, "docker-compose.yaml")
        compose_path = alt if os.path.isfile(alt) else compose_path

    if not os.path.isfile(compose_path):
        return _flag(
            PolicyCheckName.RESOURCE_LIMITS,
            "No deploy config (compose.yaml) found in the workspace; cannot verify resource caps.",
            [{"type": "missing_deploy_config", "path": "compose.yaml"}],
        )

    text = _read_text(compose_path)
    if text is None:
        return _flag(
            PolicyCheckName.RESOURCE_LIMITS,
            "compose.yaml could not be read; cannot verify resource caps.",
            [{"type": "unreadable_deploy_config", "path": "compose.yaml"}],
        )

    findings: list[dict] = []
    has_deploy_block = bool(_DEPLOY_BLOCK_PATTERN.search(text))
    has_cpu_cap = bool(_CPU_KEY_PATTERN.search(text))
    has_mem_cap = bool(_MEM_KEY_PATTERN.search(text))

    if not has_deploy_block:
        findings.append({
            "type": "missing_resource_caps",
            "path": "compose.yaml",
            "detail": "no 'deploy:' block with resource limits found",
        })
    else:
        if not has_cpu_cap:
            findings.append({"type": "missing_cpu_cap", "path": "compose.yaml"})
        if not has_mem_cap:
            findings.append({"type": "missing_memory_cap", "path": "compose.yaml"})

    if findings:
        return _flag(
            PolicyCheckName.RESOURCE_LIMITS,
            f"Resource limits check found {len(findings)} finding(s) in the deploy config.",
            findings,
        )
    return _passed(PolicyCheckName.RESOURCE_LIMITS, "CPU/memory caps present in the deploy config.")


# ---------------------------------------------------------------------------
# Check 5: template compliance
# ---------------------------------------------------------------------------


def _relative_file_set(root_dir: str) -> set[str]:
    files = set()
    for root, dirs, names in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in names:
            path = os.path.join(root, name)
            files.add(os.path.relpath(path, root_dir).replace(os.sep, "/"))
    return files


def check_template_compliance(
    workspace_dir: str, *, template_skeleton_dir: str, protected_paths: list[str], agent_writable_prefix: str = "app/"
) -> CheckResult:
    """spec: "deviations from the golden-path shape beyond agent-writable
    paths (protected-path drift is already reported at harvest; this
    re-verifies)". Re-verifies that every protected path present in the
    pinned template skeleton is present, byte-identical, in the workspace —
    a second, independent check beyond `publish.py`'s restore step, which
    should already have fixed this by the time the gate runs."""
    if not os.path.isdir(template_skeleton_dir):
        return _flag(
            PolicyCheckName.TEMPLATE_COMPLIANCE,
            "Pinned template skeleton directory not found; cannot verify template compliance.",
            [{"type": "missing_template_skeleton", "path": template_skeleton_dir}],
        )

    findings: list[dict] = []
    for rel_path in protected_paths:
        pinned_path = os.path.join(template_skeleton_dir, rel_path)
        workspace_path = os.path.join(workspace_dir, rel_path)

        if not os.path.isfile(pinned_path):
            continue  # nothing pinned to compare against for this path

        if not os.path.isfile(workspace_path):
            findings.append({
                "type": "protected_path_missing",
                "path": rel_path,
            })
            continue

        pinned_text = _read_text(pinned_path)
        workspace_text = _read_text(workspace_path)
        if pinned_text is None or workspace_text is None or pinned_text != workspace_text:
            findings.append({
                "type": "protected_path_deviation",
                "path": rel_path,
            })

    if findings:
        return _flag(
            PolicyCheckName.TEMPLATE_COMPLIANCE,
            f"Template compliance check found {len(findings)} deviation(s) from the golden-path shape.",
            findings,
        )
    return _passed(
        PolicyCheckName.TEMPLATE_COMPLIANCE,
        "Workspace matches the pinned template skeleton on all protected paths.",
    )


# ---------------------------------------------------------------------------
# Check 6: destructive DDL (ADR 0023 §Schema migrations)
# ---------------------------------------------------------------------------

# Alembic stamps every revision script it generates with a module-level
# `down_revision` assignment (see any file under backend/migrations/versions/
# for the platform's own example of the exact same boilerplate). That is a
# far more reliable "is this a migration file" signature than any one
# directory name — this matters because the
# generated-service template's own migrations live under
# `app/migrations/versions/`, not a workspace-root `migrations/`, so a
# directory-name-based signature would have missed them entirely.
_MIGRATION_SIGNATURE_PATTERN = re.compile(r"\bdown_revision\b")

# The destructive Alembic operation names from ADR 0023 §Schema migrations,
# plus `alter_column` (flagged conditionally — see below) and `execute`
# (flagged only when its raw-SQL argument contains destructive keywords).
#
# Receiver-agnostic by design: matching
# only `op.<name>(...)` misses two shapes an LLM-authored migration can
# plausibly emit even though Alembic's own `--autogenerate` never does —
# `with op.batch_alter_table(...) as batch_op:` then `batch_op.<name>(...)`
# (the idiom most Alembic tutorials show), and an aliased import
# (`from alembic import op as o`). Both are silent misses, not false
# positives: the check reports `pass` on exactly what it exists to flag,
# the tier lands `low`, and ADR 0008 auto-approves with nobody reading it —
# a materially worse failure than over-flagging. So this matches the
# destructive *method name* preceded by an optional `<identifier>.`
# receiver (`op.`, `batch_op.`, an alias, or no receiver at all) rather
# than requiring literally `op.`. See `check_destructive_ddl`'s docstring
# for what this now over-matches in exchange.
_DDL_CALL_PATTERN = re.compile(
    r"\b(?:[A-Za-z_][A-Za-z0-9_]*\.)?(drop_table|drop_column|drop_constraint|drop_index|alter_column|execute)\s*\("
)

_RAW_DROP_TRUNCATE_PATTERN = re.compile(r"(?is)\b(DROP|TRUNCATE)\b")
_RAW_ALTER_TYPE_PATTERN = re.compile(r"(?is)\bALTER\b[^;'\"]*\bTYPE\b")


def _extract_balanced_call(text: str, open_paren_index: int) -> str:
    """Returns the text strictly between a call's opening `(` — whose
    index is `open_paren_index`, i.e. the position right after it — and
    its matching closing `)`, tracking nested parens. Alembic-generated
    calls are routinely multi-line with nested parens (`op.create_table`'s
    column list, `sa.Column(...)` inside it, `varchar(10)` inside a raw
    `op.execute` string), so a non-greedy regex up to the first `)` would
    truncate mid-call; this walks the text instead."""
    depth = 1
    i = open_paren_index
    n = len(text)
    while i < n and depth > 0:
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    end = i - 1 if depth == 0 else n
    return text[open_paren_index:end]


def _iter_python_files(workspace_dir: str):
    for root, dirs, files in os.walk(workspace_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                rel = os.path.relpath(path, workspace_dir).replace(os.sep, "/")
                yield rel, path


def check_destructive_ddl(workspace_dir: str) -> CheckResult:
    """ADR 0023 §Schema migrations: 'a migration must leave the previous
    image able to run.' Flags every destructive form spelled out there,
    each with its own finding `type` (plus `path` and a truncated `detail`
    snippet of the offending call) so one form dropping from detection is
    independently pinnable rather than hiding behind a single
    `result == FLAG`:

    - `drop_table` / `drop_column` / `drop_constraint` / `drop_index` —
      unconditionally destructive, on any receiver (`op.`, a
      `batch_alter_table` alias such as `batch_op.`, an aliased `op`
      import, or none at all — see `_DDL_CALL_PATTERN`).
    - `alter_column(..., type_=...)` — a type change is narrowing unless
      proven otherwise, and this check does not get to prove it; flag and
      let a human read it (spec).
    - `alter_column(..., nullable=False)` on an existing column — the
      previous image can still write NULL.
    - `execute("...")` whose raw-SQL argument contains
      `DROP`/`TRUNCATE`/`ALTER ... TYPE`.

    **Receiver-agnostic on purpose:** matching only literally
    `op.<name>(` silently missed `batch_alter_table` blocks and an aliased
    `op` import — both plausible from an LLM-authored migration even
    though Alembic's own autogenerate never emits either. The trade-off is
    real over-matching, disclosed rather than hidden: a same-named
    function *definition* inside a migration file (e.g. a helper literally
    named `def drop_index(...):`) also flags, since the pattern cannot
    distinguish a call from a def line. That is the only false positive:
    any receiver's `execute(...)` call is matched, but its argument text
    must still contain the raw-SQL keywords above before it is a finding,
    so `conn.execute(sa.text("SELECT 1"))` produces nothing.
    The def-line over-match is accepted: within a file already known to be
    a migration (by content signature), over-flagging costs an approver a
    read; under-
    flagging here reports `pass` on exactly what this check exists to
    catch, auto-approving it (ADR 0008) with nobody reading it.

    **No diff access** (see `run_policy_checks`): scans every migration
    file present in the workspace on every run, not only ones this build
    touched — see this check's task `## Result`."""
    findings: list[dict] = []
    for rel_path, path in _iter_python_files(workspace_dir):
        text = _read_text(path)
        if text is None:
            findings.append({
                "type": "unreadable_migration_file",
                "path": rel_path,
                "detail": "file could not be read for scanning",
            })
            continue
        if not _MIGRATION_SIGNATURE_PATTERN.search(text):
            continue  # not an Alembic revision script

        for match in _DDL_CALL_PATTERN.finditer(text):
            method_name = match.group(1)
            call_text = _extract_balanced_call(text, match.end())
            # The full matched text (receiver, if any, + method name + the
            # opening paren) rather than a hardcoded "op." prefix — the
            # receiver is no longer guaranteed to be "op", and
            # a finding should show what was actually matched.
            snippet = f"{match.group(0)}{call_text.strip()[:200]})"

            if method_name == "drop_table":
                findings.append({"type": "destructive_drop_table", "path": rel_path, "detail": snippet})
            elif method_name == "drop_column":
                findings.append({"type": "destructive_drop_column", "path": rel_path, "detail": snippet})
            elif method_name == "drop_constraint":
                findings.append({"type": "destructive_drop_constraint", "path": rel_path, "detail": snippet})
            elif method_name == "drop_index":
                findings.append({"type": "destructive_drop_index", "path": rel_path, "detail": snippet})
            elif method_name == "alter_column":
                if re.search(r"\btype_\s*=", call_text):
                    findings.append({"type": "destructive_type_change", "path": rel_path, "detail": snippet})
                if re.search(r"\bnullable\s*=\s*False\b", call_text):
                    findings.append({"type": "destructive_narrowing_nullable", "path": rel_path, "detail": snippet})
            elif method_name == "execute":
                if _RAW_DROP_TRUNCATE_PATTERN.search(call_text) or _RAW_ALTER_TYPE_PATTERN.search(call_text):
                    findings.append({"type": "destructive_raw_sql", "path": rel_path, "detail": snippet})

    if findings:
        return _flag(
            PolicyCheckName.DESTRUCTIVE_DDL,
            f"Destructive DDL check found {len(findings)} finding(s) across migration files in the workspace.",
            findings,
        )
    return _passed(PolicyCheckName.DESTRUCTIVE_DDL, "No destructive DDL found in migration files.")


# ---------------------------------------------------------------------------
# Orchestration: run all six checks + compute the risk tier
# ---------------------------------------------------------------------------


def run_policy_checks(
    *,
    workspace_dir: str,
    template_skeleton_dir: str,
    protected_paths: list[str],
    network_available: bool = False,
) -> list[CheckResult]:
    """Runs the six checks in catalog order.
    Each check is wrapped in `_run_safely` so one check's internal error
    never aborts the others or silently passes (fail-closed).

    Note for `destructive_ddl` specifically: this function takes no diff —
    only `workspace_dir`, the pinned template skeleton, and the protected-
    path list — so that check scans the whole workspace rather than a
    changed-file set. That is a decision, not a gap; see
    `check_destructive_ddl`'s docstring and this check's task
    `## Result` for the consequence."""
    return [
        _run_safely(PolicyCheckName.SECRETS_PII, lambda: check_secrets_pii(workspace_dir)),
        _run_safely(
            PolicyCheckName.DEPENDENCY_VULNERABILITIES,
            lambda: check_dependency_vulnerabilities(workspace_dir, network_available=network_available),
        ),
        _run_safely(PolicyCheckName.EGRESS_DETECTION, lambda: check_egress_detection(workspace_dir)),
        _run_safely(PolicyCheckName.RESOURCE_LIMITS, lambda: check_resource_limits(workspace_dir)),
        _run_safely(
            PolicyCheckName.TEMPLATE_COMPLIANCE,
            lambda: check_template_compliance(
                workspace_dir,
                template_skeleton_dir=template_skeleton_dir,
                protected_paths=protected_paths,
            ),
        ),
        _run_safely(PolicyCheckName.DESTRUCTIVE_DDL, lambda: check_destructive_ddl(workspace_dir)),
    ]


def compute_risk_tier(
    checks: list[CheckResult], *, medium_tier_checks: list[str], high_tier_checks: list[str]
) -> str:
    """"The tier is computed per build; the
    highest matching tier wins." An unrecognized check name that flags
    (shouldn't happen with the fixed six-check catalog, but fail-closed
    covers it) is treated as high-tier rather than silently ignored."""
    flagged = {c.check_name for c in checks if c.result == PolicyCheckStatus.FLAG}
    if not flagged:
        return PolicyRiskTier.LOW

    high_set = set(high_tier_checks)
    medium_set = set(medium_tier_checks)

    if flagged & high_set:
        return PolicyRiskTier.HIGH
    if flagged - medium_set - high_set:
        # A flagged check not accounted for in either tier's config —
        # fail-closed: treat as the more severe tier rather than dropping it.
        return PolicyRiskTier.HIGH
    if flagged & medium_set:
        return PolicyRiskTier.MEDIUM
    return PolicyRiskTier.LOW


def build_policy_verdict(
    *,
    workspace_dir: str,
    template_skeleton_dir: str,
    protected_paths: list[str],
    medium_tier_checks: list[str],
    high_tier_checks: list[str],
    network_available: bool = False,
) -> PolicyVerdictResult:
    checks = run_policy_checks(
        workspace_dir=workspace_dir,
        template_skeleton_dir=template_skeleton_dir,
        protected_paths=protected_paths,
        network_available=network_available,
    )
    tier = compute_risk_tier(checks, medium_tier_checks=medium_tier_checks, high_tier_checks=high_tier_checks)
    return PolicyVerdictResult(risk_tier=tier, checks=checks)
