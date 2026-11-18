"""every container removal removes the container's anonymous volumes.

An image that declares a `VOLUME` (`postgres`, older `redis`) gets a fresh
anonymous volume per container, and Docker drops it only for `docker rm -v`
(`remove(v=True)`) or an auto-removed container. Before this task every
removal was a bare `remove(force=True)` and the dev host leaked ~99 GB.

This scan keeps it true across `app/` and `tests/`. It classifies a call by
its RECEIVER, and fails CLOSED: a `.remove(...)` / `.remove_container(...)`
call is a container removal unless the receiver is recognisably something
else -- an image (`...images...`), a volume (`...volumes...`), a network
(`...network...`), or the `os` module. An unrecognised receiver therefore fails the scan loudly instead of
slipping through, and the fix is either `v=True` or a clearer receiver name.
`containers.prune()` is refused outright: it removes containers WITHOUT
their anonymous volumes.

Behavioural proof against a real Docker Engine lives in
`test_preview_data_integration.py` and
`test_service_data_deprovision_integration.py`; named-volume survival in
`test_named_volume_survives_container_removal.py`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

# Containers started with `remove=True` are dropped (with their anonymous
# volumes) by the daemon on exit and have no `.remove(` call at all:
#   test_deploy_traefik_integration.py (control container), test_image_assembly.py,
#   test_proxy_container_integration.py (x2), test_sandbox_build_executor_integration.py.

_NOT_A_CONTAINER = re.compile(r"network|\bimages\b|\bvolumes\b|^os($|\.)")


def _receiver_text(call: ast.Call) -> str:
    return ast.unparse(call.func.value)  # type: ignore[attr-defined]


def container_removal_findings(source: str) -> tuple[int, list[tuple[int, str]]]:
    """(container-removal sites found, [(line, why) for each defect])."""
    sites = 0
    defects: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        if attr == "prune" and "containers" in _receiver_text(node):
            defects.append((node.lineno, "containers.prune() leaves anonymous volumes behind"))
            continue
        if attr not in ("remove", "remove_container"):
            continue
        if attr == "remove" and _NOT_A_CONTAINER.search(_receiver_text(node)):
            continue
        sites += 1
        v = next((kw for kw in node.keywords if kw.arg == "v"), None)
        if v is None or not (isinstance(v.value, ast.Constant) and v.value.value is True):
            defects.append((node.lineno, f"{_receiver_text(node)}.{attr}(...) without v=True"))
    return sites, defects


def _scan(subdir: str) -> tuple[int, list[str]]:
    total = 0
    defects: list[str] = []
    for path in sorted((BACKEND / subdir).rglob("*.py")):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        sites, found = container_removal_findings(path.read_text(encoding="utf-8"))
        total += sites
        defects += [f"{path.relative_to(BACKEND).as_posix()}:{line}: {why}" for line, why in found]
    return total, defects


# -- the classifier itself (so the scan cannot pass by finding nothing) ------------


def test_a_bare_container_removal_is_a_defect():
    sites, defects = container_removal_findings("container.remove(force=True)\n")
    assert sites == 1 and len(defects) == 1


def test_a_removal_with_v_false_or_a_dynamic_v_is_a_defect():
    assert container_removal_findings("c.remove(v=False, force=True)")[1]
    assert container_removal_findings("c.remove(v=keep, force=True)")[1]


def test_a_removal_with_v_true_passes():
    assert container_removal_findings("c.remove(v=True, force=True)") == (1, [])
    assert container_removal_findings("client.containers.get(n).remove(force=True, v=True)") == (1, [])


def test_the_low_level_api_removal_must_carry_v_too():
    assert container_removal_findings("client.api.remove_container(cid, force=True)")[1]
    assert container_removal_findings("client.api.remove_container(cid, v=True, force=True)") == (1, [])


def test_image_network_and_os_removals_are_not_container_removals():
    for src in (
        "docker_client.images.get(tag).remove(force=True)",
        "docker_client.images.remove(tag, force=True)",
        "docker_client.volumes.get(name).remove(force=True)",
        "network.remove()",
        "self.network.remove()",
        "network_a.remove()",
        "os.remove(path)",
        "os.path.remove(path)",
    ):
        assert container_removal_findings(src) == (0, []), src


def test_an_unrecognised_receiver_fails_closed():
    """`thing.remove(force=True)` is treated as a container, not waved through."""
    assert container_removal_findings("thing.remove(force=True)")[1]


def test_containers_prune_is_refused():
    assert container_removal_findings("client.containers.prune()")[1]
    assert container_removal_findings("client.images.prune()") == (0, [])


# -- the real tree -----------------------------------------------------------------

# Eight sites in app/: deploy_manager (1), preview_data (1), preview_manager (1;
# the former second, the "label but no build id" branch that deleted a
# project's Redis), proxy_container (1), sandbox_runner (1), service_data (2,
# the clean-up of a half-made Redis and the replacement of a Redis that
# is not the hardened one), service_data_deprovision (1). Fourteen
# in tests/ (plus 2 in test_named_volume_survives_container_removal.py, eighteen
# in all, before the rework): conftest (2), agent_tool_capture (1), deploy (3), deploy_traefik (2),
# openai_engine_up (1: the stub upstream), eval_run_integration (1: leftover
# preview containers), preview_manager (1),
# proxy_container_openai (1: the stub upstream), sandbox_runner (1),
# service_data (3). The rework changes six of those: conftest (-1, the label-wide
# sweep became a tracked one), docker_cleanup.py (+1) and
# test_docker_cleanup_integration.py (+4), service_data (+2), deploy (+1).
# deploy (+1), `_sweep_leftovers` -- the container half of the
# no-leftovers tests' own clean-up, so a red test does not also litter the host.
# app (+2): preview_manager `_abort_start` (the failed start's container, by name)
# and sandbox_runner `probe_disk_cap` (its throwaway probe container); tests (+2): the
# `storage_opt` note in test_sandbox_runner_bounds_integration and the lost-Redis monkeypatch
# in test_preview_manager_integration. Every one carries `v=True`.
# tests (+1): the recorded-wire container in test_openai_sdk_wire_integration. `v=True`.
# app (+1): the reconciler's container removal (services/reconciler.py; its network
# removal is a network and is not counted); tests (+1): the redis the missing-Redis scenario
# removes in test_reconciler_integration. Both `v=True`. Tests (+1 more): `remove_handles` in
# docker_cleanup.py, the by-handle clean-up of the Docker tests. `v=True`.
# tests (+1): `_image_files` in test_image_assembly.py removes the container it only
# created (never started) to read an image's files with `get_archive`. `v=True`.
# tests (+1): the `slow_upstream` fixture in test_proxy_container_integration.py removes the
# stub provider container that holds a call for four seconds. `v=True`.
# tests (+1): the second, wide-listening Traefik in test_deploy_traefik_integration.py (the
# loopback-check refusal test) is removed in its `finally`. `v=True`.
# A change in either count means a site was added or removed: update the
# number deliberately.
EXPECTED_APP_SITES = 11
EXPECTED_TEST_SITES = 34


def test_every_container_removal_in_app_removes_its_anonymous_volumes():
    sites, defects = _scan("app")
    assert sites == EXPECTED_APP_SITES, f"found {sites} container-removal sites in app/"
    assert defects == []


def test_every_container_removal_in_tests_removes_its_anonymous_volumes():
    sites, defects = _scan("tests")
    assert sites == EXPECTED_TEST_SITES, f"found {sites} container-removal sites in tests/"
    assert defects == []
