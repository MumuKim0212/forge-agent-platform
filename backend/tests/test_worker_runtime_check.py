"""ADR 0046 §Decision 3: the worker's startup check that every configured container runtime
(build, preview) is registered with the Docker engine. Prod refuses with a named error before the
lease is taken; dev logs a warning. A fake client stands in for the engine: no Docker is needed."""

from __future__ import annotations

import pytest

import app.worker as worker
from app.config import settings

REAL_VERIFY = worker.verify_container_runtimes


class _Engine:
    """`docker info`'s `Runtimes` is a dict keyed by runtime name; `runc` is always there."""

    def __init__(self, runtimes=("runc",), error: Exception | None = None):
        self._runtimes = {name: {"path": name} for name in runtimes}
        self._error = error
        self.info_calls = 0

    def info(self):
        self.info_calls += 1
        if self._error is not None:
            raise self._error
        return {"Runtimes": self._runtimes}


@pytest.fixture
def runtimes(monkeypatch):
    def configure(*, build=None, preview=None, env="prod"):
        monkeypatch.setattr(settings, "forge_env", env)
        monkeypatch.setattr(settings, "build_container_runtime", build)
        monkeypatch.setattr(settings, "preview_container_runtime", preview)

    return configure


def test_prod_refuses_an_unregistered_build_runtime_naming_it_and_the_setting(runtimes):
    runtimes(build="runsc")

    with pytest.raises(worker.ContainerRuntimeNotRegisteredError) as caught:
        worker.verify_container_runtimes(_Engine(("runc",)))

    message = str(caught.value)
    assert "runsc" in message
    assert "BUILD_CONTAINER_RUNTIME" in message
    assert "runc" in message  # what the engine does have


def test_prod_refuses_an_unregistered_preview_only_runtime(runtimes):
    runtimes(build=None, preview="runsc")

    with pytest.raises(worker.ContainerRuntimeNotRegisteredError) as caught:
        worker.verify_container_runtimes(_Engine(("runc",)))

    assert "PREVIEW_CONTAINER_RUNTIME" in str(caught.value)


def test_prod_checks_the_preview_runtime_when_it_differs_from_the_build_runtime(runtimes):
    """The build runtime is registered; the preview's own is not: the check does not stop at the first."""
    runtimes(build="runsc", preview="kata")

    with pytest.raises(worker.ContainerRuntimeNotRegisteredError) as caught:
        worker.verify_container_runtimes(_Engine(("runc", "runsc")))

    assert "kata" in str(caught.value)


def test_prod_passes_when_every_configured_runtime_is_registered(runtimes):
    runtimes(build="runsc", preview="runsc")

    assert worker.verify_container_runtimes(_Engine(("runc", "runsc"))) == []


def test_prod_refuses_when_the_engine_cannot_be_asked_while_a_runtime_is_configured(runtimes):
    runtimes(build="runsc")

    with pytest.raises(worker.ContainerRuntimeNotRegisteredError) as caught:
        worker.verify_container_runtimes(_Engine(error=ConnectionError("engine down")))

    assert "ConnectionError" in str(caught.value)


def test_dev_only_warns_for_an_unregistered_runtime(runtimes, caplog):
    runtimes(build="runsc", env="dev")

    with caplog.at_level("WARNING", logger="app.worker"):
        missing = worker.verify_container_runtimes(_Engine(("runc",)))

    assert missing == ["runsc"]
    assert any("runsc" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)


def test_dev_only_warns_when_the_engine_cannot_be_asked(runtimes, caplog):
    runtimes(build="runsc", env="dev")

    with caplog.at_level("WARNING", logger="app.worker"):
        missing = worker.verify_container_runtimes(_Engine(error=ConnectionError("engine down")))

    assert missing == ["runsc"]
    assert any(r.levelname == "WARNING" for r in caplog.records)


@pytest.mark.parametrize("env", ["prod", "dev"])
def test_with_no_runtime_configured_the_engine_is_never_contacted(runtimes, env):
    runtimes(env=env)
    engine = _Engine(error=ConnectionError("must not be asked"))

    assert worker.verify_container_runtimes(engine) == []
    assert engine.info_calls == 0


def test_main_exits_1_before_the_lease_when_prod_has_an_unregistered_runtime(runtimes, monkeypatch, caplog):
    runtimes(build="runsc")
    reached: list[str] = []

    def lease_reached():
        # Bounded on purpose: with the refusal removed, `main()` would take the lease and run the
        # worker loop until killed. Reaching either stub ends the test at once, as a failure.
        reached.append("lease")
        raise AssertionError("the worker went on to take the lease although the runtime is unregistered")

    monkeypatch.setattr(worker, "assert_schema_at_head", lambda engine: None)
    monkeypatch.setattr(worker, "_bind_instance_id", lambda: "forge-test-runtime-check")
    monkeypatch.setattr(worker, "verify_container_runtimes", lambda client=None: REAL_VERIFY(_Engine(("runc",))))
    monkeypatch.setattr(worker, "_acquire_worker_lease", lease_reached)
    monkeypatch.setattr(worker, "_serve", lambda lease: lease_reached())

    with caplog.at_level("ERROR", logger="app.worker"):
        with pytest.raises(SystemExit) as caught:
            worker.main()

    assert caught.value.code == 1
    assert reached == []  # the lease was never taken
    assert any("refuses to start" in r.getMessage() and "runsc" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("build", "preview", "effective", "configured"),
    [
        (None, None, None, []),
        ("runsc", None, "runsc", ["runsc"]),
        (None, "runsc", "runsc", ["runsc"]),
        ("runsc", "kata", "kata", ["runsc", "kata"]),
        ("  ", "", None, []),
        ("runsc", "runsc", "runsc", ["runsc"]),
    ],
)
def test_the_effective_preview_runtime_and_the_configured_set(monkeypatch, build, preview, effective, configured):
    monkeypatch.setattr(settings, "build_container_runtime", build)
    monkeypatch.setattr(settings, "preview_container_runtime", preview)

    assert settings.effective_preview_runtime == effective
    assert settings.configured_container_runtimes == configured
