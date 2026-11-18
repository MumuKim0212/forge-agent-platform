"""`ProxyContainer.drain`, the worker's side of the proxy's drain route. No Docker: the exec is a
fake that records the script it was given and answers with canned bytes.

The proxy's side (what the route does) is `tests/test_llm_proxy_drain.py`; the two ends meeting
through a real container is `tests/test_proxy_container_integration.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.proxy_container import ProxyContainer, ProxyStartError


class _FakeContainer:
    def __init__(self, answer: tuple[int, bytes]):
        self.answer = answer
        self.scripts: list[str] = []

    def exec_run(self, command):
        assert command[:2] == ["python", "-c"]
        self.scripts.append(command[2])
        return self.answer


def _proxy(answer: tuple[int, bytes]) -> tuple[ProxyContainer, _FakeContainer]:
    proxy = ProxyContainer(client=SimpleNamespace())
    proxy._container = _FakeContainer(answer)
    return proxy, proxy._container


def test_drain_is_a_post_to_the_tokens_drain_route_with_the_wait_and_a_margin_on_the_exec():
    proxy, container = _proxy((0, b'{"drained": true, "inflight": 0}'))

    assert proxy.drain("tok-1", timeout_seconds=30) is True

    (script,) = container.scripts
    assert "http://127.0.0.1:8802/_forge_admin/tokens/tok-1/drain?timeout_seconds=30.0" in script
    assert "method='POST'" in script
    assert "X-Forge-Admin-Secret" in script
    # the in-container call outlasts the wait it asks the proxy for, by five seconds
    assert "timeout=35.0" in script


def test_drain_reports_a_wait_that_ran_out_as_false():
    proxy, _ = _proxy((0, b'{"drained": false, "inflight": 1}'))

    assert proxy.drain("tok", timeout_seconds=1.5) is False


def test_every_other_admin_call_keeps_its_five_second_limit():
    proxy, container = _proxy((0, b'{"tokens": 0}'))

    proxy.usage("tok")

    assert "timeout=5.0" in container.scripts[0]


# The exec's exit code is the HTTP status modulo 256 (`sys.exit(e.code)` in `_exec_admin_request`'s
# script): measured against the packaged images, a 404 reads as 148 (so a 422 would read as 166). The
# stale-image case below is the real answer of `forge-llm-proxy:pre-drain`, which has no drain route.
@pytest.mark.parametrize(
    ("answer", "text"),
    [
        ((148, b'{"detail":"Not Found"}'), "rebuild forge-llm-proxy:latest"),  # an image from before the drain route
        ((148, b'{"detail":"unknown token"}'), "drain failed (148)"),
        ((166, b'{"detail":[{"type":"less_than_equal"}]}'), "drain failed (166)"),
        ((1, b"Traceback (most recent call last): ..."), "drain failed (1)"),
    ],
    ids=["stale_image", "unknown_token", "wait_out_of_bounds", "exec_failed"],
)
def test_a_failed_exec_raises_proxy_start_error(answer, text):
    proxy, _ = _proxy(answer)

    with pytest.raises(ProxyStartError) as raised:
        proxy.drain("tok", timeout_seconds=30)

    assert text in str(raised.value)


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"not json",
        b"[]",
        b"null",
        b'{"inflight": 0}',  # no `drained`
        b'{"drained": "true"}',  # a string is not a boolean
        b'{"drained": 1}',  # nor is an integer
        b'{"drained": null}',
    ],
    ids=repr,
)
def test_an_answer_that_is_not_a_boolean_drained_is_never_read_as_true(output):
    proxy, _ = _proxy((0, output))

    with pytest.raises(ProxyStartError):
        proxy.drain("tok", timeout_seconds=30)
