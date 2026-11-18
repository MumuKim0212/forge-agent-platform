"""The proxy image advertises what it can do, and the worker checks it.

The image is built by hand from the backend tree, so a worker on newer code than its image is a real
state: the `drain` route is missing from an older image, and every build's usage read then fails
closed AFTER a full paid agent run. The image says what it supports in a LABEL
(`forge.proxy.capabilities`, read with one `image inspect`: no container, no network, no key). The
worker reads it at start (a banner line naming the gap and the rebuild command, `test_worker.py`) and
`ProxyContainer.start` refuses on the same finding before any container exists, so no money is spent
on a run whose cost could never be recorded.

No Docker here: a fake client answers `images.get`. The real images (the packaged one advertises
`drain`; `forge-llm-proxy:pre-drain` does not) are in `test_proxy_container_integration.py`.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from docker.errors import APIError, DockerException, ImageNotFound

from app.services import proxy_container as pc
from app.services import sandbox_build_executor as sbe
from app.services.agent_adapter import AgentAdapterResult
from app.services.proxy_container import (
    ProxyContainer,
    ProxyImageStaleError,
    ProxyStartError,
    advertised_proxy_capabilities,
    proxy_image_gap,
)
from app.services.status import BuildFailureReason

BACKEND = Path(__file__).resolve().parent.parent
DOCKERFILE = BACKEND / "docker" / "llm-proxy" / "Dockerfile"
REBUILD = "docker build -t forge-llm-proxy:latest -f docker/llm-proxy/Dockerfile . (run from backend/)"


def _client(labels=None, *, raises: BaseException | None = None):
    """A Docker client whose `images.get(name)` answers with an image carrying `labels`, or raises."""
    asked: list[str] = []

    def get(name):
        asked.append(name)
        if raises is not None:
            raise raises
        return SimpleNamespace(labels=labels)

    client = SimpleNamespace(images=SimpleNamespace(get=get), asked=asked)
    return client


# --- the contract strings, as literals ----------------------------------------------------------


def test_the_contract_strings_are_pinned():
    """The label key, what the worker requires, and the default image are written once here as
    literals; the tests below may then read the production constants on the expected side."""
    assert pc.PROXY_CAPABILITIES_LABEL == "forge.proxy.capabilities"
    assert pc.REQUIRED_PROXY_CAPABILITIES == ("drain",)
    assert pc.PROXY_IMAGE == "forge-llm-proxy:latest"


# --- reading the label --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("drain", {"drain"}),
        (" drain , future ", {"drain", "future"}),
        ("drain,drain", {"drain"}),
        ("", set()),
        (",,", set()),
    ],
)
def test_the_label_is_a_comma_separated_list_of_names(value, expected):
    client = _client({"forge.proxy.capabilities": value})

    assert advertised_proxy_capabilities(client, "forge-llm-proxy:latest") == expected
    assert client.asked == ["forge-llm-proxy:latest"]  # the image it was asked about, not another


@pytest.mark.parametrize(
    "labels",
    [None, {}, {"forge.role": "llm-proxy"}, {"forge.proxy.capabilities": 1}, {"forge.proxy.capabilities": ["drain"]}, "drain"],
    ids=["no_labels", "empty", "other_labels_only", "number", "list", "labels_not_a_mapping"],
)
def test_an_image_with_no_readable_label_advertises_nothing(labels):
    """Fail closed: an image built before the label (no labels at all, or none of this name), and a
    value that is not text, are 'cannot do', never 'can'."""
    assert advertised_proxy_capabilities(_client(labels), "forge-llm-proxy:latest") == frozenset()


# --- the gap the worker reports at start --------------------------------------------------------


def test_an_image_that_advertises_what_the_worker_needs_has_no_gap():
    assert proxy_image_gap(_client({"forge.proxy.capabilities": "drain"})) is None


def test_names_beyond_what_the_worker_needs_are_not_a_gap():
    assert proxy_image_gap(_client({"forge.proxy.capabilities": "future,drain"})) is None


@pytest.mark.parametrize("labels", [{}, None, {"forge.proxy.capabilities": "future"}], ids=["no_label", "no_labels", "other_name"])
def test_an_image_that_does_not_advertise_drain_is_a_gap_naming_the_image_the_capability_and_the_rebuild_command(labels):
    gap = proxy_image_gap(_client(labels), "forge-llm-proxy:latest")

    assert gap is not None
    assert "'forge-llm-proxy:latest'" in gap
    assert "does not advertise drain" in gap
    assert "'forge.proxy.capabilities' label" in gap
    assert "built before the label existed has none" in gap
    assert "every build is refused before its agent runs" in gap
    assert REBUILD in gap


def test_the_rebuild_command_names_the_image_that_was_checked():
    gap = proxy_image_gap(_client({}), "forge-llm-proxy:some-other-tag")

    assert "docker build -t forge-llm-proxy:some-other-tag -f docker/llm-proxy/Dockerfile ." in gap


def test_an_absent_image_is_a_gap_that_says_to_build_it():
    gap = proxy_image_gap(_client(raises=ImageNotFound("no such image")), "forge-llm-proxy:latest")

    assert "was not found on this host" in gap
    assert REBUILD in gap


def test_an_engine_that_cannot_be_asked_is_a_gap_naming_the_exception_class_only():
    gap = proxy_image_gap(_client(raises=APIError("boom: secret-looking text")), "forge-llm-proxy:latest")

    assert "could not be inspected (APIError)" in gap
    assert "boom" not in gap


# --- `ProxyContainer.start` refuses before any container exists ---------------------------------


def _start_client(labels=None, *, inspect_raises: BaseException | None = None):
    client = MagicMock()
    if inspect_raises is not None:
        client.images.get.side_effect = inspect_raises
    else:
        client.images.get.return_value.labels = labels
    client.containers.run.side_effect = DockerException("stop here")
    return client


def test_start_refuses_an_image_without_the_capability_before_creating_anything():
    client = _start_client({})
    network = MagicMock()
    proxy = ProxyContainer(client=client)

    with pytest.raises(ProxyImageStaleError) as raised:
        proxy.start(real_api_key="k", sandbox_network=network, image="forge-llm-proxy:old")

    assert isinstance(raised.value, ProxyStartError)  # every caller that handles a failed start still does
    message = str(raised.value)
    assert "'forge-llm-proxy:old'" in message and "does not advertise drain" in message
    assert "docker build -t forge-llm-proxy:old -f docker/llm-proxy/Dockerfile ." in message
    client.images.get.assert_called_once_with("forge-llm-proxy:old")
    client.containers.run.assert_not_called()  # no container, so no key was ever handed to one
    network.connect.assert_not_called()


def test_start_goes_ahead_with_an_image_that_advertises_the_capability():
    client = _start_client({"forge.proxy.capabilities": "drain"})
    proxy = ProxyContainer(client=client)

    with pytest.raises(ProxyStartError) as raised:
        proxy.start(real_api_key="k", sandbox_network=MagicMock(), image="forge-llm-proxy:fresh")

    assert not isinstance(raised.value, ProxyImageStaleError)  # it got as far as `containers.run`
    (call,) = client.containers.run.call_args_list
    assert call.kwargs["image"] == "forge-llm-proxy:fresh"


@pytest.mark.parametrize(
    "error",
    [ImageNotFound("no such image"), APIError("engine hiccup")],
    ids=["image_not_found", "api_error"],
)
def test_start_does_not_refuse_an_image_it_could_not_inspect_the_run_fails_on_its_own(error):
    """Not found, or an engine that does not answer, is not a finding about the image's capabilities:
    `containers.run` is still attempted, so a build's failure there is the ENGINE_UNREACHABLE it always
    was and not a claim about a stale image."""
    client = _start_client(inspect_raises=error)
    proxy = ProxyContainer(client=client)

    with pytest.raises(ProxyStartError) as raised:
        proxy.start(real_api_key="k", sandbox_network=MagicMock())

    assert not isinstance(raised.value, ProxyImageStaleError)
    client.containers.run.assert_called_once()


def test_start_defaults_to_the_packaged_image_and_checks_that_one():
    client = _start_client({"forge.proxy.capabilities": "drain"})
    proxy = ProxyContainer(client=client)

    with pytest.raises(ProxyStartError):
        proxy.start(real_api_key="k", sandbox_network=MagicMock())

    client.images.get.assert_called_once_with("forge-llm-proxy:latest")


# --- the executor: a build is refused before its agent runs -------------------------------------


class _Network:
    def __init__(self):
        self.removed = False

    def remove(self):
        self.removed = True


def _executor(monkeypatch, *, start_raises: BaseException | None = None, **executor_kwargs):
    network = _Network()
    started: list[dict] = []
    issued: list = []
    ran: list = []
    reached_publish: list = []

    class _Proxy:
        base_url_for_sandbox = "http://proxy"

        def __init__(self, *a, **k):
            pass

        def start(self, **kwargs):
            started.append(kwargs)
            if start_raises is not None:
                raise start_raises

        def issue_token(self, *a, **k):
            issued.append(True)
            return "tok"

        def revoke(self, *a):
            pass

        def stop(self):
            pass

    class _Adapter:
        def run(self, adapter_input):
            ran.append(True)
            return AgentAdapterResult(ok=False, reason=BuildFailureReason.AGENT_ERROR)

    docker = SimpleNamespace(networks=SimpleNamespace(create=lambda *a, **k: network))
    monkeypatch.setattr(sbe, "ProxyContainer", _Proxy)
    executor = sbe.SandboxBuildExecutor(docker_client=docker, adapter=_Adapter(), **executor_kwargs)
    monkeypatch.setattr(executor, "_stage_workspace", lambda *a, **k: None)
    monkeypatch.setattr(
        executor,
        "_publish",
        lambda *a, **k: reached_publish.append(True) or sbe.ExecutionResult(ok=True, summary="published"),
    )
    return executor, SimpleNamespace(network=network, started=started, issued=issued, ran=ran, published=reached_publish)


def test_a_build_against_a_stale_image_is_refused_before_the_agent_runs(monkeypatch, make_build_with_request):
    sentence = (
        "the LLM proxy image 'forge-llm-proxy:latest' does not advertise drain (its 'forge.proxy.capabilities' "
        "label is absent; an image built before the label existed has none, whatever it can do), so it cannot be trusted "
        "to record a build's cost and every build is refused before its agent runs; rebuild it with: " + REBUILD
    )
    executor, seen = _executor(monkeypatch, start_raises=ProxyImageStaleError(sentence))
    build = make_build_with_request()

    result = executor.execute(build)

    assert result.ok is False
    assert result.reason == BuildFailureReason.INFRA_ERROR  # the platform's own image is out of date; nothing is unreachable
    assert result.summary == "no agent ran: " + sentence
    assert seen.ran == [] and seen.issued == [] and seen.published == []  # no token, no agent
    assert seen.network.removed is True  # the sandbox network the build had created is torn down
    assert build.cost_tokens is None and build.cost_usd is None


def test_an_ordinary_failed_start_is_still_engine_unreachable(monkeypatch, make_build_with_request):
    """Non-vacuity for the test above: only the stale-image finding gets the new reason."""
    executor, seen = _executor(monkeypatch, start_raises=ProxyStartError("proxy container did not become healthy"))

    result = executor.execute(make_build_with_request())

    assert result.reason == BuildFailureReason.ENGINE_UNREACHABLE
    assert result.summary == "the LLM proxy container failed to start; see the worker log"
    assert seen.ran == []


def test_the_executor_starts_the_proxy_from_its_image_the_packaged_one_by_default(monkeypatch, make_build_with_request):
    executor, seen = _executor(monkeypatch)
    executor.execute(make_build_with_request())
    assert seen.started[0]["image"] == "forge-llm-proxy:latest"
    assert executor.proxy_image == "forge-llm-proxy:latest"

    executor, seen = _executor(monkeypatch, proxy_image="forge-llm-proxy:pre-drain")
    executor.execute(make_build_with_request(name="request-project-two"))
    assert seen.started[0]["image"] == "forge-llm-proxy:pre-drain"
    assert executor.proxy_image == "forge-llm-proxy:pre-drain"


def test_the_executors_probe_is_the_image_check_on_its_own_image():
    """`proxy_image_gap` is the same finding `start` refuses on, asked of the executor's image."""
    advertising = _client({"forge.proxy.capabilities": "drain"})
    executor = sbe.SandboxBuildExecutor(docker_client=advertising, adapter=SimpleNamespace(), proxy_image="x:1")
    assert executor.proxy_image_gap() is None
    assert advertising.asked == ["x:1"]
    assert executor.proxy_required_capabilities == ("drain",)

    stale = _client({})
    executor = sbe.SandboxBuildExecutor(docker_client=stale, adapter=SimpleNamespace(), proxy_image="x:2")
    gap = executor.proxy_image_gap()
    assert gap is not None and "'x:2'" in gap and "does not advertise drain" in gap


