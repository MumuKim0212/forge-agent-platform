"""`PreviewManager._abort_start`, the one place a failed preview start is undone, driven with fakes
(no Docker). The engine-level guarantee -- that a failure at each point of a real start leaves no
container, network, database or Redis behind -- is `test_preview_manager_integration.py`.

What is pinned here is the shape that guarantee rests on: every step is its own guarded call, in the order
Docker needs, and none of them can replace the exception that is already propagating.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from docker.errors import APIError, NotFound

from app.services import preview_data
from app.services.preview_manager import PreviewManager


class _Network:
    name = "forge-preview-net-b1-abcd1234"

    def __init__(self, log, *, reload_raises=None, remove_raises=None, attached=("pg-id", "redis-id")):
        self._log = log
        self._reload_raises = reload_raises
        self._remove_raises = remove_raises
        self.attrs = {"Containers": {c: {} for c in attached}}

    def reload(self):
        self._log.append("network.reload")
        if self._reload_raises is not None:
            raise self._reload_raises

    def disconnect(self, container, force=False):
        self._log.append(f"network.disconnect:{container}:{force}")

    def remove(self):
        self._log.append("network.remove")
        if self._remove_raises is not None:
            raise self._remove_raises


def _manager(log, *, container_remove_raises=None, container_missing=False):
    class _Container:
        def remove(self, v=False, force=False):
            log.append(f"container.remove:v={v}:force={force}")
            if container_remove_raises is not None:
                raise container_remove_raises

    def get(name):
        log.append(f"containers.get:{name}")
        if container_missing:
            raise NotFound("no such container")
        return _Container()

    return PreviewManager(client=SimpleNamespace(containers=SimpleNamespace(get=get)))


def _drops(monkeypatch, log, raises=None):
    def drop(build_id, *, docker_client=None):
        log.append(f"drop_data:{build_id}")
        if raises is not None:
            raise raises

    monkeypatch.setattr(preview_data, "drop_preview_data_services", drop)


def test_the_steps_run_in_the_order_docker_needs(monkeypatch):
    log: list[str] = []
    _drops(monkeypatch, log)
    network = _Network(log)

    _manager(log)._abort_start(network, "b1")

    assert log == [
        "containers.get:forge-preview-b1",
        "container.remove:v=True:force=True",  # the container first, by name
        "drop_data:b1",  # then the data services
        "network.reload",
        "network.disconnect:pg-id:True",  # then every endpoint still on the network, forced
        "network.disconnect:redis-id:True",
        "network.remove",  # and last the network itself
    ]


def test_a_container_that_never_got_created_is_not_an_error(monkeypatch, caplog):
    log: list[str] = []
    _drops(monkeypatch, log)

    _manager(log, container_missing=True)._abort_start(_Network(log), "b1")

    assert "network.remove" in log
    assert "left" not in caplog.text  # nothing was left behind, so nothing is reported


@pytest.mark.parametrize(
    "broken",
    ["container", "data", "reload", "network"],
)
def test_a_step_that_raises_cannot_skip_the_steps_after_it_or_replace_the_exception_in_flight(
    monkeypatch, caplog, broken
):
    log: list[str] = []
    boom = RuntimeError("boom")
    _drops(monkeypatch, log, raises=boom if broken == "data" else None)
    network = _Network(
        log,
        reload_raises=boom if broken == "reload" else None,
        remove_raises=APIError("network has active endpoints") if broken == "network" else None,
    )
    manager = _manager(log, container_remove_raises=boom if broken == "container" else None)

    def start_that_fails_late():
        try:
            raise ValueError("the original failure")
        except BaseException:
            manager._abort_start(network, "b1")
            raise

    with pytest.raises(ValueError, match="the original failure"):  # never replaced
        start_that_fails_late()

    # ...and every later step still ran: the network's removal is always attempted.
    assert log[-1] == "network.remove"
    assert "drop_data:b1" in log
    assert caplog.text  # and the failure was said, not swallowed in silence


def test_what_could_not_be_removed_is_named_once_with_the_build_id_that_finds_it(monkeypatch, caplog):
    log: list[str] = []
    _drops(monkeypatch, log, raises=RuntimeError("db down"))
    network = _Network(log, remove_raises=APIError("still in use"))

    _manager(log, container_remove_raises=RuntimeError("daemon timeout"))._abort_start(network, "b1")

    errors = [r for r in caplog.records if r.levelname == "ERROR" and "a failed start left" in r.getMessage()]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "b1" in message
    for left in ("its container", "its data services", "network forge-preview-net-b1-abcd1234"):
        assert left in message
