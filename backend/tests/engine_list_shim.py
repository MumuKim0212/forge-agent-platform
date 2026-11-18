"""A fake engine's container list, reachable the way `sandbox_runner.list_containers_bounded` asks for it
.

The runner and the preview watcher read sizes through the client's low-level request helpers
(`api._url`, `api._get(..., timeout=...)`, `api._result`), because docker-py's `containers()` takes no
timeout. The fakes in the unit tests already answer `api.containers(all=, size=, filters=)`; this mixin
turns the helper's `GET /containers/json` back into that call, at call time (so a test that replaces
`api.containers` still intercepts it), and records the timeout it was given in `list_timeouts`, which is
what a test reads to assert the bound. It re-implements no production logic: it only decodes the
parameters the helper encoded (`filters` is the JSON docker-py's `convert_filters` makes).
"""

from __future__ import annotations

import json


class _ListResponse:
    def __init__(self, rows):
        self.rows = rows


class ContainerListViaContainers:
    """Mixed into (or attached to) a fake `client.api` that has a `containers(**kwargs)` method."""

    def _url(self, pattern, *args):
        return pattern.format(*args)

    def _list_get(self, url, params, timeout):
        assert url == "/containers/json", url
        if not hasattr(self, "list_timeouts"):
            self.list_timeouts = []
        self.list_timeouts.append(timeout)
        filters = json.loads(params["filters"]) if params.get("filters") else None
        return _ListResponse(self.containers(all=bool(params["all"]), size=bool(params["size"]), filters=filters))

    def _get(self, url, params=None, timeout=None, **kwargs):
        return self._list_get(url, params, timeout)

    def _result(self, response, json=False):
        return response.rows