def test_an_engine_that_is_down_is_a_gap_too_requests_errors_are_not_docker_errors():
    """docker-py raises `requests`' own `ConnectionError` for an engine that does not answer, and that
    is not a `DockerException`. It is still 'could not be inspected', not an exception out of the
    worker's start."""
    gap = proxy_image_gap(_client(raises=requests.exceptions.ConnectionError("engine down")))

    assert "could not be inspected (ConnectionError)" in gap


def test_start_does_not_refuse_when_the_engine_is_down_either_the_run_fails_on_its_own():
    client = _start_client(inspect_raises=requests.exceptions.ConnectionError("engine down"))
    proxy = ProxyContainer(client=client)

    with pytest.raises(ProxyStartError) as raised:
        proxy.start(real_api_key="k", sandbox_network=MagicMock())

    assert not isinstance(raised.value, ProxyImageStaleError)
    client.containers.run.assert_called_once()


def test_an_error_that_is_neither_docker_nor_transport_is_not_worded_as_a_finding():
    """A bug is not an image problem: it reaches the caller (the worker's banner catches it and says
    the check raised; `execute` maps it to ENGINE_UNREACHABLE)."""
    with pytest.raises(RuntimeError):
        proxy_image_gap(_client(raises=RuntimeError("a bug")))


