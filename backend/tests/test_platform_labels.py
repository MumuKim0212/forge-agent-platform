"""ADR 0037 §Decision 1-2: every container and network the platform creates carries the
platform's labels, and the code that makes them cannot grow a create site that does not.

Two parts.

1. The label helper itself (`services/platform_labels.py`): the closed role set and the key
   names are pinned as LITERALS here, because they cross a boundary (they are what Docker
   stores and what the reconciler later selects by); a one-sided rename would otherwise
   leave the objects labelled and the reconciler blind to them, with nothing red.

2. A guard over `app/` as source, written for the shape the defect actually has. The defect
   is not "a label is wrong"; it is "an object was created that the reconciler cannot see" --
   a `containers.run` / `containers.create` / `networks.create` call whose labels are absent,
   or are a hand-written dict that lacks the instance and role. So the scan finds every such
   call, follows its `labels` (a keyword, a `**kwargs` dict, or a variable assigned in the same
   function) to a `platform_labels(...)` call and reads the ROLE it passes. The expected table
   pins that role per site: a new site fails the test until it is added here with its role, and
   a site that swaps roles fails too. A function that only forwards a `labels` parameter to
   the Docker call (`create_isolated_redis`) is allowed once, by name, and every caller of it
   is a site in its own right.

Per-role proof that a REAL object carries the labels is in `test_platform_labels_integration.py`.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.services import platform_labels
from app.services.platform_labels import (
    InstanceIdUnsetError,
    Role,
    clear_instance_id,
    platform_labels as make_labels,
    set_instance_id,
)
from tests.instance_scope import TEST_INSTANCE_ID

BACKEND = Path(__file__).resolve().parents[1]


# -- the helper ---------------------------------------------------------------


def test_the_role_set_is_closed_and_spelled_exactly():
    """Literals on purpose: these strings are stored by Docker and selected by the reconciler."""
    assert Role.ALL == (
        "sandbox",
        "llm-proxy",
        "build-network",
        "preview",
        "preview-redis",
        "preview-network",
        "deploy",
        "deploy-network",
        "service-redis",
        "build-image",
    )


def test_the_label_keys_are_spelled_exactly():
    assert platform_labels.LABEL_INSTANCE == "forge.instance"
    assert platform_labels.LABEL_ROLE == "forge.role"
    assert platform_labels.LABEL_BUILD_ID == "forge.build_id"
    assert platform_labels.LABEL_DEPLOYMENT_ID == "forge.deployment_id"
    assert platform_labels.LABEL_PROJECT_ID == "forge.project_id"


def test_a_label_set_carries_the_instance_the_role_and_only_the_ids_given():
    labels = make_labels(Role.DEPLOY, build_id="b-1", deployment_id="d-1", project_id="p-1")

    assert labels == {
        "forge.instance": TEST_INSTANCE_ID,
        "forge.role": "deploy",
        "forge.build_id": "b-1",
        "forge.deployment_id": "d-1",
        "forge.project_id": "p-1",
    }
    # An id that was not given is absent, not an empty string a selector could match.
    assert make_labels(Role.SANDBOX) == {"forge.instance": TEST_INSTANCE_ID, "forge.role": "sandbox"}


def test_extra_labels_are_kept_but_can_never_replace_the_instance_or_the_role():
    labels = make_labels(
        Role.PREVIEW,
        build_id="b-1",
        extra={"forge.preview": "true", "forge.instance": "someone-else", "forge.role": "deploy"},
    )

    assert labels["forge.preview"] == "true"
    assert labels["forge.instance"] == TEST_INSTANCE_ID
    assert labels["forge.role"] == "preview"


def test_an_unknown_role_is_refused():
    with pytest.raises(ValueError):
        make_labels("sandbox-ish")


def test_labelling_before_an_instance_is_bound_raises_instead_of_creating_an_invisible_object():
    clear_instance_id()
    with pytest.raises(InstanceIdUnsetError):
        make_labels(Role.SANDBOX)
    with pytest.raises(InstanceIdUnsetError):
        platform_labels.instance_id()


@pytest.mark.parametrize("bad", ["", "   ", None, 7])
def test_an_instance_id_must_be_a_non_empty_string(bad):
    with pytest.raises(ValueError):
        set_instance_id(bad)  # type: ignore[arg-type]


# -- the guard over app/ -----------------------------------------------------

# The one function allowed to pass a `labels` PARAMETER straight to the Docker call. Every call
# to it is a create site of its own (its role is in EXPECTED_SITES).
FORWARDERS = {"create_isolated_redis"}


@dataclass(frozen=True)
class Site:
    path: str
    function: str
    kind: str  # "containers.run" | "containers.create" | "networks.create" | "forward:<name>"
    role: str  # the `Role.X` attribute the site passes to platform_labels


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _site_kind(call: ast.Call) -> str | None:
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr in ("run", "create")
        and isinstance(func.value, ast.Attribute)
        and func.value.attr in ("containers", "networks")
    ):
        return f"{func.value.attr}.{func.attr}"
    name = _call_name(call)
    if name in FORWARDERS:
        return f"forward:{name}"
    return None


def _assigned_values(function: ast.AST, name: str) -> list[ast.AST]:
    """Every value assigned to `name` inside `function` (`name = ...`, `name: T = ...`)."""
    values: list[ast.AST] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            values.append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            if node.value is not None:
                values.append(node.value)
    return values


def _labels_expr_of_kwargs_dict(value: ast.AST) -> ast.AST | None:
    """The `labels` entry of `dict(..., labels=X)` or `{"labels": X}`."""
    if isinstance(value, ast.Call) and _call_name(value) == "dict":
        for kw in value.keywords:
            if kw.arg == "labels":
                return kw.value
    if isinstance(value, ast.Dict):
        for key, val in zip(value.keys, value.values):
            if isinstance(key, ast.Constant) and key.value == "labels":
                return val
    return None


def _roles_reaching(expr: ast.AST, function: ast.AST, seen: set[str] | None = None) -> tuple[list[str], bool]:
    """(roles passed to `platform_labels` anywhere `expr` comes from, whether `expr` ends in a
    bare parameter of `function`). Follows names assigned in the same function."""
    seen = seen if seen is not None else set()
    roles: list[str] = []
    ends_in_parameter = False
    arguments = function.args  # type: ignore[attr-defined]
    params = {a.arg for a in arguments.posonlyargs + arguments.args + arguments.kwonlyargs}
    for node in ast.walk(expr):
        if isinstance(node, ast.Call) and _call_name(node) == "platform_labels" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Attribute) and isinstance(first.value, ast.Name) and first.value.id == "Role":
                roles.append(first.attr)
            else:
                roles.append(f"<{ast.unparse(first)}>")  # not a literal Role.X: never matches the table
        elif isinstance(node, ast.Name) and node.id not in seen:
            seen.add(node.id)
            assigned = _assigned_values(function, node.id)
            if not assigned and node.id in params:
                ends_in_parameter = True
            for value in assigned:
                sub_roles, sub_param = _roles_reaching(value, function, seen)
                roles += sub_roles
                ends_in_parameter = ends_in_parameter or sub_param
    return roles, ends_in_parameter


def scan_source(source: str, path: str = "app/x.py") -> tuple[list[Site], list[str]]:
    """(the create sites in `source`, the defects found at them)."""
    tree = ast.parse(source)
    sites: list[Site] = []
    findings: list[str] = []

    def visit(node: ast.AST, function: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if isinstance(child, ast.Call):
                kind = _site_kind(child)
                if kind is not None and function is not None:
                    _check(child, kind, function)
            visit(child, inner)

    def _check(call: ast.Call, kind: str, function: ast.AST) -> None:
        where = f"{path}:{call.lineno} ({function.name}, {kind})"  # type: ignore[attr-defined]
        expr = next((kw.value for kw in call.keywords if kw.arg == "labels"), None)
        if expr is None:
            for kw in call.keywords:
                if kw.arg is None and isinstance(kw.value, ast.Name):  # **kwargs
                    for value in _assigned_values(function, kw.value.id):
                        expr = _labels_expr_of_kwargs_dict(value)
                        if expr is not None:
                            break
        if expr is None:
            findings.append(f"{where}: no labels")
            return
        roles, ends_in_parameter = _roles_reaching(expr, function)
        name = function.name  # type: ignore[attr-defined]
        if not roles:
            if ends_in_parameter and kind.startswith("containers.") and name in FORWARDERS:
                return  # the one forwarder; its callers are sites of their own
            findings.append(f"{where}: labels do not come from platform_labels(Role.X, ...)")
            return
        if len(set(roles)) != 1:
            findings.append(f"{where}: labels name more than one role: {sorted(set(roles))}")
            return
        sites.append(Site(path=path, function=name, kind=kind, role=roles[0]))

    visit(tree, None)
    return sites, findings


# `<path>`, the enclosing function, the Docker call, the role it labels with.
EXPECTED_SITES = {
    Site("app/services/deploy_manager.py", "start_service", "networks.create", "DEPLOY_NETWORK"),
    Site("app/services/deploy_manager.py", "_start_on_network", "containers.run", "DEPLOY"),
    Site("app/services/preview_manager.py", "start_preview", "networks.create", "PREVIEW_NETWORK"),
    Site("app/services/preview_manager.py", "_start_on_network", "containers.run", "PREVIEW"),
    Site("app/services/proxy_container.py", "start", "containers.run", "LLM_PROXY"),
    Site("app/services/sandbox_build_executor.py", "_create_build_network", "networks.create", "BUILD_NETWORK"),
    Site("app/services/sandbox_runner.py", "_create_run_network", "networks.create", "BUILD_NETWORK"),
    Site("app/services/sandbox_runner.py", "_provision", "containers.create", "SANDBOX"),
    Site("app/services/sandbox_runner.py", "probe_disk_cap", "containers.create", "SANDBOX"),
    Site("app/services/service_data.py", "ensure_project_redis", "forward:create_isolated_redis", "SERVICE_REDIS"),
    Site("app/services/preview_data.py", "_create_preview_redis", "forward:create_isolated_redis", "PREVIEW_REDIS"),
}


def _scan_app() -> tuple[list[Site], list[str]]:
    sites: list[Site] = []
    findings: list[str] = []
    for path in sorted((BACKEND / "app").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(BACKEND).as_posix()
        found, defects = scan_source(path.read_text(encoding="utf-8"), relative)
        sites += found
        findings += defects
    # The forwarder's own Docker call is checked and accepted inside `scan_source`; it is not a
    # site in the table, its callers are.
    return sites, findings


def test_every_create_site_in_app_labels_through_the_helper_with_its_declared_role():
    sites, findings = _scan_app()

    assert findings == []
    assert set(sites) == EXPECTED_SITES
    assert len(sites) == len(EXPECTED_SITES)  # no site counted twice


# -- the scan itself (so it cannot pass by finding nothing) ---------------------


def _scan(src: str):
    return scan_source(src, "app/synthetic.py")


def test_a_create_with_no_labels_is_a_defect():
    sites, findings = _scan("def f(client):\n    client.containers.run(image='x', detach=True)\n")
    assert sites == [] and len(findings) == 1 and "no labels" in findings[0]


def test_the_old_hand_written_l4_labels_without_the_helper_are_a_defect():
    """The labels a deploy network carried before this task: present, and invisible to the
    reconciler, which is the exact shape of the bug."""
    sites, findings = _scan(
        "def f(client):\n"
        "    client.networks.create('n', labels={'forge.deploy': 'true', 'forge.deployment_id': 'd'})\n"
    )
    assert sites == [] and len(findings) == 1 and "platform_labels" in findings[0]


def test_labels_reaching_the_call_through_a_kwargs_dict_are_followed():
    ok = (
        "def f(client):\n"
        "    kw = dict(image='x', labels=platform_labels(Role.SANDBOX, build_id='b'))\n"
        "    client.containers.create(**kw)\n"
    )
    assert _scan(ok) == ([Site("app/synthetic.py", "f", "containers.create", "SANDBOX")], [])
    bad = "def f(client):\n    kw = dict(image='x')\n    client.containers.create(**kw)\n"
    assert _scan(bad)[1] and not _scan(bad)[0]
    bad2 = "def f(client):\n    kw = dict(image='x', labels={'a': 'b'})\n    client.containers.create(**kw)\n"
    assert _scan(bad2)[1] and not _scan(bad2)[0]


def test_labels_reaching_the_call_through_a_local_variable_are_followed():
    ok = (
        "def f(client):\n"
        "    labels = platform_labels(Role.DEPLOY, deployment_id='d')\n"
        "    labels.update({'x': 'y'})\n"
        "    client.containers.run(image='x', labels=labels)\n"
    )
    assert _scan(ok)[0] == [Site("app/synthetic.py", "f", "containers.run", "DEPLOY")]
    bad = "def f(client):\n    labels = {'forge.deploy': 'true'}\n    client.containers.run(image='x', labels=labels)\n"
    assert _scan(bad)[1] and not _scan(bad)[0]


def test_a_role_that_is_not_a_literal_role_attribute_never_matches_the_table():
    sites, findings = _scan(
        "def f(client, role):\n    client.networks.create('n', labels=platform_labels(role))\n"
    )
    assert findings == []  # it is labelled through the helper ...
    assert sites == [Site("app/synthetic.py", "f", "networks.create", "<role>")]  # ... but never as a declared role
    assert sites[0] not in EXPECTED_SITES


def test_a_function_that_forwards_a_labels_parameter_is_allowed_only_by_name():
    forwarder = (
        "def create_isolated_redis(client, *, labels):\n    client.containers.create(image='x', labels=labels)\n"
    )
    assert _scan(forwarder) == ([], [])
    stranger = "def make_it(client, *, labels):\n    client.containers.create(image='x', labels=labels)\n"
    assert _scan(stranger)[1]


def test_a_call_to_the_forwarder_without_the_helper_is_a_defect():
    bad = "def f(client):\n    create_isolated_redis(client, name='n', labels={'a': 'b'}, password='p')\n"
    assert _scan(bad)[1] and not _scan(bad)[0]
    ok = (
        "def f(client):\n"
        "    create_isolated_redis(client, name='n', labels=platform_labels(Role.PREVIEW_REDIS), password='p')\n"
    )
    assert _scan(ok)[0] == [Site("app/synthetic.py", "f", "forward:create_isolated_redis", "PREVIEW_REDIS")]


def test_a_site_with_two_roles_is_a_defect():
    sites, findings = _scan(
        "def f(client, flag):\n"
        "    labels = platform_labels(Role.SANDBOX)\n"
        "    if flag:\n"
        "        labels = platform_labels(Role.DEPLOY)\n"
        "    client.containers.run(image='x', labels=labels)\n"
    )
    assert sites == [] and len(findings) == 1 and "more than one role" in findings[0]