# --- the Dockerfile carries the label, and says what the code requires --------------------------


def _labels(dockerfile_text: str) -> dict[str, str]:
    """The `LABEL key="value"` pairs of a Dockerfile's text (comments and continuations aside)."""
    lines = [line for line in dockerfile_text.splitlines() if not line.lstrip().startswith("#")]
    code = re.sub(r"\\\s*\n", " ", "\n".join(lines))
    found: dict[str, str] = {}
    for body in re.findall(r"(?im)^LABEL\s+(.+)$", code):
        for key, quoted, bare in re.findall(r'([\w.\-]+)=(?:"([^"]*)"|(\S+))', body):
            found[key] = quoted or bare
    return found


def test_the_real_dockerfile_labels_the_image_with_the_capabilities_as_literals():
    labels = _labels(DOCKERFILE.read_text(encoding="utf-8"))

    assert labels.get("forge.proxy.capabilities") == "drain"


def test_the_dockerfiles_label_satisfies_what_the_worker_requires():
    """The two sides that cannot be one constant (a Dockerfile line and a Python tuple) are held
    together here: an image built from this Dockerfile passes the worker's check."""
    labels = _labels(DOCKERFILE.read_text(encoding="utf-8"))
    advertised = {part.strip() for part in labels[pc.PROXY_CAPABILITIES_LABEL].split(",") if part.strip()}

    assert set(pc.REQUIRED_PROXY_CAPABILITIES) <= advertised


@pytest.mark.parametrize(
    "text,expected",
    [
        ('LABEL forge.proxy.capabilities="drain"\n', {"forge.proxy.capabilities": "drain"}),
        ('LABEL a="1" b=2\n', {"a": "1", "b": "2"}),
        ('# LABEL forge.proxy.capabilities="drain"\nFROM x\n', {}),
        ("FROM x\n", {}),
    ],
    ids=["one", "two_pairs", "commented_out", "none"],
)
def test_the_label_scan_sees_a_real_line_and_not_a_comment(text, expected):
    assert _labels(text) == expected
